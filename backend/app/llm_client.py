"""Model client entry points for transcription and refinement stages."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import quote, urlparse

import httpx

from .models import ModelConfig
from .prompts import AUDIO_TRANSCRIPTION_PROMPT, REFINE_FINISH_MARKER, build_refine_transcript_prompt
from .utils import redact_secrets


OPENAI_INPUT_AUDIO_PROVIDER = "openai_compatible_input_audio"
AUDIO_DATA_URL_PREFIX = "data:audio/mpeg;base64,"
AISTUDIO_GEMINI_AUTO_PROVIDER = "aistudio_to_api_gemini_auto"
AISTUDIO_GEMINI_INLINE_PROVIDER = "aistudio_to_api_gemini_inline"
AISTUDIO_GEMINI_FILE_PROVIDER = "aistudio_to_api_gemini_file"
DEPRECATED_GEMINI_PROVIDERS = {
    AISTUDIO_GEMINI_AUTO_PROVIDER,
    AISTUDIO_GEMINI_INLINE_PROVIDER,
}
PLACEHOLDER_TRANSCRIPTION_PROVIDERS = {
    "openai_audio_transcriptions",
}
SUPPORTED_TRANSCRIPTION_PROVIDERS = {
    OPENAI_INPUT_AUDIO_PROVIDER,
    AISTUDIO_GEMINI_FILE_PROVIDER,
}
GEMINI_MIME_TYPES = ("audio/mp3", "audio/mpeg")
GEMINI_UPLOAD_SIZE_TOLERANCE_BYTES = 1024
GEMINI_GENERATION_REUSE_RETRY_CODES = {
    "transcription_api_timeout",
    "transcription_network_error",
    "transcription_api_error",
    "transcription_rate_limited",
}
THINKING_CONTENT_PATTERN = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
THINKING_TAG_PATTERN = re.compile(r"</?think\b[^>]*>", re.IGNORECASE)
KINGFALL_MARKER_PATTERN = re.compile(r"[［\[]\s*KINGFALL MODE ENABLE\s*[］\]]", re.IGNORECASE)
REFINE_XML_TAG_PATTERN = re.compile(
    r"</?(YourTask|OriginalSubtitleContent|AIAudioTranscriptionResult)\b[^>]*>",
    re.IGNORECASE,
)
REFINE_FINISH_MARKER_PATTERN = re.compile(r"\[finish\]\s*$", re.IGNORECASE)
REFINE_MAX_CONTINUATIONS = 8
REFINE_CALL_MAX_RETRIES = 3
REFINE_RETRY_DELAY_SECONDS = 3
TranscriptionLogCallback = Callable[[str, str], None]
RefineLogCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class GeminiUploadedFile:
    """A Gemini Files API upload that has passed local strong validation."""

    uri: str
    mime_type: str
    state: str
    size_bytes: int


class TranscriptionProcessingError(Exception):
    """Stage 6 model transcription error with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RefineProcessingError(Exception):
    """Stage 7 model refinement error with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_audio_transcription_payload(
    config: ModelConfig,
    audio_data_url: str,
    prompt: str = AUDIO_TRANSCRIPTION_PROMPT,
) -> dict[str, Any]:
    """Build a SiliconFlow-compatible Chat Completions audio payload."""

    return {
        "model": config.model.strip(),
        "temperature": config.temperature,
        "stream": True,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt,
                    },
                    {
                        "type": "audio_url",
                        "audio_url": {
                            "url": audio_data_url,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            },
        ],
    }


def encode_mp3_as_data_url(audio_path: Path) -> str:
    """Read one local MP3 and return an in-memory Base64 Data URL."""

    try:
        audio_base64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    except OSError as exc:
        raise TranscriptionProcessingError(
            "transcription_audio_read_failed",
            f"音频片段读取失败：{redact_secrets(exc)}",
        ) from exc
    return f"{AUDIO_DATA_URL_PREFIX}{audio_base64}"


def _chat_completions_endpoint(base_url: str) -> str:
    """Append the Chat Completions path to a user-supplied API base URL."""

    return f"{base_url.strip().rstrip('/')}/chat/completions"


def build_refine_chat_completion_payload(
    config: ModelConfig,
    prompt: str,
) -> dict[str, Any]:
    """Build an OpenAI-compatible Chat Completions payload for stage 7."""

    return {
        "model": config.model.strip(),
        "temperature": config.temperature,
        "stream": config.stream is not False,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            },
        ],
    }


def build_gemini_file_payload(
    file_uri: str,
    prompt: str = AUDIO_TRANSCRIPTION_PROMPT,
    temperature: float = 0,
    mime_type: str = "audio/mp3",
    field_style: Literal["camel", "snake"] = "camel",
    generation_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an AIStudioToAPI Gemini native fileData payload."""

    if field_style == "snake":
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {
                            "file_data": {
                                "mime_type": mime_type,
                                "file_uri": file_uri,
                            },
                        },
                        {"text": prompt},
                    ],
                },
            ],
            "generationConfig": {
                "temperature": temperature,
            },
        }
        if generation_config:
            payload["generationConfig"].update(generation_config)
        _inject_gemini_high_thinking(payload)
        return payload

    payload = {
        "contents": [
            {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {
                            "fileData": {
                                "mimeType": mime_type,
                                "fileUri": file_uri,
                            },
                        },
                        {"text": prompt},
                ],
            },
        ],
        "generationConfig": {
            "temperature": temperature,
        },
    }
    if generation_config:
        payload["generationConfig"].update(generation_config)
    _inject_gemini_high_thinking(payload)
    return payload


def strip_thinking_content(text: str) -> str:
    """Remove model thinking blocks before text is stored or returned to the UI."""

    value = str(text or "")
    closing_matches = list(re.finditer(r"</think\s*>", value, re.IGNORECASE))
    if closing_matches:
        value = value[closing_matches[-1].end() :]
    else:
        value = THINKING_CONTENT_PATTERN.sub("", value)
    value = THINKING_CONTENT_PATTERN.sub("", value)
    value = THINKING_TAG_PATTERN.sub("", value)
    value = KINGFALL_MARKER_PATTERN.sub("", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def clean_refined_markdown_output(text: str) -> str:
    """Remove model-only protocol text before the final Markdown reaches the UI."""

    value = strip_thinking_content(text)
    value = REFINE_XML_TAG_PATTERN.sub("", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


async def transcribe_mp3(
    audio_path: Path,
    config: ModelConfig,
    prompt: str = AUDIO_TRANSCRIPTION_PROMPT,
    log: TranscriptionLogCallback | None = None,
) -> str:
    """Transcribe one MP3 through the selected stage 6 provider."""

    provider = _normalize_provider(config.provider)
    if provider == OPENAI_INPUT_AUDIO_PROVIDER:
        return await transcribe_mp3_with_chat_completions(audio_path, config, prompt, log=log)
    if provider == AISTUDIO_GEMINI_FILE_PROVIDER:
        return await transcribe_mp3_with_aistudio_gemini(audio_path, config, prompt, log=log)
    if provider in PLACEHOLDER_TRANSCRIPTION_PROVIDERS:
        raise TranscriptionProcessingError(
            "transcription_provider_not_implemented",
            "当前音频识别方式还只是预留入口，尚未接入实际转写链路。请先选择AIStudioToAPI Gemini原生或OpenAI-compatible多模态音频。",
        )
    raise TranscriptionProcessingError(
        "transcription_provider_invalid",
        f"未知音频识别方式：{provider}",
    )


async def refine_markdown_with_chat_completions(
    clean_subtitle: str | None,
    ai_transcription_result: str,
    config: ModelConfig,
    log: RefineLogCallback | None = None,
) -> str:
    """Generate final Markdown with the second OpenAI-compatible model."""

    if not (ai_transcription_result or "").strip():
        raise RefineProcessingError("refine_transcript_missing", "阶段7调用前未拿到完整AI音频转文字稿。")

    accumulated = ""
    continuation_anchor: str | None = None
    for continuation_index in range(REFINE_MAX_CONTINUATIONS + 1):
        label = "首次调用" if continuation_index == 0 else f"第{continuation_index}次续写"
        prompt = build_refine_transcript_prompt(
            clean_subtitle,
            ai_transcription_result,
            continuation_anchor=continuation_anchor,
        )
        chunk, finished = await _request_refine_chunk_with_retries(config, prompt, label, log)
        if accumulated:
            accumulated = _merge_refine_continuation(accumulated, chunk, continuation_anchor or "")
        else:
            accumulated = chunk

        if finished:
            final_markdown = clean_refined_markdown_output(accumulated)
            if not final_markdown:
                raise RefineProcessingError("refine_empty_response", "文稿优化模型返回空内容或清理后没有可展示正文。")
            return final_markdown

        continuation_anchor = _extract_last_paragraph(accumulated)
        if not continuation_anchor:
            raise RefineProcessingError("refine_finish_missing", "文稿优化模型未返回 [finish]，且无法识别续写锚点。")
        _emit_refine_log(
            log,
            "warning",
            f"文稿优化模型{label}未检测到{REFINE_FINISH_MARKER}，将以最后一段作为锚点继续请求续写。",
        )

    raise RefineProcessingError(
        "refine_finish_missing",
        f"文稿优化模型连续续写{REFINE_MAX_CONTINUATIONS}次后仍未返回{REFINE_FINISH_MARKER}，可能仍然被截断。",
    )


async def _request_refine_chunk_with_retries(
    config: ModelConfig,
    prompt: str,
    label: str,
    log: RefineLogCallback | None = None,
) -> tuple[str, bool]:
    max_attempts = REFINE_CALL_MAX_RETRIES + 1
    for attempt in range(1, max_attempts + 1):
        try:
            raw_text = await _request_refine_chat_completion(config, prompt)
            return _normalize_refine_model_output(raw_text)
        except RefineProcessingError as exc:
            safe_message = redact_secrets(exc.message, [config.api_key])
            if attempt <= REFINE_CALL_MAX_RETRIES:
                _emit_refine_log(
                    log,
                    "warning",
                    (
                        f"文稿优化模型{label}失败：{safe_message}；"
                        f"{REFINE_RETRY_DELAY_SECONDS}秒后自动重试（{attempt}/{REFINE_CALL_MAX_RETRIES}）"
                    ),
                )
                await asyncio.sleep(REFINE_RETRY_DELAY_SECONDS)
                continue
            raise RefineProcessingError(
                exc.code,
                f"文稿优化模型{label}已自动重试{REFINE_CALL_MAX_RETRIES}次仍失败：{safe_message}",
            ) from exc

    raise RefineProcessingError("refine_unknown_error", f"文稿优化模型{label}失败。")


async def _request_refine_chat_completion(config: ModelConfig, prompt: str) -> str:
    _validate_refine_model_config(config)
    payload = build_refine_chat_completion_payload(config, prompt)
    endpoint = _chat_completions_endpoint(config.base_url)
    headers = {
        "Accept": "text/event-stream" if payload["stream"] else "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.api_key}",
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, read=600.0),
            follow_redirects=True,
            trust_env=_should_trust_env_for_url(config.base_url),
        ) as client:
            if payload["stream"]:
                text = await _post_refine_streaming_chat_completion(client, endpoint, headers, payload)
            else:
                text = await _post_refine_chat_completion(client, endpoint, headers, payload)
    except RefineProcessingError:
        raise
    except httpx.TimeoutException as exc:
        raise RefineProcessingError(
            "refine_api_timeout",
            f"文稿优化模型API请求超时（endpoint: {redact_secrets(endpoint, [config.api_key])}）。",
        ) from exc
    except httpx.RequestError as exc:
        raise RefineProcessingError(
            "refine_network_error",
            f"文稿优化模型API网络请求失败：{redact_secrets(exc, [config.api_key])}",
        ) from exc
    except ValueError as exc:
        raise RefineProcessingError("refine_invalid_response", "文稿优化模型返回内容不是有效JSON。") from exc

    return text


def describe_transcription_route(audio_path: Path, config: ModelConfig) -> str:
    """Return a sanitized one-line description of the selected transcription route."""

    provider = _normalize_provider(config.provider)
    if provider == OPENAI_INPUT_AUDIO_PROVIDER:
        return "OpenAI-compatible多模态音频（audio_url + Base64 Data URL）"
    if provider == AISTUDIO_GEMINI_FILE_PROVIDER:
        return "AIStudioToAPI Gemini原生Files API"
    if provider in PLACEHOLDER_TRANSCRIPTION_PROVIDERS:
        return f"{provider}（预留，未接入）"
    return provider


async def transcribe_mp3_with_chat_completions(
    audio_path: Path,
    config: ModelConfig,
    prompt: str = AUDIO_TRANSCRIPTION_PROMPT,
    log: TranscriptionLogCallback | None = None,
) -> str:
    """Transcribe one MP3 with an OpenAI-compatible /chat/completions endpoint."""

    _validate_model_config(config)
    file_size = _validate_mp3_audio_path(audio_path)
    _emit_transcription_log(
        log,
        "info",
        f"OpenAI-compatible多模态音频本地切片校验通过：{audio_path.name}，大小{_format_size_mb(file_size)}",
    )
    _emit_transcription_log(log, "info", f"{audio_path.name}开始在本机转换为Base64 Data URL")
    audio_data_url = await asyncio.to_thread(encode_mp3_as_data_url, audio_path)
    _emit_transcription_log(
        log,
        "info",
        f"{audio_path.name} Base64 Data URL转换完成，立即调用音频转文字模型API",
    )

    payload = build_audio_transcription_payload(config, audio_data_url, prompt)
    endpoint = _chat_completions_endpoint(config.base_url)
    headers = {
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.api_key}",
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, read=300.0),
            follow_redirects=True,
            trust_env=_should_trust_env_for_url(config.base_url),
        ) as client:
            text = await _post_streaming_chat_completion(client, endpoint, headers, payload)
    except TranscriptionProcessingError:
        raise
    except httpx.TimeoutException as exc:
        raise TranscriptionProcessingError(
            "transcription_api_timeout",
            f"音频转文字模型API请求超时（endpoint: {redact_secrets(endpoint, [config.api_key])}）。",
        ) from exc
    except httpx.RequestError as exc:
        raise TranscriptionProcessingError(
            "transcription_network_error",
            f"音频转文字模型API网络请求失败：{redact_secrets(exc, [config.api_key])}",
        ) from exc
    except ValueError as exc:
        raise TranscriptionProcessingError("transcription_invalid_response", "音频转文字模型返回内容不是有效JSON。") from exc

    return _ensure_transcription_text(text)


async def transcribe_mp3_with_aistudio_gemini(
    audio_path: Path,
    config: ModelConfig,
    prompt: str = AUDIO_TRANSCRIPTION_PROMPT,
    log: TranscriptionLogCallback | None = None,
) -> str:
    """Transcribe one MP3 through AIStudioToAPI's Gemini native endpoints."""

    _validate_model_config(config)
    file_size = _validate_mp3_audio_path(audio_path)
    return await _transcribe_gemini_file(audio_path, config, prompt, file_size=file_size, log=log)


async def _transcribe_gemini_file(
    audio_path: Path,
    config: ModelConfig,
    prompt: str,
    file_size: int,
    log: TranscriptionLogCallback | None = None,
) -> str:
    endpoint = _gemini_stream_generate_content_endpoint(config)
    headers = _gemini_json_headers(config)
    errors: list[str] = []
    _emit_transcription_log(
        log,
        "info",
        (
            "Gemini Files API上传前切片校验通过："
            f"{audio_path.name}，本地切片大小{_format_size_mb(file_size)}。"
            "阶段6当前执行轻量文件校验；深度ffprobe解码校验仍由阶段5时长读取边界负责。"
        ),
    )

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, read=600.0),
            follow_redirects=True,
            trust_env=_should_trust_env_for_url(config.base_url),
        ) as client:
            for mime_type in GEMINI_MIME_TYPES:
                try:
                    uploaded_file = await _upload_gemini_file(
                        client,
                        audio_path,
                        config,
                        mime_type,
                        file_size=file_size,
                        log=log,
                    )
                except TranscriptionProcessingError as exc:
                    errors.append(f"{mime_type}/upload: {exc.message}")
                    _emit_transcription_log(
                        log,
                        "warning",
                        f"fallback尝试原因：{mime_type}上传或强校验失败：{_safe_runtime_error_detail(exc.message, config.api_key)}",
                    )
                    continue

                final_mime_type = uploaded_file.mime_type or mime_type
                for field_style in ("camel", "snake"):
                    field_label = "fileData" if field_style == "camel" else "file_data"
                    payload = build_gemini_file_payload(
                        uploaded_file.uri,
                        prompt,
                        config.temperature,
                        mime_type=final_mime_type,
                        field_style=field_style,  # type: ignore[arg-type]
                    )
                    for generate_attempt in range(1, 3):
                        try:
                            _emit_transcription_log(
                                log,
                                "info",
                                (
                                    "Gemini streamGenerateContent开始："
                                    f"使用{field_label}，mimeType={final_mime_type}"
                                    + ("，复用已强校验上传文件" if generate_attempt > 1 else "")
                                ),
                            )
                            raw_text = await _post_streaming_gemini_generate_content(
                                client,
                                endpoint,
                                headers,
                                payload,
                                config.api_key,
                            )
                            text = _ensure_transcription_text(raw_text)
                        except TranscriptionProcessingError as exc:
                            errors.append(f"{mime_type}/{field_label}: {exc.message}")
                            safe_message = _safe_runtime_error_detail(exc.message, config.api_key)
                            if exc.code == "transcription_audio_unsupported":
                                _emit_transcription_log(
                                    log,
                                    "warning",
                                    f"fallback尝试原因：{field_label} + {final_mime_type}返回音频未被实际接收：{safe_message}",
                                )
                                break
                            if exc.code == "transcription_gemini_request_invalid":
                                _emit_transcription_log(
                                    log,
                                    "warning",
                                    f"fallback尝试原因：{field_label} + {final_mime_type}请求格式未被接受：{safe_message}",
                                )
                                break
                            if exc.code in GEMINI_GENERATION_REUSE_RETRY_CODES and generate_attempt == 1:
                                _emit_transcription_log(
                                    log,
                                    "warning",
                                    (
                                        "Gemini streamGenerateContent生成阶段失败，"
                                        f"将复用已通过强校验的file.uri重新发起生成：{safe_message}"
                                    ),
                                )
                                continue
                            _emit_transcription_log(
                                log,
                                "error",
                                f"Gemini当前切片失败分类：{exc.code}，原因：{safe_message}",
                            )
                            raise
                        _emit_transcription_log(
                            log,
                            "info",
                            f"Gemini streamGenerateContent完成：返回正文长度{len(text)}字",
                        )
                        _emit_transcription_log(
                            log,
                            "info",
                            f"Gemini当前切片最终成功：mimeType={final_mime_type}，引用字段={field_label}",
                        )
                        return text
    except TranscriptionProcessingError:
        raise
    except httpx.TimeoutException as exc:
        raise TranscriptionProcessingError(
            "transcription_api_timeout",
            f"Gemini原生Files API流式请求超时（endpoint: {redact_secrets(endpoint, [config.api_key])}）。",
        ) from exc
    except httpx.RequestError as exc:
        raise TranscriptionProcessingError(
            "transcription_network_error",
            f"Gemini原生Files API流式网络请求失败（endpoint: {redact_secrets(endpoint, [config.api_key])}）：{redact_secrets(exc, [config.api_key])}",
        ) from exc

    final_summary = "; ".join(errors[-4:]) or "未提供错误详情"
    _emit_transcription_log(log, "error", f"Gemini当前切片所有MIME / fileData组合均失败：{final_summary}")
    raise TranscriptionProcessingError(
        "transcription_audio_unsupported",
        f"Gemini原生Files API所有音频引用组合均失败，当前切片本轮尝试失败：{final_summary}",
    )


async def _upload_gemini_file(
    client: httpx.AsyncClient,
    audio_path: Path,
    config: ModelConfig,
    mime_type: str,
    file_size: int,
    log: TranscriptionLogCallback | None = None,
) -> GeminiUploadedFile:
    file_size_label = _format_size_mb(file_size)
    base_url = _gemini_root_base_url(config.base_url)
    start_endpoint = f"{base_url}/upload/v1beta/files"
    start_headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(file_size),
        "X-Goog-Upload-Header-Content-Type": mime_type,
    }
    start_payload = {
        "file": {
            "display_name": audio_path.name,
            "mimeType": mime_type,
        },
    }
    try:
        _emit_transcription_log(
            log,
            "info",
            f"Gemini Files API上传初始化开始：mime={mime_type}，本地切片大小={file_size_label}",
        )
        start_response = await client.post(start_endpoint, headers=start_headers, json=start_payload)
    except httpx.TimeoutException as exc:
        raise TranscriptionProcessingError(
            "transcription_api_timeout",
            f"Gemini原生Files API上传初始化超时（endpoint: {_safe_endpoint_for_log(start_endpoint, config.api_key)}，mime: {mime_type}，file: {file_size_label}）。",
        ) from exc
    except httpx.RequestError as exc:
        raise TranscriptionProcessingError(
            "transcription_network_error",
            f"Gemini原生Files API上传初始化网络失败（endpoint: {_safe_endpoint_for_log(start_endpoint, config.api_key)}，mime: {mime_type}，file: {file_size_label}）：{redact_secrets(exc, [config.api_key])}",
        ) from exc
    if start_response.status_code >= 400:
        _raise_for_gemini_status(
            start_response,
            f"Gemini原生Files API上传初始化（endpoint: {_safe_endpoint_for_log(start_endpoint, config.api_key)}，mime: {mime_type}，file: {file_size_label}）",
        )
    _emit_transcription_log(log, "info", "Gemini Files API上传初始化成功：已取得脱敏上传会话")

    upload_url = start_response.headers.get("x-goog-upload-url") or start_response.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        raise TranscriptionProcessingError(
            "transcription_gemini_upload_failed",
            f"Gemini原生Files API上传初始化未返回x-goog-upload-url（endpoint: {_safe_endpoint_for_log(start_endpoint, config.api_key)}，mime: {mime_type}，file: {file_size_label}）。",
        )
    if not upload_url.startswith(("http://", "https://")):
        upload_url = f"{base_url}/{upload_url.lstrip('/')}"

    upload_headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.api_key}",
        "Content-Length": str(file_size),
        "Content-Type": mime_type,
        "X-Goog-Upload-Offset": "0",
        "X-Goog-Upload-Command": "upload, finalize",
    }
    try:
        _emit_transcription_log(
            log,
            "info",
            f"Gemini Files API二进制上传开始：mime={mime_type}，本地切片大小={file_size_label}",
        )
        upload_response = await client.post(upload_url, headers=upload_headers, content=_iter_file_bytes(audio_path))
    except httpx.TimeoutException as exc:
        raise TranscriptionProcessingError(
            "transcription_api_timeout",
            f"Gemini原生Files API音频上传超时（upload_endpoint: 已脱敏上传会话，mime: {mime_type}，file: {file_size_label}）。",
        ) from exc
    except httpx.RequestError as exc:
        raise TranscriptionProcessingError(
            "transcription_network_error",
            f"Gemini原生Files API音频上传网络失败（upload_endpoint: 已脱敏上传会话，mime: {mime_type}，file: {file_size_label}）：{_safe_runtime_error_detail(exc, config.api_key)}",
        ) from exc
    except OSError as exc:
        raise TranscriptionProcessingError(
            "transcription_audio_read_failed",
            f"音频片段读取失败：{_safe_runtime_error_detail(exc, config.api_key)}",
        ) from exc
    if upload_response.status_code >= 400:
        _raise_for_gemini_status(
            upload_response,
            f"Gemini原生Files API音频上传（upload_endpoint: 已脱敏上传会话，mime: {mime_type}，file: {file_size_label}）",
        )

    try:
        payload = upload_response.json()
    except ValueError as exc:
        raise TranscriptionProcessingError(
            "transcription_invalid_response",
            "Gemini原生Files API音频上传响应不是有效JSON（upload_endpoint: 已脱敏上传会话）。",
        ) from exc
    uploaded_file = _validate_gemini_upload_response(payload, file_size, mime_type, api_key=config.api_key)
    _emit_transcription_log(
        log,
        "info",
        (
            "Gemini Files API上传完成："
            f"state={uploaded_file.state}，服务端sizeBytes={uploaded_file.size_bytes}，mimeType={uploaded_file.mime_type}"
        ),
    )
    return uploaded_file


def _normalize_provider(provider: str | None) -> str:
    value = (provider or OPENAI_INPUT_AUDIO_PROVIDER).strip()
    if value in DEPRECATED_GEMINI_PROVIDERS:
        return AISTUDIO_GEMINI_FILE_PROVIDER
    return value or OPENAI_INPUT_AUDIO_PROVIDER


def _inject_gemini_high_thinking(payload: dict[str, Any]) -> None:
    generation_config = payload.get("generationConfig")
    if not isinstance(generation_config, dict):
        generation_config = {}
        payload["generationConfig"] = generation_config
    thinking_config = generation_config.get("thinkingConfig")
    if not isinstance(thinking_config, dict):
        thinking_config = {}
    generation_config["thinkingConfig"] = {
        **thinking_config,
        "thinkingLevel": "high",
    }


def _validate_model_config(config: ModelConfig) -> None:
    base_url = config.base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise TranscriptionProcessingError("transcription_base_url_invalid", "音频转文字模型API Base URL必须以http:// 或https:// 开头。")
    if not config.api_key:
        raise TranscriptionProcessingError("transcription_api_key_missing", "音频转文字模型API Key为空，请先在设置中保存。")
    if not config.model.strip():
        raise TranscriptionProcessingError("transcription_model_missing", "音频转文字模型Model为空，请先在设置中保存。")
    provider = _normalize_provider(config.provider)
    if provider not in SUPPORTED_TRANSCRIPTION_PROVIDERS and provider not in PLACEHOLDER_TRANSCRIPTION_PROVIDERS:
        raise TranscriptionProcessingError("transcription_provider_invalid", f"未知音频识别方式：{provider}")


def _validate_refine_model_config(config: ModelConfig) -> None:
    base_url = config.base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise RefineProcessingError("refine_base_url_invalid", "文稿优化模型API Base URL必须以http:// 或https:// 开头。")
    if not config.api_key:
        raise RefineProcessingError("refine_api_key_missing", "文稿优化模型API Key为空，请先在设置中保存。")
    if not config.model.strip():
        raise RefineProcessingError("refine_model_missing", "文稿优化模型Model为空，请先在设置中保存。")


def _validate_mp3_audio_path(audio_path: Path) -> int:
    if audio_path.suffix.lower() != ".mp3":
        raise TranscriptionProcessingError(
            "transcription_audio_not_mp3",
            "阶段6只接受 .mp3音频切片。切片文件可能为空、损坏、临时目录被清理、任务状态丢失，或阶段5输出被异常改名。",
        )
    if not audio_path.exists():
        raise TranscriptionProcessingError(
            "transcription_audio_file_missing",
            "阶段6上传前校验失败：音频切片文件不存在。切片文件可能为空、损坏、临时目录被清理、任务状态丢失，或本地服务重启后丢失了阶段5临时文件。",
        )
    if not audio_path.is_file():
        raise TranscriptionProcessingError(
            "transcription_audio_file_invalid",
            "阶段6上传前校验失败：音频切片路径不是普通文件。切片文件可能为空、损坏、临时目录被清理、任务状态丢失。",
        )
    try:
        file_size = audio_path.stat().st_size
    except OSError as exc:
        raise TranscriptionProcessingError(
            "transcription_audio_file_invalid",
            f"阶段6上传前校验失败：无法读取音频切片文件大小。切片文件可能为空、损坏、临时目录被清理、任务状态丢失。详情：{_safe_runtime_error_detail(exc)}",
        ) from exc
    if file_size <= 0:
        raise TranscriptionProcessingError(
            "transcription_audio_file_empty",
            "阶段6上传前校验失败：音频切片文件大小为0，已拒绝上传。切片文件可能为空、损坏、临时目录被清理、任务状态丢失，或阶段5 FFmpeg切片输出异常。",
        )
    # 边界说明：阶段6当前只做轻量文件系统校验，避免在上传前把明显坏片段送入Files API。
    # 更重的ffprobe / 解码级校验仍保留在阶段5的MP3时长读取和切片流程边界内，后续如需
    # 在阶段6每片段重复ffprobe，可在这里扩展，但不应改变串行切片转写主流程。
    return file_size


def _validate_gemini_upload_response(
    payload: object,
    local_size_bytes: int,
    requested_mime_type: str,
    api_key: str = "",
) -> GeminiUploadedFile:
    file_payload = payload.get("file") if isinstance(payload, dict) else None
    if not isinstance(file_payload, dict):
        raise TranscriptionProcessingError(
            "transcription_gemini_upload_failed",
            "Gemini原生Files API上传响应缺少file信息，已中止生成阶段并进入阶段6重试机制。",
        )

    file_uri = file_payload.get("uri")
    if not isinstance(file_uri, str) or not file_uri.strip():
        raise TranscriptionProcessingError(
            "transcription_gemini_upload_failed",
            "Gemini原生Files API上传响应缺少file.uri，已中止生成阶段并进入阶段6重试机制。",
        )

    state = str(file_payload.get("state") or "").strip()
    if state != "ACTIVE":
        raise TranscriptionProcessingError(
            "transcription_gemini_upload_failed",
            f"Gemini原生Files API上传响应file.state={state or '空'}，不是ACTIVE，已中止生成阶段并进入阶段6重试机制。",
        )

    size_bytes = _parse_size_bytes(file_payload.get("sizeBytes") or file_payload.get("size_bytes"))
    if size_bytes is None:
        raise TranscriptionProcessingError(
            "transcription_gemini_upload_failed",
            "Gemini原生Files API上传响应缺少有效file.sizeBytes，无法确认服务端文件大小，已中止生成阶段并进入阶段6重试机制。",
        )
    if abs(size_bytes - local_size_bytes) > GEMINI_UPLOAD_SIZE_TOLERANCE_BYTES:
        raise TranscriptionProcessingError(
            "transcription_gemini_upload_failed",
            (
                "Gemini原生Files API上传响应file.sizeBytes与本地切片大小不一致，"
                f"local={local_size_bytes}，remote={size_bytes}，容差={GEMINI_UPLOAD_SIZE_TOLERANCE_BYTES} bytes。"
                "已中止生成阶段并进入阶段6重试机制。"
            ),
        )

    returned_mime_type = str(file_payload.get("mimeType") or file_payload.get("mime_type") or "").strip()
    if returned_mime_type not in GEMINI_MIME_TYPES:
        raise TranscriptionProcessingError(
            "transcription_gemini_upload_failed",
            (
                "Gemini原生Files API上传响应mimeType不合理，"
                f"requested={requested_mime_type}，returned={returned_mime_type or '空'}。"
                "已中止生成阶段并进入阶段6重试机制。"
            ),
        )

    return GeminiUploadedFile(
        uri=file_uri,
        mime_type=returned_mime_type,
        state=state,
        size_bytes=size_bytes,
    )


def _parse_size_bytes(value: object) -> int | None:
    try:
        size = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


async def _iter_file_bytes(path: Path, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    with path.open("rb") as file_obj:
        while True:
            chunk = await asyncio.to_thread(file_obj.read, chunk_size)
            if not chunk:
                break
            yield chunk


def _gemini_root_base_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    for suffix in ("/openai", "/v1beta", "/v1"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value.rstrip("/")


def _gemini_model_path(model: str) -> str:
    value = model.strip()
    if value.startswith("models/"):
        value = value[len("models/") :]
    return quote(value, safe="")


def _gemini_stream_generate_content_endpoint(config: ModelConfig) -> str:
    return f"{_gemini_root_base_url(config.base_url)}/v1beta/models/{_gemini_model_path(config.model)}:streamGenerateContent?alt=sse"


def _gemini_json_headers(config: ModelConfig) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.api_key}",
    }


def _format_size_mb(size_bytes: int) -> str:
    return f"{size_bytes / 1024 / 1024:.2f} MB"


def _safe_endpoint_for_log(url: str, api_key: str = "") -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme and parsed.netloc:
        safe_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    else:
        safe_url = url
    return redact_secrets(safe_url, [api_key])


def _safe_runtime_error_detail(error: object, api_key: str = "") -> str:
    value = redact_secrets(str(error), [api_key])
    value = re.sub(r"https?://\S+", "[url已脱敏]", value)
    value = re.sub(r"files/[A-Za-z0-9_.:/?=&%-]+", "files/[已脱敏]", value)
    return value[:500] or error.__class__.__name__


def _emit_transcription_log(log: TranscriptionLogCallback | None, level: str, message: str) -> None:
    if log:
        log(level, message)


def _should_trust_env_for_url(url: str) -> bool:
    """Do not route local model servers through system HTTP proxy settings."""

    hostname = (urlparse(url.strip()).hostname or "").lower()
    if hostname in {"localhost", "0.0.0.0"}:
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return not (address.is_loopback or address.is_private or address.is_link_local)


async def _post_streaming_chat_completion(
    client: httpx.AsyncClient,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> str:
    chunks: list[str] = []
    async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
        if response.status_code >= 400:
            await response.aread()
        _raise_for_model_status(response)
        async for line in response.aiter_lines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            chunks.append(_extract_stream_delta_text(event))
    return "".join(chunks)


async def _post_refine_streaming_chat_completion(
    client: httpx.AsyncClient,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> str:
    chunks: list[str] = []
    async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
        if response.status_code >= 400:
            await response.aread()
        _raise_for_refine_model_status(response)
        async for line in response.aiter_lines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            chunks.append(_extract_stream_delta_text(event))
    return "".join(chunks)


async def _post_refine_chat_completion(
    client: httpx.AsyncClient,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> str:
    response = await client.post(endpoint, headers=headers, json=payload)
    _raise_for_refine_model_status(response)
    payload = response.json()
    if not isinstance(payload, dict):
        raise RefineProcessingError("refine_invalid_response", "文稿优化模型响应不是JSON对象。")
    return _extract_refine_chat_completion_text(payload)


async def _post_streaming_gemini_generate_content(
    client: httpx.AsyncClient,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    api_key: str,
) -> str:
    chunks: list[str] = []
    label = f"Gemini原生Files API streamGenerateContent（endpoint: {_safe_endpoint_for_log(endpoint, api_key)}）"
    async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
        if response.status_code >= 400:
            await response.aread()
        _raise_for_gemini_status(response, label)
        async for line in response.aiter_lines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            chunks.extend(_extract_gemini_text_parts(event))
    return "".join(chunks)


def _raise_for_model_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return

    detail = _model_error_detail(response)
    if response.status_code in {401, 403}:
        raise TranscriptionProcessingError("transcription_auth_failed", f"音频转文字模型鉴权失败或无权限：{detail}")
    if response.status_code == 429:
        raise TranscriptionProcessingError("transcription_rate_limited", f"音频转文字模型API限流：{detail}")
    if response.status_code == 413:
        raise TranscriptionProcessingError("transcription_request_too_large", f"音频转文字模型请求体过大：{detail}")
    if response.status_code == 400:
        raise TranscriptionProcessingError(
            "transcription_audio_unsupported",
            f"音频转文字模型请求格式错误或模型不支持音频多模态：{detail}",
        )
    raise TranscriptionProcessingError(
        "transcription_api_error",
        f"音频转文字模型API返回HTTP {response.status_code}：{detail}",
    )


def _raise_for_refine_model_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return

    detail = _model_error_detail(response)
    if response.status_code in {401, 403}:
        raise RefineProcessingError("refine_auth_failed", f"文稿优化模型鉴权失败或无权限：{detail}")
    if response.status_code == 429:
        raise RefineProcessingError("refine_rate_limited", f"文稿优化模型API限流：{detail}")
    if response.status_code == 413:
        raise RefineProcessingError("refine_request_too_large", f"文稿优化模型请求体过大：{detail}")
    if response.status_code in {400, 422}:
        raise RefineProcessingError("refine_request_invalid", f"文稿优化模型请求格式错误：{detail}")
    raise RefineProcessingError(
        "refine_api_error",
        f"文稿优化模型API返回HTTP {response.status_code}：{detail}",
    )


def _raise_for_gemini_status(response: httpx.Response, label: str) -> None:
    if response.status_code < 400:
        return

    detail = _model_error_detail(response)
    if response.status_code in {401, 403}:
        raise TranscriptionProcessingError("transcription_auth_failed", f"{label}鉴权失败或无权限：{detail}")
    if response.status_code == 429:
        raise TranscriptionProcessingError("transcription_rate_limited", f"{label} API限流：{detail}")
    if response.status_code == 413:
        raise TranscriptionProcessingError("transcription_request_too_large", f"{label}请求体过大：{detail}")
    if response.status_code in {400, 422}:
        raise TranscriptionProcessingError("transcription_gemini_request_invalid", f"{label}请求格式错误：{detail}")
    raise TranscriptionProcessingError(
        "transcription_api_error",
        f"{label}返回HTTP {response.status_code}：{detail}",
    )


def _model_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = response.text

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or response.reason_phrase
        else:
            message = payload.get("message") or payload.get("detail") or response.reason_phrase
    else:
        message = str(payload or response.reason_phrase)
    return _safe_runtime_error_detail(message) or "未提供错误详情"


def _extract_chat_completion_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise TranscriptionProcessingError("transcription_invalid_response", "音频转文字模型响应缺少choices。")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise TranscriptionProcessingError("transcription_invalid_response", "音频转文字模型响应缺少message。")
    return _content_to_text(message.get("content"))


def _extract_refine_chat_completion_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RefineProcessingError("refine_invalid_response", "文稿优化模型响应缺少choices。")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise RefineProcessingError("refine_invalid_response", "文稿优化模型响应缺少message。")
    return _content_to_text(message.get("content"))


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    chunks = _extract_gemini_text_parts(payload)
    if not chunks:
        raise TranscriptionProcessingError("transcription_invalid_response", "Gemini原生响应缺少text part。")
    return "".join(chunks)


def _extract_gemini_text_parts(payload: dict[str, Any]) -> list[str]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return []

    chunks: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return chunks


def _extract_stream_delta_text(event: dict[str, Any]) -> str:
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
    if not isinstance(delta, dict):
        return ""
    return _content_to_text(delta.get("content"))


def _content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _ensure_transcription_text(text: str) -> str:
    normalized = strip_thinking_content(text)
    if not normalized:
        raise TranscriptionProcessingError("transcription_empty_response", "音频转文字模型返回空转写结果。")
    if _looks_like_audio_not_received(normalized):
        raise TranscriptionProcessingError(
            "transcription_audio_unsupported",
            "模型返回内容显示音频没有被实际接收或识别。请确认当前音频识别方式是否支持音频输入，AIStudioToAPI用户请优先选择Gemini原生链路。",
        )
    return normalized


def _ensure_refined_markdown(text: str) -> str:
    normalized, finished = _normalize_refine_model_output(text)
    if not finished:
        raise RefineProcessingError("refine_finish_missing", f"文稿优化模型输出末尾没有{REFINE_FINISH_MARKER}。")
    return normalized


def _normalize_refine_model_output(text: str) -> tuple[str, bool]:
    without_thinking = strip_thinking_content(text)
    if REFINE_XML_TAG_PATTERN.search(without_thinking):
        raise RefineProcessingError(
            "refine_output_invalid",
            "文稿优化模型输出中残留了内部XML输入标签，请更换模型或调整文稿优化模型配置后重试。",
        )
    finished = _has_refine_finish_marker(without_thinking)
    normalized = clean_refined_markdown_output(_strip_refine_finish_marker(without_thinking))
    if not normalized:
        raise RefineProcessingError("refine_empty_response", "文稿优化模型返回空内容或清理后没有可展示正文。")
    return normalized, finished


def _has_refine_finish_marker(text: str) -> bool:
    return bool(REFINE_FINISH_MARKER_PATTERN.search(str(text or "").strip()))


def _strip_refine_finish_marker(text: str) -> str:
    return REFINE_FINISH_MARKER_PATTERN.sub("", str(text or "")).strip()


def _extract_last_paragraph(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", value) if item.strip()]
    return paragraphs[-1] if paragraphs else value


def _merge_refine_continuation(previous_text: str, continuation_text: str, anchor: str) -> str:
    previous = str(previous_text or "").strip()
    continuation = str(continuation_text or "").strip()
    anchor = str(anchor or "").strip()
    if anchor and continuation.startswith(anchor):
        remainder = continuation[len(anchor) :]
        if not remainder.strip():
            return previous
        return f"{previous.rstrip()}{remainder.rstrip()}"
    if not continuation:
        return previous
    if not previous:
        return continuation
    return f"{previous.rstrip()}\n\n{continuation}"


def _emit_refine_log(log: RefineLogCallback | None, level: str, message: str) -> None:
    if log:
        log(level, message)


def _looks_like_audio_not_received(text: str) -> bool:
    lowered = text.lower()
    english_markers = (
        "as a text model",
        "cannot listen",
        "can't listen",
        "unable to listen",
        "cannot access audio",
        "cannot receive audio",
        "cannot process audio",
    )
    if any(marker in lowered for marker in english_markers):
        return True
    chinese_patterns = (
        r"无法.*(接收|播放|听取|聆听|访问).*(音频|mp3)",
        r"不能.*(接收|播放|听取|聆听|访问).*(音频|mp3)",
        r"作为.*文本模型.*(音频|mp3)",
        r"请.*(语音转文字|asr).*工具",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in chinese_patterns)


__all__ = [
    "AISTUDIO_GEMINI_FILE_PROVIDER",
    "OPENAI_INPUT_AUDIO_PROVIDER",
    "PLACEHOLDER_TRANSCRIPTION_PROVIDERS",
    "RefineProcessingError",
    "REFINE_CALL_MAX_RETRIES",
    "REFINE_MAX_CONTINUATIONS",
    "SUPPORTED_TRANSCRIPTION_PROVIDERS",
    "TranscriptionProcessingError",
    "build_audio_transcription_payload",
    "build_refine_chat_completion_payload",
    "build_gemini_file_payload",
    "clean_refined_markdown_output",
    "describe_transcription_route",
    "encode_mp3_as_data_url",
    "refine_markdown_with_chat_completions",
    "strip_thinking_content",
    "transcribe_mp3",
    "transcribe_mp3_with_aistudio_gemini",
    "transcribe_mp3_with_chat_completions",
]
