"""Register all issued provider/final writes as durable cleanup work."""

import sqlalchemy as sa

from alembic import op

revision = "0015_provider_artifacts"
down_revision = "0014_project_route_binding"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "provider_artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("generation_jobs.id"), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("generation_attempts.id"), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False, unique=True),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("retain_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("lock_token", sa.String(36)),
        sa.Column("cleaned_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("kind IN ('SOURCE','FINAL')", name="ck_artifact_kind"),
    )
    op.create_index("ix_provider_artifacts_project_id", "provider_artifacts", ["project_id"])
    op.create_index("ix_provider_artifacts_job_id", "provider_artifacts", ["job_id"])
    op.add_column(
        "storage_cleanup_objects",
        sa.Column(
            "not_before", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute("""
      CREATE FUNCTION guard_artifact_identity() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='UPDATE' AND ROW(OLD.project_id,OLD.job_id,OLD.attempt_id,OLD.object_key,OLD.kind)
          IS DISTINCT FROM ROW(NEW.project_id,NEW.job_id,NEW.attempt_id,NEW.object_key,NEW.kind)
        THEN RAISE EXCEPTION 'artifact identity is immutable' USING ERRCODE='23514'; END IF;
        IF NOT EXISTS(SELECT 1 FROM generation_jobs j JOIN generation_attempts a ON a.job_id=j.id
          WHERE j.id=NEW.job_id AND j.project_id=NEW.project_id AND a.id=NEW.attempt_id)
        THEN RAISE EXCEPTION 'artifact ownership mismatch' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER guard_artifact_identity BEFORE INSERT OR UPDATE ON provider_artifacts
        FOR EACH ROW EXECUTE FUNCTION guard_artifact_identity();
    """)


def downgrade():
    op.execute("LOCK TABLE provider_artifacts IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM provider_artifacts)")):
        raise RuntimeError("Cannot discard artifact lifecycle records")
    op.execute("DROP TRIGGER guard_artifact_identity ON provider_artifacts")
    op.execute("DROP FUNCTION guard_artifact_identity()")
    op.drop_table("provider_artifacts")
    op.drop_column("storage_cleanup_objects", "not_before")
