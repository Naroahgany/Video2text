"""Bilibili input parsing, metadata discovery and subtitle download."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.cookies import CookieLoadError, extract_cookies_from_browser
from yt_dlp.utils import DownloadError, ExtractorError

from .bilibili_cookie import cookie_header_to_httpx_cookies, simplify_bilibili_cookie_header
from .browser_profile import (
    fetch_bilibili_page_html_from_profile,
    fetch_bilibili_page_state_from_profile,
    fetch_player_wbi_payload_from_profile,
    load_bilibili_cookies_from_profile,
)


BV_PATTERN = re.compile(r"(?i)\b(BV[0-9A-Za-z]{8,12})\b")
URL_PATTERN = re.compile(r"https?://[^\s\"'<>，。]+", re.IGNORECASE)
INITIAL_STATE_PATTERN = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*(?:;|</script>)", re.DOTALL)
PLAYINFO_PATTERN = re.compile(r"window\.__playinfo__\s*=\s*(\{.*?\})\s*(?:;|</script>)", re.DOTALL)
TRAILING_URL_CHARS = "，。；;、,.!！?？)]}）】》\"'"
MIXIN_KEY_ENC_TAB = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]
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


class BilibiliAccessMode(StrEnum):
    """Local-only Bilibili access strategies for stage 4.2."""

    AUTO = "auto"
    ANONYMOUS = "anonymous"
    ENHANCED_HEADERS = "enhanced_headers"
    BILIBILI_API = "bilibili_api"
    IMPERSONATE = "impersonate"
    BROWSER_COOKIE = "browser_cookie"
    COOKIES_FILE = "cookies_file"
    COOKIE_HEADER = "cookie_header"
    LOCAL_BROWSER_PROFILE = "local_browser_profile"


class BrowserCookieSource(StrEnum):
    """Browser sources supported by yt-dlp cookies-from-browser."""

    CHROME = "chrome"
    EDGE = "edge"
    FIREFOX = "firefox"


@dataclass(frozen=True)
class AccessAttempt:
    """One sanitized yt-dlp access attempt for task logs."""

    mode: str
    label: str
    success: bool
    error_code: str = ""
    message: str = ""


@dataclass(frozen=True)
class BilibiliAccessConfig:
    """User-selected Bilibili access mode. Cookie contents are never stored here."""

    mode: BilibiliAccessMode = BilibiliAccessMode.AUTO
    browser: BrowserCookieSource = BrowserCookieSource.CHROME
    cookie_header: str = ""
    cookies_file_path: Path | None = None


@dataclass(frozen=True)
class AccessStrategy:
    """Concrete yt-dlp strategy used for one extraction attempt."""

    mode: BilibiliAccessMode
    browser: BrowserCookieSource | None = None
    cookies_file_path: Path | None = None
    cookie_header: str = ""

    @property
    def label(self) -> str:
        if self.mode == BilibiliAccessMode.ANONYMOUS:
            return "标准匿名请求"
        if self.mode == BilibiliAccessMode.ENHANCED_HEADERS:
            return "增强请求头"
        if self.mode == BilibiliAccessMode.BILIBILI_API:
            return "B站公开API"
        if self.mode == BilibiliAccessMode.IMPERSONATE:
            return "Chrome指纹模拟"
        if self.mode == BilibiliAccessMode.BROWSER_COOKIE:
            browser = _browser_label(self.browser or BrowserCookieSource.CHROME)
            return f"浏览器Cookie（{browser}）"
        if self.mode == BilibiliAccessMode.COOKIES_FILE:
            return "cookies.txt导入"
        if self.mode == BilibiliAccessMode.COOKIE_HEADER:
            return "精简Cookie"
        if self.mode == BilibiliAccessMode.LOCAL_BROWSER_PROFILE:
            return "本地专用浏览器Profile"
        return "自动访问"


class BilibiliErrorCode(StrEnum):
    """Stage 4 error codes surfaced to the task manager."""

    INPUT_UNRECOGNIZED = "input_unrecognized"
    VIDEO_INFO_FAILED = "video_info_failed"
    VIDEO_UNAVAILABLE = "video_unavailable"
    LOGIN_REQUIRED = "login_required"
    BILIBILI_RETURNED_ERROR = "bilibili_returned_error"
    BILIBILI_HTTP_412 = "bilibili_http_412"
    BILIBILI_HTTP_403 = "bilibili_http_403"
    BILIBILI_API_ERROR = "bilibili_api_error"
    BILIBILI_TIMEOUT = "bilibili_timeout"
    COOKIE_DATABASE_COPY_FAILED = "cookie_database_copy_failed"
    COOKIE_INVALID = "cookie_invalid"
    ACCESS_MODE_UNSUPPORTED = "access_mode_unsupported"
    YTDLP_ERROR = "ytdlp_error"
    SUBTITLE_NOT_FOUND = "subtitle_not_found"
    SUBTITLE_TIMEOUT = "subtitle_timeout"
    SUBTITLE_FETCH_FAILED = "subtitle_fetch_failed"
    SUBTITLE_BILIBILI_RETURNED_ERROR = "subtitle_bilibili_returned_error"


class BilibiliError(Exception):
    """Human-readable error with a stable code."""

    def __init__(
        self,
        code: BilibiliErrorCode,
        message: str,
        access_attempts: list[AccessAttempt] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.access_attempts = access_attempts or []


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
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SubtitlePathAttempt:
    """One sanitized subtitle discovery path attempt."""

    source: str
    label: str
    success: bool
    message: str = ""


@dataclass(frozen=True)
class WbiKeys:
    """WBI key pair returned by Bilibili nav API."""

    img_key: str
    sub_key: str
    mixin_key: str


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
    access_strategy: str = ""
    access_attempts: list[AccessAttempt] = field(default_factory=list)
    raw_info: dict[str, Any] = field(default_factory=dict)


def parse_bilibili_input(raw_input: str) -> ParsedBilibiliInput:
    """Extract a playable Bilibili URL or BV id from user text."""

    text = raw_input.strip()
    if not text:
        raise BilibiliError(BilibiliErrorCode.INPUT_UNRECOGNIZED, "请输入B站视频链接、分享文本或BV号")

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
        "未识别到B站视频链接或BV号，请粘贴普通B站网址、完整分享文本或BV号",
    )


async def fetch_video_info(
    parsed: ParsedBilibiliInput,
    access_config: BilibiliAccessConfig | None = None,
    debug_logger: Callable[[str], None] | None = None,
) -> BilibiliVideoInfo:
    """Fetch Bilibili metadata through yt-dlp without downloading media."""

    attempts: list[AccessAttempt] = []
    errors: list[BilibiliError] = []
    best_video_info: BilibiliVideoInfo | None = None
    config = access_config or BilibiliAccessConfig()

    for strategy in _build_strategy_plan(config):
        _emit_debug(debug_logger, f"开始视频信息策略：{strategy.label}")
        try:
            strategy_config = _strategy_access_config(config, strategy)
            if strategy.mode in {
                BilibiliAccessMode.BILIBILI_API,
                BilibiliAccessMode.COOKIE_HEADER,
                BilibiliAccessMode.BROWSER_COOKIE,
                BilibiliAccessMode.COOKIES_FILE,
            }:
                video_info = await _extract_video_info_via_bilibili_api(parsed, strategy_config)
            elif strategy.mode == BilibiliAccessMode.LOCAL_BROWSER_PROFILE:
                video_info = await _extract_video_info_via_local_profile(parsed)
            else:
                info = await asyncio.to_thread(_extract_video_info_sync, parsed.url, strategy)
                video_info = _normalize_video_info(parsed, info)
        except (DownloadError, ExtractorError) as exc:
            error = _map_ytdlp_error(exc)
        except BilibiliError as exc:
            error = exc
        except Exception as exc:  # pragma: no cover - yt-dlp can raise mixed internal errors
            error = BilibiliError(
                BilibiliErrorCode.YTDLP_ERROR,
                f"yt-dlp获取视频信息失败：{_sanitize_external_message(exc)}",
            )
        else:
            attempts.append(AccessAttempt(strategy.mode.value, strategy.label, True))
            enriched = await _enrich_player_api_subtitles(
                parsed,
                video_info,
                strategy_config,
                debug_logger=debug_logger,
            )
            attempt_snapshot = [*attempts, *enriched.access_attempts]
            if enriched.subtitle_candidates or best_video_info is None:
                best_video_info = replace(
                    enriched,
                    access_strategy=strategy.label,
                    access_attempts=attempt_snapshot,
                )
            if enriched.subtitle_candidates:
                return best_video_info
            attempts = attempt_snapshot
            continue

        errors.append(error)
        attempts.append(
            AccessAttempt(
                mode=strategy.mode.value,
                label=strategy.label,
                success=False,
                error_code=error.code.value,
                message=error.message,
            ),
        )

    if best_video_info is not None:
        return best_video_info

    final_error = _choose_final_access_error(errors)
    raise BilibiliError(
        final_error.code,
        _with_attempt_summary(final_error.message, attempts),
        access_attempts=attempts,
    )


def choose_subtitle_candidate(video_info: BilibiliVideoInfo) -> SubtitleCandidate | None:
    """Pick the best subtitle by language first, then source, then format."""

    supported = [candidate for candidate in video_info.subtitle_candidates if candidate.url]
    if not supported:
        return None

    return min(supported, key=_subtitle_priority)


async def download_subtitle(
    candidate: SubtitleCandidate,
    access_config: BilibiliAccessConfig | None = None,
    debug_logger: Callable[[str], None] | None = None,
) -> str:
    """Download subtitle text from the selected candidate URL."""

    url = candidate.url
    if url.startswith("//"):
        url = f"https:{url}"

    try:
        cookies = _load_cookies_for_httpx(access_config)
        headers = _subtitle_headers(candidate)
        _emit_debug(
            debug_logger,
            (
                "字幕文件请求前："
                f"subtitle_url={url}，"
                f"Cookie是否存在：{_cookie_presence_label(cookies)}，"
                f"Referer={headers.get('Referer') or headers.get('referer') or '未设置'}"
            ),
        )
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers, cookies=cookies)
    except httpx.TimeoutException as exc:
        raise BilibiliError(BilibiliErrorCode.SUBTITLE_TIMEOUT, "字幕获取超时") from exc
    except BilibiliError:
        raise
    except httpx.RequestError as exc:
        raise BilibiliError(
            BilibiliErrorCode.SUBTITLE_FETCH_FAILED,
            f"字幕获取失败，请检查网络或B站返回：{_sanitize_external_message(exc)}",
        ) from exc

    if response.status_code >= 400:
        raise BilibiliError(
            BilibiliErrorCode.SUBTITLE_BILIBILI_RETURNED_ERROR,
            f"B站字幕地址返回异常：HTTP {response.status_code}",
        )

    if not response.text.strip():
        raise BilibiliError(BilibiliErrorCode.SUBTITLE_FETCH_FAILED, "字幕响应为空")

    _emit_debug(
        debug_logger,
        f"字幕文件下载完成：HTTP {response.status_code}，subtitleJsonPreview={_preview_text(response.text)}",
    )
    return response.text


def _emit_debug(debug_logger: Callable[[str], None] | None, message: str) -> None:
    if debug_logger is None:
        return
    try:
        debug_logger(message)
    except Exception:
        return


def _preview_text(value: object, limit: int = 200) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = text.replace("\n", "\\n")
    if len(text) > limit:
        return f"{text[:limit]}..."
    return text or "空"


def _cookie_presence_label(cookies: httpx.Cookies | None) -> str:
    if cookies is None:
        return "否"
    try:
        count = len(list(cookies.jar))
    except Exception:
        return "是"
    return f"是（{count}条）" if count else "否"


def _debug_params(params: dict[str, object]) -> str:
    keys = ("bvid", "aid", "cid", "ep_id", "wts", "w_rid")
    safe_params = {key: params[key] for key in keys if key in params}
    return json.dumps(safe_params, ensure_ascii=False, sort_keys=True)


def _emit_subtitle_list_debug(
    debug_logger: Callable[[str], None] | None,
    label: str,
    payload: dict[str, Any],
) -> None:
    subtitles = (
        payload.get("data", {}).get("subtitle", {}).get("subtitles", [])
        if isinstance(payload, dict)
        else []
    )
    subtitle_count = len(subtitles) if isinstance(subtitles, list) else 0
    _emit_debug(debug_logger, f"{label}响应：subtitleList.count={subtitle_count}")
    if not isinstance(subtitles, list):
        return

    for index, item in enumerate(subtitles, start=1):
        if not isinstance(item, dict):
            continue
        subtitle_url = str(item.get("subtitle_url") or item.get("url") or "")
        _emit_debug(
            debug_logger,
            (
                f"{label} subtitleList[{index}]: "
                f"lan={item.get('lan') or item.get('language') or '未知'}，"
                f"lan_doc={item.get('lan_doc') or item.get('name') or '未知'}，"
                f"subtitle_url={subtitle_url or '空'}"
            ),
        )


def _strategy_access_config(config: BilibiliAccessConfig, strategy: AccessStrategy) -> BilibiliAccessConfig:
    return BilibiliAccessConfig(
        mode=strategy.mode,
        browser=strategy.browser or config.browser,
        cookie_header=strategy.cookie_header or config.cookie_header,
        cookies_file_path=strategy.cookies_file_path or config.cookies_file_path,
    )


def _extract_video_info_sync(url: str, strategy: AccessStrategy) -> dict[str, Any]:
    ydl_options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 30,
        "extractor_retries": 2,
        "retries": 2,
        "logger": _YtDlpQuietLogger(),
    }
    ydl_options.update(_strategy_ydl_options(strategy))
    with YoutubeDL(ydl_options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise BilibiliError(BilibiliErrorCode.VIDEO_INFO_FAILED, "yt-dlp返回的视频信息格式无法识别")
    return info


def _extract_subtitle_only_info_sync(url: str, access_config: BilibiliAccessConfig) -> dict[str, Any]:
    strategy = AccessStrategy(access_config.mode, browser=access_config.browser, cookies_file_path=access_config.cookies_file_path, cookie_header=access_config.cookie_header)
    ydl_options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["zh-Hans", "zh-Hant", "zh", "en", "all"],
        "subtitlesformat": "json3/vtt/srt/best",
        "socket_timeout": 30,
        "extractor_retries": 2,
        "retries": 2,
        "logger": _YtDlpQuietLogger(),
    }
    ydl_options.update(_strategy_ydl_options(strategy))
    with YoutubeDL(ydl_options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise BilibiliError(BilibiliErrorCode.VIDEO_INFO_FAILED, "yt-dlp字幕路径返回格式无法识别")
    return info


def _strategy_ydl_options(strategy: AccessStrategy) -> dict[str, Any]:
    options: dict[str, Any] = {}

    if strategy.mode in {
        BilibiliAccessMode.ENHANCED_HEADERS,
        BilibiliAccessMode.IMPERSONATE,
        BilibiliAccessMode.BROWSER_COOKIE,
        BilibiliAccessMode.COOKIES_FILE,
        BilibiliAccessMode.COOKIE_HEADER,
        BilibiliAccessMode.LOCAL_BROWSER_PROFILE,
    }:
        options["http_headers"] = _bilibili_headers()

    if strategy.mode == BilibiliAccessMode.IMPERSONATE:
        options["impersonate"] = "chrome"

    if strategy.mode == BilibiliAccessMode.BROWSER_COOKIE:
        browser = strategy.browser or BrowserCookieSource.CHROME
        options["cookiesfrombrowser"] = (browser.value, None, None, None)

    if strategy.mode == BilibiliAccessMode.COOKIES_FILE:
        if not strategy.cookies_file_path:
            raise BilibiliError(BilibiliErrorCode.COOKIE_INVALID, "已选择cookies.txt模式，但没有收到cookies.txt文件")
        options["cookiefile"] = str(strategy.cookies_file_path)

    if strategy.mode == BilibiliAccessMode.COOKIE_HEADER:
        if not strategy.cookie_header:
            raise BilibiliError(BilibiliErrorCode.COOKIE_INVALID, "已选择精简Cookie模式，但没有收到Cookie")
        options["http_headers"] = {
            **_bilibili_headers(),
            "Cookie": strategy.cookie_header,
        }

    if strategy.mode == BilibiliAccessMode.LOCAL_BROWSER_PROFILE:
        options["http_headers"] = _bilibili_headers()

    return options


def _bilibili_headers() -> dict[str, str]:
    return {
        **DEFAULT_HEADERS,
        "Referer": "https://www.bilibili.com/",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def _build_strategy_plan(config: BilibiliAccessConfig) -> list[AccessStrategy]:
    if config.mode == BilibiliAccessMode.AUTO:
        return [
            AccessStrategy(BilibiliAccessMode.BILIBILI_API),
            AccessStrategy(BilibiliAccessMode.LOCAL_BROWSER_PROFILE),
        ]
    if config.mode == BilibiliAccessMode.ANONYMOUS:
        return [AccessStrategy(BilibiliAccessMode.ANONYMOUS)]
    if config.mode == BilibiliAccessMode.ENHANCED_HEADERS:
        return [AccessStrategy(BilibiliAccessMode.ENHANCED_HEADERS)]
    if config.mode == BilibiliAccessMode.BILIBILI_API:
        return [AccessStrategy(BilibiliAccessMode.BILIBILI_API)]
    if config.mode == BilibiliAccessMode.IMPERSONATE:
        return [AccessStrategy(BilibiliAccessMode.IMPERSONATE)]
    if config.mode == BilibiliAccessMode.BROWSER_COOKIE:
        return [
            AccessStrategy(BilibiliAccessMode.BILIBILI_API),
            AccessStrategy(BilibiliAccessMode.BROWSER_COOKIE, browser=config.browser),
        ]
    if config.mode == BilibiliAccessMode.COOKIES_FILE:
        return [
            AccessStrategy(BilibiliAccessMode.BILIBILI_API),
            AccessStrategy(BilibiliAccessMode.COOKIES_FILE, cookies_file_path=config.cookies_file_path),
        ]
    if config.mode == BilibiliAccessMode.COOKIE_HEADER:
        return [
            AccessStrategy(BilibiliAccessMode.COOKIE_HEADER, cookie_header=config.cookie_header),
        ]
    if config.mode == BilibiliAccessMode.LOCAL_BROWSER_PROFILE:
        return [
            AccessStrategy(BilibiliAccessMode.LOCAL_BROWSER_PROFILE),
        ]

    return [AccessStrategy(BilibiliAccessMode.BILIBILI_API)]


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
    raw_info = _merge_selected_raw_info(parsed, info, selected_info, bv_id or parsed.bv_id)

    return BilibiliVideoInfo(
        title=title,
        bv_id=bv_id or parsed.bv_id,
        p_index=p_index,
        duration_seconds=duration,
        webpage_url=webpage_url,
        parts=parts,
        subtitle_candidates=subtitle_candidates,
        raw_info=raw_info,
    )


async def _extract_video_info_via_local_profile(parsed: ParsedBilibiliInput) -> BilibiliVideoInfo:
    attempts: list[SubtitlePathAttempt] = []
    html_info = await _extract_video_info_via_html(
        parsed,
        BilibiliAccessConfig(mode=BilibiliAccessMode.LOCAL_BROWSER_PROFILE),
        attempts,
    )
    if html_info:
        return html_info
    message = attempts[-1].message if attempts else "本地专用浏览器Profile未能获取视频页信息"
    raise BilibiliError(BilibiliErrorCode.BILIBILI_HTTP_412 if "412" in message else BilibiliErrorCode.COOKIE_INVALID, message)


async def _extract_video_info_via_bilibili_api(
    parsed: ParsedBilibiliInput,
    access_config: BilibiliAccessConfig,
) -> BilibiliVideoInfo:
    bvid = parsed.bv_id or _extract_bv_id(parsed.url)
    if not bvid:
        raise BilibiliError(BilibiliErrorCode.VIDEO_INFO_FAILED, "B站公开API需要BV号，当前输入无法提取")

    endpoint = "https://api.bilibili.com/x/web-interface/view"
    params: dict[str, object] = {"bvid": bvid}
    source_name = _video_info_api_label(access_config.mode)
    try:
        cookies = _load_cookies_for_httpx(access_config)
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(endpoint, params=params, headers=_bilibili_headers(), cookies=cookies)
            if response.status_code == 412 and access_config.mode in {
                BilibiliAccessMode.LOCAL_BROWSER_PROFILE,
                BilibiliAccessMode.COOKIE_HEADER,
                BilibiliAccessMode.COOKIES_FILE,
                BilibiliAccessMode.BROWSER_COOKIE,
            }:
                signed_params = await _signed_generic_wbi_params(client, cookies, {"bvid": bvid})
                endpoint = "https://api.bilibili.com/x/web-interface/wbi/view"
                params = signed_params
                source_name = f"{_video_info_api_label(access_config.mode)}（WBI view）"
                response = await client.get(endpoint, params=params, headers=_bilibili_headers(), cookies=cookies)
    except httpx.TimeoutException as exc:
        raise BilibiliError(BilibiliErrorCode.BILIBILI_TIMEOUT, f"{source_name}获取视频信息超时") from exc
    except httpx.RequestError as exc:
        raise BilibiliError(
            BilibiliErrorCode.BILIBILI_API_ERROR,
            f"{source_name}获取视频信息失败：{_sanitize_external_message(exc)}",
        ) from exc

    if response.status_code >= 400:
        raise _map_http_status(response.status_code, source_name)

    try:
        payload = response.json()
    except ValueError as exc:
        raise BilibiliError(BilibiliErrorCode.BILIBILI_API_ERROR, f"{source_name}返回内容不是有效JSON") from exc

    code = payload.get("code") if isinstance(payload, dict) else None
    if code != 0:
        message = str(payload.get("message") or f"{source_name}返回异常") if isinstance(payload, dict) else f"{source_name}返回异常"
        if code in {-404, 404}:
            raise BilibiliError(BilibiliErrorCode.VIDEO_UNAVAILABLE, "B站视频不存在或无法访问")
        if code in {-403, 403}:
            raise BilibiliError(BilibiliErrorCode.BILIBILI_HTTP_403, f"{source_name}返回访问被拒绝或权限受限")
        raise BilibiliError(BilibiliErrorCode.BILIBILI_API_ERROR, f"{source_name}返回异常：{message}")

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise BilibiliError(BilibiliErrorCode.BILIBILI_API_ERROR, f"{source_name}返回的视频信息格式无法识别")

    return _normalize_api_video_info(parsed, data)


def _video_info_api_label(mode: BilibiliAccessMode) -> str:
    if mode == BilibiliAccessMode.COOKIE_HEADER:
        return "精简Cookie视频信息API"
    if mode == BilibiliAccessMode.LOCAL_BROWSER_PROFILE:
        return "本地Profile视频信息API"
    if mode == BilibiliAccessMode.BROWSER_COOKIE:
        return "浏览器Cookie视频信息API"
    if mode == BilibiliAccessMode.COOKIES_FILE:
        return "cookies.txt视频信息API"
    return "B站公开API"


def _normalize_api_video_info(parsed: ParsedBilibiliInput, data: dict[str, Any]) -> BilibiliVideoInfo:
    bvid = str(data.get("bvid") or parsed.bv_id or "")
    title = str(data.get("title") or bvid or "未命名视频").strip()
    pages = [page for page in data.get("pages") or [] if isinstance(page, dict)]
    selected_page = _select_api_page(parsed, pages)
    playinfo = data.get("playinfo") if isinstance(data.get("playinfo"), dict) else {}
    p_index = _read_int(selected_page.get("page")) or parsed.p_index or 1
    duration = _read_float(selected_page.get("duration")) or _read_float(data.get("duration"))
    webpage_url = f"https://www.bilibili.com/video/{bvid}/" if bvid else parsed.url
    if p_index > 1 and bvid:
        webpage_url = f"{webpage_url}?{urlencode({'p': p_index})}"

    parts = [
        PartInfo(
            p_index=_read_int(page.get("page")) or index,
            title=str(page.get("part") or page.get("title") or f"P{index}").strip(),
            duration_seconds=_read_float(page.get("duration")),
            bv_id=bvid,
            url=f"https://www.bilibili.com/video/{bvid}/?{urlencode({'p': _read_int(page.get('page')) or index})}"
            if bvid
            else "",
        )
        for index, page in enumerate(pages, start=1)
    ]
    if not parts:
        parts = [
            PartInfo(
                p_index=p_index,
                title=title,
                duration_seconds=duration,
                bv_id=bvid,
                url=webpage_url,
            ),
        ]

    raw_info = {
        "title": title,
        "aid": data.get("aid"),
        "display_id": bvid,
        "bvid": bvid,
        "webpage_url": webpage_url,
        "page": p_index,
        "duration": duration,
        "cid": selected_page.get("cid") or data.get("cid") or playinfo.get("cid"),
        "epid": selected_page.get("epid")
        or selected_page.get("ep_id")
        or data.get("epid")
        or data.get("ep_id")
        or playinfo.get("epid")
        or playinfo.get("ep_id"),
        "pages": pages,
    }
    subtitle_candidates: list[SubtitleCandidate] = []
    _append_player_subtitle_candidates({"data": data}, "html_initial_state", subtitle_candidates, set())

    return BilibiliVideoInfo(
        title=title,
        bv_id=bvid,
        p_index=p_index,
        duration_seconds=duration,
        webpage_url=webpage_url,
        parts=parts,
        subtitle_candidates=subtitle_candidates,
        raw_info=raw_info,
    )


def _select_api_page(parsed: ParsedBilibiliInput, pages: list[dict[str, Any]]) -> dict[str, Any]:
    if not pages:
        return {}
    if parsed.p_index:
        for index, page in enumerate(pages, start=1):
            if (_read_int(page.get("page")) or index) == parsed.p_index:
                return page
    return pages[0]


async def _enrich_player_api_subtitles(
    parsed: ParsedBilibiliInput,
    video_info: BilibiliVideoInfo,
    access_config: BilibiliAccessConfig,
    debug_logger: Callable[[str], None] | None = None,
) -> BilibiliVideoInfo:
    candidates = list(video_info.subtitle_candidates)
    existing_urls = {candidate.url for candidate in candidates}
    raw_info = dict(video_info.raw_info)
    attempts: list[SubtitlePathAttempt] = []

    async def run_player_requests(current_info: dict[str, Any], config: BilibiliAccessConfig) -> None:
        bvid = str(current_info.get("bvid") or current_info.get("display_id") or video_info.bv_id or parsed.bv_id or "")
        cid = _read_int(current_info.get("cid"))
        aid = _read_int(current_info.get("aid"))
        ep_id = _read_int(current_info.get("epid")) or _read_int(current_info.get("ep_id"))
        debug_hint = _subtitle_request_debug_hint(bvid, aid, cid, ep_id)
        if not cid or not (bvid or aid):
            attempts.append(SubtitlePathAttempt("player_api", "B站播放器字幕接口", False, f"缺少aid/bvid/cid；{debug_hint}"))
            _emit_debug(debug_logger, f"播放器字幕接口跳过：缺少aid/bvid/cid；{debug_hint}")
            return

        if config.mode == BilibiliAccessMode.LOCAL_BROWSER_PROFILE:
            for source, label, params in _profile_page_subtitle_requests(bvid, aid, cid, ep_id):
                before_count = len(candidates)
                try:
                    _emit_debug(
                        debug_logger,
                        (
                            f"{label}请求前："
                            f"playerApiParams={_debug_params(params)}，"
                            f"bvid={bvid or '未提供'}，aid={aid or '未提供'}，cid={cid}，"
                            f"Cookie是否存在：本地Profile页面上下文，"
                            f"Referer=当前B站视频页"
                        ),
                    )
                    payload = await asyncio.to_thread(fetch_player_wbi_payload_from_profile, parsed.url, params)
                    _append_player_subtitle_candidates(payload, source, candidates, existing_urls, aid=aid, cid=cid)
                    subtitle_count = _player_subtitle_count(payload)
                    _emit_subtitle_list_debug(debug_logger, label, payload)
                    added = len(candidates) - before_count
                    attempts.append(
                        SubtitlePathAttempt(
                            source,
                            label,
                            added > 0,
                            f"页面内fetch返回{subtitle_count}条，新增{added}条字幕候选；{debug_hint}",
                        ),
                    )
                except RuntimeError as exc:
                    attempts.append(SubtitlePathAttempt(source, label, False, _sanitize_external_message(exc)))
            return

        cookies = _load_cookies_for_httpx(config)
        headers = _bilibili_headers()
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for source, label, endpoint, params in _player_subtitle_requests(bvid, aid, cid, ep_id):
                before_count = len(candidates)
                try:
                    _emit_debug(
                        debug_logger,
                        (
                            f"{label}请求前：endpoint={endpoint}，"
                            f"playerApiParams={_debug_params(params)}，"
                            f"bvid={bvid or '未提供'}，aid={aid or '未提供'}，cid={cid}，"
                            f"Cookie是否存在：{_cookie_presence_label(cookies)}，"
                            f"Referer={headers.get('Referer') or '未设置'}"
                        ),
                    )
                    response = await client.get(endpoint, params=params, headers=headers, cookies=cookies)
                    if response.status_code >= 400:
                        error = _map_http_status(response.status_code, label)
                        attempts.append(SubtitlePathAttempt(source, label, False, error.message))
                        _emit_debug(debug_logger, f"{label}请求失败：HTTP {response.status_code}")
                        continue
                    payload = response.json()
                    _append_player_subtitle_candidates(payload, source, candidates, existing_urls, aid=aid, cid=cid)
                    subtitle_count = _player_subtitle_count(payload)
                    _emit_subtitle_list_debug(debug_logger, label, payload)
                    added = len(candidates) - before_count
                    attempts.append(
                        SubtitlePathAttempt(
                            source,
                            label,
                            added > 0,
                            f"接口返回{subtitle_count}条，新增{added}条字幕候选；{debug_hint}",
                        ),
                    )
                except ValueError:
                    attempts.append(SubtitlePathAttempt(source, label, False, "返回内容不是有效JSON"))
                except (httpx.RequestError, BilibiliError) as exc:
                    attempts.append(SubtitlePathAttempt(source, label, False, _sanitize_external_message(exc)))

            before_count = len(candidates)
            try:
                signed_params = await _signed_player_wbi_params(client, cookies, bvid, aid, cid, ep_id)
                _emit_debug(
                    debug_logger,
                    (
                        "WBI签名播放器字幕接口请求前："
                        "endpoint=https://api.bilibili.com/x/player/wbi/v2，"
                        f"playerApiParams={_debug_params(signed_params)}，"
                        f"bvid={bvid or '未提供'}，aid={aid or '未提供'}，cid={cid}，"
                        f"Cookie是否存在：{_cookie_presence_label(cookies)}，"
                        f"Referer={headers.get('Referer') or '未设置'}"
                    ),
                )
                response = await client.get(
                    "https://api.bilibili.com/x/player/wbi/v2",
                    params=signed_params,
                    headers=headers,
                    cookies=cookies,
                )
                if response.status_code >= 400:
                    error = _map_http_status(response.status_code, "WBI签名播放器字幕接口")
                    attempts.append(SubtitlePathAttempt("player_wbi_signed_api", "WBI签名播放器字幕接口", False, error.message))
                    _emit_debug(debug_logger, f"WBI签名播放器字幕接口请求失败：HTTP {response.status_code}")
                else:
                    payload = response.json()
                    _append_player_subtitle_candidates(
                        payload,
                        "player_wbi_signed_api",
                        candidates,
                        existing_urls,
                        aid=aid,
                        cid=cid,
                    )
                    subtitle_count = _player_subtitle_count(payload)
                    _emit_subtitle_list_debug(debug_logger, "WBI签名播放器字幕接口", payload)
                    added = len(candidates) - before_count
                    attempts.append(
                        SubtitlePathAttempt(
                            "player_wbi_signed_api",
                            "WBI签名播放器字幕接口",
                            added > 0,
                            f"接口返回{subtitle_count}条，新增{added}条字幕候选；{debug_hint}",
                        ),
                    )
            except ValueError:
                attempts.append(SubtitlePathAttempt("player_wbi_signed_api", "WBI签名播放器字幕接口", False, "返回内容不是有效JSON"))
            except (httpx.RequestError, BilibiliError) as exc:
                attempts.append(SubtitlePathAttempt("player_wbi_signed_api", "WBI签名播放器字幕接口", False, _sanitize_external_message(exc)))

    try:
        await run_player_requests(raw_info, access_config)
    except BilibiliError as exc:
        attempts.append(SubtitlePathAttempt("player_api", "B站播放器字幕接口", False, exc.message))

    if not candidates:
        before_count = len(candidates)
        html_info = await _extract_video_info_via_html(parsed, access_config, attempts)
        if html_info:
            _emit_debug(debug_logger, "HTML初始化数据回退已返回视频信息，准备合并aid/cid/pages/subtitle候选")
            raw_info = _merge_raw_info(raw_info, html_info.raw_info)
            for candidate in html_info.subtitle_candidates:
                if candidate.url not in existing_urls:
                    candidates.append(candidate)
                    existing_urls.add(candidate.url)
            if len(candidates) == before_count:
                try:
                    await run_player_requests(raw_info, access_config)
                except BilibiliError as exc:
                    attempts.append(SubtitlePathAttempt("html_initial_state", "HTML初始化数据回退", False, exc.message))

    if not candidates:
        before_count = len(candidates)
        try:
            _emit_debug(debug_logger, "yt-dlp字幕路径请求前：--skip-download + --write-subs + --write-auto-subs")
            info = await asyncio.to_thread(_extract_subtitle_only_info_sync, parsed.url, access_config)
            for candidate in _collect_subtitle_candidates(_select_current_info(parsed, info)):
                if candidate.url not in existing_urls:
                    candidates.append(candidate)
                    existing_urls.add(candidate.url)
            added = len(candidates) - before_count
            _emit_debug(debug_logger, f"yt-dlp字幕路径返回字幕候选：新增{added}条")
            attempts.append(SubtitlePathAttempt("yt_dlp_subtitle", "yt-dlp字幕路径", True, f"新增{added}条字幕候选"))
        except (DownloadError, ExtractorError) as exc:
            error = _map_ytdlp_error(exc)
            attempts.append(SubtitlePathAttempt("yt_dlp_subtitle", "yt-dlp字幕路径", False, error.message))
        except Exception as exc:
            attempts.append(SubtitlePathAttempt("yt_dlp_subtitle", "yt-dlp字幕路径", False, _sanitize_external_message(exc)))

    if attempts:
        existing_access_attempts = list(video_info.access_attempts)
        existing_access_attempts.extend(
            AccessAttempt(
                mode=attempt.source,
                label=attempt.label,
                success=attempt.success,
                error_code="" if attempt.success else BilibiliErrorCode.SUBTITLE_FETCH_FAILED.value,
                message=attempt.message,
            )
            for attempt in attempts
        )
    else:
        existing_access_attempts = list(video_info.access_attempts)

    return replace(
        video_info,
        subtitle_candidates=candidates,
        raw_info=raw_info,
        access_attempts=existing_access_attempts,
    )


def _player_subtitle_requests(
    bvid: str,
    aid: int | None,
    cid: int,
    ep_id: int | None,
) -> list[tuple[str, str, str, dict[str, object]]]:
    requests: list[tuple[str, str, str, dict[str, object]]] = []
    if bvid:
        requests.append(
            (
                "player_api",
                "B站播放器字幕接口",
                "https://api.bilibili.com/x/player/v2",
                {"bvid": bvid, "cid": cid},
            ),
        )

    for source, label, params in _wbi_subtitle_param_variants(bvid, aid, cid, ep_id):
        requests.append((source, label, "https://api.bilibili.com/x/player/wbi/v2", params))
    return requests


def _profile_page_subtitle_requests(
    bvid: str,
    aid: int | None,
    cid: int,
    ep_id: int | None,
) -> list[tuple[str, str, dict[str, object]]]:
    return [
        (f"{source}_profile", f"{label}（页面内fetch）", params)
        for source, label, params in _wbi_subtitle_param_variants(bvid, aid, cid, ep_id)
    ]


def _wbi_subtitle_param_variants(
    bvid: str,
    aid: int | None,
    cid: int,
    ep_id: int | None,
) -> list[tuple[str, str, dict[str, object]]]:
    variants: list[tuple[str, str, dict[str, object]]] = []
    response_params: dict[str, object] = {"cid": cid}
    if aid:
        response_params["aid"] = aid
    elif bvid:
        response_params["bvid"] = bvid
    variants.append(("player_wbi_api", "B站播放器wbi字幕接口", response_params))
    if ep_id:
        ep_params = dict(response_params)
        ep_params["ep_id"] = ep_id
        variants.append(("player_wbi_api_ep", "B站播放器wbi字幕接口（ep_id）", ep_params))
    return variants


def _player_subtitle_count(payload: dict[str, Any]) -> int:
    subtitles = (
        payload.get("data", {}).get("subtitle", {}).get("subtitles", [])
        if isinstance(payload, dict)
        else []
    )
    return len(subtitles) if isinstance(subtitles, list) else 0


def _subtitle_request_debug_hint(bvid: str, aid: int | None, cid: int | None, ep_id: int | None) -> str:
    return (
        f"参数：aid={'yes' if aid else 'no'}，"
        f"bvid={'yes' if bvid else 'no'}，"
        f"cid={'yes' if cid else 'no'}，"
        f"ep_id={'yes' if ep_id else 'no'}"
    )


def _merge_raw_info(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key in ("aid", "bvid", "display_id", "cid", "epid", "ep_id", "pages", "title", "duration", "webpage_url", "page"):
        value = extra.get(key)
        if merged.get(key) in (None, "", []) and value not in (None, "", []):
            merged[key] = value
    return merged


async def _signed_player_wbi_params(
    client: httpx.AsyncClient,
    cookies: httpx.Cookies | None,
    bvid: str,
    aid: int | None,
    cid: int,
    ep_id: int | None,
) -> dict[str, object]:
    keys = await _fetch_wbi_keys(client, cookies)
    params: dict[str, object] = {"cid": cid}
    if aid:
        params["aid"] = aid
    elif bvid:
        params["bvid"] = bvid
    if ep_id:
        params["ep_id"] = ep_id
    return _sign_wbi_params(params, keys.mixin_key)


async def _signed_generic_wbi_params(
    client: httpx.AsyncClient,
    cookies: httpx.Cookies | None,
    params: dict[str, object],
) -> dict[str, object]:
    keys = await _fetch_wbi_keys(client, cookies)
    return _sign_wbi_params(params, keys.mixin_key)


async def _fetch_wbi_keys(client: httpx.AsyncClient, cookies: httpx.Cookies | None) -> WbiKeys:
    try:
        response = await client.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers=_bilibili_headers(),
            cookies=cookies,
        )
    except httpx.TimeoutException as exc:
        raise BilibiliError(BilibiliErrorCode.BILIBILI_TIMEOUT, "WBI nav获取超时") from exc
    except httpx.RequestError as exc:
        raise BilibiliError(
            BilibiliErrorCode.BILIBILI_API_ERROR,
            f"WBI nav获取失败：{_sanitize_external_message(exc)}",
        ) from exc

    if response.status_code >= 400:
        raise _map_http_status(response.status_code, "WBI nav")
    try:
        payload = response.json()
    except ValueError as exc:
        raise BilibiliError(BilibiliErrorCode.BILIBILI_API_ERROR, "WBI nav返回内容不是有效JSON") from exc

    wbi_img = payload.get("data", {}).get("wbi_img", {}) if isinstance(payload, dict) else {}
    img_key = _wbi_key_from_url(str(wbi_img.get("img_url") or ""))
    sub_key = _wbi_key_from_url(str(wbi_img.get("sub_url") or ""))
    if not img_key or not sub_key:
        raise BilibiliError(BilibiliErrorCode.BILIBILI_API_ERROR, "WBI nav未返回可用key")
    mixin_key = "".join((img_key + sub_key)[index] for index in MIXIN_KEY_ENC_TAB)[:32]
    return WbiKeys(img_key=img_key, sub_key=sub_key, mixin_key=mixin_key)


def _wbi_key_from_url(url: str) -> str:
    path = urlsplit(url).path
    filename = path.rsplit("/", 1)[-1]
    return filename.split(".", 1)[0]


def _sign_wbi_params(params: dict[str, object], mixin_key: str) -> dict[str, object]:
    signed = {key: value for key, value in params.items() if value not in (None, "")}
    signed["wts"] = int(time.time())
    filtered: dict[str, str] = {}
    for key, value in signed.items():
        text = str(value)
        filtered[key] = re.sub(r"[!'()*]", "", text)
    query = "&".join(f"{quote(key, safe='')}={quote(filtered[key], safe='')}" for key in sorted(filtered))
    signed["w_rid"] = hashlib.md5(f"{query}{mixin_key}".encode("utf-8")).hexdigest()
    return signed


async def _extract_video_info_via_html(
    parsed: ParsedBilibiliInput,
    access_config: BilibiliAccessConfig,
    attempts: list[SubtitlePathAttempt],
) -> BilibiliVideoInfo | None:
    try:
        page_state: dict[str, Any] = {}
        if access_config.mode == BilibiliAccessMode.LOCAL_BROWSER_PROFILE:
            page_state = await asyncio.to_thread(fetch_bilibili_page_state_from_profile, parsed.url)
            html = str(page_state.get("html") or "")
        else:
            cookies = _load_cookies_for_httpx(access_config)
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.get(parsed.url, headers=_bilibili_headers(), cookies=cookies)
            if response.status_code >= 400:
                error = _map_http_status(response.status_code, "HTML初始化数据回退")
                attempts.append(SubtitlePathAttempt("html_initial_state", "HTML初始化数据回退", False, error.message))
                return None
            html = response.text
    except (httpx.RequestError, BilibiliError, RuntimeError) as exc:
        attempts.append(SubtitlePathAttempt("html_initial_state", "HTML初始化数据回退", False, _sanitize_external_message(exc)))
        return None

    try:
        initial_state = page_state.get("initialState") if isinstance(page_state.get("initialState"), dict) else {}
        playinfo = page_state.get("playinfo") if isinstance(page_state.get("playinfo"), dict) else {}
        if not initial_state:
            initial_state = _extract_json_assignment(html, INITIAL_STATE_PATTERN)
        if not playinfo:
            playinfo = _extract_json_assignment(html, PLAYINFO_PATTERN)
        info = _html_payload_to_info(parsed, initial_state, playinfo)
        _merge_player_resource_params(info, page_state.get("resourceUrls") if isinstance(page_state, dict) else None)
    except ValueError as exc:
        attempts.append(SubtitlePathAttempt("html_initial_state", "HTML初始化数据回退", False, str(exc)))
        return None

    video_info = _normalize_api_video_info(parsed, info)
    attempts.append(SubtitlePathAttempt("html_initial_state", "HTML初始化数据回退", True, "已补齐视频和字幕候选信息"))
    return video_info


def _extract_json_assignment(html: str, pattern: re.Pattern[str]) -> dict[str, Any]:
    match = pattern.search(html)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("HTML初始化数据不是有效JSON") from exc
    return payload if isinstance(payload, dict) else {}


def _html_payload_to_info(
    parsed: ParsedBilibiliInput,
    initial_state: dict[str, Any],
    playinfo: dict[str, Any],
) -> dict[str, Any]:
    video_data = initial_state.get("videoData") if isinstance(initial_state.get("videoData"), dict) else {}
    pages = [page for page in video_data.get("pages") or [] if isinstance(page, dict)]
    selected_page = _select_api_page(parsed, pages)
    cid = selected_page.get("cid") or video_data.get("cid") or _deep_first(playinfo, ("cid",))
    aid = video_data.get("aid") or _deep_first(playinfo, ("aid",))
    ep_id = (
        selected_page.get("epid")
        or selected_page.get("ep_id")
        or video_data.get("epid")
        or video_data.get("ep_id")
        or _deep_first(initial_state, ("epid", "ep_id", "episode_id"))
        or _deep_first(playinfo, ("epid", "ep_id", "episode_id"))
    )
    bvid = str(video_data.get("bvid") or initial_state.get("bvid") or parsed.bv_id or "")
    if not (aid or bvid or cid):
        raise ValueError("HTML初始化数据缺少aid/bvid/cid")

    subtitles = _extract_html_subtitles(initial_state, playinfo)
    return {
        "aid": aid,
        "bvid": bvid,
        "title": str(video_data.get("title") or initial_state.get("title") or bvid or "未命名视频"),
        "duration": selected_page.get("duration") or video_data.get("duration"),
        "cid": cid,
        "epid": ep_id,
        "ep_id": ep_id,
        "pages": pages,
        "subtitle": {"subtitles": subtitles},
    }


def _extract_html_subtitles(initial_state: dict[str, Any], playinfo: dict[str, Any]) -> list[dict[str, Any]]:
    subtitles: list[dict[str, Any]] = []
    for payload in (initial_state, playinfo):
        _collect_subtitle_dicts(payload, subtitles)
    return subtitles


def _merge_player_resource_params(info: dict[str, Any], resource_urls: object) -> None:
    if not isinstance(resource_urls, list):
        return
    for raw_url in resource_urls:
        if not isinstance(raw_url, str) or "/x/player/" not in raw_url:
            continue
        try:
            query = parse_qs(urlsplit(raw_url).query)
        except ValueError:
            continue
        for target, keys in (("aid", ("aid",)), ("cid", ("cid",)), ("epid", ("ep_id", "epid"))):
            if info.get(target):
                continue
            for key in keys:
                value = _read_int((query.get(key) or [None])[0])
                if value:
                    info[target] = value
                    if target == "epid":
                        info["ep_id"] = value
                    break
        if info.get("aid") and info.get("cid"):
            return


def _deep_first(value: object, keys: tuple[str, ...]) -> object | None:
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if item not in (None, "", []):
                return item
        for child in value.values():
            found = _deep_first(child, keys)
            if found not in (None, "", []):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _deep_first(child, keys)
            if found not in (None, "", []):
                return found
    return None


def _collect_subtitle_dicts(value: object, subtitles: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        subtitle_url = value.get("subtitle_url") or value.get("url")
        if isinstance(subtitle_url, str) and _is_probable_subtitle_url(subtitle_url):
            subtitles.append(value)
        for key, child in value.items():
            if key in {"dash", "audio", "video", "playurl", "baseUrl", "base_url", "backupUrl", "backup_url"}:
                continue
            _collect_subtitle_dicts(child, subtitles)
    elif isinstance(value, list):
        for child in value:
            _collect_subtitle_dicts(child, subtitles)


def _is_probable_subtitle_url(url: str) -> bool:
    lowered = url.lower()
    if "subtitle" in lowered or "/sub" in lowered:
        return True
    path = urlsplit(url if not url.startswith("//") else f"https:{url}").path.lower()
    return path.endswith((".json", ".json3", ".vtt", ".srt", ".srv3"))


def _append_player_subtitle_candidates(
    payload: dict[str, Any],
    source: str,
    candidates: list[SubtitleCandidate],
    existing_urls: set[str],
    aid: int | None = None,
    cid: int | None = None,
) -> None:
    subtitles = (
        payload.get("data", {}).get("subtitle", {}).get("subtitles", [])
        if isinstance(payload, dict)
        else []
    )
    if not isinstance(subtitles, list):
        return

    for item in subtitles:
        if not isinstance(item, dict):
            continue
        url = str(item.get("subtitle_url") or item.get("url") or "")
        if not url or url in existing_urls or not _is_probable_subtitle_url(url):
            continue
        if not _subtitle_url_matches_current_video(url, aid=aid, cid=cid):
            continue
        candidates.append(
            SubtitleCandidate(
                language=str(item.get("lan") or item.get("language") or ""),
                url=url,
                ext=_subtitle_ext_from_url(url),
                source=source,
                name=str(item.get("lan_doc") or item.get("name") or ""),
            ),
        )
        existing_urls.add(url)


def _subtitle_url_matches_current_video(url: str, aid: int | None = None, cid: int | None = None) -> bool:
    lowered = url.lower()
    if "aisubtitle" not in lowered and "ai_subtitle" not in lowered:
        return True
    compact_url = re.sub(r"\D+", "", url)
    if aid and str(aid) not in compact_url:
        return False
    if cid and str(cid) not in compact_url:
        return False
    return True


def _subtitle_ext_from_url(url: str) -> str:
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return "json"
    for ext in ("vtt", "srt", "json", "json3", "srv3"):
        if path.endswith(f".{ext}"):
            return ext
    return "json"


def _merge_selected_raw_info(
    parsed: ParsedBilibiliInput,
    info: dict[str, Any],
    selected_info: dict[str, Any],
    bvid: str,
) -> dict[str, Any]:
    raw_info = dict(selected_info)
    for key in ("aid", "cid", "epid", "ep_id", "pages"):
        if raw_info.get(key) is None and info.get(key) is not None:
            raw_info[key] = info.get(key)
    raw_info.setdefault("bvid", bvid or parsed.bv_id)
    raw_info.setdefault("display_id", bvid or parsed.bv_id)
    raw_info.setdefault("webpage_url", selected_info.get("webpage_url") or info.get("webpage_url") or parsed.url)
    return raw_info


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
    for container_name, source in (
        ("requested_subtitles", "requested"),
        ("subtitles", "uploaded"),
        ("automatic_captions", "automatic"),
    ):
        container = info.get(container_name)
        if not isinstance(container, dict):
            continue
        for language, raw_entries in container.items():
            entries = raw_entries if isinstance(raw_entries, list) else [raw_entries]
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
                            headers=_normalize_candidate_headers(entry),
                        ),
                    )
    return candidates


def _normalize_candidate_headers(entry: dict[str, Any]) -> dict[str, str]:
    headers = entry.get("http_headers") or entry.get("headers") or {}
    if not isinstance(headers, dict):
        return {}
    return {str(key): str(value) for key, value in headers.items() if key and value}


def _subtitle_headers(candidate: SubtitleCandidate) -> dict[str, str]:
    return {
        **_bilibili_headers(),
        **candidate.headers,
    }


def _subtitle_priority(candidate: SubtitleCandidate) -> tuple[int, int, int]:
    language = candidate.language.lower()
    if _is_chinese_language(language):
        language_rank = 0
    elif language.startswith("en") or "eng" in language or "英文" in language:
        language_rank = 1
    else:
        language_rank = 2

    source_rank = {
        "uploaded": 0,
        "requested": 1,
        "player_api": 1,
        "player_wbi_api": 1,
        "player_wbi_api_ep": 1,
        "player_wbi_api_profile": 1,
        "player_wbi_api_ep_profile": 1,
        "player_wbi_signed_api": 1,
        "html_initial_state": 1,
        "yt_dlp_subtitle": 1,
        "automatic": 2,
    }.get(candidate.source, 3)
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
    message = _sanitize_external_message(exc)
    lowered = message.lower()

    if "could not copy" in lowered and "cookie" in lowered and "database" in lowered:
        return BilibiliError(
            BilibiliErrorCode.COOKIE_DATABASE_COPY_FAILED,
            "浏览器Cookie数据库复制失败。可尝试关闭浏览器和后台进程、换用Edge / Firefox，或改用cookies.txt导入模式。",
        )
    if any(token in lowered for token in ("http error 412", "precondition failed", "http 412")):
        return BilibiliError(
            BilibiliErrorCode.BILIBILI_HTTP_412,
            "B站返回HTTP 412 Precondition Failed，通常是风控或访问前置条件失败。可重新打开本地B站登录窗口后再试。",
        )
    if any(token in lowered for token in ("http error 403", "forbidden", "http 403")):
        return BilibiliError(BilibiliErrorCode.BILIBILI_HTTP_403, "B站返回HTTP 403，访问被拒绝或视频权限受限")
    if any(token in lowered for token in ("404", "not found", "不存在", "已失效")):
        return BilibiliError(BilibiliErrorCode.VIDEO_UNAVAILABLE, "B站视频不存在或无法访问")
    if any(token in lowered for token in ("timed out", "timeout")):
        return BilibiliError(BilibiliErrorCode.BILIBILI_TIMEOUT, "B站访问超时，请稍后重试或切换访问模式")
    if "unable to load cookies" in lowered or "cookie file" in lowered or "invalid cookie" in lowered:
        return BilibiliError(BilibiliErrorCode.COOKIE_INVALID, "cookies.txt无法读取或Cookie已失效，请重新导出后再试")
    if "impersonate" in lowered and "curl_cffi" in lowered:
        return BilibiliError(
            BilibiliErrorCode.VIDEO_INFO_FAILED,
            "Chrome指纹模拟不可用：当前Python环境缺少curl_cffi或yt-dlp无法启用该能力",
        )
    if any(token in lowered for token in ("login", "cookies", "cookie", "private", "会员", "登录")):
        return BilibiliError(BilibiliErrorCode.LOGIN_REQUIRED, "该视频可能需要登录权限、付费权限或有效Cookie，MVP暂不支持受限内容")

    return BilibiliError(BilibiliErrorCode.YTDLP_ERROR, f"yt-dlp获取视频信息失败：{message}")


def _map_http_status(status_code: int, source: str) -> BilibiliError:
    if status_code == 412:
        return BilibiliError(
            BilibiliErrorCode.BILIBILI_HTTP_412,
            f"{source}返回HTTP 412 Precondition Failed，通常是风控或访问前置条件失败",
        )
    if status_code == 403:
        return BilibiliError(BilibiliErrorCode.BILIBILI_HTTP_403, f"{source}返回HTTP 403，访问被拒绝或权限受限")
    if status_code == 404:
        return BilibiliError(BilibiliErrorCode.VIDEO_UNAVAILABLE, "B站视频不存在或无法访问")
    return BilibiliError(BilibiliErrorCode.BILIBILI_RETURNED_ERROR, f"{source}返回异常：HTTP {status_code}")


def _choose_final_access_error(errors: list[BilibiliError]) -> BilibiliError:
    if not errors:
        return BilibiliError(BilibiliErrorCode.YTDLP_ERROR, "yt-dlp获取视频信息失败")

    priority = {
        BilibiliErrorCode.BILIBILI_HTTP_412: 0,
        BilibiliErrorCode.COOKIE_DATABASE_COPY_FAILED: 1,
        BilibiliErrorCode.COOKIE_INVALID: 2,
        BilibiliErrorCode.BILIBILI_HTTP_403: 3,
        BilibiliErrorCode.LOGIN_REQUIRED: 4,
        BilibiliErrorCode.VIDEO_UNAVAILABLE: 5,
        BilibiliErrorCode.BILIBILI_TIMEOUT: 6,
    }
    return min(errors, key=lambda error: priority.get(error.code, 99))


def _with_attempt_summary(message: str, attempts: list[AccessAttempt]) -> str:
    if not attempts:
        return message
    labels = "、".join(attempt.label for attempt in attempts)
    return f"{message}已尝试：{labels}。"


def _load_cookies_for_httpx(access_config: BilibiliAccessConfig | None) -> httpx.Cookies | None:
    if not access_config:
        return None
    if access_config.mode == BilibiliAccessMode.BROWSER_COOKIE:
        return _load_browser_cookies_for_httpx(access_config.browser)
    if access_config.mode == BilibiliAccessMode.COOKIES_FILE:
        if not access_config.cookies_file_path:
            raise BilibiliError(BilibiliErrorCode.COOKIE_INVALID, "已选择cookies.txt模式，但没有收到cookies.txt文件")
        return _load_netscape_cookie_file(access_config.cookies_file_path)
    if access_config.mode == BilibiliAccessMode.COOKIE_HEADER:
        return _load_cookie_header(access_config.cookie_header)
    if access_config.mode == BilibiliAccessMode.LOCAL_BROWSER_PROFILE:
        try:
            return load_bilibili_cookies_from_profile()
        except RuntimeError as exc:
            raise BilibiliError(BilibiliErrorCode.COOKIE_INVALID, _sanitize_external_message(exc)) from exc
    return None


def _load_browser_cookies_for_httpx(browser: BrowserCookieSource) -> httpx.Cookies:
    try:
        jar = extract_cookies_from_browser(browser.value, logger=_YtDlpQuietLogger())
    except CookieLoadError as exc:
        raise _map_ytdlp_error(exc) from exc
    except Exception as exc:  # pragma: no cover - browser cookie stores vary by OS and browser
        raise _map_ytdlp_error(exc) from exc

    cookies = httpx.Cookies()
    valid_count = 0
    for cookie in jar:
        domain = str(getattr(cookie, "domain", "") or "")
        if not _is_bilibili_cookie_domain(domain):
            continue
        name = str(getattr(cookie, "name", "") or "")
        value = getattr(cookie, "value", None)
        if not name or value is None:
            continue

        set_kwargs = {"path": str(getattr(cookie, "path", "") or "/")}
        if domain:
            set_kwargs["domain"] = domain
        cookies.set(name=name, value=str(value), **set_kwargs)
        valid_count += 1

    if valid_count == 0:
        raise BilibiliError(
            BilibiliErrorCode.COOKIE_INVALID,
            f"未从{_browser_label(browser)}读取到可用于B站的Cookie，请确认该浏览器已登录B站，或改用cookies.txt导入模式。",
        )

    return cookies


def _is_bilibili_cookie_domain(domain: str) -> bool:
    normalized = domain.lstrip(".").lower()
    return normalized == "bilibili.com" or normalized.endswith(".bilibili.com") or normalized.endswith(".hdslb.com")


def _load_cookie_header(raw_cookie: str) -> httpx.Cookies:
    simplified = simplify_bilibili_cookie_header(raw_cookie)
    if not simplified:
        raise BilibiliError(
            BilibiliErrorCode.COOKIE_INVALID,
            "未找到可用的6项精简B站Cookie，请重新打开本地B站登录窗口或重新粘贴Cookie。",
        )
    return cookie_header_to_httpx_cookies(simplified)


def _load_netscape_cookie_file(path: Path) -> httpx.Cookies:
    cookies = httpx.Cookies()
    valid_count = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as exc:
        raise BilibiliError(BilibiliErrorCode.COOKIE_INVALID, "cookies.txt无法读取，请重新选择文件") from exc

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        elif line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            continue

        domain, _include_subdomains, cookie_path, secure, _expires, name, value = parts[:7]
        if not domain or not name:
            continue

        cookies.set(
            name=name,
            value=value,
            domain=domain,
            path=cookie_path or "/",
        )
        valid_count += 1

    if valid_count == 0:
        raise BilibiliError(BilibiliErrorCode.COOKIE_INVALID, "cookies.txt中没有可用Cookie，请确认文件格式为Netscape cookies.txt")

    return cookies


def _sanitize_external_message(value: object) -> str:
    text = str(value)
    text = re.sub(r"(?i)(cookie\s*[:=]\s*)[^\r\n]+", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)\b((?:SESSDATA|bili_jct|DedeUserID|DedeUserID__ckMd5|bili_ticket|bili_ticket_expires)\s*=\s*)[^;\s]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)[^\r\n;]+", r"\1[REDACTED]", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*[^\\/:*?\"<>|\r\n]*", "[LOCAL_PATH]", text)
    text = re.sub(r"/(?:Users|home)/[^\s'\"]+", "[LOCAL_PATH]", text)
    return text


def _browser_label(browser: BrowserCookieSource) -> str:
    labels = {
        BrowserCookieSource.CHROME: "Chrome",
        BrowserCookieSource.EDGE: "Edge",
        BrowserCookieSource.FIREFOX: "Firefox",
    }
    return labels.get(browser, browser.value)


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


__all__ = [
    "MIXIN_KEY_ENC_TAB",
    "AccessAttempt",
    "BilibiliAccessConfig",
    "BilibiliAccessMode",
    "BilibiliError",
    "BilibiliErrorCode",
    "BilibiliVideoInfo",
    "BrowserCookieSource",
    "PartInfo",
    "ParsedBilibiliInput",
    "SubtitleCandidate",
    "WbiKeys",
    "_extract_json_assignment",
    "_html_payload_to_info",
    "_sign_wbi_params",
    "choose_subtitle_candidate",
    "download_subtitle",
    "fetch_video_info",
    "parse_bilibili_input",
]
