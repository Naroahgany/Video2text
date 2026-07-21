"""Simplified Bilibili Cookie helpers for stage 4.3."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import httpx


SIMPLIFIED_BILIBILI_COOKIE_FIELDS = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "bili_ticket",
    "bili_ticket_expires",
)

_FIELD_SET = set(SIMPLIFIED_BILIBILI_COOKIE_FIELDS)
_BILIBILI_COOKIE_DOMAIN = ".bilibili.com"
_BILIBILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
_BILIBILI_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


def simplify_bilibili_cookie_header(raw_cookie: str) -> str:
    """Return a whitelist-only standard Cookie Header in fixed field order."""

    text = str(raw_cookie or "").strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()

    values: dict[str, str] = {}
    for part in text.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or value == "" or key not in _FIELD_SET:
            continue
        values[key] = value

    return _format_cookie_header(values)


def simplify_bilibili_cookie_collection(cookies: Iterable[Any]) -> str:
    """Simplify cookies read from Playwright, cookiejar, or dict-like records."""

    values: dict[str, str] = {}
    for cookie in cookies:
        key, value = _read_cookie_name_value(cookie)
        if not key or value == "" or key not in _FIELD_SET:
            continue
        values[key] = value

    return _format_cookie_header(values)


def describe_bilibili_cookie_header(cookie_header: str) -> dict[str, list[str]]:
    """Return present and missing whitelist fields for a simplified header."""

    simplified = simplify_bilibili_cookie_header(cookie_header)
    present = []
    values = _parse_cookie_header_values(simplified)
    for field in SIMPLIFIED_BILIBILI_COOKIE_FIELDS:
        if field in values:
            present.append(field)
    missing = [field for field in SIMPLIFIED_BILIBILI_COOKIE_FIELDS if field not in values]
    return {"fields": present, "missing_fields": missing}


def cookie_header_to_httpx_cookies(cookie_header: str) -> httpx.Cookies:
    """Convert a raw or simplified Cookie Header into an httpx cookie jar."""

    simplified = simplify_bilibili_cookie_header(cookie_header)
    cookies = httpx.Cookies()
    for key, value in _parse_cookie_header_values(simplified).items():
        cookies.set(key, value, domain=_BILIBILI_COOKIE_DOMAIN, path="/")
    return cookies


async def validate_bilibili_cookie_header(cookie_header: str, timeout: float = 15.0) -> dict[str, Any]:
    """Lightly validate the simplified Cookie Header through x/web-interface/nav."""

    simplified = simplify_bilibili_cookie_header(cookie_header)
    description = describe_bilibili_cookie_header(simplified)
    if not simplified:
        return {
            "valid": False,
            "is_logged_in": False,
            **description,
            "message": "未找到可用的 6 项精简 B站 Cookie，请先打开本地 B站登录窗口刷新凭据。",
        }

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(
                _BILIBILI_NAV_URL,
                headers=_BILIBILI_HEADERS,
                cookies=cookie_header_to_httpx_cookies(simplified),
            )
    except httpx.TimeoutException:
        return {
            "valid": False,
            "is_logged_in": False,
            **description,
            "message": "精简 Cookie 校验超时，请稍后重试或重新打开本地 B站登录窗口刷新 Cookie。",
        }
    except httpx.RequestError:
        return {
            "valid": False,
            "is_logged_in": False,
            **description,
            "message": "精简 Cookie 校验失败，请检查网络后重试。",
        }

    if response.status_code == 412:
        return {
            "valid": False,
            "is_logged_in": False,
            **description,
            "message": "B站返回 HTTP 412，可能是 Cookie 失效或风控，请重新打开本地 B站登录窗口刷新 Cookie。",
        }
    if response.status_code >= 400:
        return {
            "valid": False,
            "is_logged_in": False,
            **description,
            "message": f"精简 Cookie 校验失败：HTTP {response.status_code}，请重新刷新 Cookie。",
        }

    try:
        payload = response.json()
    except ValueError:
        return {
            "valid": False,
            "is_logged_in": False,
            **description,
            "message": "精简 Cookie 校验响应不是有效 JSON，请稍后重试。",
        }

    code = payload.get("code") if isinstance(payload, dict) else None
    data = payload.get("data") if isinstance(payload, dict) else None
    is_logged_in = isinstance(data, dict) and data.get("isLogin") is True
    if code == 0 and is_logged_in:
        return {
            "valid": True,
            "is_logged_in": True,
            **description,
            "message": "精简 B站 Cookie 校验通过。",
        }

    message = str(payload.get("message") or "精简 Cookie 未登录或已失效") if isinstance(payload, dict) else "精简 Cookie 未登录或已失效"
    return {
        "valid": False,
        "is_logged_in": False,
        **description,
        "message": f"{message}，请重新打开本地 B站登录窗口刷新 Cookie。",
    }


def _format_cookie_header(values: Mapping[str, str]) -> str:
    return "; ".join(
        f"{field}={values[field]}"
        for field in SIMPLIFIED_BILIBILI_COOKIE_FIELDS
        if field in values
    )


def _parse_cookie_header_values(cookie_header: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in str(cookie_header or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value != "":
            values[key] = value
    return values


def _read_cookie_name_value(cookie: Any) -> tuple[str, str]:
    if isinstance(cookie, Mapping):
        name = str(cookie.get("name") or "").strip()
        value = cookie.get("value")
    else:
        name = str(getattr(cookie, "name", "") or "").strip()
        value = getattr(cookie, "value", None)

    if value is None:
        return name, ""
    return name, str(value).strip()
