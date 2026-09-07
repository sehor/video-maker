from sqlalchemy.orm import Session

from app.ledger import finish_reservation
from app.models import GenerationJob
from app.project_routing import solidify_project_route


class ProviderSettlementService:
    """Applies the one-time ledger outcome inside the caller's terminal transaction."""

    @staticmethod
    def _settle_succeeded(db: Session, job: GenerationJob) -> None:
        solidify_project_route(db, job)
        finish_reservation(db, job, settle=True)

    @staticmethod
    def _settle_released(db: Session, job: GenerationJob) -> None:
        finish_reservation(db, job, settle=False)
