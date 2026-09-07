import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ApiError, not_found
from app.models import (
    AppUser,
    BatchStatus,
    GenerationBatch,
    GenerationJob,
    JobStatus,
    LedgerAccount,
    LedgerPosting,
    LedgerTransaction,
    PriceVersion,
    Project,
    ProjectStatus,
    QualityTier,
    Quote,
    QuoteStatus,
    SettlementStatus,
    Shot,
    WalletBalance,
)
from app.state_machine import JOB_TERMINAL_STATUSES, transition_job

USER_AVAILABLE = "USER_AVAILABLE"
USER_RESERVED = "USER_RESERVED"
PLATFORM_ISSUED = "PLATFORM_ISSUED"
PLATFORM_CONSUMED = "PLATFORM_CONSUMED"
PLATFORM_EXPIRED = "PLATFORM_EXPIRED"


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def ensure_default_catalog(db: Session) -> None:
    tiers = (
        ("FAST", "快速", "FAST_MS", True),
        ("STUDIO", "工作室", "STUDIO_MS", True),
        ("CINEMA", "电影", "CINEMA_MS", False),
    )
    for code, display_name, billing_unit, enabled in tiers:
        if db.get(QualityTier, code) is None:
            db.add(
                QualityTier(
                    code=code,
                    display_name=display_name,
                    billing_unit=billing_unit,
                    enabled=enabled,
                )
            )
    db.flush()

    prices = (
        ("FAST", 1, "720P", True),
        ("STUDIO", 1, "720P", True),
        ("STUDIO", 1, "1080P", False),
        ("CINEMA", 1, "720P", False),
    )
    for tier_code, version, resolution, enabled in prices:
        exists = db.scalar(
            select(PriceVersion.id).where(
                PriceVersion.tier_code == tier_code,
                PriceVersion.version == version,
                PriceVersion.resolution == resolution,
            )
        )
        if exists is None:
            db.add(
                PriceVersion(
                    tier_code=tier_code,
                    version=version,
                    resolution=resolution,
                    charge_numerator=1,
                    charge_denominator=1,
                    enabled=enabled,
                )
            )
    db.flush()


def create_quote(
    db: Session,
    user: AppUser,
    shot: Shot,
    *,
    tier_code: str,
    resolution: str,
    variant_count: int,
) -> Quote:
    ensure_default_catalog(db)
    now = utcnow()
    tier = db.get(QualityTier, tier_code)
    price = db.scalar(
        select(PriceVersion)
        .where(
            PriceVersion.tier_code == tier_code,
            PriceVersion.resolution == resolution,
            PriceVersion.enabled.is_(True),
            PriceVersion.effective_from <= now,
            or_(PriceVersion.effective_until.is_(None), PriceVersion.effective_until > now),
        )
        .order_by(PriceVersion.version.desc())
        .limit(1)
    )
    if tier is None or not tier.enabled or price is None:
        raise ApiError(422, "TIER_UNAVAILABLE", "该质量档或分辨率当前不可用")

    duration_ms = shot.duration_seconds * 1000
    reserved_ms = (
        duration_ms * variant_count * price.charge_numerator + price.charge_denominator - 1
    ) // price.charge_denominator
    quote = Quote(
        user_id=user.id,
        project_id=shot.project_id,
        shot_id=shot.id,
        price_version_id=price.id,
        tier_code=tier.code,
        billing_unit=tier.billing_unit,
        duration_ms=duration_ms,
        variant_count=variant_count,
        resolution=resolution,
        aspect_ratio=shot.aspect_ratio,
        reserved_ms=reserved_ms,
        status=QuoteStatus.OPEN,
        expires_at=now + timedelta(minutes=15),
    )
    db.add(quote)
    db.flush()
    return quote


def _scope(owner_id: uuid.UUID | None) -> tuple[str, str]:
    if owner_id is None:
        return "platform", "PLATFORM"
    return f"user:{owner_id}", "USER"


def ensure_account(
    db: Session,
    owner_id: uuid.UUID | None,
    account_type: str,
    unit: str,
) -> LedgerAccount:
    scope_key, owner_type = _scope(owner_id)
    account = db.scalar(
        select(LedgerAccount).where(
            LedgerAccount.scope_key == scope_key,
            LedgerAccount.account_type == account_type,
            LedgerAccount.unit == unit,
        )
    )
    if account is not None:
        return account

    try:
        with db.begin_nested():
            account = LedgerAccount(
                scope_key=scope_key,
                owner_type=owner_type,
                owner_id=owner_id,
                account_type=account_type,
                unit=unit,
            )
            db.add(account)
            db.flush()
            db.add(WalletBalance(account_id=account.id, balance_ms=0, version=0))
            db.flush()
        return account
    except IntegrityError:
        account = db.scalar(
            select(LedgerAccount).where(
                LedgerAccount.scope_key == scope_key,
                LedgerAccount.account_type == account_type,
                LedgerAccount.unit == unit,
            )
        )
        if account is None:
            raise
        return account


def _transfer_signature(
    *,
    debit: LedgerAccount,
    credit: LedgerAccount,
    amount_ms: int,
    reason: str | None,
) -> dict[str, str | int | None]:
    return {
        "debit_account_id": str(debit.id),
        "credit_account_id": str(credit.id),
        "amount_ms": amount_ms,
        "reason": reason,
    }


def _assert_same_transfer(
    transaction: LedgerTransaction,
    *,
    tx_type: str,
    reference_type: str,
    reference_id: str,
    unit: str,
    signature: dict[str, str | int | None],
) -> None:
    if (
        transaction.tx_type != tx_type
        or transaction.reference_type != reference_type
        or transaction.reference_id != reference_id
        or transaction.unit != unit
        or transaction.metadata_json.get("transfer") != signature
    ):
        raise ApiError(
            409,
            "LEDGER_IDEMPOTENCY_CONFLICT",
            "幂等键已用于不同的账本操作",
        )


def _change_balance(
    db: Session,
    account: LedgerAccount,
    delta_ms: int,
    *,
    allow_negative: bool,
) -> None:
    statement = update(WalletBalance).where(WalletBalance.account_id == account.id)
    if not allow_negative:
        statement = statement.where(WalletBalance.balance_ms + delta_ms >= 0)
    result = db.execute(
        statement.values(
            balance_ms=WalletBalance.balance_ms + delta_ms,
            version=WalletBalance.version + 1,
            updated_at=utcnow(),
        )
    )
    if result.rowcount == 1:
        return
    if delta_ms < 0 and not allow_negative:
        raise ApiError(409, "WALLET_INSUFFICIENT", "可用生成秒数不足")
    raise ApiError(500, "LEDGER_ACCOUNT_MISSING", "账本账户不存在")


def post_transfer(
    db: Session,
    *,
    tx_type: str,
    idempotency_key: str,
    reference_type: str,
    reference_id: str,
    unit: str,
    debit: LedgerAccount,
    credit: LedgerAccount,
    amount_ms: int,
    reason: str | None = None,
) -> tuple[LedgerTransaction, bool]:
    if amount_ms <= 0:
        raise ValueError("amount_ms must be positive")
    if debit.unit != unit or credit.unit != unit:
        raise ValueError("ledger account units must match the transaction unit")
    signature = _transfer_signature(
        debit=debit,
        credit=credit,
        amount_ms=amount_ms,
        reason=reason,
    )
    existing = db.scalar(
        select(LedgerTransaction).where(
            or_(
                LedgerTransaction.idempotency_key == idempotency_key,
                (
                    (LedgerTransaction.tx_type == tx_type)
                    & (LedgerTransaction.reference_type == reference_type)
                    & (LedgerTransaction.reference_id == reference_id)
                ),
            )
        )
    )
    if existing is not None:
        _assert_same_transfer(
            existing,
            tx_type=tx_type,
            reference_type=reference_type,
            reference_id=reference_id,
            unit=unit,
            signature=signature,
        )
        return existing, False

    transaction = LedgerTransaction(
        tx_type=tx_type,
        idempotency_key=idempotency_key,
        reference_type=reference_type,
        reference_id=reference_id,
        unit=unit,
        metadata_json={"transfer": signature},
    )
    try:
        with db.begin_nested():
            db.add(transaction)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(LedgerTransaction).where(
                or_(
                    LedgerTransaction.idempotency_key == idempotency_key,
                    (
                        (LedgerTransaction.tx_type == tx_type)
                        & (LedgerTransaction.reference_type == reference_type)
                        & (LedgerTransaction.reference_id == reference_id)
                    ),
                )
            )
        )
        if existing is None:
            raise
        _assert_same_transfer(
            existing,
            tx_type=tx_type,
            reference_type=reference_type,
            reference_id=reference_id,
            unit=unit,
            signature=signature,
        )
        return existing, False

    _change_balance(
        db,
        debit,
        -amount_ms,
        allow_negative=debit.owner_type == "PLATFORM",
    )
    _change_balance(db, credit, amount_ms, allow_negative=True)
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
    db.flush()
    return transaction, True


def grant_seconds(
    db: Session,
    user: AppUser,
    *,
    tier_code: str,
    amount_ms: int,
    idempotency_key: str,
    reason: str,
) -> LedgerTransaction:
    ensure_default_catalog(db)
    tier = db.get(QualityTier, tier_code)
    if tier is None or not tier.enabled:
        raise ApiError(422, "TIER_UNAVAILABLE", "该质量档当前不可用")
    issued = ensure_account(db, None, PLATFORM_ISSUED, tier.billing_unit)
    available = ensure_account(db, user.id, USER_AVAILABLE, tier.billing_unit)
    ensure_account(db, user.id, USER_RESERVED, tier.billing_unit)
    transaction, _ = post_transfer(
        db,
        tx_type="GRANT",
        idempotency_key=idempotency_key,
        reference_type="test_grant",
        reference_id=idempotency_key,
        unit=tier.billing_unit,
        debit=issued,
        credit=available,
        amount_ms=amount_ms,
        reason=reason,
    )
    return transaction


def _quote_snapshot(db: Session, quote: Quote) -> dict[str, object]:
    price = db.get(PriceVersion, quote.price_version_id)
    if price is None:
        raise ApiError(500, "QUOTE_PRICE_MISSING", "报价价格版本不存在")
    return {
        "quote_id": str(quote.id),
        "tier_code": quote.tier_code,
        "billing_unit": quote.billing_unit,
        "price_version_id": str(price.id),
        "price_version": price.version,
        "charge_numerator": price.charge_numerator,
        "charge_denominator": price.charge_denominator,
        "duration_ms": quote.duration_ms,
        "variant_count": quote.variant_count,
        "resolution": quote.resolution,
        "aspect_ratio": quote.aspect_ratio,
        "reserved_ms": quote.reserved_ms,
        "quoted_at": quote.created_at.isoformat(),
        "expires_at": quote.expires_at.isoformat(),
    }


def _validate_quote_terms(
    db: Session,
    user: AppUser,
    shot: Shot,
    quote: Quote,
    now: datetime,
) -> None:
    if quote.user_id != user.id or quote.project_id != shot.project_id or quote.shot_id != shot.id:
        raise not_found("quote")
    if quote.status != QuoteStatus.OPEN:
        raise ApiError(409, "QUOTE_ALREADY_USED", "报价已使用")
    if aware(quote.expires_at) <= now:
        raise ApiError(409, "QUOTE_EXPIRED", "报价已过期")
    if quote.duration_ms != shot.duration_seconds * 1000 or quote.aspect_ratio != shot.aspect_ratio:
        raise ApiError(409, "QUOTE_PARAMETERS_CHANGED", "镜头参数已变化，请重新报价")
    tier = db.get(QualityTier, quote.tier_code)
    if tier is None or not tier.enabled or quote.resolution != "720P":
        raise ApiError(422, "TIER_UNAVAILABLE", "该质量档或分辨率当前不可用")


def reserve_quotes_for_batch(
    db: Session,
    user: AppUser,
    batch: GenerationBatch,
    quote_ids: list[uuid.UUID],
) -> list[tuple[Quote, Shot, dict[str, object]]]:
    quotes = list(
        db.scalars(
            select(Quote)
            .where(Quote.id.in_(quote_ids), Quote.user_id == user.id)
            .order_by(Quote.id)
        )
    )
    by_id = {quote.id: quote for quote in quotes}
    if len(by_id) != len(quote_ids):
        raise not_found("quote")
    project_ids = {quote.project_id for quote in quotes}
    if len(project_ids) != 1:
        raise ApiError(422, "BATCH_PROJECT_MISMATCH", "Batch 中的报价必须属于同一项目")
    project = db.scalar(
        select(Project)
        .where(
            Project.id == next(iter(project_ids)),
            Project.owner_id == user.id,
            Project.status == ProjectStatus.ACTIVE,
        )
        .with_for_update()
    )
    if project is None:
        raise not_found("project")

    # Match single-job lock order: Project, then Quote, then wallet accounts.
    quotes = list(db.scalars(select(Quote).where(Quote.id.in_(quote_ids), Quote.user_id == user.id)
                            .order_by(Quote.id).with_for_update()
                            .execution_options(populate_existing=True)))
    by_id = {quote.id: quote for quote in quotes}

    now = utcnow()
    claimed: list[tuple[Quote, Shot, dict[str, object]]] = []
    project_id: uuid.UUID | None = None
    billing_unit: str | None = None
    for quote_id in quote_ids:
        quote = by_id[quote_id]
        shot = db.get(Shot, quote.shot_id)
        if shot is None:
            raise not_found("shot")
        _validate_quote_terms(db, user, shot, quote, now)
        if project_id is None:
            project_id = quote.project_id
        elif quote.project_id != project_id:
            raise ApiError(422, "BATCH_PROJECT_MISMATCH", "Batch 中的报价必须属于同一项目")
        if billing_unit is None:
            billing_unit = quote.billing_unit
        elif quote.billing_unit != billing_unit:
            raise ApiError(422, "BATCH_LEDGER_UNIT_MISMATCH", "Batch 中的报价账本单位必须一致")
        claimed.append((quote, shot, _quote_snapshot(db, quote)))

    changed = db.execute(
        update(Quote)
        .where(
            Quote.id.in_(quote_ids),
            Quote.user_id == user.id,
            Quote.status == QuoteStatus.OPEN,
            Quote.expires_at > now,
        )
        .values(status=QuoteStatus.USED, used_at=now)
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != len(quote_ids):
        raise ApiError(409, "QUOTE_ALREADY_USED", "Batch 中的报价已使用或已过期")

    assert project_id is not None and billing_unit is not None
    total_reserved_ms = sum(quote.reserved_ms for quote, _, _ in claimed)
    available = ensure_account(db, user.id, USER_AVAILABLE, billing_unit)
    reserved = ensure_account(db, user.id, USER_RESERVED, billing_unit)
    transaction, _ = post_transfer(
        db,
        tx_type="RESERVE",
        idempotency_key=f"batch:{batch.id}:reserve:v1",
        reference_type="batch",
        reference_id=str(batch.id),
        unit=billing_unit,
        debit=available,
        credit=reserved,
        amount_ms=total_reserved_ms,
    )
    batch.project_id = project_id
    batch.ledger_unit = billing_unit
    batch.reserved_amount_ms = total_reserved_ms
    batch.reserved_tx_id = transaction.id
    db.add(batch)
    db.flush()
    return claimed


def reserve_quote_for_job(
    db: Session,
    user: AppUser,
    shot: Shot,
    quote_id: uuid.UUID,
    job: GenerationJob,
) -> None:
    quote = db.scalar(
        select(Quote).where(
            Quote.id == quote_id,
            Quote.user_id == user.id,
            Quote.project_id == shot.project_id,
            Quote.shot_id == shot.id,
        )
    )
    if quote is None:
        raise not_found("quote")
    now = utcnow()
    _validate_quote_terms(db, user, shot, quote, now)

    claimed = db.execute(
        update(Quote)
        .where(
            Quote.id == quote.id,
            Quote.status == QuoteStatus.OPEN,
            Quote.expires_at > now,
        )
        .values(status=QuoteStatus.USED, used_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise ApiError(409, "QUOTE_ALREADY_USED", "报价已使用或已过期")

    available = ensure_account(db, user.id, USER_AVAILABLE, quote.billing_unit)
    reserved = ensure_account(db, user.id, USER_RESERVED, quote.billing_unit)
    transaction, _ = post_transfer(
        db,
        tx_type="RESERVE",
        idempotency_key=f"job:{job.id}:reserve:v1",
        reference_type="job",
        reference_id=str(job.id),
        unit=quote.billing_unit,
        debit=available,
        credit=reserved,
        amount_ms=quote.reserved_ms,
    )
    job.quote_id = quote.id
    job.quote_snapshot_json = _quote_snapshot(db, quote)
    job.tier_code = quote.tier_code
    job.duration_ms = quote.duration_ms
    job.resolution = quote.resolution
    job.aspect_ratio = quote.aspect_ratio
    job.ledger_unit = quote.billing_unit
    job.reserved_amount_ms = quote.reserved_ms
    job.reserved_tx_id = transaction.id
    job.settlement_status = SettlementStatus.RESERVED
    if not transition_job(
        db,
        job,
        JobStatus.RESERVED,
        "job.reserved",
        f"job:{job.id}:reserved:v1",
        {"ledger_transaction_id": str(transaction.id)},
    ):
        raise ApiError(409, "JOB_STATE_CONFLICT", "任务状态已变化")
    db.flush()


def finish_reservation(db: Session, job: GenerationJob, *, settle: bool) -> bool:
    desired = SettlementStatus.SETTLED if settle else SettlementStatus.RELEASED
    if job.settlement_status == desired:
        return False
    if job.settlement_status != SettlementStatus.RESERVED:
        raise ApiError(409, "LEDGER_ALREADY_FINALIZED", "任务冻结已完成其他终结操作")
    if job.ledger_unit is None or job.reserved_amount_ms is None:
        raise ApiError(500, "LEDGER_RESERVATION_MISSING", "任务缺少冻结账本信息")

    changed = db.execute(
        update(GenerationJob)
        .where(
            GenerationJob.id == job.id,
            GenerationJob.settlement_status == SettlementStatus.RESERVED,
        )
        .values(settlement_status=desired)
        .execution_options(synchronize_session="fetch")
    )
    if changed.rowcount != 1:
        db.refresh(job)
        if job.settlement_status == desired:
            return False
        raise ApiError(409, "LEDGER_ALREADY_FINALIZED", "任务冻结已完成其他终结操作")

    reserved = ensure_account(db, job.user_id, USER_RESERVED, job.ledger_unit)
    if settle:
        target = ensure_account(db, None, PLATFORM_CONSUMED, job.ledger_unit)
        tx_type = "SETTLE"
    else:
        target = ensure_account(db, job.user_id, USER_AVAILABLE, job.ledger_unit)
        tx_type = "RELEASE"
    post_transfer(
        db,
        tx_type=tx_type,
        idempotency_key=f"job:{job.id}:{tx_type.lower()}:v1",
        reference_type="job",
        reference_id=str(job.id),
        unit=job.ledger_unit,
        debit=reserved,
        credit=target,
        amount_ms=job.reserved_amount_ms,
    )
    if job.batch_id is not None:
        refresh_batch_status(db, job.batch_id)
    return True


def refresh_batch_status(db: Session, batch_id: uuid.UUID) -> BatchStatus:
    batch = db.scalar(
        select(GenerationBatch).where(GenerationBatch.id == batch_id).with_for_update()
    )
    if batch is None:
        raise ApiError(500, "BATCH_MISSING", "任务关联的 Batch 不存在")
    statuses = list(
        db.scalars(select(GenerationJob.status).where(GenerationJob.batch_id == batch_id))
    )
    if statuses and all(status in JOB_TERMINAL_STATUSES for status in statuses):
        succeeded = sum(status == JobStatus.SUCCEEDED for status in statuses)
        if succeeded == len(statuses):
            desired = BatchStatus.SUCCEEDED
        elif succeeded:
            desired = BatchStatus.PARTIAL
        else:
            desired = BatchStatus.FAILED_FINAL
    else:
        desired = BatchStatus.RUNNING
    batch.status = desired
    return desired


def wallet_balances(db: Session, user_id: uuid.UUID) -> dict[str, dict[str, int]]:
    balances: dict[str, dict[str, int]] = {}
    for unit, account_type, balance_ms in db.execute(
        select(LedgerAccount.unit, LedgerAccount.account_type, WalletBalance.balance_ms)
        .join(WalletBalance, WalletBalance.account_id == LedgerAccount.id)
        .where(LedgerAccount.owner_id == user_id)
    ):
        balances.setdefault(unit, {})[account_type] = balance_ms
    return balances
