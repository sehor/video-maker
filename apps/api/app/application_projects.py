import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.application_queries import (
    encode_cursor,
    owned_project,
    owned_shot,
    parse_cursor,
)
from app.errors import ApiError, not_found
from app.models import (
    AppUser,
    Project,
    ProjectAsset,
    ProjectAssetStatus,
    ProjectStatus,
    Shot,
    ShotReference,
)
from app.project_cleanup import request_project_deletion
from app.routing import get_route_registry
from app.schemas import (
    GenerationOptionsOut,
    ProjectCreate,
    ProjectList,
    ProjectUpdate,
    ShotCreate,
    ShotInputUpdate,
    ShotList,
    ShotReferenceCreate,
    ShotUpdate,
)


def create_project(payload: ProjectCreate, user: AppUser, db: Session) -> Project:
    project = Project(owner_id=user.id, **payload.model_dump())
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def list_projects(
    user: AppUser, db: Session, limit: int = 20, cursor: str | None = None
) -> ProjectList:
    statement = select(Project).where(
        Project.owner_id == user.id, Project.status == ProjectStatus.ACTIVE
    )
    parsed = parse_cursor(cursor)
    if parsed:
        created_at, item_id = parsed
        statement = statement.where(
            or_(
                Project.created_at < created_at,
                and_(Project.created_at == created_at, Project.id < item_id),
            )
        )
    items = list(
        db.scalars(
            statement.order_by(Project.created_at.desc(), Project.id.desc()).limit(limit + 1)
        )
    )
    next_cursor = None
    if len(items) > limit:
        last = items[limit - 1]
        next_cursor = encode_cursor(last.created_at.isoformat(), last.id)
        items = items[:limit]
    return ProjectList(items=items, next_cursor=next_cursor)


def get_project(project_id: uuid.UUID, user: AppUser, db: Session) -> Project:
    return owned_project(db, project_id, user.id)


def update_project(
    project_id: uuid.UUID, payload: ProjectUpdate, user: AppUser, db: Session
) -> Project:
    project = owned_project(db, project_id, user.id, for_update=True)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, key, value)
    db.commit()
    db.refresh(project)
    return project


def delete_project(project_id: uuid.UUID, user: AppUser, db: Session) -> None:
    project = owned_project(db, project_id, user.id, include_deleted=True, for_update=True)
    request_project_deletion(db, project)
    db.commit()
    return


def create_shot(project_id: uuid.UUID, payload: ShotCreate, user: AppUser, db: Session) -> Shot:
    owned_project(db, project_id, user.id, for_update=True)
    shot = Shot(project_id=project_id, **payload.model_dump())
    db.add(shot)
    db.commit()
    db.refresh(shot)
    return shot


def list_shots(project_id: uuid.UUID, user: AppUser, db: Session) -> ShotList:
    owned_project(db, project_id, user.id)
    shots = list(
        db.scalars(
            select(Shot)
            .where(Shot.project_id == project_id)
            .options(selectinload(Shot.references))
            .order_by(Shot.created_at, Shot.id)
        )
    )
    return ShotList(items=shots)


def get_shot(shot_id: uuid.UUID, user: AppUser, db: Session) -> Shot:
    return owned_shot(db, shot_id, user.id)


def update_shot(shot_id: uuid.UUID, payload: ShotUpdate, user: AppUser, db: Session) -> Shot:
    shot = owned_shot(db, shot_id, user.id, for_update=True)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(shot, key, value)
    db.commit()
    db.refresh(shot)
    return shot


def create_shot_reference(
    shot_id: uuid.UUID, payload: ShotReferenceCreate, user: AppUser, db: Session
) -> ShotReference:
    shot = owned_shot(db, shot_id, user.id, for_update=True)
    asset = db.scalar(
        select(ProjectAsset).where(
            ProjectAsset.id == payload.asset_id,
            ProjectAsset.project_id == shot.project_id,
            ProjectAsset.owner_id == user.id,
            ProjectAsset.status == ProjectAssetStatus.READY,
        )
    )
    if asset is None:
        raise not_found("asset")
    reference = ShotReference(
        project_id=shot.project_id,
        shot_id=shot.id,
        asset_id=asset.id,
        reference_role=payload.reference_role,
    )
    db.add(reference)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "SHOT_REFERENCE_EXISTS", "该镜头引用已存在") from exc
    db.refresh(reference)
    return reference


def set_shot_input(shot_id: uuid.UUID, payload: ShotInputUpdate, user: AppUser, db: Session):
    shot = owned_shot(db, shot_id, user.id, for_update=True)
    if payload.asset_id is not None:
        asset = db.scalar(
            select(ProjectAsset).where(
                ProjectAsset.id == payload.asset_id,
                ProjectAsset.project_id == shot.project_id,
                ProjectAsset.owner_id == user.id,
                ProjectAsset.status == ProjectAssetStatus.READY,
            )
        )
        if asset is None:
            raise not_found("asset")
        if not asset.media_type.startswith("image/"):
            raise ApiError(422, "REFERENCE_TYPE_INVALID", "首帧参考素材必须是图片")
    references = list(
        db.scalars(
            select(ShotReference).where(
                ShotReference.shot_id == shot.id, ShotReference.reference_role == "FIRST_FRAME"
            )
        )
    )
    for reference in references:
        db.delete(reference)
    db.flush()
    if payload.asset_id is not None:
        db.add(
            ShotReference(
                project_id=shot.project_id,
                shot_id=shot.id,
                asset_id=payload.asset_id,
                reference_role="FIRST_FRAME",
            )
        )
    db.commit()
    return owned_shot(db, shot.id, user.id)


def project_generation_options(project_id: uuid.UUID, user: AppUser, db: Session):
    project = owned_project(db, project_id, user.id)
    if project.route_binding_status == "REVIEW":
        raise ApiError(409, "PROJECT_ROUTE_REVIEW_REQUIRED", "项目生成配置需要核查")
    registry = get_route_registry()
    route = (
        registry.by_candidate_id(project.route_candidate_id)
        if project.route_candidate_id is not None
        else registry.get(registry.active_key)
    )
    return GenerationOptionsOut(
        requires_reference_image=route.requires_input_claim,
        duration_seconds=[duration // 1000 for duration in sorted(route.durations_ms)],
        resolutions=sorted(route.resolutions),
    )


def delete_shot_reference(
    shot_id: uuid.UUID, reference_id: uuid.UUID, user: AppUser, db: Session
) -> None:
    owned_shot(db, shot_id, user.id, for_update=True)
    reference = db.scalar(
        select(ShotReference).where(
            ShotReference.id == reference_id, ShotReference.shot_id == shot_id
        )
    )
    if reference is None:
        raise not_found("shot_reference")
    db.delete(reference)
    db.commit()
    return
