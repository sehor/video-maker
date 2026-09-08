import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import Response

from app import application_projects as use_cases
from app.auth import CurrentUser
from app.http_dependencies import Db
from app.models import (
    Project,
    Shot,
    ShotReference,
)
from app.schemas import (
    GenerationOptionsOut,
    ProjectCreate,
    ProjectList,
    ProjectOut,
    ProjectUpdate,
    ShotCreate,
    ShotInputUpdate,
    ShotList,
    ShotOut,
    ShotReferenceCreate,
    ShotReferenceOut,
    ShotUpdate,
)

router = APIRouter(prefix="/v1")


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate, user: CurrentUser, db: Db) -> Project:
    return use_cases.create_project(payload, user, db)


@router.get("/projects", response_model=ProjectList)
def list_projects(
    user: CurrentUser,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> ProjectList:
    return use_cases.list_projects(user, db, limit, cursor)


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: uuid.UUID, user: CurrentUser, db: Db) -> Project:
    return use_cases.get_project(project_id, user, db)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: uuid.UUID, payload: ProjectUpdate, user: CurrentUser, db: Db
) -> Project:
    return use_cases.update_project(project_id, payload, user, db)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: uuid.UUID, user: CurrentUser, db: Db) -> Response:
    use_cases.delete_project(project_id, user, db)
    return Response(status_code=204)


@router.post("/projects/{project_id}/shots", response_model=ShotOut, status_code=201)
def create_shot(project_id: uuid.UUID, payload: ShotCreate, user: CurrentUser, db: Db) -> Shot:
    return use_cases.create_shot(project_id, payload, user, db)


@router.get("/projects/{project_id}/shots", response_model=ShotList)
def list_shots(project_id: uuid.UUID, user: CurrentUser, db: Db) -> ShotList:
    return use_cases.list_shots(project_id, user, db)


@router.get("/shots/{shot_id}", response_model=ShotOut)
def get_shot(shot_id: uuid.UUID, user: CurrentUser, db: Db) -> Shot:
    return use_cases.get_shot(shot_id, user, db)


@router.patch("/shots/{shot_id}", response_model=ShotOut)
def update_shot(shot_id: uuid.UUID, payload: ShotUpdate, user: CurrentUser, db: Db) -> Shot:
    return use_cases.update_shot(shot_id, payload, user, db)


@router.post("/shots/{shot_id}/references", response_model=ShotReferenceOut, status_code=201)
def create_shot_reference(
    shot_id: uuid.UUID, payload: ShotReferenceCreate, user: CurrentUser, db: Db
) -> ShotReference:
    return use_cases.create_shot_reference(shot_id, payload, user, db)


@router.put("/shots/{shot_id}/input", response_model=ShotOut)
def set_shot_input(shot_id: uuid.UUID, payload: ShotInputUpdate, user: CurrentUser, db: Db):
    return use_cases.set_shot_input(shot_id, payload, user, db)


@router.get("/projects/{project_id}/generation-options", response_model=GenerationOptionsOut)
def project_generation_options(project_id: uuid.UUID, user: CurrentUser, db: Db):
    return use_cases.project_generation_options(project_id, user, db)


@router.delete("/shots/{shot_id}/references/{reference_id}", status_code=204)
def delete_shot_reference(
    shot_id: uuid.UUID, reference_id: uuid.UUID, user: CurrentUser, db: Db
) -> Response:
    use_cases.delete_shot_reference(shot_id, reference_id, user, db)
    return Response(status_code=204)
