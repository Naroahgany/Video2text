"""Bilibili input parsing, metadata discovery and subtitle download."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError


BV_PATTERN = re.compile(r"(?i)\b(BV[0-9A-Za-z]{8,12})\b")
URL_PATTERN = re.compile(r"https?://[^\s\"'<>，。]+", re.IGNORECASE)
TRAILING_URL_CHARS = "，。；;、,.!！?？)]}）】》\"'"
DEFAULT_HEADERS = {
    "Accept": "*/*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}


class _YtDlpQuietLogger:
    def debug(self, message: str) -> None:
        return

    def warning(self, message: str) -> None:
        return

    def error(self, message: str) -> None:
        return


class BilibiliErrorCode(StrEnum):
    """Stage 4 error codes surfaced to the task manager."""

    INPUT_UNRECOGNIZED = "input_unrecognized"
    VIDEO_INFO_FAILED = "video_info_failed"
    VIDEO_UNAVAILABLE = "video_unavailable"
    LOGIN_REQUIRED = "login_required"
    BILIBILI_RETURNED_ERROR = "bilibili_returned_error"
    YTDLP_ERROR = "ytdlp_error"
    SUBTITLE_NOT_FOUND = "subtitle_not_found"
    SUBTITLE_TIMEOUT = "subtitle_timeout"
    SUBTITLE_FETCH_FAILED = "subtitle_fetch_failed"
    SUBTITLE_BILIBILI_RETURNED_ERROR = "subtitle_bilibili_returned_error"


class BilibiliError(Exception):
    """Human-readable error with a stable code."""

    def __init__(self, code: BilibiliErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ParsedBilibiliInput:
    """Normalized user input for yt-dlp."""

    raw_input: str
    url: str
    display: str
    bv_id: str = ""
    p_index: int | None = None
    source: str = "unknown"


@dataclass(frozen=True)
class SubtitleCandidate:
    """One subtitle file advertised by yt-dlp."""

    language: str
    url: str
    ext: str
    source: str
    name: str = ""


@dataclass(frozen=True)
class PartInfo:
    """A single video part or future subtask candidate."""

    p_index: int
    title: str
    duration_seconds: float | None = None
    bv_id: str = ""
    url: str = ""


@dataclass(frozen=True)
class BilibiliVideoInfo:
    """Normalized metadata needed by current and future workflow stages."""

    title: str
    bv_id: str
    p_index: int
    duration_seconds: float | None
    webpage_url: str
    parts: list[PartInfo] = field(default_factory=list)
    subtitle_candidates: list[SubtitleCandidate] = field(default_factory=list)
    raw_info: dict[str, Any] = field(default_factory=dict)


def parse_bilibili_input(raw_input: str) -> ParsedBilibiliInput:
    """Extract a playable Bilibili URL or BV id from user text."""

    text = raw_input.strip()
    if not text:
        raise BilibiliError(BilibiliErrorCode.INPUT_UNRECOGNIZED, "请输入 B站视频链接、分享文本或 BV 号")

    bv_id = _extract_bv_id(text)
    url = _extract_bilibili_url(text)
    p_index = _extract_p_index(url) if url else None

    if url:
        url_bv_id = _extract_bv_id(url)
        bv_id = url_bv_id or bv_id
        normalized_url = _normalize_url(url, bv_id, p_index)
        return ParsedBilibiliInput(
            raw_input=raw_input,
            url=normalized_url,
            display=normalized_url,
            bv_id=bv_id,
            p_index=p_index,
            source="url",
        )

    if bv_id:
        normalized_url = f"https://www.bilibili.com/video/{bv_id}/"
        return ParsedBilibiliInput(
            raw_input=raw_input,
            url=normalized_url,
            display=bv_id,
            bv_id=bv_id,
            p_index=None,
            source="bv",
        )

    raise BilibiliError(
        BilibiliErrorCode.INPUT_UNRECOGNIZED,
        "未识别到 B站视频链接或 BV 号，请粘贴普通 B站网址、完整分享文本或 BV 号",
    )


async def fetch_video_info(parsed: ParsedBilibiliInput) -> BilibiliVideoInfo:
    """Fetch Bilibili metadata through yt-dlp without downloading media."""

    try:
        info = await asyncio.to_thread(_extract_video_info_sync, parsed.url)
    except (DownloadError, ExtractorError) as exc:
        raise _map_ytdlp_error(exc) from exc
    except Exception as exc:  # pragma: no cover - yt-dlp can raise mixed internal errors
        raise BilibiliError(
            BilibiliErrorCode.YTDLP_ERROR,
            f"yt-dlp 获取视频信息失败：{exc}",
        ) from exc

    return _normalize_video_info(parsed, info)


def choose_subtitle_candidate(video_info: BilibiliVideoInfo) -> SubtitleCandidate | None:
    """Pick the best subtitle by language first, then source, then format."""

    supported = [candidate for candidate in video_info.subtitle_candidates if candidate.url]
    if not supported:
        return None

    return min(supported, key=_subtitle_priority)


async def download_subtitle(candidate: SubtitleCandidate) -> str:
    """Download subtitle text from the selected candidate URL."""

    url = candidate.url
    if url.startswith("//"):
        url = f"https:{url}"

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, headers=DEFAULT_HEADERS)
    except httpx.TimeoutException as exc:
        raise BilibiliError(BilibiliErrorCode.SUBTITLE_TIMEOUT, "字幕获取超时") from exc
    except httpx.RequestError as exc:
        raise BilibiliError(
            BilibiliErrorCode.SUBTITLE_FETCH_FAILED,
            f"字幕获取失败，请检查网络或 B站返回：{exc}",
        ) from exc

    if response.status_code >= 400:
        raise BilibiliError(
            BilibiliErrorCode.SUBTITLE_BILIBILI_RETURNED_ERROR,
            f"B站字幕地址返回异常：HTTP {response.status_code}",
        )

    if not response.text.strip():
        raise BilibiliError(BilibiliErrorCode.SUBTITLE_FETCH_FAILED, "字幕响应为空")

    return response.text


def _extract_video_info_sync(url: str) -> dict[str, Any]:
    ydl_options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 30,
        "extractor_retries": 2,
        "retries": 2,
        "http_headers": {
            **DEFAULT_HEADERS,
            "Referer": "https://www.bilibili.com/",
        },
        "logger": _YtDlpQuietLogger(),
    }
    with YoutubeDL(ydl_options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise BilibiliError(BilibiliErrorCode.VIDEO_INFO_FAILED, "yt-dlp 返回的视频信息格式无法识别")
    return info


def _extract_bilibili_url(text: str) -> str:
    for match in URL_PATTERN.finditer(text):
        candidate = match.group(0).rstrip(TRAILING_URL_CHARS)
        if _is_bilibili_url(candidate):
            return candidate
    return ""


def _is_bilibili_url(url: str) -> bool:
    try:
        hostname = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return hostname.endswith("bilibili.com") or hostname.endswith("b23.tv")


def _extract_bv_id(text: str) -> str:
    match = BV_PATTERN.search(text)
    if not match:
        return ""
    candidate = match.group(1)
    return f"BV{candidate[2:]}"


def _extract_p_index(url: str) -> int | None:
    if not url:
        return None
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return None
    for key in ("p", "page"):
        value = query.get(key, [""])[0]
        if str(value).isdigit() and int(value) > 0:
            return int(value)
    return None


def _normalize_url(url: str, bv_id: str, p_index: int | None) -> str:
    if not bv_id:
        return url

    normalized = f"https://www.bilibili.com/video/{bv_id}/"
    if p_index:
        normalized = f"{normalized}?{urlencode({'p': p_index})}"
    return normalized


def _normalize_video_info(parsed: ParsedBilibiliInput, info: dict[str, Any]) -> BilibiliVideoInfo:
    selected_info = _select_current_info(parsed, info)
    title = _safe_title(selected_info) or _safe_title(info) or parsed.bv_id or "未命名视频"
    webpage_url = str(selected_info.get("webpage_url") or info.get("webpage_url") or parsed.url)
    bv_id = _extract_bv_id(
        " ".join(
            str(value or "")
            for value in (
                selected_info.get("display_id"),
                selected_info.get("id"),
                selected_info.get("url"),
                webpage_url,
                parsed.bv_id,
            )
        ),
    )
    p_index = _read_int(selected_info.get("page")) or parsed.p_index or _infer_part_index(info, selected_info) or 1
    duration = _read_float(selected_info.get("duration")) or _read_float(info.get("duration"))
    parts = _collect_parts(parsed, info, selected_info)
    subtitle_candidates = _collect_subtitle_candidates(selected_info)

    return BilibiliVideoInfo(
        title=title,
        bv_id=bv_id or parsed.bv_id,
        p_index=p_index,
        duration_seconds=duration,
        webpage_url=webpage_url,
        parts=parts,
        subtitle_candidates=subtitle_candidates,
        raw_info=selected_info,
    )


def _select_current_info(parsed: ParsedBilibiliInput, info: dict[str, Any]) -> dict[str, Any]:
    entries = [entry for entry in info.get("entries") or [] if isinstance(entry, dict)]
    if not entries:
        return info

    if parsed.p_index:
        for index, entry in enumerate(entries, start=1):
            entry_page = _read_int(entry.get("page")) or index
            if entry_page == parsed.p_index:
                return entry

    return entries[0]


def _safe_title(info: dict[str, Any]) -> str:
    return str(info.get("title") or info.get("fulltitle") or info.get("part") or "").strip()


def _collect_parts(
    parsed: ParsedBilibiliInput,
    info: dict[str, Any],
    selected_info: dict[str, Any],
) -> list[PartInfo]:
    entries = [entry for entry in info.get("entries") or [] if isinstance(entry, dict)]
    if entries:
        return [
            PartInfo(
                p_index=_read_int(entry.get("page")) or index,
                title=_safe_title(entry) or f"P{index}",
                duration_seconds=_read_float(entry.get("duration")),
                bv_id=_extract_bv_id(
                    " ".join(str(value or "") for value in (entry.get("display_id"), entry.get("id"), entry.get("url"))),
                )
                or parsed.bv_id,
                url=str(entry.get("webpage_url") or entry.get("url") or ""),
            )
            for index, entry in enumerate(entries, start=1)
        ]

    pages = info.get("pages")
    if isinstance(pages, list) and pages:
        parts: list[PartInfo] = []
        for index, page in enumerate(pages, start=1):
            if not isinstance(page, dict):
                continue
            parts.append(
                PartInfo(
                    p_index=_read_int(page.get("page")) or index,
                    title=str(page.get("part") or page.get("title") or f"P{index}").strip(),
                    duration_seconds=_read_float(page.get("duration")),
                    bv_id=parsed.bv_id,
                    url=_part_url(parsed, _read_int(page.get("page")) or index),
                ),
            )
        if parts:
            return parts

    return [
        PartInfo(
            p_index=_read_int(selected_info.get("page")) or parsed.p_index or 1,
            title=_safe_title(selected_info) or _safe_title(info) or "当前分P",
            duration_seconds=_read_float(selected_info.get("duration")) or _read_float(info.get("duration")),
            bv_id=parsed.bv_id,
            url=str(selected_info.get("webpage_url") or parsed.url),
        ),
    ]


def _part_url(parsed: ParsedBilibiliInput, p_index: int) -> str:
    bv_id = parsed.bv_id or _extract_bv_id(parsed.url)
    if bv_id:
        return f"https://www.bilibili.com/video/{bv_id}/?{urlencode({'p': p_index})}"
    split = urlsplit(parsed.url)
    query = parse_qs(split.query)
    query["p"] = [str(p_index)]
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query, doseq=True), split.fragment))


def _infer_part_index(info: dict[str, Any], selected_info: dict[str, Any]) -> int | None:
    entries = [entry for entry in info.get("entries") or [] if isinstance(entry, dict)]
    for index, entry in enumerate(entries, start=1):
        if entry is selected_info:
            return index
    return None


def _collect_subtitle_candidates(info: dict[str, Any]) -> list[SubtitleCandidate]:
    candidates: list[SubtitleCandidate] = []
    for container_name, source in (("subtitles", "uploaded"), ("automatic_captions", "automatic")):
        container = info.get(container_name)
        if not isinstance(container, dict):
            continue
        for language, entries in container.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                url = str(entry.get("url") or "")
                ext = str(entry.get("ext") or "").lower()
                name = str(entry.get("name") or entry.get("format") or "")
                if url:
                    candidates.append(
                        SubtitleCandidate(
                            language=str(language),
                            url=url,
                            ext=ext,
                            source=source,
                            name=name,
                        ),
                    )
    return candidates


def _subtitle_priority(candidate: SubtitleCandidate) -> tuple[int, int, int]:
    language = candidate.language.lower()
    if _is_chinese_language(language):
        language_rank = 0
    elif language.startswith("en") or "eng" in language or "英文" in language:
        language_rank = 1
    else:
        language_rank = 2

    source_rank = 0 if candidate.source == "uploaded" else 1
    ext_rank = {
        "vtt": 0,
        "json": 1,
        "json3": 1,
        "srv3": 2,
        "srt": 3,
    }.get(candidate.ext, 9)
    return (language_rank, source_rank, ext_rank)


def _is_chinese_language(language: str) -> bool:
    return (
        language.startswith("zh")
        or "chi" in language
        or "cmn" in language
        or "中文" in language
        or "汉" in language
        or "漢" in language
    )


def _map_ytdlp_error(exc: Exception) -> BilibiliError:
    message = str(exc)
    lowered = message.lower()
    if any(token in lowered for token in ("login", "cookies", "cookie", "private", "会员", "登录")):
        return BilibiliError(BilibiliErrorCode.LOGIN_REQUIRED, "该视频可能需要登录权限，MVP 暂不支持")
    if any(token in lowered for token in ("404", "not found", "不存在", "已失效")):
        return BilibiliError(BilibiliErrorCode.VIDEO_UNAVAILABLE, "B站视频不存在或无法访问")
    if any(token in lowered for token in ("http error 412", "precondition failed")):
        return BilibiliError(
            BilibiliErrorCode.BILIBILI_RETURNED_ERROR,
            "B站返回异常：HTTP 412 Precondition Failed，可能与当前网络、请求风控或视频访问限制有关",
        )
    if any(token in lowered for token in ("timed out", "timeout")):
        return BilibiliError(BilibiliErrorCode.VIDEO_INFO_FAILED, "B站视频信息获取超时")
    return BilibiliError(BilibiliErrorCode.YTDLP_ERROR, f"yt-dlp 获取视频信息失败：{message}")


def _read_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _read_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
