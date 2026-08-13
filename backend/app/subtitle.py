"""Subtitle parsing and cleaning utilities for stage 4."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


TIMECODE_PATTERN = re.compile(
    r"^\s*(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}\s+-->\s+"
    r"(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}",
)
SRT_TIMECODE_PATTERN = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s+-->\s+")
PURE_NUMBER_PATTERN = re.compile(r"^\s*\d+\s*$")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
VTT_METADATA_PREFIXES = ("WEBVTT", "NOTE", "STYLE", "REGION")


class SubtitleErrorCode(StrEnum):
    """Stable subtitle parsing error codes."""

    FORMAT_UNRECOGNIZED = "subtitle_format_unrecognized"
    EMPTY_AFTER_CLEANING = "subtitle_empty_after_cleaning"


class SubtitleProcessingError(Exception):
    """Raised when subtitle content cannot be parsed into ordered text."""

    def __init__(self, code: SubtitleErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CleanedSubtitle:
    """Clean subtitle output plus lightweight metadata."""

    text: str
    format: str
    line_count: int


def clean_subtitle(raw_content: str, ext: str = "") -> CleanedSubtitle:
    """Clean WEBVTT/SRT-like or JSON subtitle content into plain ordered text."""

    raw = raw_content.lstrip("\ufeff").strip()
    if not raw:
        raise SubtitleProcessingError(SubtitleErrorCode.FORMAT_UNRECOGNIZED, "字幕内容为空")

    normalized_ext = ext.lower().strip(".")
    if _looks_like_json(normalized_ext, raw):
        lines = _extract_json_lines(raw)
        return _build_cleaned(lines, "json")

    if _looks_like_vtt(normalized_ext, raw):
        lines = _extract_vtt_lines(raw)
        return _build_cleaned(lines, "vtt")

    if normalized_ext in {"srt", "srv3", "ttml"} or "-->" in raw:
        lines = _extract_vtt_lines(raw)
        return _build_cleaned(lines, normalized_ext or "subtitle")

    raise SubtitleProcessingError(
        SubtitleErrorCode.FORMAT_UNRECOGNIZED,
        f"字幕格式无法解析：{ext or 'unknown'}",
    )


def _build_cleaned(lines: list[str], detected_format: str) -> CleanedSubtitle:
    cleaned_lines = [line for line in (_clean_text_line(line) for line in lines) if line]
    if not cleaned_lines:
        raise SubtitleProcessingError(
            SubtitleErrorCode.EMPTY_AFTER_CLEANING,
            "字幕清理后没有可用正文",
        )
    return CleanedSubtitle(
        text="\n".join(cleaned_lines),
        format=detected_format,
        line_count=len(cleaned_lines),
    )


def _looks_like_json(ext: str, raw: str) -> bool:
    return ext in {"json", "json3"} or raw.startswith("{") or raw.startswith("[")


def _looks_like_vtt(ext: str, raw: str) -> bool:
    return ext == "vtt" or raw.startswith("WEBVTT") or "-->" in raw


def _extract_vtt_lines(raw: str) -> list[str]:
    lines: list[str] = []
    skip_metadata_block = False

    for original_line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = original_line.strip()

        if not line:
            skip_metadata_block = False
            continue

        upper_line = line.upper()
        if upper_line.startswith(VTT_METADATA_PREFIXES):
            skip_metadata_block = upper_line.startswith(("NOTE", "STYLE", "REGION"))
            continue

        if skip_metadata_block:
            continue

        if PURE_NUMBER_PATTERN.match(line):
            continue

        if TIMECODE_PATTERN.match(line) or SRT_TIMECODE_PATTERN.match(line):
            continue

        if _is_vtt_cue_setting(line):
            continue

        lines.append(line)

    return lines


def _is_vtt_cue_setting(line: str) -> bool:
    prefixes = ("Kind:", "Language:", "X-TIMESTAMP-MAP=", "align:", "position:", "line:")
    return line.startswith(prefixes)


def _extract_json_lines(raw: str) -> list[str]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SubtitleProcessingError(
            SubtitleErrorCode.FORMAT_UNRECOGNIZED,
            "字幕JSON格式无法解析",
        ) from exc

    rows = _json_rows(payload)
    rows.sort(key=lambda row: row[0])
    return [row[1] for row in rows]


def _json_rows(payload: Any) -> list[tuple[float, str]]:
    rows: list[tuple[float, str]] = []

    if isinstance(payload, dict):
        if isinstance(payload.get("body"), list):
            for index, item in enumerate(payload["body"]):
                _append_row(rows, item, index)
            return rows

        if isinstance(payload.get("events"), list):
            for index, event in enumerate(payload["events"]):
                start = _read_start_time(event, index)
                segments = event.get("segs") if isinstance(event, dict) else None
                if isinstance(segments, list):
                    text = "".join(str(segment.get("utf8") or "") for segment in segments if isinstance(segment, dict))
                    if text.strip():
                        rows.append((start, text))
                else:
                    _append_row(rows, event, index)
            return rows

        for key in ("subtitles", "captions", "data"):
            if isinstance(payload.get(key), list):
                for index, item in enumerate(payload[key]):
                    _append_row(rows, item, index)
                return rows

    if isinstance(payload, list):
        for index, item in enumerate(payload):
            _append_row(rows, item, index)

    return rows


def _append_row(rows: list[tuple[float, str]], item: Any, fallback_index: int) -> None:
    if not isinstance(item, dict):
        text = str(item).strip()
        if text:
            rows.append((float(fallback_index), text))
        return

    text = _read_text(item)
    if not text:
        return
    rows.append((_read_start_time(item, fallback_index), text))


def _read_text(item: dict[str, Any]) -> str:
    for key in ("content", "text", "line", "utf8", "caption"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value

    segments = item.get("segs")
    if isinstance(segments, list):
        return "".join(str(segment.get("utf8") or "") for segment in segments if isinstance(segment, dict))

    return ""


def _read_start_time(item: Any, fallback_index: int) -> float:
    if not isinstance(item, dict):
        return float(fallback_index)

    for key in ("from", "start", "startTime", "start_time", "tStartMs", "timestamp"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value) / 1000 if key == "tStartMs" else float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue

    return float(fallback_index)


def _clean_text_line(line: str) -> str:
    text = HTML_TAG_PATTERN.sub("", line)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").strip()
    return re.sub(r"\s+", " ", text)
