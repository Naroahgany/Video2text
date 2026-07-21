"""Stage 5 audio download, MP3 conversion and chunking."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .bilibili import (
    BilibiliAccessConfig,
    BilibiliError,
    BilibiliErrorCode,
    BilibiliVideoInfo,
    _bilibili_headers,
    _load_cookies_for_httpx,
    _map_http_status,
    _read_float,
    _read_int,
    _sanitize_external_message,
)


PLAYURL_ENDPOINT = "https://api.bilibili.com/x/player/playurl"
DEFAULT_QN = 80
DEFAULT_FNVAL = 4048
DEFAULT_FNVER = 0
DEFAULT_FOURK = 1
DEFAULT_MP3_BITRATE = "128k"
DEFAULT_NO_SLICE_MAX_MINUTES = 15
DEFAULT_TARGET_CHUNK_MINUTES = 15
DEFAULT_CHUNK_OVERLAP_MINUTES = 0.5
HALF_SPLIT_MAX_MINUTES = 30
FFMPEG_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


class AudioProcessingError(Exception):
    """Human-readable stage 5 error with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PlayurlAudioStream:
    """Selected Bilibili DASH audio stream."""

    primary_url: str
    backup_urls: tuple[str, ...]
    audio_id: int | None = None
    bandwidth: int | None = None
    codecs: str = ""
    mime_type: str = ""

    @property
    def urls(self) -> tuple[str, ...]:
        return (self.primary_url, *self.backup_urls)


@dataclass(frozen=True)
class AudioPart:
    """One MP3 part prepared for later stage 6 transcription."""

    index: int
    filename: str
    path: Path
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    overlap_seconds: float

    def to_public_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "filename": self.filename,
            "startSeconds": round(self.start_seconds, 3),
            "endSeconds": round(self.end_seconds, 3),
            "durationSeconds": round(self.duration_seconds, 3),
            "overlapSeconds": round(self.overlap_seconds, 3),
        }


@dataclass(frozen=True)
class AudioProcessingResult:
    """Stage 5 output held in memory until later workflow stages."""

    mp3_path: Path
    duration_seconds: float
    parts: list[AudioPart]
    selected_stream: PlayurlAudioStream

    def public_parts(self) -> list[dict[str, object]]:
        return [part.to_public_dict() for part in self.parts]


async def fetch_playurl_audio_stream(
    video_info: BilibiliVideoInfo,
    access_config: BilibiliAccessConfig,
) -> PlayurlAudioStream:
    """Fetch and select one audio stream through x/player/playurl only."""

    bvid = video_info.bv_id or str(video_info.raw_info.get("bvid") or video_info.raw_info.get("display_id") or "")
    cid = _read_int(video_info.raw_info.get("cid"))
    aid = _read_int(video_info.raw_info.get("aid"))
    if not cid or not bvid:
        raise AudioProcessingError(
            "audio_playurl_missing_identity",
            "阶段 5A playurl 主路径缺少 bvid 或 cid，无法请求音频地址。",
        )

    params: dict[str, object] = {
        "bvid": bvid,
        "cid": cid,
        "qn": DEFAULT_QN,
        "fnval": DEFAULT_FNVAL,
        "fnver": DEFAULT_FNVER,
        "fourk": DEFAULT_FOURK,
    }
    if aid:
        params["avid"] = aid

    headers = _audio_headers(video_info)
    try:
        cookies = _load_cookies_for_httpx(access_config)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(PLAYURL_ENDPOINT, params=params, headers=headers, cookies=cookies)
    except httpx.TimeoutException as exc:
        raise AudioProcessingError("audio_playurl_timeout", "B站 playurl 音频地址获取超时。") from exc
    except BilibiliError as exc:
        raise AudioProcessingError(exc.code.value, exc.message) from exc
    except httpx.RequestError as exc:
        raise AudioProcessingError(
            "audio_playurl_request_failed",
            f"B站 playurl 音频地址获取失败：{_sanitize_external_message(exc)}",
        ) from exc

    if response.status_code >= 400:
        error = _map_http_status(response.status_code, "playurl 音频主路径")
        raise AudioProcessingError(error.code.value, error.message)

    try:
        payload = response.json()
    except ValueError as exc:
        raise AudioProcessingError("audio_playurl_invalid_json", "playurl 音频主路径返回内容不是有效 JSON。") from exc

    code = payload.get("code") if isinstance(payload, dict) else None
    if code != 0:
        message = str(payload.get("message") or "playurl 音频主路径返回异常") if isinstance(payload, dict) else "playurl 音频主路径返回异常"
        raise _playurl_payload_error(code, message)

    audio_streams = _extract_dash_audio_streams(payload)
    if not audio_streams:
        raise AudioProcessingError(
            "audio_playurl_no_audio",
            "playurl 主路径没有返回 data.dash.audio，未下载任何视频画面或后备路径。",
        )
    return _select_audio_stream(audio_streams)


async def download_audio_stream(
    stream: PlayurlAudioStream,
    video_info: BilibiliVideoInfo,
    access_config: BilibiliAccessConfig,
    output_dir: Path,
    log: Callable[[str], None] | None = None,
) -> Path:
    """Download selected audio URL to the task temporary directory."""

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"bilibili_audio{_audio_suffix(stream)}"
    headers = _audio_download_headers(video_info)
    cookies = _load_cookies_for_httpx(access_config)
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=60.0), follow_redirects=True) as client:
        for index, url in enumerate(stream.urls, start=1):
            if not url:
                continue
            _emit(log, f"开始下载音频流：playurl 第 {index} 个候选，streamId={stream.audio_id or '未知'}")
            try:
                bytes_written = await _download_url_to_file(client, url, output_path, headers, cookies)
            except AudioProcessingError as exc:
                errors.append(exc.message)
                _emit(log, f"音频流候选下载失败：{exc.message}")
                continue
            if bytes_written > 0:
                _emit(log, f"音频下载完成：已写入 {bytes_written} 字节到任务临时目录")
                return output_path
            errors.append("下载响应为空")

    summary = "；".join(errors[-3:]) if errors else "没有可用音频 URL"
    raise AudioProcessingError("audio_download_failed", f"playurl 音频下载失败：{summary}")


async def convert_audio_to_mp3(
    input_path: Path,
    output_path: Path,
    duration_hint_seconds: float | None = None,
) -> float:
    """Convert downloaded audio to MP3 and return the verified MP3 duration."""

    ffmpeg = _require_binary("ffmpeg", "ffmpeg_unavailable", "FFmpeg 不可用，无法转换 MP3。")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    timeout = _ffmpeg_timeout(duration_hint_seconds)
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        DEFAULT_MP3_BITRATE,
        str(output_path),
    ]
    await asyncio.to_thread(_run_subprocess, command, timeout, "mp3_conversion_failed", "MP3 转换失败")
    return await probe_audio_duration(output_path)


async def probe_audio_duration(path: Path) -> float:
    """Read media duration using ffprobe, falling back to ffmpeg output."""

    try:
        return await _probe_audio_duration_with_ffprobe(path)
    except AudioProcessingError as exc:
        if exc.code != "ffmpeg_unavailable":
            try:
                return await _probe_audio_duration_with_ffmpeg(path)
            except AudioProcessingError:
                raise exc
        return await _probe_audio_duration_with_ffmpeg(path)


async def _probe_audio_duration_with_ffprobe(path: Path) -> float:
    """Read media duration using ffprobe."""

    ffprobe = _require_binary("ffprobe", "ffmpeg_unavailable", "FFprobe 不可用，无法读取音频时长。")
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    output = await asyncio.to_thread(_run_subprocess, command, 60, "audio_duration_failed", "音频时长读取失败")
    duration = _read_float(output.strip())
    if duration is None or duration <= 0:
        raise AudioProcessingError("audio_duration_failed", "音频时长读取失败：FFprobe 返回空时长。")
    return duration


async def _probe_audio_duration_with_ffmpeg(path: Path) -> float:
    """Read media duration from ffmpeg banner output when ffprobe is unavailable."""

    ffmpeg = _require_binary("ffmpeg", "ffmpeg_unavailable", "FFmpeg 不可用，无法读取音频时长。")
    command = [ffmpeg, "-hide_banner", "-i", str(path)]
    output = await asyncio.to_thread(
        _run_subprocess_allow_failure,
        command,
        60,
        "audio_duration_failed",
        "音频时长读取失败",
    )
    duration = _read_duration_from_ffmpeg_output(output)
    if duration is None or duration <= 0:
        raise AudioProcessingError("audio_duration_failed", "音频时长读取失败：FFmpeg 未返回可识别时长。")
    return duration


async def split_mp3_by_rule(
    mp3_path: Path,
    output_dir: Path,
    duration_seconds: float,
    no_slice_max_minutes: float = DEFAULT_NO_SLICE_MAX_MINUTES,
    target_chunk_minutes: float = DEFAULT_TARGET_CHUNK_MINUTES,
    chunk_overlap_minutes: float = DEFAULT_CHUNK_OVERLAP_MINUTES,
) -> list[AudioPart]:
    """Create part_XXX.mp3 files according to the stage 5 chunking rule."""

    specs = calculate_audio_part_specs(
        duration_seconds,
        no_slice_max_minutes=no_slice_max_minutes,
        target_chunk_minutes=target_chunk_minutes,
        chunk_overlap_minutes=chunk_overlap_minutes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    if len(specs) == 1:
        part_path = output_dir / "part_001.mp3"
        shutil.copy2(mp3_path, part_path)
        start, end, overlap = specs[0]
        return [
            AudioPart(
                index=1,
                filename=part_path.name,
                path=part_path,
                start_seconds=start,
                end_seconds=end,
                duration_seconds=end - start,
                overlap_seconds=overlap,
            ),
        ]

    ffmpeg = _require_binary("ffmpeg", "ffmpeg_unavailable", "FFmpeg 不可用，无法切片音频。")
    parts: list[AudioPart] = []
    for index, (start, end, overlap) in enumerate(specs, start=1):
        part_path = output_dir / f"part_{index:03d}.mp3"
        part_duration = end - start
        command = [
            ffmpeg,
            "-y",
            "-ss",
            _seconds_arg(start),
            "-i",
            str(mp3_path),
            "-t",
            _seconds_arg(part_duration),
            "-vn",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            DEFAULT_MP3_BITRATE,
            str(part_path),
        ]
        await asyncio.to_thread(
            _run_subprocess,
            command,
            _ffmpeg_timeout(part_duration),
            "audio_split_failed",
            f"音频切片失败：part_{index:03d}",
        )
        parts.append(
            AudioPart(
                index=index,
                filename=part_path.name,
                path=part_path,
                start_seconds=start,
                end_seconds=end,
                duration_seconds=part_duration,
                overlap_seconds=overlap,
            ),
        )
    return parts


def calculate_audio_part_specs(
    duration_seconds: float,
    no_slice_max_minutes: float = DEFAULT_NO_SLICE_MAX_MINUTES,
    target_chunk_minutes: float = DEFAULT_TARGET_CHUNK_MINUTES,
    chunk_overlap_minutes: float = DEFAULT_CHUNK_OVERLAP_MINUTES,
) -> list[tuple[float, float, float]]:
    """Return (start, end, overlap_with_previous) tuples for MP3 parts."""

    duration = max(0.0, float(duration_seconds))
    if duration <= 0:
        raise AudioProcessingError("audio_duration_failed", "音频时长无效，无法切片。")

    no_slice_seconds = float(no_slice_max_minutes) * 60
    chunk_seconds = float(target_chunk_minutes) * 60
    overlap_seconds = float(chunk_overlap_minutes) * 60
    if chunk_seconds <= 0:
        raise AudioProcessingError("audio_split_invalid_config", "切片周期必须大于 0。")
    if overlap_seconds < 0 or overlap_seconds >= chunk_seconds:
        raise AudioProcessingError("audio_split_invalid_config", "切片重合时长必须大于等于 0 且小于切片周期。")

    if duration <= no_slice_seconds:
        return [(0.0, duration, 0.0)]

    half_split_max_seconds = HALF_SPLIT_MAX_MINUTES * 60
    if duration <= half_split_max_seconds:
        midpoint = duration / 2
        return [
            (0.0, midpoint, 0.0),
            (midpoint, duration, 0.0),
        ]

    specs: list[tuple[float, float, float]] = []
    start = 0.0
    previous_end = 0.0
    step = chunk_seconds - overlap_seconds
    while start < duration:
        end = min(start + chunk_seconds, duration)
        overlap = max(0.0, previous_end - start) if specs else 0.0
        specs.append((start, end, overlap))
        if end >= duration:
            break
        previous_end = end
        start += step
    return specs


def _extract_dash_audio_streams(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    dash = data.get("dash") if isinstance(data, dict) else None
    audio = dash.get("audio") if isinstance(dash, dict) else None
    return [item for item in audio if isinstance(item, dict)] if isinstance(audio, list) else []


def _select_audio_stream(streams: list[dict[str, Any]]) -> PlayurlAudioStream:
    valid_streams = [item for item in streams if _stream_url(item)]
    if not valid_streams:
        raise AudioProcessingError("audio_playurl_no_audio_url", "playurl 返回了音频流，但没有可用 baseUrl 或 backupUrl。")

    def sort_key(item: dict[str, Any]) -> tuple[int, int]:
        return (_read_int(item.get("bandwidth")) or 0, _read_int(item.get("id")) or 0)

    selected = max(valid_streams, key=sort_key)
    backups = selected.get("backupUrl") or selected.get("backup_url") or []
    if not isinstance(backups, list):
        backups = []
    return PlayurlAudioStream(
        primary_url=_stream_url(selected),
        backup_urls=tuple(str(url) for url in backups if url),
        audio_id=_read_int(selected.get("id")),
        bandwidth=_read_int(selected.get("bandwidth")),
        codecs=str(selected.get("codecs") or ""),
        mime_type=str(selected.get("mimeType") or selected.get("mime_type") or ""),
    )


def _stream_url(stream: dict[str, Any]) -> str:
    return str(stream.get("baseUrl") or stream.get("base_url") or stream.get("url") or "")


async def _download_url_to_file(
    client: httpx.AsyncClient,
    url: str,
    output_path: Path,
    headers: dict[str, str],
    cookies: httpx.Cookies | None,
) -> int:
    try:
        async with client.stream("GET", url, headers=headers, cookies=cookies) as response:
            if response.status_code >= 400:
                error = _map_http_status(response.status_code, "音频 CDN")
                raise AudioProcessingError(error.code.value, error.message)
            bytes_written = 0
            with output_path.open("wb") as handle:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    bytes_written += len(chunk)
            return bytes_written
    except AudioProcessingError:
        raise
    except httpx.TimeoutException as exc:
        raise AudioProcessingError("audio_download_timeout", "音频 URL 下载超时。") from exc
    except httpx.RequestError as exc:
        raise AudioProcessingError(
            "audio_download_request_failed",
            f"音频 URL 下载失败：{_sanitize_external_message(exc)}",
        ) from exc


def _playurl_payload_error(code: object, message: str) -> AudioProcessingError:
    if code in {-101, -102} or "登录" in message or "账号" in message:
        return AudioProcessingError(
            "login_required",
            "playurl 主路径需要有效登录态或精简 Cookie，请重新打开本地 B站登录窗口刷新 Cookie。",
        )
    if code in {-403, 403} or "权限" in message or "付费" in message or "会员" in message:
        return AudioProcessingError("bilibili_http_403", "该视频可能需要付费、会员或受限权限，MVP 暂不支持。")
    if code in {-404, 404}:
        return AudioProcessingError(BilibiliErrorCode.VIDEO_UNAVAILABLE.value, "B站视频不存在或无法访问。")
    return AudioProcessingError("audio_playurl_bilibili_error", f"playurl 主路径返回异常：{message}")


def _audio_headers(video_info: BilibiliVideoInfo) -> dict[str, str]:
    return {
        **_bilibili_headers(),
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.bilibili.com",
        "Referer": _referer(video_info),
    }


def _audio_download_headers(video_info: BilibiliVideoInfo) -> dict[str, str]:
    return {
        **_bilibili_headers(),
        "Accept": "*/*",
        "Origin": "https://www.bilibili.com",
        "Referer": _referer(video_info),
    }


def _referer(video_info: BilibiliVideoInfo) -> str:
    if video_info.webpage_url:
        return video_info.webpage_url
    if video_info.bv_id:
        return f"https://www.bilibili.com/video/{video_info.bv_id}/"
    return "https://www.bilibili.com/"


def _audio_suffix(stream: PlayurlAudioStream) -> str:
    suffix = Path(urlsplit(stream.primary_url).path).suffix.lower()
    if suffix in {".m4s", ".m4a", ".aac", ".flac", ".eac3", ".mp3"}:
        return suffix
    if "mp4" in stream.mime_type or "m4a" in stream.mime_type:
        return ".m4s"
    return ".bin"


def _require_binary(name: str, code: str, message: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise AudioProcessingError(code, message)
    return executable


def _run_subprocess(command: list[str], timeout: int, code: str, message: str) -> str:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AudioProcessingError(code, f"{message}：处理超时。") from exc
    except OSError as exc:
        raise AudioProcessingError(code, f"{message}：{_sanitize_external_message(exc)}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        detail = _sanitize_external_message(detail)
        raise AudioProcessingError(code, f"{message}：{detail or 'FFmpeg 返回非 0 状态。'}")
    return completed.stdout or ""


def _run_subprocess_allow_failure(command: list[str], timeout: int, code: str, message: str) -> str:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AudioProcessingError(code, f"{message}：处理超时。") from exc
    except OSError as exc:
        raise AudioProcessingError(code, f"{message}：{_sanitize_external_message(exc)}") from exc

    return "\n".join(part for part in (completed.stderr, completed.stdout) if part)


def _read_duration_from_ffmpeg_output(output: str) -> float | None:
    match = FFMPEG_DURATION_RE.search(output or "")
    if not match:
        return None
    hours = _read_float(match.group(1))
    minutes = _read_float(match.group(2))
    seconds = _read_float(match.group(3))
    if hours is None or minutes is None or seconds is None:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _ffmpeg_timeout(duration_seconds: float | None) -> int:
    if duration_seconds is None or duration_seconds <= 0:
        return 1800
    return max(300, min(24 * 60 * 60, int(duration_seconds * 4 + 120)))


def _seconds_arg(value: float) -> str:
    return f"{max(0.0, value):.3f}"


def _emit(log: Callable[[str], None] | None, message: str) -> None:
    if log is None:
        return
    log(message)


__all__ = [
    "AudioPart",
    "AudioProcessingError",
    "AudioProcessingResult",
    "PlayurlAudioStream",
    "calculate_audio_part_specs",
    "convert_audio_to_mp3",
    "download_audio_stream",
    "fetch_playurl_audio_stream",
    "probe_audio_duration",
    "split_mp3_by_rule",
]
