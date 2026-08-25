import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import ApiIdempotencyRecord, ApiIdempotencyStatus


@dataclass(frozen=True)
class IdempotencyDecision:
    record: ApiIdempotencyRecord | None
    is_replay: bool


def _validate_key(key: str) -> None:
    has_invalid_character = any(
        ord(character) < 33 or ord(character) > 126 for character in key
    )
    if not 1 <= len(key) <= 255 or has_invalid_character:
        raise ApiError(
            400,
            "IDEMPOTENCY_KEY_INVALID",
            "Idempotency-Key 必须为 1 到 255 个可见 ASCII 字符",
        )


def request_hash(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _assert_reusable(record: ApiIdempotencyRecord, expected_hash: str) -> None:
    if record.request_hash != expected_hash:
        raise ApiError(
            409,
            "IDEMPOTENCY_KEY_CONFLICT",
            "相同 Idempotency-Key 不能用于不同请求",
        )
    if record.status != ApiIdempotencyStatus.COMPLETED or record.result_id is None:
        raise ApiError(409, "IDEMPOTENCY_IN_PROGRESS", "相同请求正在处理中，请稍后重试")


def acquire(
    db: Session,
    *,
    user_id: uuid.UUID,
    scope: str,
    key: str | None,
    payload: Any,
) -> IdempotencyDecision:
    if key is None:
        return IdempotencyDecision(record=None, is_replay=False)
    _validate_key(key)
    digest = request_hash(payload)
    identity = (
        ApiIdempotencyRecord.user_id == user_id,
        ApiIdempotencyRecord.scope == scope,
        ApiIdempotencyRecord.idempotency_key == key,
    )
    existing = db.scalar(select(ApiIdempotencyRecord).where(*identity))
    if existing is not None:
        _assert_reusable(existing, digest)
        return IdempotencyDecision(record=existing, is_replay=True)

    record = ApiIdempotencyRecord(
        user_id=user_id,
        scope=scope,
        idempotency_key=key,
        request_hash=digest,
    )
    try:
        with db.begin_nested():
            db.add(record)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(ApiIdempotencyRecord).where(*identity))
        if existing is None:
            raise
        _assert_reusable(existing, digest)
        return IdempotencyDecision(record=existing, is_replay=True)
    return IdempotencyDecision(record=record, is_replay=False)


def complete(
    db: Session,
    record: ApiIdempotencyRecord | None,
    *,
    result_type: str,
    result_id: uuid.UUID,
    response_status: int,
) -> None:
    if record is None:
        return
    record.result_type = result_type
    record.result_id = result_id
    record.response_status = response_status
    record.status = ApiIdempotencyStatus.COMPLETED
    db.flush()


def replay_result_id(decision: IdempotencyDecision, expected_type: str) -> uuid.UUID | None:
    if not decision.is_replay:
        return None
    record = decision.record
    if record is None or record.result_type != expected_type or record.result_id is None:
        raise ApiError(500, "IDEMPOTENCY_RESULT_INVALID", "幂等请求结果记录无效")
    return record.result_id
