"""Validation and temporary storage helpers for locally uploaded media."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import UploadFile


VIDEO_EXTENSIONS = frozenset(
    {
        ".3g2",
        ".3gp",
        ".asf",
        ".avi",
        ".divx",
        ".f4v",
        ".flv",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpe",
        ".mpeg",
        ".mpg",
        ".mts",
        ".ogv",
        ".rm",
        ".rmvb",
        ".ts",
        ".vob",
        ".webm",
        ".wmv",
    }
)
AUDIO_EXTENSIONS = frozenset(
    {
        ".aac",
        ".ac3",
        ".aif",
        ".aiff",
        ".alac",
        ".amr",
        ".ape",
        ".au",
        ".caf",
        ".dts",
        ".eac3",
        ".flac",
        ".m4a",
        ".mka",
        ".mp2",
        ".mp3",
        ".mpa",
        ".oga",
        ".ogg",
        ".opus",
        ".ra",
        ".snd",
        ".wav",
        ".weba",
        ".wma",
        ".wv",
    }
)
AMBIGUOUS_MEDIA_EXTENSIONS = frozenset({".m4s"})
SUPPORTED_LOCAL_MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | AMBIGUOUS_MEDIA_EXTENSIONS
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class LocalMediaValidationError(ValueError):
    """A user-facing local upload validation failure."""


def classify_local_media(filename: str, content_type: str = "") -> str:
    """Return ``video``, ``audio``, or ``media`` for a supported upload."""

    suffix = Path(filename or "").suffix.lower()
    normalized_content_type = str(content_type or "").lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in AMBIGUOUS_MEDIA_EXTENSIONS:
        if normalized_content_type.startswith("audio/"):
            return "audio"
        if normalized_content_type.startswith("video/"):
            return "video"
        # M4S may contain either an audio or video track. FFmpeg validates the
        # actual streams during conversion, so do not guess from the suffix.
        return "media"

    if normalized_content_type.startswith("video/"):
        return "video"
    if normalized_content_type.startswith("audio/"):
        return "audio"

    readable_extensions = "、".join(sorted(extension.lstrip(".") for extension in SUPPORTED_LOCAL_MEDIA_EXTENSIONS))
    raise LocalMediaValidationError(
        f"不支持该文件格式。请选择常见音频或视频文件（支持：{readable_extensions}）。"
    )


def sanitize_local_media_filename(filename: str, media_kind: str) -> str:
    """Remove path and control characters while keeping the media suffix."""

    raw_name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE_FILENAME_RE.sub("", raw_name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    fallback = {
        "video": "本地视频.mp4",
        "audio": "本地音频.mp3",
        "media": "本地音视频.m4s",
    }.get(media_kind, "本地音视频文件")
    return (cleaned[:180].strip(" .") or fallback)


async def store_local_media_upload(upload: UploadFile, output_path: Path) -> int:
    """Stream one browser upload to a task-owned temporary file."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = 0
    try:
        with output_path.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                output.write(chunk)
                bytes_written += len(chunk)
    finally:
        await upload.close()

    if bytes_written <= 0:
        output_path.unlink(missing_ok=True)
        raise LocalMediaValidationError("上传的文件为空，请重新选择有效的音频或视频文件。")
    return bytes_written


__all__ = [
    "AUDIO_EXTENSIONS",
    "LocalMediaValidationError",
    "SUPPORTED_LOCAL_MEDIA_EXTENSIONS",
    "VIDEO_EXTENSIONS",
    "classify_local_media",
    "sanitize_local_media_filename",
    "store_local_media_upload",
]
