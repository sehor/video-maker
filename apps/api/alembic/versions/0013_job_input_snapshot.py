"""Add immutable input snapshots without inventing historical inputs or enabling cutover."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_job_input_snapshot"
down_revision: str | None = "0012_optional_media_hashes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("generation_jobs", sa.Column("input_snapshot_json", sa.JSON(), nullable=True))
    op.execute(r"""
        CREATE FUNCTION guard_job_input_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE s jsonb; ref jsonb;
        BEGIN
          s := NEW.input_snapshot_json::jsonb;
          IF TG_OP = 'UPDATE' THEN
            IF s IS DISTINCT FROM OLD.input_snapshot_json::jsonb THEN
              RAISE EXCEPTION 'job input snapshot is immutable' USING ERRCODE = '23514';
            END IF;
          END IF;
          IF NEW.input_snapshot_json IS NULL THEN RETURN NEW; END IF;
          IF jsonb_typeof(s) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'invalid job input snapshot' USING ERRCODE = '23514';
          END IF;
          IF NOT (
            s ?& ARRAY['version','prompt','negative_prompt','references','duration_ms',
                       'resolution','aspect_ratio','mode']
            AND s - ARRAY['version','prompt','negative_prompt','references','duration_ms',
                          'resolution','aspect_ratio','mode'] = '{}'::jsonb
            AND s->'version' = '1'::jsonb
            AND s->>'version' = '1'
            AND jsonb_typeof(s->'prompt') = 'string'
            AND length(s->>'prompt') BETWEEN 1 AND 4000
            AND (s->'negative_prompt' = 'null'::jsonb OR (
              jsonb_typeof(s->'negative_prompt') = 'string'
              AND length(s->>'negative_prompt') <= 4000))
            AND s->'duration_ms' = to_jsonb(NEW.duration_ms)
            AND s->>'duration_ms' ~ '^[1-9][0-9]*$'
            AND s->'resolution' = to_jsonb(NEW.resolution)
            AND s->'aspect_ratio' = to_jsonb(NEW.aspect_ratio)
            AND s->'mode' = to_jsonb(NEW.mock_mode)
            AND s->>'mode' IN ('success','delayed','failure','timeout','duplicate','corrupt')
            AND jsonb_typeof(s->'references') = 'array'
          ) IS TRUE THEN
            RAISE EXCEPTION 'invalid job input snapshot' USING ERRCODE = '23514';
          END IF;
          IF jsonb_array_length(s->'references') > 1 THEN
            RAISE EXCEPTION 'snapshot v1 supports at most one reference'
              USING ERRCODE = '23514';
          END IF;
          FOR ref IN SELECT value FROM jsonb_array_elements(s->'references') LOOP
            IF jsonb_typeof(ref) IS DISTINCT FROM 'object' THEN
              RAISE EXCEPTION 'invalid input reference' USING ERRCODE = '23514';
            END IF;
            IF NOT (
              ref ?& ARRAY['asset_id','object_key','reference_role']
              AND ref - ARRAY['asset_id','object_key','reference_role'] = '{}'::jsonb
              AND jsonb_typeof(ref->'asset_id') = 'string'
              AND ref->>'asset_id' ~
                '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
              AND ref->>'reference_role' = 'FIRST_FRAME'
              AND jsonb_typeof(ref->'object_key') = 'string'
              AND length(ref->>'object_key') BETWEEN 1 AND 512
              AND ref->>'object_key' !~ '(^/|/$|//|\\|:|(^|/)\.{1,2}(/|$))'
            ) IS TRUE THEN
              RAISE EXCEPTION 'invalid input reference' USING ERRCODE = '23514';
            END IF;
          END LOOP;
          RETURN NEW;
        END $$;
        CREATE TRIGGER generation_job_input_immutable
          BEFORE INSERT OR UPDATE ON generation_jobs
          FOR EACH ROW EXECUTE FUNCTION guard_job_input_snapshot();

        CREATE FUNCTION require_job_input_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.input_snapshot_json IS NULL AND (TG_OP = 'INSERT' OR NEW.status NOT IN
            ('SUCCEEDED','FAILED_FINAL','CANCELLED','EXPIRED','REJECTED_POLICY')) THEN
            RAISE EXCEPTION 'job input snapshot is required' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER generation_job_input_required
          BEFORE INSERT OR UPDATE ON generation_jobs
          FOR EACH ROW EXECUTE FUNCTION require_job_input_snapshot();
        ALTER TABLE generation_jobs DISABLE TRIGGER generation_job_input_required;
    """)


def downgrade() -> None:
    connection = op.get_bind()
    # Prevent inserts/cutover from racing the non-destructive rollback guard.
    connection.execute(sa.text("LOCK TABLE generation_jobs IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM generation_jobs WHERE input_snapshot_json IS NOT NULL)"
        )
    ) or connection.scalar(
        sa.text(
            "SELECT tgenabled <> 'D' FROM pg_trigger "
            "WHERE tgrelid = 'generation_jobs'::regclass "
            "AND tgname = 'generation_job_input_required'"
        )
    ):
        raise RuntimeError("Cannot drop input snapshots after capture or requirement activation")
    op.execute("DROP TRIGGER generation_job_input_required ON generation_jobs")
    op.execute("DROP TRIGGER generation_job_input_immutable ON generation_jobs")
    op.execute("DROP FUNCTION require_job_input_snapshot()")
    op.execute("DROP FUNCTION guard_job_input_snapshot()")
    op.drop_column("generation_jobs", "input_snapshot_json")
