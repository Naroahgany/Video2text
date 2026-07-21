"""In-memory task lifecycle management for stage 3."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .audio import (
    AudioPart,
    AudioProcessingError,
    convert_audio_to_mp3,
    download_audio_stream,
    fetch_playurl_audio_stream,
    split_mp3_by_rule,
)
from .bilibili import (
    AccessAttempt,
    BilibiliAccessConfig,
    BilibiliAccessMode,
    BilibiliError,
    BilibiliErrorCode,
    BilibiliVideoInfo,
    BrowserCookieSource,
    PartInfo,
    choose_subtitle_candidate,
    download_subtitle,
    fetch_video_info,
    parse_bilibili_input,
)
from .bilibili_cookie import simplify_bilibili_cookie_header
from .cleanup import TaskTempDirManager
from .llm_client import (
    OPENAI_INPUT_AUDIO_PROVIDER,
    RefineProcessingError,
    TranscriptionProcessingError,
    describe_transcription_route,
    refine_markdown_with_chat_completions,
    strip_thinking_content,
    transcribe_mp3,
)
from .models import (
    ModelConfig,
    TaskCreateRequest,
    TaskLogEntry,
    TaskOptions,
    TaskResult,
    TaskStage,
    TaskStatus,
    TaskStatusResponse,
    TranscriptionRetryRequest,
)
from .subtitle import SubtitleProcessingError, clean_subtitle
from .utils import redact_secrets, utc_now_iso


TERMINAL_STATUSES = {
    TaskStatus.FAILED,
    TaskStatus.COMPLETED,
    TaskStatus.CANCELED,
    TaskStatus.ABANDONED,
}
RECOVERABLE_MODEL_STATUSES = {TaskStatus.WAITING_MODEL_RETRY}
OVERLAP_RETRY_NOTE = (
    "（系统提示：本段开头的几句话与上一个文段的末尾内容是首尾相连的，有0.5分钟的转录重合。"
    "请AI把上一个文段的末尾以及本段的开头视为同一段话的重复转写，并且在生成最终文稿的过程中去掉本段系统提示词。）"
)

TASK_LOGGER = logging.getLogger("uvicorn.error")


class TaskNotFoundError(KeyError):
    """Raised when a task_id does not exist."""


@dataclass
class TaskRecord:
    """Internal task state. API keys are kept only for in-memory redaction."""

    task_id: str
    original_input: str
    options: TaskOptions
    transcription_model_config: ModelConfig
    refine_model_config: ModelConfig
    secret_values: list[str]
    status: TaskStatus = TaskStatus.PENDING
    stage: TaskStage = TaskStage.PARSE_INPUT
    progress: int = 0
    logs: list[TaskLogEntry] = field(default_factory=list)
    result: TaskResult | None = None
    error: str | None = None
    error_code: str | None = None
    current_item: str | None = None
    temp_dir: Path | None = None
    audio_parts: list[AudioPart] = field(default_factory=list)
    created_at: float = field(default_factory=time.monotonic)
    last_polled_at: float = field(default_factory=time.monotonic)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    worker_task: asyncio.Task | None = None
    retry_bilibili_cookie_header: str = ""
    retry_bilibili_cookies_file_content: str = ""


class TaskManager:
    """Manage task creation, polling, cancellation, logs and cleanup."""

    def __init__(self, temp_dirs: TaskTempDirManager | None = None) -> None:
        global_limit = int(os.getenv("GLOBAL_TASK_CONCURRENCY", "1"))
        self.global_concurrency = max(1, global_limit)
        self.audio_request_concurrency = 2
        self.abandoned_after_seconds = max(
            60,
            int(os.getenv("TASK_ABANDONED_AFTER_SECONDS", "1800")),
        )
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = asyncio.Lock()
        self._global_semaphore = asyncio.Semaphore(self.global_concurrency)
        self._temp_dirs = temp_dirs or TaskTempDirManager()

    async def create_task(self, request: TaskCreateRequest) -> TaskStatusResponse:
        await self.sweep_abandoned_tasks()
        task_id = str(uuid.uuid4())
        options = request.options
        options.max_audio_request_concurrency = min(
            options.max_audio_request_concurrency,
            self.audio_request_concurrency,
        )

        record = TaskRecord(
            task_id=task_id,
            original_input=request.input,
            options=options,
            transcription_model_config=request.transcription_model_config,
            refine_model_config=request.refine_model_config,
            secret_values=[
                request.transcription_model_config.api_key,
                request.refine_model_config.api_key,
                request.options.bilibili_cookie_header,
            ],
            retry_bilibili_cookie_header=request.options.bilibili_cookie_header,
            retry_bilibili_cookies_file_content=request.options.bilibili_cookies_file_content,
        )
        self._add_log(record, "info", f"任务已创建，taskId={task_id}，等待资源调度")
        self._add_log(record, "info", f"全局重任务并发上限：{self.global_concurrency}")
        self._add_log(
            record,
            "info",
            f"OpenAI-compatible 音频片段并发上限：{options.max_audio_request_concurrency}",
        )

        async with self._lock:
            self._tasks[task_id] = record

        record.worker_task = asyncio.create_task(self._run_task(task_id))
        return self._to_response(record)

    async def get_task(self, task_id: str) -> TaskStatusResponse:
        async with self._lock:
            record = self._get_record_locked(task_id)
            record.last_polled_at = time.monotonic()
        await self.sweep_abandoned_tasks(exclude_task_id=task_id)
        async with self._lock:
            return self._to_response(record)

    async def cancel_task(self, task_id: str) -> TaskStatusResponse:
        worker_task: asyncio.Task | None = None
        async with self._lock:
            record = self._get_record_locked(task_id)
            if record.status in TERMINAL_STATUSES:
                return self._to_response(record)

            record.cancel_event.set()
            self._add_log(record, "warning", "收到取消请求，正在停止任务")
            record.stage = TaskStage.CLEANUP_TEMP
            record.current_item = None
            record.status = TaskStatus.CANCELED
            if record.worker_task and not record.worker_task.done():
                worker_task = record.worker_task
                worker_task.cancel()

        if worker_task and worker_task is not asyncio.current_task():
            await asyncio.gather(worker_task, return_exceptions=True)

        async with self._lock:
            self._cleanup_record(record)
            self._clear_secrets(record)
            return self._to_response(record)

    async def retry_transcription(self, task_id: str, request: TranscriptionRetryRequest) -> TaskStatusResponse:
        async with self._lock:
            record = self._get_record_locked(task_id)
            if record.status != TaskStatus.WAITING_MODEL_RETRY:
                return self._to_response(record)

            record.transcription_model_config = request.transcription_model_config
            record.secret_values.append(request.transcription_model_config.api_key)
            record.error = None
            record.error_code = None
            record.cancel_event = asyncio.Event()
            record.status = TaskStatus.RUNNING
            record.stage = TaskStage.TRANSCRIBE_AUDIO
            record.current_item = "等待重新准备音频并重试阶段 6"
            self._add_log(record, "info", "用户已请求重新尝试阶段 6；此前临时音频已清理，本次将重新下载并切片")
            record.worker_task = asyncio.create_task(self._resume_stage6_task(task_id))
            return self._to_response(record)

    def cleanup_stale_temp_dirs(self) -> None:
        """Remove artifacts left behind by a previous unclean process exit."""

        self._temp_dirs.cleanup_all()

    async def sweep_abandoned_tasks(self, exclude_task_id: str | None = None) -> None:
        """Cancel and clean tasks that are no longer being polled."""

        now = time.monotonic()
        abandoned_records: list[TaskRecord] = []
        worker_tasks: list[asyncio.Task] = []

        async with self._lock:
            for task_id, record in self._tasks.items():
                if task_id == exclude_task_id or record.status in TERMINAL_STATUSES:
                    continue
                if now - record.last_polled_at < self.abandoned_after_seconds:
                    continue

                record.cancel_event.set()
                record.stage = TaskStage.CLEANUP_TEMP
                record.error = "长时间没有前端轮询，任务已标记为 abandoned"
                record.current_item = None
                record.status = TaskStatus.ABANDONED
                self._add_log(record, "warning", record.error)
                abandoned_records.append(record)
                if record.worker_task and not record.worker_task.done():
                    record.worker_task.cancel()
                    worker_tasks.append(record.worker_task)

        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)

        async with self._lock:
            for record in abandoned_records:
                self._cleanup_record(record)
                self._clear_secrets(record)

    async def shutdown(self) -> None:
        """Stop every task and remove all temporary artifacts before exit."""

        records: list[TaskRecord] = []
        worker_tasks: list[asyncio.Task] = []

        async with self._lock:
            records = list(self._tasks.values())
            for record in records:
                if record.status not in TERMINAL_STATUSES:
                    record.cancel_event.set()
                    record.stage = TaskStage.CLEANUP_TEMP
                    record.current_item = None
                    record.status = TaskStatus.CANCELED
                    self._add_log(record, "warning", "本地服务正在关闭，任务已停止并清理")
                if record.worker_task and not record.worker_task.done():
                    record.worker_task.cancel()
                    worker_tasks.append(record.worker_task)

        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)

        async with self._lock:
            for record in records:
                self._cleanup_record(record)
                self._clear_secrets(record)
        self._temp_dirs.cleanup_all()


    async def _run_task(self, task_id: str) -> None:
        record = self._tasks[task_id]
        try:
            async with self._global_semaphore:
                await self._raise_if_cancelled(record)
                record.status = TaskStatus.RUNNING
                record.temp_dir = self._temp_dirs.create(record.task_id)
                self._add_log(record, "info", "已创建任务独立临时目录")

                await self._run_stage4_workflow(record)
                await self._run_stage6_workflow(record)
                await self._run_stage7_workflow(record)

                record.stage = TaskStage.CLEANUP_TEMP
                record.progress = 98
                self._add_log(record, "info", "开始清理临时文件")
                self._cleanup_record(record)

                record.stage = TaskStage.COMPLETED
                record.progress = 100
                record.current_item = None
                self._add_log(record, "info", "阶段 7：最终 Markdown 文稿已生成")
                self._clear_secrets(record)
                record.status = TaskStatus.COMPLETED
        except asyncio.CancelledError:
            if record.status not in TERMINAL_STATUSES:
                record.stage = TaskStage.CLEANUP_TEMP
                record.error = "任务已取消"
                self._add_log(record, "warning", "任务已取消")
                record.status = TaskStatus.CANCELED
            self._cleanup_record(record)
            self._clear_secrets(record)
        except (BilibiliError, SubtitleProcessingError) as exc:
            record.stage = TaskStage.CLEANUP_TEMP
            record.error = redact_secrets(exc.message, record.secret_values)
            record.error_code = exc.code.value
            self._add_log(record, "error", f"任务失败：{record.error}")
            self._cleanup_record(record)
            self._clear_secrets(record)
            record.status = TaskStatus.FAILED
        except AudioProcessingError as exc:
            record.stage = TaskStage.CLEANUP_TEMP
            record.error = redact_secrets(exc.message, record.secret_values)
            record.error_code = exc.code
            self._add_log(record, "error", f"任务失败：{record.error}")
            self._cleanup_record(record)
            self._clear_secrets(record)
            record.status = TaskStatus.FAILED
        except TranscriptionProcessingError as exc:
            self._pause_stage6_for_retry(record, exc)
        except RefineProcessingError as exc:
            record.stage = TaskStage.CLEANUP_TEMP
            record.error = redact_secrets(exc.message, record.secret_values)
            record.error_code = exc.code
            self._add_log(record, "error", f"任务失败：{record.error}")
            self._cleanup_record(record)
            self._clear_secrets(record)
            record.status = TaskStatus.FAILED
        except Exception as exc:  # pragma: no cover - defensive safety net
            record.stage = TaskStage.CLEANUP_TEMP
            record.error = redact_secrets(exc, record.secret_values)
            record.error_code = "internal_error"
            self._add_log(record, "error", f"任务失败：{record.error}")
            self._cleanup_record(record)
            self._clear_secrets(record)
            record.status = TaskStatus.FAILED
        finally:
            if record.status in TERMINAL_STATUSES:
                self._cleanup_record(record)
                self._clear_secrets(record)

    async def _resume_stage6_task(self, task_id: str) -> None:
        record = self._tasks[task_id]
        try:
            async with self._global_semaphore:
                if not record.temp_dir or not record.audio_parts:
                    record.temp_dir = self._temp_dirs.create(record.task_id)
                    self._add_log(record, "info", "阶段 6 重试前重新准备任务临时目录和音频")
                    await self._run_stage4_workflow(record)

                await self._run_stage6_workflow(record)
                await self._run_stage7_workflow(record)

                record.stage = TaskStage.CLEANUP_TEMP
                record.progress = 98
                self._add_log(record, "info", "开始清理临时文件")
                self._cleanup_record(record)

                record.stage = TaskStage.COMPLETED
                record.progress = 100
                record.current_item = None
                self._add_log(record, "info", "阶段 6 重试成功，阶段 7 最终 Markdown 文稿已生成")
                self._clear_secrets(record)
                record.status = TaskStatus.COMPLETED
        except asyncio.CancelledError:
            if record.status not in TERMINAL_STATUSES:
                record.stage = TaskStage.CLEANUP_TEMP
                record.error = "任务已取消"
                self._add_log(record, "warning", "任务已取消")
                record.status = TaskStatus.CANCELED
            self._cleanup_record(record)
            self._clear_secrets(record)
        except (BilibiliError, SubtitleProcessingError) as exc:
            record.stage = TaskStage.CLEANUP_TEMP
            record.error = redact_secrets(exc.message, record.secret_values)
            record.error_code = exc.code.value
            self._add_log(record, "error", f"任务失败：{record.error}")
            record.status = TaskStatus.FAILED
        except AudioProcessingError as exc:
            record.stage = TaskStage.CLEANUP_TEMP
            record.error = redact_secrets(exc.message, record.secret_values)
            record.error_code = exc.code
            self._add_log(record, "error", f"任务失败：{record.error}")
            record.status = TaskStatus.FAILED
        except TranscriptionProcessingError as exc:
            self._pause_stage6_for_retry(record, exc)
        except RefineProcessingError as exc:
            record.stage = TaskStage.CLEANUP_TEMP
            record.error = redact_secrets(exc.message, record.secret_values)
            record.error_code = exc.code
            self._add_log(record, "error", f"任务失败：{record.error}")
            self._cleanup_record(record)
            self._clear_secrets(record)
            record.status = TaskStatus.FAILED
        except Exception as exc:  # pragma: no cover - defensive safety net
            record.stage = TaskStage.CLEANUP_TEMP
            record.error = redact_secrets(exc, record.secret_values)
            record.error_code = "internal_error"
            self._add_log(record, "error", f"任务失败：{record.error}")
            self._cleanup_record(record)
            self._clear_secrets(record)
            record.status = TaskStatus.FAILED
        finally:
            if record.status in TERMINAL_STATUSES:
                self._cleanup_record(record)
                self._clear_secrets(record)

    async def _run_stage4_workflow(self, record: TaskRecord) -> None:
        await self._raise_if_cancelled(record)
        record.stage = TaskStage.PARSE_INPUT
        record.progress = 2
        record.current_item = "解析输入"
        self._add_log(record, "info", "开始解析用户输入")
        parsed = parse_bilibili_input(record.original_input)
        record.current_item = parsed.display
        self._add_log(record, "info", f"已识别 B站输入：{parsed.display}")
        self._log_subtitle_debug(record, f"inputUrl={parsed.url}")
        self._log_subtitle_debug(record, f"rawInputPreview={self._preview_text(record.original_input)}")
        self._log_subtitle_debug(record, f"parsedBvid={parsed.bv_id or '未解析'}")
        self._log_subtitle_debug(record, f"selectedPage={parsed.p_index or '默认P1'}")
        access_config = self._prepare_bilibili_access_config(record)
        self._add_log(record, "info", f"B站访问模式：{self._access_config_label(access_config)}")

        await self._raise_if_cancelled(record)
        record.stage = TaskStage.FETCH_VIDEO_INFO
        record.progress = 4
        self._add_log(record, "info", "开始获取 B站视频信息")
        try:
            video_info = await fetch_video_info(
                parsed,
                access_config,
                debug_logger=lambda message: self._log_subtitle_debug(record, message),
            )
        except BilibiliError as exc:
            self._log_access_attempts(record, exc.access_attempts)
            raise
        self._log_access_attempts(record, video_info.access_attempts)
        record.current_item = video_info.title
        record.result = self._build_video_metadata_result(parsed.display, video_info)
        self._log_video_info(record, video_info)
        self._log_subtitle_candidates(record, video_info)

        await self._raise_if_cancelled(record)
        record.stage = TaskStage.FETCH_SUBTITLE
        record.progress = 6
        self._add_log(record, "info", "开始选择 B站自带字幕")
        candidate = choose_subtitle_candidate(video_info)
        clean_text = ""
        subtitle_language = ""
        subtitle_source = ""

        if candidate is None:
            message = "没有检测到 B站自带字幕"
            if not record.options.skip_subtitle_if_failed:
                raise BilibiliError(
                    BilibiliErrorCode.SUBTITLE_NOT_FOUND,
                    f"{message}。如需继续，请选择跳过字幕后重新提交任务。",
                )
            self._add_log(record, "warning", f"{message}，已按用户选择跳过字幕")
        else:
            subtitle_language = candidate.language
            subtitle_source = candidate.source
            self._add_log(
                record,
                "info",
                (
                    f"已选择字幕：language={candidate.language}，"
                    f"source={self._subtitle_source_label(candidate.source)}，ext={candidate.ext or 'unknown'}"
                ),
            )
            self._log_subtitle_debug(record, f"selectedSubtitleUrl={self._normalize_subtitle_url(candidate.url)}")
            try:
                raw_subtitle = await download_subtitle(
                    candidate,
                    access_config,
                    debug_logger=lambda message: self._log_subtitle_debug(record, message),
                )
                self._log_subtitle_debug(record, f"subtitleJsonPreview={self._preview_text(raw_subtitle)}")

                await self._raise_if_cancelled(record)
                record.stage = TaskStage.CLEAN_SUBTITLE
                record.progress = 8
                self._add_log(record, "info", "开始清理字幕正文")
                try:
                    cleaned = clean_subtitle(raw_subtitle, candidate.ext)
                except SubtitleProcessingError as exc:
                    if candidate.source == "html_initial_state":
                        self._add_log(record, "warning", f"HTML 回退发现的字幕候选无法解析：{exc.message}，将继续尝试其他字幕路径")
                        fallback_candidate = choose_subtitle_candidate(
                            BilibiliVideoInfo(
                                title=video_info.title,
                                bv_id=video_info.bv_id,
                                p_index=video_info.p_index,
                                duration_seconds=video_info.duration_seconds,
                                webpage_url=video_info.webpage_url,
                                parts=video_info.parts,
                                subtitle_candidates=[
                                    item for item in video_info.subtitle_candidates if item.url != candidate.url
                                ],
                                raw_info=video_info.raw_info,
                            ),
                        )
                        if fallback_candidate:
                            self._add_log(
                                record,
                                "info",
                                (
                                    f"改用字幕：language={fallback_candidate.language}，"
                                    f"source={self._subtitle_source_label(fallback_candidate.source)}，ext={fallback_candidate.ext or 'unknown'}"
                                ),
                            )
                            candidate = fallback_candidate
                            subtitle_language = candidate.language
                            subtitle_source = candidate.source
                            self._log_subtitle_debug(record, f"selectedSubtitleUrl={self._normalize_subtitle_url(candidate.url)}")
                            raw_subtitle = await download_subtitle(
                                candidate,
                                access_config,
                                debug_logger=lambda message: self._log_subtitle_debug(record, message),
                            )
                            self._log_subtitle_debug(record, f"subtitleJsonPreview={self._preview_text(raw_subtitle)}")
                            cleaned = clean_subtitle(raw_subtitle, candidate.ext)
                        else:
                            raise
                    else:
                        raise
                clean_text = cleaned.text
                self._log_subtitle_debug(record, f"cleanedSubtitlePreview={self._preview_text(clean_text)}")
                self._add_log(
                    record,
                    "info",
                    f"字幕清理完成：格式 {cleaned.format}，正文 {cleaned.line_count} 行",
                )
            except (BilibiliError, SubtitleProcessingError) as exc:
                if not record.options.skip_subtitle_if_failed:
                    raise
                record.stage = TaskStage.CLEAN_SUBTITLE
                record.progress = 8
                subtitle_language = ""
                subtitle_source = ""
                self._add_log(record, "warning", f"字幕处理失败：{exc.message}；已按用户选择跳过字幕")

        await self._raise_if_cancelled(record)
        if not record.temp_dir:
            raise AudioProcessingError("audio_temp_dir_missing", "任务临时目录未就绪，无法处理音频。")

        await self._raise_if_cancelled(record)
        record.stage = TaskStage.DOWNLOAD_AUDIO
        record.progress = 10
        record.current_item = "playurl 主路径"
        self._add_log(record, "info", "开始阶段 5A：通过 x/player/playurl 主路径获取音频流")
        stream = await fetch_playurl_audio_stream(video_info, access_config)
        self._add_log(
            record,
            "info",
            (
                "playurl 主路径已选择音频流："
                f"streamId={stream.audio_id or '未知'}，"
                f"bandwidth={stream.bandwidth or '未知'}，"
                f"backupUrl数量={len(stream.backup_urls)}"
            ),
        )
        downloaded_audio = await download_audio_stream(
            stream,
            video_info,
            access_config,
            record.temp_dir / "stage5" / "download",
            log=lambda message: self._add_log(record, "info", message),
        )

        await self._raise_if_cancelled(record)
        record.stage = TaskStage.CONVERT_MP3
        record.progress = 14
        record.current_item = "MP3 转换"
        self._add_log(record, "info", "开始使用 FFmpeg 将下载音频转换为 MP3")
        mp3_path = record.temp_dir / "stage5" / "converted" / "source.mp3"
        mp3_duration = await convert_audio_to_mp3(downloaded_audio, mp3_path, video_info.duration_seconds)
        self._add_log(record, "info", f"MP3 转换完成，校验时长：{self._format_duration(mp3_duration)}")

        await self._raise_if_cancelled(record)
        record.stage = TaskStage.SPLIT_AUDIO
        record.progress = 17
        record.current_item = "音频切片"
        self._add_log(
            record,
            "info",
            (
                "开始按阶段 5 规则处理切片："
                f"不切片阈值={record.options.no_slice_max_minutes}分钟，"
                f"切片周期={record.options.target_chunk_minutes}分钟，"
                f"重合={record.options.chunk_overlap_minutes}分钟"
            ),
        )
        audio_parts = await split_mp3_by_rule(
            mp3_path,
            record.temp_dir / "stage5" / "parts",
            mp3_duration,
            no_slice_max_minutes=record.options.no_slice_max_minutes,
            target_chunk_minutes=record.options.target_chunk_minutes,
            chunk_overlap_minutes=record.options.chunk_overlap_minutes,
        )
        self._log_audio_parts(record, audio_parts)
        record.audio_parts = audio_parts
        record.progress = 20
        record.current_item = None
        self._add_log(record, "info", "阶段 5 已完成；已在同一任务状态中登记音频片段，准备进入阶段 6")
        record.result = self._build_stage5_result(
            parsed_input=parsed.display,
            video_info=video_info,
            clean_subtitle=clean_text,
            subtitle_language=subtitle_language,
            subtitle_source=subtitle_source,
            mp3_duration_seconds=mp3_duration,
            audio_parts=[part.to_public_dict() for part in audio_parts],
        )

    async def _run_stage6_workflow(self, record: TaskRecord) -> None:
        await self._raise_if_cancelled(record)
        record.stage = TaskStage.TRANSCRIBE_AUDIO
        record.progress = max(record.progress, 20)
        record.current_item = "校验音频片段"
        self._add_log(record, "info", "开始阶段 6：同任务音频片段接收与存在性校验")

        audio_parts = self._validate_stage6_audio_parts(record)
        total = len(audio_parts)
        self._add_log(record, "info", f"阶段 6 检测到 {total} 个音频片段，全部为 MP3、文件仍存在且大小大于 0")
        transcription_provider = record.transcription_model_config.provider or OPENAI_INPUT_AUDIO_PROVIDER
        self._add_log(record, "info", f"阶段 6 音频识别方式：{transcription_provider}")

        transcript_by_index = await self._transcribe_audio_parts(record, audio_parts)

        await self._raise_if_cancelled(record)
        record.stage = TaskStage.MERGE_TRANSCRIPT
        record.progress = 72
        record.current_item = "合并转写稿"
        self._add_log(record, "info", "所有音频片段转写成功，开始按编号顺序合并 AI 音频转文字稿")
        merged = self._merge_audio_transcripts(audio_parts, transcript_by_index)
        if not merged.strip():
            raise TranscriptionProcessingError("transcription_empty_response", "阶段 6 合并后转写结果为空。")

        if record.result:
            record.result.ai_transcript = merged
            record.result.final_markdown = self._build_stage6_markdown(record.result, audio_parts)
            record.result.filename = "stage-6-audio-transcription-result.md"

        record.current_item = None
        self._add_log(record, "info", "阶段 6 已生成完整 AI 音频转文字稿，后续阶段再进入文稿优化")

    async def _transcribe_audio_parts(
        self,
        record: TaskRecord,
        audio_parts: list[AudioPart],
    ) -> dict[int, str]:
        """Transcribe parts concurrently while preserving their indexed result mapping."""

        total = len(audio_parts)
        provider = record.transcription_model_config.provider or OPENAI_INPUT_AUDIO_PROVIDER
        is_openai_compatible = provider == OPENAI_INPUT_AUDIO_PROVIDER
        requested_limit = record.options.max_audio_request_concurrency
        concurrency = requested_limit if is_openai_compatible else 1
        concurrency = min(max(1, concurrency), total)
        mode = "有界并行" if concurrency > 1 else "串行"
        route_note = (
            "每段在本机完成 Base64 转换后立即调用一次 API，完成顺序不影响最终拼接顺序"
            if is_openai_compatible
            else "当前 provider 保持单片串行处理，结果仍按片段编号合并"
        )
        self._add_log(
            record,
            "info",
            f"阶段 6 将以{mode}方式处理音频片段：并发上限 {concurrency}；{route_note}",
        )

        semaphore = asyncio.Semaphore(concurrency)
        completed_count = 0

        async def run_part(part: AudioPart) -> tuple[int, str]:
            nonlocal completed_count
            async with semaphore:
                await self._raise_if_cancelled(record)
                text = await self._transcribe_audio_part_with_retries(record, part, total)
            completed_count += 1
            record.progress = min(70, 20 + int((completed_count / max(1, total)) * 50))
            record.current_item = f"已完成 {completed_count}/{total} 个音频片段"
            return part.index, text

        tasks = [asyncio.create_task(run_part(part)) for part in audio_parts]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return dict(results)

    async def _run_stage7_workflow(self, record: TaskRecord) -> None:
        await self._raise_if_cancelled(record)
        if not record.result:
            raise RefineProcessingError("refine_missing_result", "阶段 7 调用前缺少任务结果结构。")

        ai_transcript = strip_thinking_content(record.result.ai_transcript)
        if not ai_transcript:
            raise RefineProcessingError("refine_transcript_missing", "阶段 7 调用前未拿到完整 AI 音频转文字稿。")
        record.result.ai_transcript = ai_transcript
        record.result.final_markdown = ""
        record.result.filename = ""

        record.stage = TaskStage.REFINE_MARKDOWN
        record.progress = 75
        record.current_item = "第二模型文稿优化"
        self._add_log(record, "info", "开始阶段 7：调用第二模型合并字幕与 AI 音频转文字稿")
        self._add_log(
            record,
            "info",
            "第二模型链路：OpenAI-compatible Chat Completions，"
            f"stream={'开启' if record.refine_model_config.stream is not False else '关闭'}",
        )

        subtitle_for_prompt = record.result.clean_subtitle if record.result.subtitle_source else ""
        final_markdown = await refine_markdown_with_chat_completions(
            subtitle_for_prompt,
            ai_transcript,
            record.refine_model_config,
            log=lambda level, message: self._add_log(record, level, message),
        )

        await self._raise_if_cancelled(record)
        record.stage = TaskStage.GENERATE_MARKDOWN
        record.progress = 95
        record.current_item = "生成 Markdown 文件名"
        record.result.final_markdown = final_markdown
        record.result.filename = self._build_markdown_filename(record.result)
        self._add_log(record, "info", f"阶段 7 文稿优化完成，最终 Markdown 正文长度 {len(final_markdown)} 字")
        self._add_log(record, "info", f"已生成下载文件名：{record.result.filename}")

    def _validate_stage6_audio_parts(self, record: TaskRecord) -> list[AudioPart]:
        possible_reasons = (
            "可能原因：阶段 5 未成功产出音频、阶段 5 报错后仍进入阶段 6、"
            "临时目录被提前清理、任务状态丢失或本地服务异常重启。"
        )
        if not record.audio_parts:
            message = f"阶段 6 未拿到阶段 5 的音频片段清单，或片段清单为空。{possible_reasons}"
            self._add_log(record, "error", message)
            raise AudioProcessingError("stage6_audio_parts_missing", message)

        sorted_parts = sorted(record.audio_parts, key=lambda item: item.index)
        missing = [part.filename for part in sorted_parts if not part.path.exists()]
        not_mp3 = [part.filename for part in sorted_parts if part.path.suffix.lower() != ".mp3"]
        empty_or_unreadable: list[str] = []
        for part in sorted_parts:
            if part.filename in missing:
                continue
            try:
                if part.path.stat().st_size <= 0:
                    empty_or_unreadable.append(part.filename)
            except OSError:
                empty_or_unreadable.append(part.filename)
        if missing:
            message = (
                f"阶段 6 拿到音频片段清单，但以下 MP3 临时文件不存在：{', '.join(missing)}。"
                f"{possible_reasons}"
            )
            self._add_log(record, "error", message)
            raise AudioProcessingError("stage6_audio_file_missing", message)
        if not_mp3:
            message = f"阶段 6 只接受 MP3 音频片段，以下片段格式不符合要求：{', '.join(not_mp3)}。"
            self._add_log(record, "error", message)
            raise AudioProcessingError("transcription_audio_not_mp3", message)
        if empty_or_unreadable:
            message = (
                f"阶段 6 上传前校验失败，以下 MP3 切片文件为空或无法读取大小：{', '.join(empty_or_unreadable)}。"
                "切片文件可能为空、损坏、临时目录被清理、任务状态丢失，或阶段 5 FFmpeg 切片输出异常。"
            )
            self._add_log(record, "error", message)
            raise AudioProcessingError("transcription_audio_file_empty", message)
        return sorted_parts

    async def _transcribe_audio_part_with_retries(
        self,
        record: TaskRecord,
        part: AudioPart,
        total: int,
    ) -> str:
        max_retries = 3
        max_attempts = max_retries + 1
        for attempt in range(1, max_attempts + 1):
            await self._raise_if_cancelled(record)
            retry_label = f"第 {attempt - 1}/{max_retries} 次重试" if attempt > 1 else "首次请求"
            part_label = f"{Path(part.filename).stem} / {total}"
            record.current_item = part_label
            try:
                size_bytes = part.path.stat().st_size
            except OSError:
                size_bytes = 0
            size_mb = size_bytes / 1024 / 1024
            self._add_log(
                record,
                "info",
                f"当前处理 {part_label}（{retry_label}，本地切片大小 {size_bytes} bytes / {size_mb:.2f} MB）",
            )
            self._add_log(
                record,
                "info",
                f"{part.filename} 转写链路：{describe_transcription_route(part.path, record.transcription_model_config)}",
            )
            try:
                text = await transcribe_mp3(
                    part.path,
                    record.transcription_model_config,
                    log=lambda level, message: self._add_log(record, level, message),
                )
            except TranscriptionProcessingError as exc:
                safe_message = redact_secrets(exc.message, record.secret_values)
                if attempt <= max_retries:
                    self._add_log(
                        record,
                        "warning",
                        f"{part.filename} 转写失败：{safe_message}；3 秒后自动重试（{attempt}/{max_retries}）",
                    )
                    await self._sleep_or_cancel(record, 3)
                    continue
                message = f"{part.filename} 已自动重试 {max_retries} 次仍失败：{safe_message}"
                self._add_log(record, "error", message)
                raise TranscriptionProcessingError(exc.code, message) from exc

            self._add_log(record, "info", f"{part.filename} 转写成功，模型返回正文长度 {len(text)} 字")
            return text

        raise TranscriptionProcessingError("transcription_unknown_error", f"{part.filename} 转写失败。")

    def _merge_audio_transcripts(self, audio_parts: list[AudioPart], transcript_by_index: dict[int, str]) -> str:
        ordered = sorted(audio_parts, key=lambda item: item.index)
        merged = ""
        for part in ordered:
            current = strip_thinking_content(transcript_by_index.get(part.index, ""))
            if not current:
                continue
            if not merged:
                merged = current
                continue
            if part.overlap_seconds > 0:
                trim_index = self._find_overlap_trim_index(merged, current)
                if trim_index is not None:
                    current = current[trim_index:].lstrip()
                else:
                    current = f"{OVERLAP_RETRY_NOTE}\n\n{current}"
            merged = f"{merged.rstrip()}\n\n{current.strip()}"
        return merged.strip()

    def _find_overlap_trim_index(self, previous_text: str, current_text: str) -> int | None:
        previous_norm, _ = self._normalize_with_positions(previous_text)
        current_norm, current_positions = self._normalize_with_positions(current_text)
        max_len = min(240, len(previous_norm), len(current_norm))
        min_len = min(12, max_len)
        for length in range(max_len, min_len - 1, -1):
            if previous_norm.endswith(current_norm[:length]):
                return current_positions[length - 1] + 1 if length - 1 < len(current_positions) else 0
        return None

    def _normalize_with_positions(self, text: str) -> tuple[str, list[int]]:
        chars: list[str] = []
        positions: list[int] = []
        for index, char in enumerate(text):
            if re.match(r"[\s，。！？；：、,.!?;:\"'“”‘’（）()\[\]【】\-—_]+", char):
                continue
            chars.append(char.lower())
            positions.append(index)
        return "".join(chars), positions

    def _build_stage6_markdown(self, result: TaskResult, audio_parts: list[AudioPart]) -> str:
        audio_summary = "未切片，单段 MP3 已转写" if len(audio_parts) == 1 else f"{len(audio_parts)} 段 MP3 已按顺序转写并合并"
        return (
            "### 阶段 6 结果\n\n"
            f"- 视频标题：{result.title}\n"
            f"- BV 号：{result.bv_id or '未知'}\n"
            f"- 当前分P：P{result.p_index}\n"
            f"- 音频转写状态：{audio_summary}\n\n"
            "#### AI 音频转文字稿\n\n"
            f"{result.ai_transcript}\n\n"
            "本轮已完成第一模型音频转文字。阶段 7 的第二模型文稿优化尚未执行。"
        )

    def _build_markdown_filename(self, result: TaskResult) -> str:
        p_label = f"P{result.p_index}" if result.p_index else "P1"
        title = self._sanitize_filename_part(result.title or "B站视频转文字")
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        return f"{p_label}_{title}_{timestamp}.md"

    def _sanitize_filename_part(self, value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(value or ""))
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        return (cleaned[:80].strip(" .") or "未命名视频")

    def _pause_stage6_for_retry(self, record: TaskRecord, exc: TranscriptionProcessingError) -> None:
        record.stage = TaskStage.TRANSCRIBE_AUDIO
        record.error = redact_secrets(exc.message, record.secret_values)
        record.error_code = exc.code
        record.current_item = None
        record.status = TaskStatus.WAITING_MODEL_RETRY
        self._add_log(
            record,
            "error",
            f"阶段 6 暂停：{record.error}。可在前端选择重试、重新配置第一模型后重试，或取消任务。",
        )
        self._cleanup_record(record)
        self._add_log(record, "info", "阶段 6 暂停后已清理全部临时音频；重试时会重新下载")

    async def _sleep_or_cancel(self, record: TaskRecord, delay_seconds: float) -> None:
        try:
            await asyncio.wait_for(record.cancel_event.wait(), timeout=delay_seconds)
        except TimeoutError:
            return
        raise asyncio.CancelledError

    async def _raise_if_cancelled(self, record: TaskRecord) -> None:
        if record.cancel_event.is_set():
            raise asyncio.CancelledError

    def _cleanup_record(self, record: TaskRecord) -> None:
        if not record.temp_dir:
            return

        try:
            self._temp_dirs.cleanup(record.task_id)
            self._add_log(record, "info", "任务临时目录已清理")
        except Exception as exc:  # pragma: no cover - filesystem edge case
            self._add_log(record, "warning", f"临时目录清理失败：{exc}")
        else:
            record.temp_dir = None
        finally:
            record.audio_parts = []

    def _get_record_locked(self, task_id: str) -> TaskRecord:
        record = self._tasks.get(task_id)
        if record is None:
            raise TaskNotFoundError(task_id)
        return record

    def _add_log(self, record: TaskRecord, level: str, message: object) -> None:
        safe_message = redact_secrets(message, record.secret_values)
        record.logs.append(
            TaskLogEntry(
                time=utc_now_iso(),
                level=level,  # type: ignore[arg-type]
                message=safe_message,
            ),
        )
        record.logs = record.logs[-500:]
        log_level = getattr(logging, str(level).upper(), logging.INFO)
        TASK_LOGGER.log(log_level, "[task:%s] %s", record.task_id, safe_message)

    def _clear_secrets(self, record: TaskRecord) -> None:
        record.secret_values = []
        record.retry_bilibili_cookie_header = ""
        record.retry_bilibili_cookies_file_content = ""
        record.options.bilibili_cookie_header = ""
        record.options.bilibili_cookies_file_content = ""
        record.transcription_model_config.api_key = ""
        record.refine_model_config.api_key = ""

    def _prepare_bilibili_access_config(self, record: TaskRecord) -> BilibiliAccessConfig:
        mode = self._coerce_access_mode(record.options.bilibili_access_mode)
        browser = self._coerce_browser(record.options.bilibili_cookie_browser)
        cookies_file_path = None
        cookie_source = record.retry_bilibili_cookie_header or record.options.bilibili_cookie_header
        cookies_source = record.retry_bilibili_cookies_file_content or record.options.bilibili_cookies_file_content
        cookie_header = simplify_bilibili_cookie_header(cookie_source)
        cookies_content = cookies_source.strip()
        record.retry_bilibili_cookie_header = cookie_header
        record.retry_bilibili_cookies_file_content = cookies_content
        record.options.bilibili_cookie_header = ""
        record.options.bilibili_cookies_file_content = ""

        if mode == BilibiliAccessMode.COOKIE_HEADER:
            if not cookie_header:
                raise BilibiliError(
                    BilibiliErrorCode.COOKIE_INVALID,
                    "当前默认使用精简 B站 Cookie，但本轮没有收到可用凭据。请先按新手引导打开本地 B站登录窗口刷新 Cookie。",
                )
            else:
                self._add_log(record, "info", "已接收精简 B站 Cookie；后端仅在本机当前任务中使用，不写入日志或后端持久文件")

        if mode == BilibiliAccessMode.COOKIES_FILE:
            if not cookies_content:
                raise BilibiliError(
                    BilibiliErrorCode.COOKIE_INVALID,
                    "已选择 cookies.txt 导入模式，但没有选择 cookies.txt 文件。",
                )
            if not record.temp_dir:
                raise BilibiliError(BilibiliErrorCode.COOKIE_INVALID, "任务临时目录未就绪，无法使用 cookies.txt")
            cookies_file_path = record.temp_dir / "bilibili-cookies.txt"
            cookies_file_path.write_text(cookies_content, encoding="utf-8")
            self._add_log(record, "info", "已接收 cookies.txt，仅写入本次任务临时目录，任务结束后清理")

        return BilibiliAccessConfig(
            mode=mode,
            browser=browser,
            cookie_header=cookie_header,
            cookies_file_path=cookies_file_path,
        )

    def _coerce_access_mode(self, value: str) -> BilibiliAccessMode:
        deprecated_user_modes = {
            "anonymous",
            "enhanced_headers",
            "bilibili_api",
            "impersonate",
            "browser_cookie",
            "cookies_file",
        }
        if str(value or "").strip() in deprecated_user_modes:
            return BilibiliAccessMode.COOKIE_HEADER
        try:
            return BilibiliAccessMode(str(value or "auto"))
        except ValueError:
            return BilibiliAccessMode.COOKIE_HEADER

    def _coerce_browser(self, value: str) -> BrowserCookieSource:
        try:
            return BrowserCookieSource(str(value or "chrome"))
        except ValueError:
            return BrowserCookieSource.CHROME

    def _access_config_label(self, config: BilibiliAccessConfig) -> str:
        if config.mode == BilibiliAccessMode.AUTO:
            return "自动（尝试 B站 API、播放器 API、WBI、HTML 回退、yt-dlp 字幕路径；失败后再尝试本地 Profile）"
        if config.mode == BilibiliAccessMode.BROWSER_COOKIE:
            return f"浏览器 Cookie（{self._browser_label(config.browser)}，优先使用凭据路径）"
        if config.mode == BilibiliAccessMode.COOKIES_FILE:
            return "cookies.txt 导入（优先使用凭据路径，Cookie 文件仅用于本次任务）"
        if config.mode == BilibiliAccessMode.COOKIE_HEADER:
            return "精简 Cookie（前端保存到 IndexedDB，后端仅本机任务使用）"
        if config.mode == BilibiliAccessMode.LOCAL_BROWSER_PROFILE:
            return "本地专用浏览器 Profile（优先使用登录态路径，登录态仅保存在本机）"
        labels = {
            BilibiliAccessMode.ANONYMOUS: "标准匿名请求",
            BilibiliAccessMode.ENHANCED_HEADERS: "增强请求头",
            BilibiliAccessMode.BILIBILI_API: "B站公开 API",
            BilibiliAccessMode.IMPERSONATE: "Chrome 指纹模拟",
        }
        return labels.get(config.mode, config.mode.value)

    def _log_access_attempts(self, record: TaskRecord, attempts: list[AccessAttempt]) -> None:
        for attempt in attempts:
            if attempt.success:
                self._add_log(record, "info", f"B站访问策略成功：{attempt.label}")
            else:
                self._add_log(record, "warning", f"B站访问策略失败：{attempt.label}；{attempt.message}")

    def _to_response(self, record: TaskRecord) -> TaskStatusResponse:
        stage = record.stage
        progress = record.progress
        current_item = record.current_item

        if record.status == TaskStatus.COMPLETED:
            stage = TaskStage.COMPLETED
            progress = 100
            current_item = None
        elif record.status in {TaskStatus.CANCELED, TaskStatus.FAILED, TaskStatus.ABANDONED}:
            stage = TaskStage.CLEANUP_TEMP
            current_item = None

        return TaskStatusResponse(
            task_id=record.task_id,
            status=record.status,
            stage=stage,
            progress=progress,
            logs=[log.model_copy() for log in record.logs],
            result=record.result.model_copy() if record.result else None,
            error=record.error,
            error_code=record.error_code,
            current_item=current_item,
        )

    def _log_video_info(self, record: TaskRecord, video_info: BilibiliVideoInfo) -> None:
        duration = self._format_duration(video_info.duration_seconds)
        self._add_log(
            record,
            "info",
            (
                f"视频信息获取完成：标题={video_info.title}，BV={video_info.bv_id or '未知'}，"
                f"P={video_info.p_index}，时长={duration}"
            ),
        )
        self._log_subtitle_debug(record, f"viewTitle={video_info.title}")
        self._log_subtitle_debug(record, f"aid={video_info.raw_info.get('aid') or '未知'}")
        self._log_subtitle_debug(record, f"pages={self._format_pages_debug(video_info)}")
        self._log_subtitle_debug(record, f"selectedCid={video_info.raw_info.get('cid') or '未知'}")
        self._log_subtitle_debug(record, f"selectedPart={self._selected_part_title(video_info)}")
        part_count = len(video_info.parts)
        self._add_log(record, "info", f"识别到 {part_count} 个分P或子任务候选")
        if part_count > 1:
            self._add_log(record, "warning", "本阶段仅处理当前分P；多P子任务结构已保留，后续阶段继续接入")

    def _log_subtitle_debug(self, record: TaskRecord, message: str) -> None:
        self._add_log(record, "info", f"字幕下载DEBUG：{message}")

    def _log_subtitle_candidates(self, record: TaskRecord, video_info: BilibiliVideoInfo) -> None:
        candidates = video_info.subtitle_candidates
        self._log_subtitle_debug(record, f"subtitleList.count={len(candidates)}")
        for index, candidate in enumerate(candidates, start=1):
            self._log_subtitle_debug(
                record,
                (
                    f"subtitleList[{index}]: "
                    f"lan={candidate.language or '未知'}，"
                    f"lan_doc={candidate.name or '未知'}，"
                    f"source={self._subtitle_source_label(candidate.source)}，"
                    f"subtitle_url={self._normalize_subtitle_url(candidate.url)}"
                ),
            )

    def _preview_text(self, value: object, limit: int = 200) -> str:
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        text = text.replace("\n", "\\n")
        if len(text) > limit:
            return f"{text[:limit]}..."
        return text or "空"

    def _normalize_subtitle_url(self, url: str) -> str:
        return f"https:{url}" if url.startswith("//") else url

    def _format_pages_debug(self, video_info: BilibiliVideoInfo) -> str:
        raw_pages = video_info.raw_info.get("pages")
        if isinstance(raw_pages, list) and raw_pages:
            rows = []
            for index, page in enumerate(raw_pages, start=1):
                if not isinstance(page, dict):
                    continue
                rows.append(
                    {
                        "page": page.get("page") or index,
                        "cid": page.get("cid") or "",
                        "part": page.get("part") or page.get("title") or f"P{index}",
                    },
                )
            return str(rows) if rows else "[]"

        rows = [
            {
                "page": part.p_index,
                "cid": video_info.raw_info.get("cid") if part.p_index == video_info.p_index else "",
                "part": part.title,
            }
            for part in video_info.parts
        ]
        return str(rows)

    def _selected_part_title(self, video_info: BilibiliVideoInfo) -> str:
        for part in video_info.parts:
            if part.p_index == video_info.p_index:
                return part.title
        return video_info.title

    def _log_audio_parts(self, record: TaskRecord, audio_parts: list[object]) -> None:
        part_count = len(audio_parts)
        self._add_log(record, "info", f"音频切片处理完成：共 {part_count} 段")
        for part in audio_parts:
            start = self._format_duration(getattr(part, "start_seconds", None))
            end = self._format_duration(getattr(part, "end_seconds", None))
            duration = self._format_duration(getattr(part, "duration_seconds", None))
            overlap = self._format_duration(getattr(part, "overlap_seconds", None))
            filename = getattr(part, "filename", "part_unknown.mp3")
            self._add_log(
                record,
                "info",
                f"切片 {filename}：开始={start}，结束={end}，时长={duration}，与上一段重合={overlap}",
            )

    def _build_video_metadata_result(
        self,
        parsed_input: str,
        video_info: BilibiliVideoInfo,
    ) -> TaskResult:
        return TaskResult(
            title=video_info.title,
            bv_id=video_info.bv_id,
            p_index=video_info.p_index,
            parsed_input=parsed_input,
            webpage_url=video_info.webpage_url,
            duration_seconds=video_info.duration_seconds,
            sub_tasks=[self._part_to_dict(part) for part in video_info.parts],
        )

    def _build_stage5_result(
        self,
        parsed_input: str,
        video_info: BilibiliVideoInfo,
        clean_subtitle: str,
        subtitle_language: str,
        subtitle_source: str,
        mp3_duration_seconds: float,
        audio_parts: list[dict[str, object]],
    ) -> TaskResult:
        subtitle_summary = "已获取并清理 B站自带字幕" if clean_subtitle else "已按用户选择跳过字幕"
        audio_summary = "未切片，登记为单段 MP3" if len(audio_parts) == 1 else f"已切为 {len(audio_parts)} 段 MP3"
        part_lines = "\n".join(
            (
                f"- {part.get('filename')}: "
                f"{self._format_duration(part.get('startSeconds'))}-"
                f"{self._format_duration(part.get('endSeconds'))}，"
                f"时长 {self._format_duration(part.get('durationSeconds'))}，"
                f"重合 {self._format_duration(part.get('overlapSeconds'))}"
            )
            for part in audio_parts
        )
        final_markdown = (
            "### 阶段 5 结果\n\n"
            f"- 视频标题：{video_info.title}\n"
            f"- BV 号：{video_info.bv_id or '未知'}\n"
            f"- 当前分P：P{video_info.p_index}\n"
            f"- 视频时长：{self._format_duration(video_info.duration_seconds)}\n"
            f"- MP3 校验时长：{self._format_duration(mp3_duration_seconds)}\n"
            f"- 字幕状态：{subtitle_summary}\n"
            f"- 音频状态：{audio_summary}\n\n"
            "#### 音频片段\n\n"
            f"{part_lines or '- 暂无片段'}\n\n"
            "本轮已完成 playurl 主路径音频下载、MP3 转换和切片文件准备。"
            "阶段 6 将在同一任务中继续消费这些临时音频片段。"
        )
        return TaskResult(
            title=video_info.title,
            bv_id=video_info.bv_id,
            p_index=video_info.p_index,
            parsed_input=parsed_input,
            webpage_url=video_info.webpage_url,
            duration_seconds=video_info.duration_seconds,
            subtitle_language=subtitle_language,
            subtitle_source=subtitle_source,
            final_markdown=final_markdown,
            clean_subtitle=clean_subtitle or "用户已选择跳过字幕，后续阶段将仅依赖 AI 音频转文字稿。",
            ai_transcript="阶段 5 已准备 MP3 或 MP3 切片，等待阶段 6 音频转文字。",
            filename="stage-5-audio-processing-result.md",
            audio_parts=audio_parts,
            sub_tasks=[self._part_to_dict(part) for part in video_info.parts],
        )

    def _build_stage4_result(
        self,
        parsed_input: str,
        video_info: BilibiliVideoInfo,
        clean_subtitle: str,
        subtitle_language: str,
        subtitle_source: str,
    ) -> TaskResult:
        subtitle_summary = "已获取并清理 B站自带字幕" if clean_subtitle else "已按用户选择跳过字幕"
        final_markdown = (
            "### 阶段 4 结果\n\n"
            f"- 视频标题：{video_info.title}\n"
            f"- BV 号：{video_info.bv_id or '未知'}\n"
            f"- 当前分P：P{video_info.p_index}\n"
            f"- 视频时长：{self._format_duration(video_info.duration_seconds)}\n"
            f"- 字幕状态：{subtitle_summary}\n\n"
            "本轮已完成 B站输入解析、视频信息获取、字幕获取与字幕清理。"
            "音频下载、MP3 转换、切片和模型转写属于后续阶段，本阶段未提前实现。"
        )
        return TaskResult(
            title=video_info.title,
            bv_id=video_info.bv_id,
            p_index=video_info.p_index,
            parsed_input=parsed_input,
            webpage_url=video_info.webpage_url,
            duration_seconds=video_info.duration_seconds,
            subtitle_language=subtitle_language,
            subtitle_source=subtitle_source,
            final_markdown=final_markdown,
            clean_subtitle=clean_subtitle or "用户已选择跳过字幕，后续阶段将仅依赖 AI 音频转文字稿。",
            ai_transcript="阶段 4 未执行音频转文字，阶段 6 将接入。",
            filename="stage-4-bilibili-subtitle-result.md",
            sub_tasks=[self._part_to_dict(part) for part in video_info.parts],
        )

    def _part_to_dict(self, part: PartInfo) -> dict:
        return {
            "id": f"p{part.p_index}",
            "title": part.title,
            "pIndex": part.p_index,
            "durationSeconds": part.duration_seconds,
            "bvId": part.bv_id,
            "url": part.url,
        }

    def _format_duration(self, duration_seconds: float | None) -> str:
        if duration_seconds is None:
            return "未知"
        total_seconds = max(0, int(duration_seconds))
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    def _subtitle_source_label(self, source: str) -> str:
        labels = {
            "uploaded": "UP 主上传字幕",
            "automatic": "B站自动字幕",
            "requested": "yt-dlp 选中字幕",
            "player_api": "B站播放器字幕接口",
            "player_wbi_api": "B站播放器 wbi 字幕接口",
            "player_wbi_api_ep": "B站播放器 wbi 字幕接口（ep_id）",
            "player_wbi_api_profile": "B站播放器 wbi 字幕接口（页面内 fetch）",
            "player_wbi_api_ep_profile": "B站播放器 wbi 字幕接口（ep_id 页面内 fetch）",
            "player_wbi_signed_api": "WBI 签名播放器字幕接口",
            "html_initial_state": "HTML 初始化数据回退",
            "yt_dlp_subtitle": "yt-dlp 字幕路径",
        }
        return labels.get(source, source or "未知字幕")

    def _browser_label(self, browser: BrowserCookieSource) -> str:
        labels = {
            BrowserCookieSource.CHROME: "Chrome",
            BrowserCookieSource.EDGE: "Edge",
            BrowserCookieSource.FIREFOX: "Firefox",
        }
        return labels.get(browser, browser.value)
