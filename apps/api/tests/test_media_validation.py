import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.media import (
    MediaErrorCode,
    MediaPolicy,
    MediaProcessLimits,
    MediaValidationError,
    MediaValidator,
    inspect_ffmpeg_build,
)
from app.provider import FailureCode, mock_video_fixture

FIXTURES = Path(__file__).parent / "fixtures" / "media"
POLICY = MediaPolicy(expected_duration_ms=5_000, expected_aspect_ratio="16:9")
pytestmark = pytest.mark.media


def test_mock_success_fixture_uses_playable_h264_and_truthful_metadata(tmp_path):
    path = tmp_path / "mock.mp4"
    path.write_bytes(mock_video_fixture())
    facts = MediaValidator().validate(
        path, MediaPolicy(expected_duration_ms=2_000, expected_aspect_ratio="16:9")
    )
    assert (facts.codec, facts.frame_rate, facts.duration_ms) == ("h264", 25, 2_000)


def assert_media_error(
    path: Path,
    code: MediaErrorCode,
    failure_code: FailureCode,
) -> MediaValidationError:
    with pytest.raises(MediaValidationError) as caught:
        MediaValidator().validate(path, POLICY)
    assert caught.value.code == code
    assert caught.value.failure_code == failure_code
    assert caught.value.details
    return caught.value


def test_valid_720p_h264_fixture_passes_probe_policy_and_full_decode() -> None:
    facts = MediaValidator().validate(FIXTURES / "valid-720p-h264.mp4", POLICY)

    assert facts.container == "mp4"
    assert facts.codec == "h264"
    assert facts.duration_ms == 5_000
    assert (facts.width, facts.height) == (1280, 720)
    assert facts.aspect_ratio == "16:9"
    assert facts.pixel_format == "yuv420p"
    assert facts.frame_rate == 5
    assert facts.decodable is True


@pytest.mark.parametrize(
    ("fixture", "code"),
    [
        ("wrong-container.mkv", MediaErrorCode.CONTAINER_INVALID),
        ("wrong-resolution.mp4", MediaErrorCode.RESOLUTION_INVALID),
        ("wrong-duration.mp4", MediaErrorCode.DURATION_INVALID),
        ("wrong-codec.mp4", MediaErrorCode.CODEC_INVALID),
    ],
)
def test_media_policy_rejects_wrong_facts(fixture: str, code: MediaErrorCode) -> None:
    error = assert_media_error(
        FIXTURES / fixture,
        code,
        FailureCode.OUTPUT_INVALID_MEDIA,
    )
    assert set(error.details) == {"expected", "actual"}


def test_full_decode_rejects_a_damaged_mp4_that_ffprobe_can_describe() -> None:
    error = assert_media_error(
        FIXTURES / "corrupt-decode.mp4",
        MediaErrorCode.DECODE_FAILED,
        FailureCode.OUTPUT_CORRUPTED,
    )
    assert error.details["returncode"] != 0
    assert len(str(error.details["stderr"])) <= 2_048


def test_expected_portrait_policy_rejects_a_landscape_fixture() -> None:
    portrait_policy = MediaPolicy(
        expected_duration_ms=5_000,
        expected_aspect_ratio="9:16",
    )

    with pytest.raises(MediaValidationError) as caught:
        MediaValidator().validate(FIXTURES / "valid-720p-h264.mp4", portrait_policy)

    assert caught.value.code == MediaErrorCode.ASPECT_RATIO_INVALID
    assert caught.value.details == {"expected": "9:16", "actual": "16:9"}


def test_missing_media_has_a_structured_output_missing_error(tmp_path: Path) -> None:
    with pytest.raises(MediaValidationError) as caught:
        MediaValidator().validate(tmp_path / "missing.mp4", POLICY)

    assert caught.value.code == MediaErrorCode.FILE_MISSING
    assert caught.value.failure_code == FailureCode.OUTPUT_MISSING


def test_media_path_is_one_argument_and_cannot_inject_a_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "injected"
    suspicious = tmp_path / "valid;touch injected.mp4"
    shutil.copyfile(FIXTURES / "valid-720p-h264.mp4", suspicious)
    real_popen = subprocess.Popen
    calls: list[tuple[tuple[str, ...], bool]] = []

    def recording_popen(argv: tuple[str, ...], **kwargs: object):
        calls.append((argv, bool(kwargs.get("shell"))))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", recording_popen)

    facts = MediaValidator().validate(suspicious, POLICY)

    assert facts.decodable is True
    assert not marker.exists()
    assert len(calls) == 2
    assert all(shell is False for _, shell in calls)
    assert all(argv[-1] == str(suspicious) or str(suspicious) in argv for argv, _ in calls)


@pytest.mark.posix
def test_ffprobe_timeout_kills_its_child_process(tmp_path: Path) -> None:
    fake_ffprobe = tmp_path / "ffprobe-sleeper"
    fake_ffprobe.write_text(
        """#!/usr/bin/env python3
import pathlib
import subprocess
import sys
import time

media_path = pathlib.Path(sys.argv[-1])
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
media_path.with_suffix(".child.pid").write_text(str(child.pid))
time.sleep(60)
""",
        encoding="utf-8",
    )
    fake_ffprobe.chmod(0o755)
    media_path = tmp_path / "input.mp4"
    media_path.write_bytes(b"input")
    limits = MediaProcessLimits(
        timeout_seconds=0.3,
        cpu_time_seconds=2,
        max_memory_bytes=128 * 1024 * 1024,
    )
    validator = MediaValidator(ffprobe_binary=str(fake_ffprobe), probe_limits=limits)

    started = time.monotonic()
    with pytest.raises(MediaValidationError) as caught:
        validator.validate(media_path, POLICY)
    elapsed = time.monotonic() - started

    assert caught.value.code == MediaErrorCode.FFPROBE_TIMEOUT
    assert elapsed < 3
    child_pid = int(media_path.with_suffix(".child.pid").read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
        except FileNotFoundError:
            break
        if state == "Z":
            break
        time.sleep(0.05)
    else:
        pytest.fail("ffprobe child process remained alive after timeout")


def test_ffmpeg_build_inventory_records_configuration_codec_and_license() -> None:
    facts = inspect_ffmpeg_build()

    assert facts.version
    assert facts.configuration
    assert facts.license_status in {"LGPL", "GPL", "NONFREE"}
    assert "h264" in facts.h264_decoders
    assert facts.h264_encoders


def test_process_limits_reject_unbounded_values() -> None:
    with pytest.raises(ValueError):
        MediaProcessLimits(
            timeout_seconds=0,
            cpu_time_seconds=1,
            max_memory_bytes=128 * 1024 * 1024,
        )
    with pytest.raises(ValueError):
        MediaProcessLimits(
            timeout_seconds=1,
            cpu_time_seconds=1,
            max_memory_bytes=1,
        )


def test_ffprobe_not_found_is_structured() -> None:
    validator = MediaValidator(ffprobe_binary="definitely-not-a-real-ffprobe")

    with pytest.raises(MediaValidationError) as caught:
        validator.validate(FIXTURES / "valid-720p-h264.mp4", POLICY)

    assert caught.value.code == MediaErrorCode.FFPROBE_NOT_FOUND
    assert caught.value.failure_code == FailureCode.INTERNAL_ERROR
    assert caught.value.details == {"tool": "definitely-not-a-real-ffprobe"}
