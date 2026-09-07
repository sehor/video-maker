"""Persist route versions, independent admission, and truthful project binding history."""

import json
import uuid

import sqlalchemy as sa

from alembic import op

revision = "0014_project_route_binding"
down_revision = "0013_job_input_snapshot"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "generation_route_versions",
        sa.Column("candidate_id", sa.Uuid(), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False, unique=True),
        sa.Column("definition_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "route_admission",
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("generation_route_versions.candidate_id"),
            primary_key=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    definitions = [
        ("mock_video_v1", "mock", "mock:v1", list(range(1000, 10001, 1000)), False),
        ("runpod_simulated_v1", "runpod-simulator", "fast_wan_i2v_720_v1", [5000], True),
    ]
    for key, provider, workflow, durations, requires_input in definitions:
        candidate = uuid.uuid5(uuid.NAMESPACE_URL, f"video-maker:route:{key}")
        definition = {
            "provider_code": provider,
            "workflow_id": workflow,
            "durations_ms": durations,
            "resolutions": ["720P"],
            "aspect_ratios": ["16:9", "9:16"],
            "requires_input_claim": requires_input,
        }
        op.get_bind().execute(
            sa.text(
                "INSERT INTO generation_route_versions "
                "VALUES (:id, :key, CAST(:definition AS json))"
            ),
            {"id": candidate, "key": key, "definition": json.dumps(definition)},
        )
        op.get_bind().execute(
            sa.text("INSERT INTO route_admission (candidate_id) VALUES (:id)"), {"id": candidate}
        )
    op.add_column(
        "projects",
        sa.Column("route_binding_status", sa.String(16), nullable=False, server_default="UNBOUND"),
    )
    op.add_column("projects", sa.Column("route_candidate_id", sa.Uuid()))
    op.add_column("projects", sa.Column("route_binding_source", sa.String(32)))
    op.add_column("projects", sa.Column("route_bound_at", sa.DateTime(timezone=True)))
    op.add_column(
        "projects", sa.Column("route_history_json", sa.JSON(), nullable=False, server_default="{}")
    )
    op.create_foreign_key(
        "fk_project_route",
        "projects",
        "generation_route_versions",
        ["route_candidate_id"],
        ["candidate_id"],
    )
    op.execute("""
      WITH history AS (
        SELECT j.project_id, count(*) AS jobs,
          count(DISTINCT j.selected_route_candidate_id) AS routes,
          count(*) FILTER (WHERE r.candidate_id IS NULL) AS unknown,
          min(j.selected_route_candidate_id::text)::uuid AS candidate,
          min(COALESCE(j.finished_at, o.created_at))
            FILTER (WHERE o.validation_status = 'VALID') AS succeeded_at,
          jsonb_agg(DISTINCT j.selected_route_candidate_id) AS candidates
        FROM generation_jobs j LEFT JOIN generation_route_versions r
          ON r.candidate_id = j.selected_route_candidate_id
        LEFT JOIN generation_outputs o ON o.id = j.final_output_id AND j.status = 'SUCCEEDED'
        GROUP BY j.project_id
      ) UPDATE projects p SET
        route_binding_status = CASE WHEN h.routes <> 1 OR h.unknown > 0 THEN 'REVIEW'
          WHEN h.succeeded_at IS NOT NULL THEN 'LOCKED' ELSE 'PROVISIONAL' END,
        route_candidate_id = CASE WHEN h.routes = 1 AND h.unknown = 0 THEN h.candidate END,
        route_binding_source = 'HISTORICAL_JOBS',
        route_bound_at = CASE WHEN h.routes = 1 AND h.unknown = 0 THEN h.succeeded_at END,
        route_history_json = jsonb_build_object('job_count', h.jobs, 'candidates', h.candidates,
                                                'unknown_count', h.unknown)
      FROM history h WHERE p.id = h.project_id;
    """)
    op.create_check_constraint(
        "ck_project_route_state",
        "projects",
        """
      (route_binding_status = 'UNBOUND' AND route_candidate_id IS NULL
        AND route_binding_source IS NULL AND route_bound_at IS NULL) OR
      (route_binding_status = 'PROVISIONAL' AND route_candidate_id IS NOT NULL
        AND route_binding_source IS NOT NULL AND route_bound_at IS NULL) OR
      (route_binding_status = 'LOCKED' AND route_candidate_id IS NOT NULL
        AND route_binding_source IS NOT NULL AND route_bound_at IS NOT NULL) OR
      (route_binding_status = 'REVIEW' AND route_candidate_id IS NULL
        AND route_binding_source IS NOT NULL AND route_bound_at IS NULL)
    """,
    )
    op.execute("""
      CREATE FUNCTION immutable_route_version() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'route version is immutable' USING ERRCODE = '23514'; END $$;
      CREATE TRIGGER immutable_route_version BEFORE UPDATE OR DELETE ON generation_route_versions
        FOR EACH ROW EXECUTE FUNCTION immutable_route_version();
      CREATE FUNCTION guard_project_route() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF OLD.route_history_json::jsonb IS DISTINCT FROM NEW.route_history_json::jsonb OR
          (OLD.route_binding_status <> 'UNBOUND' AND (
            OLD.route_candidate_id IS DISTINCT FROM NEW.route_candidate_id OR
            OLD.route_binding_source IS DISTINCT FROM NEW.route_binding_source OR
            (OLD.route_binding_status <> NEW.route_binding_status AND NOT
              (OLD.route_binding_status = 'PROVISIONAL'
               AND NEW.route_binding_status = 'LOCKED')))) OR
          (OLD.route_bound_at IS NOT NULL
           AND OLD.route_bound_at IS DISTINCT FROM NEW.route_bound_at)
        THEN RAISE EXCEPTION 'project route binding is immutable' USING ERRCODE = '23514'; END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER guard_project_route BEFORE UPDATE ON projects
        FOR EACH ROW EXECUTE FUNCTION guard_project_route();
      CREATE FUNCTION guard_job_route() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF OLD.selected_route_candidate_id IS DISTINCT FROM NEW.selected_route_candidate_id THEN
          RAISE EXCEPTION 'job route is immutable' USING ERRCODE = '23514'; END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER guard_job_route BEFORE UPDATE ON generation_jobs
        FOR EACH ROW EXECUTE FUNCTION guard_job_route();
    """)


def downgrade():
    op.execute("LOCK TABLE projects IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM projects WHERE route_binding_status <> 'UNBOUND')")
    ):
        raise RuntimeError(
            "Cannot discard project route bindings; use a reviewed forward migration"
        )
    for trigger, table in [
        ("guard_job_route", "generation_jobs"),
        ("guard_project_route", "projects"),
        ("immutable_route_version", "generation_route_versions"),
    ]:
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
        op.execute(f"DROP FUNCTION {trigger}()")
    op.drop_constraint("ck_project_route_state", "projects")
    op.drop_constraint("fk_project_route", "projects", type_="foreignkey")
    for column in [
        "route_history_json",
        "route_bound_at",
        "route_binding_source",
        "route_candidate_id",
        "route_binding_status",
    ]:
        op.drop_column("projects", column)
    op.drop_table("route_admission")
    op.drop_table("generation_route_versions")
