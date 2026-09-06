import uuid
from dataclasses import replace
from unittest.mock import Mock

import pytest

from app.artifacts import ArtifactReceiptError, RemoteArtifactReceiver
from app.media import MediaErrorCode, MediaPolicy, MediaValidationError, MediaValidator
from app.provider import FailureCode, ProviderOutput
from app.storage import LocalObjectStorage

POLICY = MediaPolicy(expected_duration_ms=5000, expected_aspect_ratio="16:9")
OUTPUT = ProviderOutput(duration_ms=5000, width=1280, height=720, fps=16, codec="h264")


@pytest.mark.parametrize("width,height,aspect", [(1280, 720, "16:9"), (720, 1280, "9:16")])
def test_metadata_validation_has_no_file_or_process_dependency(width, height, aspect):
    facts = MediaValidator().validate(
        replace(OUTPUT, width=width, height=height),
        MediaPolicy(expected_duration_ms=5000, expected_aspect_ratio=aspect),
    )
    assert (facts.width, facts.height, facts.duration_ms, facts.codec) == (
        width,
        height,
        5000,
        "h264",
    )
    assert facts.frame_rate == 16
    assert not hasattr(facts, "decodable")


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("media_type", "video/webm", MediaErrorCode.CONTAINER_INVALID),
        ("codec", None, MediaErrorCode.CODEC_INVALID),
        ("codec", "vp9", MediaErrorCode.CODEC_INVALID),
        ("width", None, MediaErrorCode.RESOLUTION_INVALID),
        ("height", 1080, MediaErrorCode.RESOLUTION_INVALID),
        ("width", True, MediaErrorCode.RESOLUTION_INVALID),
        ("width", 1280.0, MediaErrorCode.RESOLUTION_INVALID),
        ("duration_ms", None, MediaErrorCode.DURATION_INVALID),
        ("duration_ms", True, MediaErrorCode.DURATION_INVALID),
        ("duration_ms", "5000", MediaErrorCode.DURATION_INVALID),
        ("duration_ms", 0, MediaErrorCode.DURATION_INVALID),
        ("duration_ms", 4749, MediaErrorCode.DURATION_INVALID),
        ("duration_ms", 5251, MediaErrorCode.DURATION_INVALID),
        ("fps", None, MediaErrorCode.FRAME_RATE_INVALID),
        ("fps", True, MediaErrorCode.FRAME_RATE_INVALID),
        ("fps", "16", MediaErrorCode.FRAME_RATE_INVALID),
        ("fps", 0, MediaErrorCode.FRAME_RATE_INVALID),
        ("fps", -1, MediaErrorCode.FRAME_RATE_INVALID),
        ("fps", float("nan"), MediaErrorCode.FRAME_RATE_INVALID),
        ("fps", float("inf"), MediaErrorCode.FRAME_RATE_INVALID),
        ("fps", 241, MediaErrorCode.FRAME_RATE_INVALID),
    ],
)
def test_invalid_or_missing_metadata_is_rejected(field, value, code):
    with pytest.raises(MediaValidationError) as caught:
        MediaValidator().validate(replace(OUTPUT, **{field: value}), POLICY)
    assert caught.value.code == code
    assert caught.value.failure_code == FailureCode.OUTPUT_INVALID_MEDIA


@pytest.mark.parametrize("duration", [4750, 5000, 5250])
def test_duration_tolerance_is_inclusive(duration):
    assert (
        MediaValidator().validate(replace(OUTPUT, duration_ms=duration), POLICY).duration_ms
        == duration
    )


def test_wrong_orientation_is_rejected():
    with pytest.raises(MediaValidationError, match="画幅"):
        MediaValidator().validate(replace(OUTPUT, width=720, height=1280), POLICY)


def test_invalid_metadata_is_rejected_before_storage_access():
    job, attempt = uuid.uuid4(), uuid.uuid4()
    storage = Mock()
    receiver = RemoteArtifactReceiver(storage, MediaValidator(), max_bytes=1024)
    output = replace(
        OUTPUT,
        object_key=f"provider-outputs/{job}/{attempt}/output.mp4",
        size_bytes=1,
        sha256="a" * 64,
        duration_ms=None,
    )
    with pytest.raises(ArtifactReceiptError) as caught:
        receiver.receive_and_publish(job_id=job, attempt_id=attempt, output=output, policy=POLICY)
    assert caught.value.failure.code == FailureCode.OUTPUT_INVALID_MEDIA
    assert storage.mock_calls == []


def test_receipt_checks_integrity_without_decoding_video(tmp_path):
    # Deliberately opaque bytes: media content is not decoded by the control plane.
    content = b"opaque-provider-video"
    job, attempt = uuid.uuid4(), uuid.uuid4()
    storage = LocalObjectStorage(tmp_path, b"test-storage-secret-at-least-32-bytes")
    claim = storage.write_claim(
        f"provider-outputs/{job}/{attempt}", mime_type="video/mp4", max_bytes=len(content)
    )
    source = storage.put(claim, content, "video/mp4")
    output = replace(
        OUTPUT, object_key=source.key, size_bytes=source.size_bytes, sha256=source.sha256
    )
    published = RemoteArtifactReceiver(
        storage, MediaValidator(), max_bytes=1024
    ).receive_and_publish(
        job_id=job,
        attempt_id=attempt,
        output=output,
        policy=POLICY,
    )
    assert published.stored.sha256 == source.sha256
    assert published.stored.size_bytes == len(content)
    assert published.facts.duration_ms == 5000


pytestmark = pytest.mark.media
