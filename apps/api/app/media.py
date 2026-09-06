"""Constant-cost validation of provider-reported metadata; no media processes or decoding."""

import math
from dataclasses import dataclass
from enum import StrEnum

from app.provider import FailureCode, ProviderOutput


class MediaErrorCode(StrEnum):
    CONTAINER_INVALID = "MEDIA_CONTAINER_INVALID"
    CODEC_INVALID = "MEDIA_CODEC_INVALID"
    RESOLUTION_INVALID = "MEDIA_RESOLUTION_INVALID"
    ASPECT_RATIO_INVALID = "MEDIA_ASPECT_RATIO_INVALID"
    DURATION_INVALID = "MEDIA_DURATION_INVALID"
    FRAME_RATE_INVALID = "MEDIA_FRAME_RATE_INVALID"


class MediaValidationError(Exception):
    def __init__(self, code: MediaErrorCode, message: str) -> None:
        self.code = code
        self.failure_code = FailureCode.OUTPUT_INVALID_MEDIA
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class MediaFacts:
    """Validated declarations, not proof that the video was decoded or is playable."""

    codec: str
    duration_ms: int
    width: int
    height: int
    frame_rate: float


@dataclass(frozen=True, slots=True)
class MediaPolicy:
    expected_duration_ms: int
    expected_aspect_ratio: str
    duration_tolerance_ms: int = 250
    allowed_dimensions: frozenset[tuple[int, int]] = frozenset({(1280, 720), (720, 1280)})
    allowed_codecs: frozenset[str] = frozenset({"h264"})

    def __post_init__(self) -> None:
        if self.expected_duration_ms <= 0 or self.duration_tolerance_ms < 0:
            raise ValueError("duration must be positive and tolerance nonnegative")
        if self.expected_aspect_ratio not in {"16:9", "9:16"}:
            raise ValueError("expected_aspect_ratio must be 16:9 or 9:16")


class MediaValidator:
    def validate(self, output: ProviderOutput, policy: MediaPolicy) -> MediaFacts:
        if output.media_type != "video/mp4":
            raise MediaValidationError(MediaErrorCode.CONTAINER_INVALID, "输出媒体类型必须为 MP4")
        if not isinstance(output.codec, str) or output.codec not in policy.allowed_codecs:
            raise MediaValidationError(MediaErrorCode.CODEC_INVALID, "输出编码声明无效")
        width, height = output.width, output.height
        if (
            type(width) is not int
            or type(height) is not int
            or (width, height) not in policy.allowed_dimensions
        ):
            raise MediaValidationError(MediaErrorCode.RESOLUTION_INVALID, "输出尺寸声明无效")
        divisor = math.gcd(width, height)
        if f"{width // divisor}:{height // divisor}" != policy.expected_aspect_ratio:
            raise MediaValidationError(MediaErrorCode.ASPECT_RATIO_INVALID, "输出画幅与任务不符")
        duration = output.duration_ms
        if (
            type(duration) is not int
            or duration <= 0
            or abs(duration - policy.expected_duration_ms) > policy.duration_tolerance_ms
        ):
            raise MediaValidationError(MediaErrorCode.DURATION_INVALID, "输出时长与任务不符")
        fps = output.fps
        if type(fps) not in {int, float} or not 0 < fps <= 240:
            raise MediaValidationError(MediaErrorCode.FRAME_RATE_INVALID, "输出帧率声明无效")
        return MediaFacts(output.codec, duration, width, height, float(fps))
