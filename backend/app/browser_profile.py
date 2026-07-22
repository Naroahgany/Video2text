"""Local browser profile helpers for Bilibili login fallback."""

from __future__ import annotations

import argparse
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .bilibili_cookie import (
    cookie_header_to_httpx_cookies,
    describe_bilibili_cookie_header,
    simplify_bilibili_cookie_collection,
)
from .utils import redact_secrets

ROOT_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = ROOT_DIR / "runtime"
PROFILE_DIR = RUNTIME_DIR / "browser-profile"
CACHE_DIR = RUNTIME_DIR / "browser-cache"
BILIBILI_HOME_URL = "https://www.bilibili.com/"
BILIBILI_COOKIE_URLS = [
    "https://www.bilibili.com/",
    "https://api.bilibili.com/",
    "https://passport.bilibili.com/",
]
COOKIE_EXTRACT_TOKEN_TTL_SECONDS = 20 * 60
PROFILE_READ_RETRY_ATTEMPTS = 12
PROFILE_READ_RETRY_DELAY_SECONDS = 0.5
LOGIN_WINDOW_START_TIMEOUT_SECONDS = 40
_COOKIE_EXTRACT_TOKENS: dict[str, float] = {}
_COOKIE_EXTRACT_RESULTS: dict[str, dict[str, Any]] = {}
_COOKIE_EXTRACT_ERRORS: dict[str, str] = {}
_ACTIVE_LOGIN_SESSIONS: dict[str, "_LoginWindowSession"] = {}


class _LoginWindowSession:
    def __init__(self) -> None:
        self.startup_ready = threading.Event()
        self.stop_requested = threading.Event()
        self.extract_requested = threading.Event()
        self.result_ready = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: str | None = None


def profile_relative_path() -> str:
    """Return a safe, non-absolute profile path for UI and logs."""

    return "runtime/browser-profile/"


def profile_status() -> dict[str, Any]:
    """Return sanitized local profile status without exposing absolute paths."""

    exists = PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir())
    return {
        "available": exists,
        "profile_path_hint": profile_relative_path(),
        "message": "本地专用浏览器 Profile 已创建" if exists else "尚未创建本地专用浏览器 Profile",
    }


def create_cookie_extract_session() -> str:
    """Create a short-lived local token for the sensitive cookie extraction API."""

    _cleanup_cookie_extract_sessions()
    token = secrets.token_urlsafe(24)
    _COOKIE_EXTRACT_TOKENS[token] = time.monotonic() + COOKIE_EXTRACT_TOKEN_TTL_SECONDS
    return token


def validate_cookie_extract_session(token: str) -> bool:
    """Return whether a cookie extraction token is still valid."""

    _cleanup_cookie_extract_sessions()
    expires_at = _COOKIE_EXTRACT_TOKENS.get(str(token or ""))
    return bool(expires_at and expires_at >= time.monotonic())


def consume_cookie_extract_session(token: str) -> None:
    """Consume a cookie extraction token after a successful extraction."""

    normalized = str(token or "")
    _COOKIE_EXTRACT_TOKENS.pop(normalized, None)
    _COOKIE_EXTRACT_RESULTS.pop(normalized, None)
    _COOKIE_EXTRACT_ERRORS.pop(normalized, None)
    _ACTIVE_LOGIN_SESSIONS.pop(normalized, None)


def open_login_window() -> dict[str, Any]:
    """Open a dedicated local Bilibili login window in a background thread."""

    _ensure_playwright_available()
    _cleanup_cookie_extract_sessions()
    for active_token in list(_ACTIVE_LOGIN_SESSIONS):
        if validate_cookie_extract_session(active_token):
            return {
                "opened": True,
                "profile_path_hint": profile_relative_path(),
                "session_token": active_token,
                "message": "本地专用 B站登录窗口已打开，请在该窗口完成登录后回到本页提取 Cookie。",
            }
        _ACTIVE_LOGIN_SESSIONS.pop(active_token, None)

    session_token = create_cookie_extract_session()
    _COOKIE_EXTRACT_RESULTS.pop(session_token, None)
    _COOKIE_EXTRACT_ERRORS.pop(session_token, None)
    session = _LoginWindowSession()
    _ACTIVE_LOGIN_SESSIONS[session_token] = session
    thread = threading.Thread(target=_run_login_window, args=(session_token,), daemon=True)
    try:
        thread.start()
    except Exception as exc:
        consume_cookie_extract_session(session_token)
        raise RuntimeError(_login_window_error_message(exc)) from exc

    if not session.startup_ready.wait(timeout=LOGIN_WINDOW_START_TIMEOUT_SECONDS):
        session.stop_requested.set()
        consume_cookie_extract_session(session_token)
        raise RuntimeError(
            "B站登录窗口启动超时，请检查网络或安全软件后重试；"
            "如果仍失败，请重新运行项目启动脚本修复 Playwright Chromium。"
        )
    if session.error:
        error = session.error
        consume_cookie_extract_session(session_token)
        raise RuntimeError(error)

    return {
        "opened": True,
        "profile_path_hint": profile_relative_path(),
        "session_token": session_token,
        "message": "已打开本地专用 B站登录窗口，请在该窗口中完成登录。",
    }


def load_bilibili_cookies_from_profile() -> httpx.Cookies:
    """Load simplified Bilibili cookies from the local Playwright profile."""

    cookie_header = extract_simplified_cookie_header_from_profile()["cookie_header"]
    return cookie_header_to_httpx_cookies(cookie_header)


def extract_simplified_cookie_header_from_profile(session_token: str | None = None) -> dict[str, Any]:
    """Read local Profile cookies and return only the stage 4.3 whitelist header."""

    token = str(session_token or "")
    if token and token in _COOKIE_EXTRACT_RESULTS:
        payload = _COOKIE_EXTRACT_RESULTS.pop(token)
        _COOKIE_EXTRACT_ERRORS.pop(token, None)
        return payload
    if token and token in _ACTIVE_LOGIN_SESSIONS:
        return _extract_from_active_login_session(token, _ACTIVE_LOGIN_SESSIONS[token])
    if token and token in _COOKIE_EXTRACT_ERRORS:
        error = _COOKIE_EXTRACT_ERRORS.pop(token)
        raise RuntimeError(error)
    if token:
        raise RuntimeError("登录窗口会话已结束或本地服务已重启，请重新打开 B站登录窗口后再提取 Cookie。")

    raw_cookies = _read_profile_cookies()
    bilibili_cookies: list[dict[str, Any]] = []
    for cookie in raw_cookies:
        domain = str(cookie.get("domain") or "")
        if not _is_bilibili_cookie_domain(domain):
            continue
        bilibili_cookies.append(cookie)

    cookie_header = simplify_bilibili_cookie_collection(bilibili_cookies)
    if not cookie_header:
        fields = _inspect_cookie_db_whitelist_fields()
        if fields:
            raise RuntimeError(
                "本地专用浏览器 Profile 的 Cookie 数据库中已存在精简字段，"
                "但 Playwright 关闭后重开 Profile 时没有返回 Cookie。"
                "请重新打开 B站登录窗口，保持窗口打开并点击提取；如仍失败，可先使用手动 Cookie 输入作为临时排错路径。"
            )
        raise RuntimeError("本地专用浏览器 Profile 未读取到 6 项精简 B站 Cookie，请确认已登录后重试。")

    return _cookie_response_payload(cookie_header, "已从本地专用 Profile 提取并精简 B站 Cookie。")


def _cleanup_cookie_extract_sessions() -> None:
    now = time.monotonic()
    expired = [token for token, expires_at in _COOKIE_EXTRACT_TOKENS.items() if expires_at < now]
    for token in expired:
        _COOKIE_EXTRACT_TOKENS.pop(token, None)
        _COOKIE_EXTRACT_RESULTS.pop(token, None)
        _COOKIE_EXTRACT_ERRORS.pop(token, None)
        _ACTIVE_LOGIN_SESSIONS.pop(token, None)


def fetch_bilibili_page_html_from_profile(url: str) -> str:
    """Fetch page HTML through the persistent local browser context."""

    return str(fetch_bilibili_page_state_from_profile(url).get("html") or "")


def fetch_bilibili_page_state_from_profile(url: str) -> dict[str, Any]:
    """Fetch page HTML and runtime globals through the persistent browser context."""

    def read_page(page: Any) -> dict[str, Any]:
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if response and response.status >= 400:
            raise RuntimeError(f"本地专用浏览器 Profile 访问视频页失败：HTTP {response.status}")
        page.wait_for_timeout(5000)
        state = page.evaluate(
            """
            () => ({
              initialState: window.__INITIAL_STATE__ || null,
              playinfo: window.__playinfo__ || null,
              resourceUrls: performance.getEntriesByType('resource')
                .map((entry) => entry.name)
                .filter((name) => /x\/player\/(wbi\/)?v2|x\/web-interface\/(wbi\/)?view/.test(name)),
            })
            """,
        )
        if not isinstance(state, dict):
            state = {}
        state["html"] = page.content()
        return state

    return _with_profile_page(read_page)


def fetch_player_wbi_payload_from_profile(url: str, params: dict[str, object]) -> dict[str, Any]:
    """Run x/player/wbi/v2 fetch inside the persistent Bilibili page context."""

    safe_params = {key: value for key, value in params.items() if value not in (None, "")}

    def read_payload(page: Any) -> dict[str, Any]:
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if response and response.status >= 400:
            raise RuntimeError(f"本地专用浏览器 Profile 访问视频页失败：HTTP {response.status}")
        path = f"/x/player/wbi/v2?{urlencode(safe_params)}"
        payload = page.evaluate(
            """
            async (path) => {
              const response = await fetch(path, {
                credentials: 'include',
                headers: { accept: 'application/json, text/plain, */*' },
              });
              return {
                ok: response.ok,
                status: response.status,
                text: await response.text(),
              };
            }
            """,
            path,
        )
        if not isinstance(payload, dict):
            raise RuntimeError("本地专用浏览器 Profile 字幕接口返回格式无法识别")
        if not payload.get("ok"):
            raise RuntimeError(f"本地专用浏览器 Profile 字幕接口失败：HTTP {payload.get('status')}")
        import json

        try:
            data = json.loads(str(payload.get("text") or ""))
        except json.JSONDecodeError as exc:
            raise RuntimeError("本地专用浏览器 Profile 字幕接口返回内容不是有效 JSON") from exc
        return data if isinstance(data, dict) else {}

    return _with_profile_page(read_payload)


def _with_profile_page(callback: Any) -> Any:
    def with_page(context: Any) -> Any:
        page = context.pages[0] if context.pages else context.new_page()
        return callback(page)

    return _with_profile_context(with_page)


def _read_profile_cookies() -> list[dict[str, Any]]:
    def read_cookies(context: Any) -> list[dict[str, Any]]:
        return context.cookies(BILIBILI_COOKIE_URLS)

    return _with_profile_context(read_cookies)


def _cookie_response_payload(cookie_header: str, message: str) -> dict[str, Any]:
    description = describe_bilibili_cookie_header(cookie_header)
    return {
        "cookie_header": cookie_header,
        "fields": description["fields"],
        "missing_fields": description["missing_fields"],
        "message": message,
    }


def _bilibili_cookie_header_from_collection(cookies: list[dict[str, Any]]) -> str:
    bilibili_cookies: list[dict[str, Any]] = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        if _is_bilibili_cookie_domain(domain):
            bilibili_cookies.append(cookie)
    return simplify_bilibili_cookie_collection(bilibili_cookies)


def _extract_from_active_login_session(token: str, session: _LoginWindowSession) -> dict[str, Any]:
    session.result = None
    session.error = None
    session.result_ready.clear()
    session.extract_requested.set()
    if not session.result_ready.wait(timeout=20):
        raise RuntimeError("正在等待 B站登录窗口响应提取请求，请确认登录窗口仍然打开后重试。")
    if session.result:
        _ACTIVE_LOGIN_SESSIONS.pop(token, None)
        _COOKIE_EXTRACT_ERRORS.pop(token, None)
        return session.result
    if session.error:
        raise RuntimeError(session.error)
    raise RuntimeError("B站登录窗口没有返回 Cookie 提取结果，请重试。")


def _store_login_window_cookie_result(session_token: str | None, context: Any, success_message: str) -> bool:
    token = str(session_token or "")
    if not token:
        return False

    try:
        cookie_header = _bilibili_cookie_header_from_collection(context.cookies(BILIBILI_COOKIE_URLS))
    except Exception as exc:
        error = f"B站登录窗口读取 Cookie 失败：{_safe_playwright_error_detail(exc)}"
        _COOKIE_EXTRACT_ERRORS[token] = error
        if token in _ACTIVE_LOGIN_SESSIONS:
            _ACTIVE_LOGIN_SESSIONS[token].error = error
            _ACTIVE_LOGIN_SESSIONS[token].result_ready.set()
        return False

    if not cookie_header:
        error = (
            "当前 B站登录窗口没有读取到 6 项精简 Cookie。"
            "请确认窗口右上角已显示登录账号，保持窗口打开后再次点击提取。"
        )
        _COOKIE_EXTRACT_ERRORS[token] = error
        if token in _ACTIVE_LOGIN_SESSIONS:
            _ACTIVE_LOGIN_SESSIONS[token].error = error
            _ACTIVE_LOGIN_SESSIONS[token].result_ready.set()
        return False

    payload = _cookie_response_payload(
        cookie_header,
        success_message,
    )
    _COOKIE_EXTRACT_RESULTS[token] = payload
    if token in _ACTIVE_LOGIN_SESSIONS:
        _ACTIVE_LOGIN_SESSIONS[token].result = payload
        _ACTIVE_LOGIN_SESSIONS[token].error = None
        _ACTIVE_LOGIN_SESSIONS[token].result_ready.set()
    return True


def _extract_login_window_cookie_for_session(session_token: str | None, context: Any, session: _LoginWindowSession) -> bool:
    token = str(session_token or "")
    _COOKIE_EXTRACT_RESULTS.pop(token, None)
    _COOKIE_EXTRACT_ERRORS.pop(token, None)
    ok = _store_login_window_cookie_result(token, context, "已从当前打开的 B站登录窗口提取并精简 Cookie。")
    session.result = _COOKIE_EXTRACT_RESULTS.pop(token, None)
    session.error = _COOKIE_EXTRACT_ERRORS.pop(token, None)
    session.result_ready.set()
    return ok


def _with_profile_context(callback: Any) -> Any:
    _ensure_playwright_available()
    if not PROFILE_DIR.exists():
        raise RuntimeError("本地专用浏览器 Profile 尚未创建，请先打开 B站登录窗口。")

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    for attempt in range(PROFILE_READ_RETRY_ATTEMPTS):
        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(PROFILE_DIR),
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                try:
                    return callback(context)
                finally:
                    try:
                        context.close()
                    except PlaywrightError:
                        pass
        except PlaywrightError as exc:
            if _is_profile_busy_error(exc) and attempt < PROFILE_READ_RETRY_ATTEMPTS - 1:
                time.sleep(PROFILE_READ_RETRY_DELAY_SECONDS)
                continue
            raise RuntimeError(_playwright_error_message(exc)) from exc

    raise RuntimeError("本地专用浏览器 Profile 读取失败，请稍后重试。")


def _run_login_window(session_token: str | None = None) -> None:
    session = _ACTIVE_LOGIN_SESSIONS.get(str(session_token or ""))

    try:
        _ensure_playwright_available()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(BILIBILI_HOME_URL, wait_until="domcontentloaded", timeout=30000)
                if session and session.stop_requested.is_set():
                    return
                if session:
                    session.startup_ready.set()
                while True:
                    if session and session.extract_requested.is_set():
                        session.extract_requested.clear()
                        if _extract_login_window_cookie_for_session(session_token, context, session):
                            context.close()
                            return
                    if page.is_closed():
                        _store_login_window_cookie_result(session_token, context, "已在 B站登录窗口关闭前提取并精简 Cookie。")
                        return
                    try:
                        page.wait_for_event("close", timeout=250)
                        _store_login_window_cookie_result(session_token, context, "已在 B站登录窗口关闭前提取并精简 Cookie。")
                        return
                    except PlaywrightTimeoutError:
                        continue
            finally:
                try:
                    context.close()
                except Exception as exc:
                    token = str(session_token or "")
                    if token:
                        _COOKIE_EXTRACT_ERRORS[token] = _login_window_error_message(exc)
    except Exception as exc:
        token = str(session_token or "")
        if token:
            _COOKIE_EXTRACT_ERRORS[token] = _login_window_error_message(exc)
            if session:
                session.error = _COOKIE_EXTRACT_ERRORS[token]
                session.result_ready.set()
    finally:
        if session:
            session.startup_ready.set()
        if str(session_token or "") in _COOKIE_EXTRACT_RESULTS or str(session_token or "") in _COOKIE_EXTRACT_ERRORS:
            _ACTIVE_LOGIN_SESSIONS.pop(str(session_token or ""), None)


def _ensure_playwright_available() -> None:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("本地登录窗口需要 Playwright，请先重新运行启动脚本安装后端依赖。") from exc


def _playwright_error_message(exc: Exception) -> str:
    message = str(exc).lower()
    if "executable doesn't exist" in message or "playwright install" in message:
        return "Playwright Chromium 缺失或损坏，请关闭本地服务后重新运行项目启动脚本，程序会自动下载并修复。"
    if _is_profile_busy_error(exc):
        return "本地专用浏览器 Profile 仍被登录窗口或 Chromium 后台进程占用，请等待几秒后重试；如果仍失败，请确认本工具打开的 B站登录窗口都已关闭。"
    return f"本地专用浏览器 Profile 读取失败：{_safe_playwright_error_detail(exc)}"


def _login_window_error_message(exc: Exception) -> str:
    message = _playwright_error_message(exc)
    prefix = "本地专用浏览器 Profile 读取失败："
    if message.startswith(prefix):
        return f"本地专用 B站登录窗口启动失败：{message.removeprefix(prefix)}"
    return message


def _safe_playwright_error_detail(exc: Exception) -> str:
    detail = redact_secrets(str(exc)).replace(str(ROOT_DIR), "[PROJECT]")
    detail = re.sub(r"[A-Za-z]:\\[^\r\n\"']+", "[LOCAL_PATH]", detail)
    detail = re.sub(r"--user-data-dir=(\"[^\"]+\"|'[^']+'|[^\s]+)", "--user-data-dir=[REDACTED]", detail)
    first_line = next((line.strip() for line in detail.splitlines() if line.strip()), "")
    if not first_line:
        return "底层浏览器未返回具体错误，请重启本地服务后再试。"
    return first_line[:240]


def _is_profile_busy_error(exc: Exception) -> bool:
    message = str(exc).lower()
    busy_markers = (
        "user data directory is already in use",
        "profile is in use",
        "profile in use",
        "processsingleton",
        "singletonlock",
        "another process",
        "already running",
    )
    return any(marker in message for marker in busy_markers)


def _is_bilibili_cookie_domain(domain: str) -> bool:
    normalized = domain.lstrip(".").lower()
    return normalized == "bilibili.com" or normalized.endswith(".bilibili.com") or normalized.endswith(".hdslb.com")


def _inspect_cookie_db_whitelist_fields() -> list[str]:
    cookie_db = PROFILE_DIR / "Default" / "Network" / "Cookies"
    if not cookie_db.exists():
        return []

    import sqlite3

    try:
        connection = sqlite3.connect(f"file:{cookie_db.as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                """
                select distinct name
                from cookies
                where host_key like '%bilibili%' or host_key like '%hdslb%'
                """
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return []

    names = {str(row[0] or "") for row in rows}
    return [
        field
        for field in (
            "SESSDATA",
            "bili_jct",
            "DedeUserID",
            "DedeUserID__ckMd5",
            "bili_ticket",
            "bili_ticket_expires",
        )
        if field in names
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-login", action="store_true")
    args = parser.parse_args()
    if args.open_login:
        _run_login_window()


if __name__ == "__main__":
    main()
