"""Capture input only while the caller holds the owning project's row lock."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.input_snapshot import InputReference, JobInputSnapshot
from app.models import GenerationJob, ProjectAsset, ProjectAssetStatus, Shot, ShotReference


def selected_references(db: Session, shot: Shot) -> tuple[InputReference, ...]:
    rows = db.execute(
        select(ShotReference, ProjectAsset)
        .join(ProjectAsset, ProjectAsset.id == ShotReference.asset_id)
        .where(ShotReference.shot_id == shot.id, ShotReference.reference_role == "FIRST_FRAME")
    ).all()
    if len(rows) > 1:
        raise ApiError(422, "REFERENCE_AMBIGUOUS", "请为镜头选择一张首帧参考图")
    for _reference, asset in rows:
        if asset.project_id != shot.project_id or asset.status != ProjectAssetStatus.READY:
            raise ApiError(422, "REFERENCE_UNAVAILABLE", "参考素材不可用，请重新选择")
        if not asset.media_type.startswith("image/"):
            raise ApiError(422, "REFERENCE_TYPE_INVALID", "首帧参考素材必须是图片")
    return tuple(
        InputReference(asset_id=asset.id, object_key=asset.object_key, reference_role="FIRST_FRAME")
        for _, asset in rows
    )


def capture_input(db: Session, shot: Shot, job: GenerationJob) -> dict:
    return JobInputSnapshot(
        version=1,
        prompt=shot.prompt,
        negative_prompt=None,
        references=selected_references(db, shot),
        duration_ms=job.duration_ms,
        resolution=job.resolution,
        aspect_ratio=job.aspect_ratio,
        mode=job.mock_mode,
    ).model_dump(mode="json")
