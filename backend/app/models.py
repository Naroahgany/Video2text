"""Shared request and response models."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ModelListRequest(BaseModel):
    """Request body for the OpenAI-compatible model list proxy."""

    base_url: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    provider: str = "openai_compatible_input_audio"


class ModelListResponse(BaseModel):
    """Normalized model list response returned to the frontend."""

    models: list[str]


class BilibiliProfileStatusResponse(BaseModel):
    """Sanitized local Bilibili profile status."""

    available: bool = False
    opened: bool = False
    profile_path_hint: str = "runtime/browser-profile/"
    message: str = ""
    session_token: str | None = None


class BilibiliProfileCookieExtractRequest(BaseModel):
    """Request body for extracting simplified cookies from the local profile."""

    session_token: str = Field(..., min_length=16, max_length=256)


class BilibiliCookieResponse(BaseModel):
    """Simplified Bilibili Cookie Header response."""

    cookie_header: str = ""
    fields: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    message: str = ""


class BilibiliCookieValidateRequest(BaseModel):
    """Request body for validating a simplified Bilibili Cookie Header."""

    cookie_header: str = Field(default="", max_length=64 * 1024)


class BilibiliCookieValidateResponse(BaseModel):
    """Lightweight Bilibili Cookie validation response."""

    valid: bool = False
    is_logged_in: bool = False
    fields: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    message: str = ""


class ModelConfig(BaseModel):
    """Model config sent by the frontend for the current task only."""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0
    stream: bool = True
    provider: str = "openai_compatible_input_audio"


class TaskOptions(BaseModel):
    """Resource and workflow options for a task."""

    skip_subtitle_if_failed: bool = False
    bilibili_access_mode: str = "cookie_header"
    bilibili_cookie_browser: str = "chrome"
    bilibili_cookie_header: str = Field(default="", max_length=64 * 1024)
    bilibili_cookies_file_content: str = Field(default="", max_length=1024 * 1024)
    audio_part_interval_seconds: int = Field(default=20, ge=0, le=120)
    no_slice_max_minutes: int = Field(default=15, ge=1, le=240)
    target_chunk_minutes: int = Field(default=15, ge=1, le=120)
    chunk_overlap_minutes: float = Field(default=0.5, ge=0, le=10)
    max_audio_request_concurrency: int = Field(default=2, ge=1, le=8)


class TaskCreateRequest(BaseModel):
    """Request body for creating a task."""

    input: str = Field(..., min_length=1)
    transcription_model_config: ModelConfig = Field(default_factory=ModelConfig)
    refine_model_config: ModelConfig = Field(default_factory=ModelConfig)
    options: TaskOptions = Field(default_factory=TaskOptions)


class TranscriptionRetryRequest(BaseModel):
    """Request body for retrying stage 6 after temporary audio is recreated."""

    transcription_model_config: ModelConfig = Field(default_factory=ModelConfig)


class TaskStatus(StrEnum):
    """Task lifecycle states."""

    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELED = "canceled"
    ABANDONED = "abandoned"
    WAITING_MODEL_RETRY = "waiting_model_retry"


class TaskStage(StrEnum):
    """Workflow stages shown to the frontend."""

    PARSE_INPUT = "解析用户输入"
    READ_LOCAL_MEDIA = "读取本地音视频文件"
    FETCH_VIDEO_INFO = "获取视频信息"
    FETCH_SUBTITLE = "获取 B站字幕"
    CLEAN_SUBTITLE = "清理 B站字幕"
    DOWNLOAD_AUDIO = "下载音频"
    CONVERT_MP3 = "转换 MP3"
    SPLIT_AUDIO = "音频切片"
    TRANSCRIBE_AUDIO = "音频切片转文字"
    MERGE_TRANSCRIPT = "合并 AI 音频转文字稿"
    REFINE_MARKDOWN = "文稿优化"
    GENERATE_MARKDOWN = "生成最终 Markdown"
    CLEANUP_TEMP = "清理临时文件"
    COMPLETED = "完成"


class TaskLogEntry(BaseModel):
    """A single sanitized task log entry."""

    time: str
    level: Literal["info", "warning", "error"]
    message: str


class TaskResult(BaseModel):
    """Task result structure used by later workflow stages."""

    source_type: Literal["bilibili", "local_upload"] = "bilibili"
    title: str = ""
    bv_id: str = ""
    p_index: int | None = None
    parsed_input: str = ""
    webpage_url: str = ""
    duration_seconds: float | None = None
    subtitle_language: str = ""
    subtitle_source: str = ""
    final_markdown: str = ""
    clean_subtitle: str = ""
    ai_transcript: str = ""
    filename: str = ""
    audio_parts: list[dict] = Field(default_factory=list)
    sub_tasks: list[dict] = Field(default_factory=list)


class TaskStatusResponse(BaseModel):
    """Public task status response. Secrets must never be included here."""

    task_id: str
    status: TaskStatus
    stage: TaskStage
    progress: int
    logs: list[TaskLogEntry]
    result: TaskResult | None = None
    error: str | None = None
    error_code: str | None = None
    current_item: str | None = None
