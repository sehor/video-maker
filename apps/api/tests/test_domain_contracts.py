import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from app.models import (
    AppUser,
    AttemptStatus,
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    JobStatus,
    OutputValidationStatus,
    Project,
    ProjectAsset,
    ProjectAssetStatus,
    Shot,
    ShotReference,
)


@pytest.fixture
def contract_db() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    # PostgreSQL AddConstraint DDL changes per-constraint compilation state. Clone
    # metadata so an earlier PostgreSQL fixture cannot suppress SQLite's inline FKs.
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def seed_projects(db: Session) -> tuple[AppUser, AppUser, Project, Project, Shot, Shot]:
    owner = AppUser(auth_subject="owner")
    other = AppUser(auth_subject="other")
    db.add_all([owner, other])
    db.flush()
    project = Project(owner_id=owner.id, name="owner-project")
    other_project = Project(owner_id=other.id, name="other-project")
    db.add_all([project, other_project])
    db.flush()
    shot = Shot(
        project_id=project.id,
        title="shot",
        prompt="prompt",
        duration_seconds=2,
        aspect_ratio="16:9",
    )
    other_shot = Shot(
        project_id=other_project.id,
        title="other-shot",
        prompt="prompt",
        duration_seconds=2,
        aspect_ratio="16:9",
    )
    db.add_all([shot, other_shot])
    db.commit()
    return owner, other, project, other_project, shot, other_shot


def new_asset(project: Project, owner: AppUser, suffix: str) -> ProjectAsset:
    return ProjectAsset(
        project_id=project.id,
        owner_id=owner.id,
        object_key=f"assets/{suffix}",
        original_filename=f"{suffix}.png",
        media_type="image/png",
        size_bytes=8,
        sha256=suffix.rjust(64, "0"),
        status=ProjectAssetStatus.READY,
    )


def new_job(owner: AppUser, project: Project, shot: Shot) -> GenerationJob:
    return GenerationJob(
        user_id=owner.id,
        project_id=project.id,
        shot_id=shot.id,
        tier_code="FAST",
        duration_ms=2000,
        resolution="720P",
        aspect_ratio="16:9",
        variant_index=0,
        quote_snapshot_json={},
        status=JobStatus.CREATED,
        mock_mode="success",
    )


def test_asset_job_and_shot_must_share_the_project_owner(contract_db: Session) -> None:
    owner, other, project, _other_project, shot, other_shot = seed_projects(contract_db)

    contract_db.add(new_asset(project, other, "wrong-owner"))
    with pytest.raises(IntegrityError):
        contract_db.commit()
    contract_db.rollback()

    contract_db.add(new_job(other, project, shot))
    with pytest.raises(IntegrityError):
        contract_db.commit()
    contract_db.rollback()

    contract_db.add(new_job(owner, project, other_shot))
    with pytest.raises(IntegrityError):
        contract_db.commit()


def test_shot_reference_cannot_cross_projects(contract_db: Session) -> None:
    owner, other, project, other_project, shot, _other_shot = seed_projects(contract_db)
    asset = new_asset(project, owner, "same-project")
    other_asset = new_asset(other_project, other, "other-project")
    contract_db.add_all([asset, other_asset])
    contract_db.commit()

    contract_db.add(
        ShotReference(
            project_id=project.id,
            shot_id=shot.id,
            asset_id=other_asset.id,
            reference_role="STYLE",
        )
    )
    with pytest.raises(IntegrityError):
        contract_db.commit()


def test_attempt_numbers_start_at_one_and_are_unique_per_job(contract_db: Session) -> None:
    owner, _other, project, _other_project, shot, _other_shot = seed_projects(contract_db)
    job = new_job(owner, project, shot)
    contract_db.add(job)
    contract_db.commit()
    contract_db.add(
        GenerationAttempt(
            job_id=job.id,
            attempt_no=0,
            provider_code="mock",
            workflow_version="mock:v1",
            status=AttemptStatus.CREATED,
        )
    )
    with pytest.raises(IntegrityError):
        contract_db.commit()
    contract_db.rollback()

    contract_db.add_all(
        [
            GenerationAttempt(
                job_id=job.id,
                attempt_no=1,
                provider_code="mock",
                workflow_version="mock:v1",
                status=AttemptStatus.CREATED,
            ),
            GenerationAttempt(
                job_id=job.id,
                attempt_no=1,
                provider_code="mock",
                workflow_version="mock:v1",
                status=AttemptStatus.CREATED,
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        contract_db.commit()


def test_final_output_must_belong_to_its_job(contract_db: Session) -> None:
    owner, _other, project, _other_project, shot, _other_shot = seed_projects(contract_db)
    first_job = new_job(owner, project, shot)
    second_job = new_job(owner, project, shot)
    contract_db.add_all([first_job, second_job])
    contract_db.flush()
    first_attempt = GenerationAttempt(
        job_id=first_job.id,
        attempt_no=1,
        provider_code="mock",
        workflow_version="mock:v1",
        status=AttemptStatus.SUCCEEDED,
    )
    second_attempt = GenerationAttempt(
        job_id=second_job.id,
        attempt_no=1,
        provider_code="mock",
        workflow_version="mock:v1",
        status=AttemptStatus.SUCCEEDED,
    )
    contract_db.add_all([first_attempt, second_attempt])
    contract_db.flush()
    first_output = GenerationOutput(
        job_id=first_job.id,
        attempt_id=first_attempt.id,
        object_key="outputs/first.mp4",
        media_type="video/mp4",
        size_bytes=1,
        sha256="1" * 64,
        validation_status=OutputValidationStatus.VALID,
    )
    second_output = GenerationOutput(
        job_id=second_job.id,
        attempt_id=second_attempt.id,
        object_key="outputs/second.mp4",
        media_type="video/mp4",
        size_bytes=1,
        sha256="2" * 64,
        validation_status=OutputValidationStatus.VALID,
    )
    contract_db.add_all([first_output, second_output])
    contract_db.commit()

    first_job.final_output_id = second_output.id
    with pytest.raises(IntegrityError):
        contract_db.commit()
    contract_db.rollback()

    first_job.final_output_id = first_output.id
    contract_db.commit()
    assert contract_db.get(GenerationJob, first_job.id).final_output_id == first_output.id


def test_output_attempt_must_belong_to_the_same_job(contract_db: Session) -> None:
    owner, _other, project, _other_project, shot, _other_shot = seed_projects(contract_db)
    first_job = new_job(owner, project, shot)
    second_job = new_job(owner, project, shot)
    contract_db.add_all([first_job, second_job])
    contract_db.flush()
    attempt = GenerationAttempt(
        job_id=first_job.id,
        attempt_no=1,
        provider_code="mock",
        workflow_version="mock:v1",
        status=AttemptStatus.SUCCEEDED,
    )
    contract_db.add(attempt)
    contract_db.flush()
    contract_db.add(
        GenerationOutput(
            id=uuid.uuid4(),
            job_id=second_job.id,
            attempt_id=attempt.id,
            object_key="outputs/mismatch.mp4",
            media_type="video/mp4",
            size_bytes=1,
            sha256="3" * 64,
            validation_status=OutputValidationStatus.VALID,
        )
    )
    with pytest.raises(IntegrityError):
        contract_db.commit()


def test_shot_stores_specs_while_assets_and_references_have_separate_lifecycles() -> None:
    shot_columns = set(Shot.__table__.columns.keys())
    assert {"title", "prompt", "duration_seconds", "aspect_ratio"} <= shot_columns
    assert {
        "asset_id",
        "object_key",
        "media_type",
        "size_bytes",
        "sha256",
        "generation_output_id",
    }.isdisjoint(shot_columns)
    assert ShotReference.__table__.name == "shot_references"
    assert ProjectAsset.__table__.name == "project_assets"
    assert GenerationOutput.__table__.name == "generation_outputs"
