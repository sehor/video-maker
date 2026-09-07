from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import GenerationJob, GenerationRouteVersion, Project, RouteAdmission
from app.routing import RouteUnavailableError, RouteVersion, get_route_registry


def project_route(db: Session, project_id) -> RouteVersion:
    project = db.scalar(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None or project.route_binding_status == "REVIEW":
        raise ApiError(409, "PROJECT_ROUTE_REVIEW_REQUIRED", "项目历史生成配置需要核查")
    registry = get_route_registry()
    try:
        route = (
            registry.active()
            if project.route_binding_status == "UNBOUND"
            else registry.enabled_route(registry.by_candidate_id(project.route_candidate_id).key)
        )
    except RouteUnavailableError as exc:
        raise ApiError(503, "ROUTE_DISABLED", "该项目的生成服务已暂停接单") from exc
    record = db.get(GenerationRouteVersion, route.candidate_id)
    expected = {
        "provider_code": route.provider_code,
        "workflow_id": route.workflow_id,
        "durations_ms": sorted(route.durations_ms),
        "resolutions": sorted(route.resolutions),
        "aspect_ratios": sorted(route.aspect_ratios),
        "requires_input_claim": route.requires_input_claim,
    }
    if record is None or record.key != route.key or record.definition_json != expected:
        raise ApiError(503, "ROUTE_VERSION_MISMATCH", "生成配置版本尚未就绪")
    # Shared admission locks last until the accepting transaction commits. A
    # database stop update takes an exclusive row lock and therefore serializes.
    admission = db.scalar(
        select(RouteAdmission)
        .where(RouteAdmission.candidate_id == route.candidate_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if admission is None or not admission.enabled:
        raise ApiError(503, "ROUTE_DISABLED", "该项目的生成服务已暂停接单")
    if project.route_binding_status == "UNBOUND":
        project.route_candidate_id = route.candidate_id
        project.route_binding_status = "PROVISIONAL"
        project.route_binding_source = "FIRST_ACCEPTED_JOB"
        db.flush()
    return route


def solidify_project_route(db: Session, job: GenerationJob) -> None:
    project = db.scalar(
        select(Project)
        .where(Project.id == job.project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None or project.route_candidate_id != job.selected_route_candidate_id:
        raise RuntimeError("Successful job does not match its project binding")
    if project.route_binding_status == "PROVISIONAL":
        project.route_binding_status = "LOCKED"
        project.route_bound_at = datetime.now(UTC)
        db.flush()
