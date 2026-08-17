import math
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import (
    AppUser,
    Batch,
    Job,
    LedgerAccount,
    LedgerPosting,
    LedgerTransaction,
    PriceVersion,
    QualityTier,
    Quote,
    QuoteStatus,
    SettlementStatus,
    Shot,
    WalletBalance,
)

USER_AVAILABLE = "USER_AVAILABLE"
USER_RESERVED = "USER_RESERVED"
PLATFORM_ISSUED = "PLATFORM_ISSUED"
PLATFORM_CONSUMED = "PLATFORM_CONSUMED"


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def ensure_default_catalog(db: Session) -> None:
    if db.get(QualityTier, "FAST") is not None:
        return
    tiers = [
        QualityTier(code="FAST", display_name="快速", ledger_unit="FAST_MS", enabled=True),
        QualityTier(code="STUDIO", display_name="工作室", ledger_unit="STUDIO_MS", enabled=True),
        QualityTier(code="CINEMA", display_name="电影", ledger_unit="CINEMA_MS", enabled=False),
    ]
    db.add_all(tiers)
    db.flush()
    db.add_all(
        [
            PriceVersion(tier_code="FAST", version=1, resolution="720p", enabled=True),
            PriceVersion(tier_code="STUDIO", version=1, resolution="720p", enabled=True),
            PriceVersion(tier_code="STUDIO", version=1, resolution="1080p", enabled=False),
            PriceVersion(tier_code="CINEMA", version=1, resolution="720p", enabled=False),
        ]
    )
    db.flush()


def create_quote(
    db: Session,
    user: AppUser,
    shot: Shot,
    tier_code: str,
    resolution: str,
    variant_count: int,
) -> Quote:
    ensure_default_catalog(db)
    tier = db.get(QualityTier, tier_code)
    price = db.scalar(
        select(PriceVersion)
        .where(
            PriceVersion.tier_code == tier_code,
            PriceVersion.resolution == resolution,
            PriceVersion.effective_from <= utcnow(),
            (PriceVersion.effective_until.is_(None) | (PriceVersion.effective_until > utcnow())),
        )
        .order_by(PriceVersion.version.desc())
    )
    if tier is None or not tier.enabled or price is None or not price.enabled:
        raise ApiError(422, "TIER_UNAVAILABLE", "该质量档或分辨率当前不可用")
    duration_ms = shot.duration_seconds * 1000
    reserved_ms = math.ceil(
        duration_ms * variant_count * price.charge_numerator / price.charge_denominator
    )
    quote = Quote(
        owner_id=user.id,
        project_id=shot.project_id,
        shot_id=shot.id,
        price_version_id=price.id,
        tier_code=tier.code,
        ledger_unit=tier.ledger_unit,
        duration_ms=duration_ms,
        variant_count=variant_count,
        resolution=resolution,
        aspect_ratio=shot.aspect_ratio,
        reserved_ms=reserved_ms,
        status=QuoteStatus.OPEN,
        expires_at=utcnow() + timedelta(minutes=15),
    )
    db.add(quote)
    db.commit()
    db.refresh(quote)
    return quote


def account_scope(owner_id: uuid.UUID | None) -> str:
    return f"user:{owner_id}" if owner_id else "platform"


def ensure_account(db: Session, owner_id: uuid.UUID | None, code: str, unit: str) -> LedgerAccount:
    scope = account_scope(owner_id)
    account = db.scalar(
        select(LedgerAccount).where(
            LedgerAccount.scope_key == scope,
            LedgerAccount.code == code,
            LedgerAccount.unit == unit,
        )
    )
    if account is None:
        account = LedgerAccount(scope_key=scope, owner_id=owner_id, code=code, unit=unit)
        db.add(account)
        db.flush()
        db.add(WalletBalance(account_id=account.id, balance_ms=0))
        db.flush()
    return account


def change_balance(
    db: Session, account_id: uuid.UUID, delta: int, *, allow_negative: bool = False
) -> None:
    statement = update(WalletBalance).where(WalletBalance.account_id == account_id)
    if not allow_negative:
        statement = statement.where(WalletBalance.balance_ms + delta >= 0)
    result = db.execute(
        statement.values(balance_ms=WalletBalance.balance_ms + delta, updated_at=utcnow())
    )
    if result.rowcount != 1:
        raise ApiError(409, "WALLET_INSUFFICIENT", "可用生成秒数不足")


def post_transfer(
    db: Session,
    *,
    kind: str,
    idempotency_key: str,
    reference_type: str,
    reference_id: str,
    unit: str,
    debit: LedgerAccount,
    credit: LedgerAccount,
    amount_ms: int,
    reason: str | None = None,
) -> bool:
    if amount_ms <= 0:
        raise ValueError("amount_ms must be positive")
    if debit.unit != unit or credit.unit != unit:
        raise ValueError("ledger units must match")
    if db.scalar(
        select(LedgerTransaction.id).where(
            LedgerTransaction.idempotency_key == idempotency_key
        )
    ):
        return False
    transaction = LedgerTransaction(
        kind=kind,
        idempotency_key=idempotency_key,
        reference_type=reference_type,
        reference_id=reference_id,
        reason=reason,
    )
    db.add(transaction)
    db.flush()
    change_balance(db, debit.id, -amount_ms, allow_negative=debit.code == PLATFORM_ISSUED)
    change_balance(db, credit.id, amount_ms)
    db.add_all(
        [
            LedgerPosting(
                transaction_id=transaction.id,
                account_id=debit.id,
                unit=unit,
                amount_ms=-amount_ms,
            ),
            LedgerPosting(
                transaction_id=transaction.id,
                account_id=credit.id,
                unit=unit,
                amount_ms=amount_ms,
            ),
        ]
    )
    return True


def grant_test_seconds(
    db: Session,
    user: AppUser,
    tier_code: str,
    amount_ms: int,
    idempotency_key: str,
    reason: str,
) -> None:
    ensure_default_catalog(db)
    tier = db.get(QualityTier, tier_code)
    if tier is None:
        raise ApiError(404, "TIER_NOT_FOUND", "质量档不存在")
    issued = ensure_account(db, None, PLATFORM_ISSUED, tier.ledger_unit)
    ensure_account(db, None, PLATFORM_CONSUMED, tier.ledger_unit)
    available = ensure_account(db, user.id, USER_AVAILABLE, tier.ledger_unit)
    ensure_account(db, user.id, USER_RESERVED, tier.ledger_unit)
    if post_transfer(
        db,
        kind="ISSUE",
        idempotency_key=idempotency_key,
        reference_type="test_grant",
        reference_id=str(user.id),
        unit=tier.ledger_unit,
        debit=issued,
        credit=available,
        amount_ms=amount_ms,
        reason=reason,
    ):
        db.commit()
    else:
        db.rollback()


def reserve_quote(
    db: Session,
    quote_id: uuid.UUID,
    user: AppUser,
    shot: Shot,
    job_id: uuid.UUID,
) -> Quote:
    quote = db.scalar(select(Quote).where(Quote.id == quote_id).with_for_update())
    if quote is None or quote.owner_id != user.id:
        raise ApiError(404, "QUOTE_NOT_FOUND", "报价不存在或无权访问")
    if quote.status != QuoteStatus.OPEN:
        raise ApiError(409, "QUOTE_ALREADY_USED", "报价已使用")
    if aware(quote.expires_at) <= utcnow():
        quote.status = QuoteStatus.EXPIRED
        raise ApiError(409, "QUOTE_EXPIRED", "报价已过期")
    if (
        quote.shot_id != shot.id
        or quote.project_id != shot.project_id
        or quote.duration_ms != shot.duration_seconds * 1000
        or quote.aspect_ratio != shot.aspect_ratio
    ):
        raise ApiError(409, "QUOTE_PARAMETERS_CHANGED", "镜头参数已变化，请重新报价")
    tier = db.get(QualityTier, quote.tier_code)
    price = db.get(PriceVersion, quote.price_version_id)
    if tier is None or price is None or not tier.enabled or not price.enabled:
        raise ApiError(409, "QUOTE_TIER_DISABLED", "该质量档已停止接单")
    available = ensure_account(db, user.id, USER_AVAILABLE, quote.ledger_unit)
    reserved = ensure_account(db, user.id, USER_RESERVED, quote.ledger_unit)
    post_transfer(
        db,
        kind="RESERVE",
        idempotency_key=f"job:{job_id}:reserve:v1",
        reference_type="job",
        reference_id=str(job_id),
        unit=quote.ledger_unit,
        debit=available,
        credit=reserved,
        amount_ms=quote.reserved_ms,
    )
    quote.status = QuoteStatus.USED
    quote.used_at = utcnow()
    return quote


def validate_quote(db: Session, quote: Quote, user: AppUser, shot: Shot) -> None:
    if quote.owner_id != user.id:
        raise ApiError(404, "QUOTE_NOT_FOUND", "报价不存在或无权访问")
    if quote.status != QuoteStatus.OPEN:
        raise ApiError(409, "QUOTE_ALREADY_USED", "报价已使用")
    if aware(quote.expires_at) <= utcnow():
        quote.status = QuoteStatus.EXPIRED
        raise ApiError(409, "QUOTE_EXPIRED", "报价已过期")
    if (
        quote.shot_id != shot.id
        or quote.project_id != shot.project_id
        or quote.duration_ms != shot.duration_seconds * 1000
        or quote.aspect_ratio != shot.aspect_ratio
    ):
        raise ApiError(409, "QUOTE_PARAMETERS_CHANGED", "镜头参数已变化，请重新报价")
    tier = db.get(QualityTier, quote.tier_code)
    price = db.get(PriceVersion, quote.price_version_id)
    if tier is None or price is None or not tier.enabled or not price.enabled:
        raise ApiError(409, "QUOTE_TIER_DISABLED", "该质量档已停止接单")


def reserve_batch_quotes(
    db: Session,
    batch: Batch,
    user: AppUser,
    quote_shots: list[tuple[Quote, Shot]],
) -> None:
    if not quote_shots:
        raise ApiError(422, "BATCH_EMPTY", "批次至少包含一个镜头")
    units = {item.ledger_unit for item, _ in quote_shots}
    if len(units) != 1:
        raise ApiError(422, "BATCH_MIXED_UNITS", "一个批次只能使用同一质量档")
    for item, shot in quote_shots:
        validate_quote(db, item, user, shot)
    total_ms = sum(item.reserved_ms for item, _ in quote_shots)
    unit = units.pop()
    available = ensure_account(db, user.id, USER_AVAILABLE, unit)
    reserved = ensure_account(db, user.id, USER_RESERVED, unit)
    post_transfer(
        db,
        kind="BATCH_RESERVE",
        idempotency_key=f"batch:{batch.id}:reserve:v1",
        reference_type="batch",
        reference_id=str(batch.id),
        unit=unit,
        debit=available,
        credit=reserved,
        amount_ms=total_ms,
    )
    batch.ledger_unit = unit
    batch.reserved_ms = total_ms
    for item, _ in quote_shots:
        item.status = QuoteStatus.USED
        item.used_at = utcnow()


def finish_reservation(db: Session, job: Job, *, settle: bool) -> bool:
    if job.settlement_status != SettlementStatus.RESERVED:
        return False
    assert job.ledger_unit is not None and job.reserved_ms is not None
    reserved = ensure_account(db, job.owner_id, USER_RESERVED, job.ledger_unit)
    if settle:
        target = ensure_account(db, None, PLATFORM_CONSUMED, job.ledger_unit)
        changed = post_transfer(
            db,
            kind="SETTLE",
            idempotency_key=f"job:{job.id}:settle:v1",
            reference_type="job",
            reference_id=str(job.id),
            unit=job.ledger_unit,
            debit=reserved,
            credit=target,
            amount_ms=job.reserved_ms,
        )
        if changed:
            job.settlement_status = SettlementStatus.SETTLED
        return changed
    target = ensure_account(db, job.owner_id, USER_AVAILABLE, job.ledger_unit)
    changed = post_transfer(
        db,
        kind="RELEASE",
        idempotency_key=f"job:{job.id}:release:v1",
        reference_type="job",
        reference_id=str(job.id),
        unit=job.ledger_unit,
        debit=reserved,
        credit=target,
        amount_ms=job.reserved_ms,
    )
    if changed:
        job.settlement_status = SettlementStatus.RELEASED
    return changed


def reserve_for_admin_retry(db: Session, job: Job, audit_id: uuid.UUID) -> None:
    assert job.ledger_unit is not None and job.reserved_ms is not None
    available = ensure_account(db, job.owner_id, USER_AVAILABLE, job.ledger_unit)
    reserved = ensure_account(db, job.owner_id, USER_RESERVED, job.ledger_unit)
    post_transfer(
        db,
        kind="ADMIN_RETRY_RESERVE",
        idempotency_key=f"job:{job.id}:admin-retry:{audit_id}:reserve:v1",
        reference_type="job",
        reference_id=str(job.id),
        unit=job.ledger_unit,
        debit=available,
        credit=reserved,
        amount_ms=job.reserved_ms,
    )
    job.settlement_status = SettlementStatus.RESERVED


def reconciliation_report(db: Session) -> dict[str, object]:
    unbalanced = [
        {"transaction_id": str(tx_id), "unit": unit, "sum_ms": total}
        for tx_id, unit, total in db.execute(
            select(
                LedgerPosting.transaction_id,
                LedgerPosting.unit,
                func.sum(LedgerPosting.amount_ms),
            )
            .group_by(LedgerPosting.transaction_id, LedgerPosting.unit)
            .having(func.sum(LedgerPosting.amount_ms) != 0)
        )
    ]
    projection_mismatches = []
    for account_id, balance_ms in db.execute(
        select(WalletBalance.account_id, WalletBalance.balance_ms)
    ):
        posting_total = db.scalar(
            select(func.coalesce(func.sum(LedgerPosting.amount_ms), 0)).where(
                LedgerPosting.account_id == account_id
            )
        )
        if posting_total != balance_ms:
            projection_mismatches.append(
                {
                    "account_id": str(account_id),
                    "balance_ms": balance_ms,
                    "posting_total_ms": posting_total,
                }
            )
    negative_user_balances = [
        {"account_id": str(account_id), "balance_ms": balance_ms}
        for account_id, balance_ms in db.execute(
            select(WalletBalance.account_id, WalletBalance.balance_ms)
            .join(LedgerAccount, LedgerAccount.id == WalletBalance.account_id)
            .where(
                LedgerAccount.code.in_([USER_AVAILABLE, USER_RESERVED]),
                WalletBalance.balance_ms < 0,
            )
        )
    ]
    return {
        "ok": not unbalanced and not projection_mismatches and not negative_user_balances,
        "unbalanced_transactions": unbalanced,
        "projection_mismatches": projection_mismatches,
        "negative_user_balances": negative_user_balances,
    }


def wallet_balances(db: Session, user_id: uuid.UUID) -> dict[str, dict[str, int]]:
    rows = db.execute(
        select(LedgerAccount.unit, LedgerAccount.code, WalletBalance.balance_ms)
        .join(WalletBalance, WalletBalance.account_id == LedgerAccount.id)
        .where(LedgerAccount.owner_id == user_id)
    )
    result: dict[str, dict[str, int]] = {}
    for unit, code, balance in rows:
        result.setdefault(unit, {})[code] = balance
    return result
