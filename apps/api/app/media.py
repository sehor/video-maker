from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.provider import FailureCode

if TYPE_CHECKING:
    from app.config import Settings


class MediaErrorCode(StrEnum):
    FILE_MISSING = "MEDIA_FILE_MISSING"
    FFPROBE_NOT_FOUND = "FFPROBE_NOT_FOUND"
    FFPROBE_TIMEOUT = "FFPROBE_TIMEOUT"
    FFPROBE_FAILED = "FFPROBE_FAILED"
    FFPROBE_INVALID_JSON = "FFPROBE_INVALID_JSON"
    VIDEO_STREAM_MISSING = "MEDIA_VIDEO_STREAM_MISSING"
    CONTAINER_INVALID = "MEDIA_CONTAINER_INVALID"
    CODEC_INVALID = "MEDIA_CODEC_INVALID"
    PIXEL_FORMAT_INVALID = "MEDIA_PIXEL_FORMAT_INVALID"
    RESOLUTION_INVALID = "MEDIA_RESOLUTION_INVALID"
    ASPECT_RATIO_INVALID = "MEDIA_ASPECT_RATIO_INVALID"
    DURATION_INVALID = "MEDIA_DURATION_INVALID"
    FRAME_RATE_INVALID = "MEDIA_FRAME_RATE_INVALID"
    FFMPEG_NOT_FOUND = "FFMPEG_NOT_FOUND"
    FFMPEG_TIMEOUT = "FFMPEG_TIMEOUT"
    DECODE_FAILED = "MEDIA_DECODE_FAILED"
    RESOURCE_LIMIT_FAILED = "MEDIA_RESOURCE_LIMIT_FAILED"
    BUILD_INSPECTION_FAILED = "FFMPEG_BUILD_INSPECTION_FAILED"


class MediaValidationError(Exception):
    def __init__(
        self,
        code: MediaErrorCode,
        message: str,
        *,
        failure_code: FailureCode,
        details: dict[str, object] | None = None,
    ) -> None:
        self.code = code
        self.failure_code = failure_code
        self.message = message
        self.details = details or {}
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class MediaFacts:
    container: str
    codec: str
    duration_ms: int
    width: int
    height: int
    aspect_ratio: str
    pixel_format: str
    frame_rate: float
    size_bytes: int
    video_stream_index: int
    decodable: bool


@dataclass(frozen=True, slots=True)
class MediaPolicy:
    expected_duration_ms: int
    expected_aspect_ratio: str
    duration_tolerance_ms: int = 250
    allowed_dimensions: frozenset[tuple[int, int]] = frozenset(
        {(1280, 720), (720, 1280)}
    )
    allowed_containers: frozenset[str] = frozenset({"mp4"})
    allowed_codecs: frozenset[str] = frozenset({"h264"})
    allowed_pixel_formats: frozenset[str] = frozenset({"yuv420p"})

    def __post_init__(self) -> None:
        if self.expected_duration_ms <= 0:
            raise ValueError("expected_duration_ms must be positive")
        if self.duration_tolerance_ms < 0:
            raise ValueError("duration_tolerance_ms must not be negative")
        if self.expected_aspect_ratio not in {"16:9", "9:16"}:
            raise ValueError("expected_aspect_ratio must be 16:9 or 9:16")


@dataclass(frozen=True, slots=True)
class MediaProcessLimits:
    timeout_seconds: float
    cpu_time_seconds: int
    max_memory_bytes: int
    max_output_bytes: int = 1_048_576
    cpu_count: int = 1

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.cpu_time_seconds <= 0:
            raise ValueError("cpu_time_seconds must be positive")
        if self.max_memory_bytes < 64 * 1024 * 1024:
            raise ValueError("max_memory_bytes must be at least 64 MiB")
        if self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        if self.cpu_count <= 0:
            raise ValueError("cpu_count must be positive")


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _apply_posix_limits(process: subprocess.Popen[bytes], limits: MediaProcessLimits) -> None:
    if os.name != "posix":
        return
    try:
        import resource

        prlimit = resource.prlimit
        prlimit(process.pid, resource.RLIMIT_CPU, (limits.cpu_time_seconds,) * 2)
        prlimit(process.pid, resource.RLIMIT_AS, (limits.max_memory_bytes,) * 2)
        prlimit(process.pid, resource.RLIMIT_FSIZE, (limits.max_output_bytes,) * 2)
    except ProcessLookupError as exc:
        if process.poll() is not None:
            return
        _terminate_process_tree(process)
        raise MediaValidationError(
            MediaErrorCode.RESOURCE_LIMIT_FAILED,
            "无法为媒体子进程应用资源限制",
            failure_code=FailureCode.INTERNAL_ERROR,
            details={"reason": type(exc).__name__},
        ) from exc
    except (AttributeError, OSError, ValueError) as exc:
        _terminate_process_tree(process)
        raise MediaValidationError(
            MediaErrorCode.RESOURCE_LIMIT_FAILED,
            "无法为媒体子进程应用资源限制",
            failure_code=FailureCode.INTERNAL_ERROR,
            details={"reason": type(exc).__name__},
        ) from exc


def _read_bounded(stream: Any, max_bytes: int) -> str:
    stream.seek(0)
    payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        payload = payload[:max_bytes]
    return payload.decode("utf-8", errors="replace")


def _run_process(
    argv: tuple[str, ...],
    *,
    limits: MediaProcessLimits,
    not_found_code: MediaErrorCode,
    timeout_code: MediaErrorCode,
    failed_code: MediaErrorCode,
    failure_code: FailureCode = FailureCode.OUTPUT_CORRUPTED,
) -> _ProcessResult:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            raise MediaValidationError(
                not_found_code,
                "媒体工具不可用",
                failure_code=FailureCode.INTERNAL_ERROR,
                details={"tool": Path(argv[0]).name},
            ) from exc
        _apply_posix_limits(process, limits)
        try:
            process.wait(timeout=limits.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_tree(process)
            raise MediaValidationError(
                timeout_code,
                "媒体工具执行超时",
                failure_code=failure_code,
                details={"tool": Path(argv[0]).name, "timeout_seconds": limits.timeout_seconds},
            ) from exc
        stdout.seek(0, os.SEEK_END)
        stderr.seek(0, os.SEEK_END)
        if stdout.tell() > limits.max_output_bytes or stderr.tell() > limits.max_output_bytes:
            raise MediaValidationError(
                MediaErrorCode.RESOURCE_LIMIT_FAILED,
                "媒体工具输出超过资源限制",
                failure_code=failure_code,
                details={"tool": Path(argv[0]).name},
            )
        stdout_text = _read_bounded(stdout, limits.max_output_bytes)
        stderr_text = _read_bounded(stderr, limits.max_output_bytes)
    if process.returncode != 0:
        raise MediaValidationError(
            failed_code,
            "媒体工具执行失败",
            failure_code=failure_code,
            details={
                "tool": Path(argv[0]).name,
                "returncode": process.returncode,
                "stderr": stderr_text[-2048:],
            },
        )
    return _ProcessResult(process.returncode, stdout_text, stderr_text)


class MediaValidator:
    def __init__(
        self,
        *,
        ffprobe_binary: str = "ffprobe",
        ffmpeg_binary: str = "ffmpeg",
        probe_limits: MediaProcessLimits | None = None,
        decode_limits: MediaProcessLimits | None = None,
    ) -> None:
        self._ffprobe_binary = ffprobe_binary
        self._ffmpeg_binary = ffmpeg_binary
        self._probe_limits = probe_limits or MediaProcessLimits(
            timeout_seconds=10,
            cpu_time_seconds=10,
            max_memory_bytes=256 * 1024 * 1024,
        )
        self._decode_limits = decode_limits or MediaProcessLimits(
            timeout_seconds=30,
            cpu_time_seconds=30,
            max_memory_bytes=512 * 1024 * 1024,
        )

    def validate(self, path: Path, policy: MediaPolicy) -> MediaFacts:
        if not path.is_file():
            raise MediaValidationError(
                MediaErrorCode.FILE_MISSING,
                "媒体文件不存在",
                failure_code=FailureCode.OUTPUT_MISSING,
            )
        facts = self._probe(path)
        self._validate_policy(facts, policy)
        self._decode(path)
        return MediaFacts(**{**asdict(facts), "decodable": True})

    def _probe(self, path: Path) -> MediaFacts:
        result = _run_process(
            (
                self._ffprobe_binary,
                "-v",
                "error",
                "-max_alloc",
                str(self._probe_limits.max_memory_bytes),
                "-cpucount",
                str(self._probe_limits.cpu_count),
                "-select_streams",
                "v:0",
                "-show_entries",
                (
                    "stream=index,codec_name,width,height,pix_fmt,duration,avg_frame_rate,"
                    "sample_aspect_ratio,display_aspect_ratio:format=format_name,duration,size"
                ),
                "-of",
                "json",
                "-i",
                str(path),
            ),
            limits=self._probe_limits,
            not_found_code=MediaErrorCode.FFPROBE_NOT_FOUND,
            timeout_code=MediaErrorCode.FFPROBE_TIMEOUT,
            failed_code=MediaErrorCode.FFPROBE_FAILED,
        )
        try:
            payload = json.loads(result.stdout)
            stream = payload["streams"][0]
            media_format = payload["format"]
            width = int(stream["width"])
            height = int(stream["height"])
            duration_ms = round(
                float(stream.get("duration") or media_format["duration"]) * 1000
            )
            frame_rate = float(Fraction(stream["avg_frame_rate"]))
            format_names = set(str(media_format["format_name"]).split(","))
            container = "mp4" if "mp4" in format_names else sorted(format_names)[0]
            aspect_ratio = self._aspect_ratio(stream, width, height)
            return MediaFacts(
                container=container,
                codec=str(stream["codec_name"]),
                duration_ms=duration_ms,
                width=width,
                height=height,
                aspect_ratio=aspect_ratio,
                pixel_format=str(stream["pix_fmt"]),
                frame_rate=frame_rate,
                size_bytes=int(media_format["size"]),
                video_stream_index=int(stream["index"]),
                decodable=False,
            )
        except (
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            ZeroDivisionError,
            json.JSONDecodeError,
        ) as exc:
            raise MediaValidationError(
                MediaErrorCode.FFPROBE_INVALID_JSON,
                "ffprobe 未返回完整媒体事实",
                failure_code=FailureCode.OUTPUT_CORRUPTED,
                details={"reason": type(exc).__name__},
            ) from exc

    @staticmethod
    def _aspect_ratio(stream: dict[str, object], width: int, height: int) -> str:
        display = str(stream.get("display_aspect_ratio") or "")
        if re.fullmatch(r"\d+:\d+", display) and display != "0:1":
            numerator, denominator = (int(part) for part in display.split(":"))
        else:
            sample = str(stream.get("sample_aspect_ratio") or "1:1")
            if not re.fullmatch(r"\d+:\d+", sample) or sample == "0:1":
                sample = "1:1"
            sample_numerator, sample_denominator = (
                int(part) for part in sample.split(":")
            )
            numerator = width * sample_numerator
            denominator = height * sample_denominator
        divisor = math.gcd(numerator, denominator)
        return f"{numerator // divisor}:{denominator // divisor}"

    @staticmethod
    def _reject(
        code: MediaErrorCode,
        message: str,
        *,
        expected: object,
        actual: object,
    ) -> None:
        raise MediaValidationError(
            code,
            message,
            failure_code=FailureCode.OUTPUT_INVALID_MEDIA,
            details={"expected": expected, "actual": actual},
        )

    def _validate_policy(self, facts: MediaFacts, policy: MediaPolicy) -> None:
        if facts.container not in policy.allowed_containers:
            self._reject(
                MediaErrorCode.CONTAINER_INVALID,
                "媒体容器不符合路线要求",
                expected=sorted(policy.allowed_containers),
                actual=facts.container,
            )
        if facts.codec not in policy.allowed_codecs:
            self._reject(
                MediaErrorCode.CODEC_INVALID,
                "视频编码不符合路线要求",
                expected=sorted(policy.allowed_codecs),
                actual=facts.codec,
            )
        if facts.pixel_format not in policy.allowed_pixel_formats:
            self._reject(
                MediaErrorCode.PIXEL_FORMAT_INVALID,
                "像素格式不符合路线要求",
                expected=sorted(policy.allowed_pixel_formats),
                actual=facts.pixel_format,
            )
        dimensions = (facts.width, facts.height)
        if dimensions not in policy.allowed_dimensions:
            self._reject(
                MediaErrorCode.RESOLUTION_INVALID,
                "视频分辨率不符合路线要求",
                expected=sorted(policy.allowed_dimensions),
                actual=dimensions,
            )
        if facts.aspect_ratio != policy.expected_aspect_ratio:
            self._reject(
                MediaErrorCode.ASPECT_RATIO_INVALID,
                "视频画幅不符合路线要求",
                expected=policy.expected_aspect_ratio,
                actual=facts.aspect_ratio,
            )
        duration_delta = abs(facts.duration_ms - policy.expected_duration_ms)
        if duration_delta > policy.duration_tolerance_ms:
            self._reject(
                MediaErrorCode.DURATION_INVALID,
                "视频时长不符合路线要求",
                expected={
                    "duration_ms": policy.expected_duration_ms,
                    "tolerance_ms": policy.duration_tolerance_ms,
                },
                actual=facts.duration_ms,
            )
        if not math.isfinite(facts.frame_rate) or facts.frame_rate <= 0:
            self._reject(
                MediaErrorCode.FRAME_RATE_INVALID,
                "视频帧率无效",
                expected="positive finite frame rate",
                actual=facts.frame_rate,
            )

    def _decode(self, path: Path) -> None:
        _run_process(
            (
                self._ffmpeg_binary,
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-max_alloc",
                str(self._decode_limits.max_memory_bytes),
                "-cpucount",
                str(self._decode_limits.cpu_count),
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-threads",
                str(self._decode_limits.cpu_count),
                "-f",
                "null",
                "-",
            ),
            limits=self._decode_limits,
            not_found_code=MediaErrorCode.FFMPEG_NOT_FOUND,
            timeout_code=MediaErrorCode.FFMPEG_TIMEOUT,
            failed_code=MediaErrorCode.DECODE_FAILED,
        )


def create_media_validator(settings: Settings) -> MediaValidator:
    shared = {
        "cpu_time_seconds": settings.media_cpu_time_seconds,
        "max_output_bytes": settings.media_max_output_bytes,
        "cpu_count": settings.media_cpu_count,
    }
    return MediaValidator(
        ffprobe_binary=settings.ffprobe_binary,
        ffmpeg_binary=settings.ffmpeg_binary,
        probe_limits=MediaProcessLimits(
            timeout_seconds=settings.media_probe_timeout_seconds,
            max_memory_bytes=settings.media_probe_max_memory_bytes,
            **shared,
        ),
        decode_limits=MediaProcessLimits(
            timeout_seconds=settings.media_decode_timeout_seconds,
            max_memory_bytes=settings.media_decode_max_memory_bytes,
            **shared,
        ),
    )


@dataclass(frozen=True, slots=True)
class FFmpegBuildFacts:
    version: str
    configuration: tuple[str, ...]
    license_status: str
    h264_decoders: tuple[str, ...]
    h264_encoders: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _codec_names(output: str, codec: str) -> tuple[str, ...]:
    names: set[str] = set()
    for line in output.splitlines():
        match = re.match(r"^\s*[VAS.][A-Z.]{5}\s+(\S+)", line)
        if match and (match.group(1) == codec or f"(codec {codec})" in line):
            names.add(match.group(1))
    return tuple(sorted(names))


def inspect_ffmpeg_build(
    ffmpeg_binary: str = "ffmpeg",
    *,
    limits: MediaProcessLimits | None = None,
) -> FFmpegBuildFacts:
    build_limits = limits or MediaProcessLimits(
        timeout_seconds=10,
        cpu_time_seconds=10,
        max_memory_bytes=256 * 1024 * 1024,
    )

    def run(*args: str) -> str:
        return _run_process(
            (ffmpeg_binary, *args),
            limits=build_limits,
            not_found_code=MediaErrorCode.FFMPEG_NOT_FOUND,
            timeout_code=MediaErrorCode.FFMPEG_TIMEOUT,
            failed_code=MediaErrorCode.BUILD_INSPECTION_FAILED,
            failure_code=FailureCode.INTERNAL_ERROR,
        ).stdout

    version_output = run("-version")
    decoder_output = run("-hide_banner", "-decoders")
    encoder_output = run("-hide_banner", "-encoders")
    first_line = version_output.splitlines()[0]
    version = first_line.removeprefix("ffmpeg version ").split(" Copyright", 1)[0]
    configuration_line = next(
        (line for line in version_output.splitlines() if line.startswith("configuration:")),
        "configuration:",
    )
    configuration = tuple(configuration_line.removeprefix("configuration:").split())
    if "--enable-nonfree" in configuration:
        license_status = "NONFREE"
    elif "--enable-gpl" in configuration:
        license_status = "GPL"
    else:
        license_status = "LGPL"
    return FFmpegBuildFacts(
        version=version,
        configuration=configuration,
        license_status=license_status,
        h264_decoders=_codec_names(decoder_output, "h264"),
        h264_encoders=_codec_names(encoder_output, "h264"),
    )
