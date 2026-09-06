from sqlalchemy.orm import Session

from app.ledger import finish_reservation
from app.models import GenerationJob


class ProviderSettlementService:
    """Applies the one-time ledger outcome inside the caller's terminal transaction."""

    @staticmethod
    def _settle_succeeded(db: Session, job: GenerationJob) -> None:
        finish_reservation(db, job, settle=True)

    @staticmethod
    def _settle_released(db: Session, job: GenerationJob) -> None:
        finish_reservation(db, job, settle=False)
