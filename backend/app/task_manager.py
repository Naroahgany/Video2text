"""In-memory task lifecycle management for stage 3."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .bilibili import (
    BilibiliError,
    BilibiliErrorCode,
    BilibiliVideoInfo,
    PartInfo,
    choose_subtitle_candidate,
    download_subtitle,
    fetch_video_info,
    parse_bilibili_input,
)
from .cleanup import TaskTempDirManager
from .models import (
    TaskCreateRequest,
    TaskLogEntry,
    TaskOptions,
    TaskResult,
    TaskStage,
    TaskStatus,
    TaskStatusResponse,
)
from .subtitle import SubtitleProcessingError, clean_subtitle
from .utils import redact_secrets, utc_now_iso


TERMINAL_STATUSES = {
    TaskStatus.FAILED,
    TaskStatus.COMPLETED,
    TaskStatus.CANCELED,
    TaskStatus.ABANDONED,
}


class TaskNotFoundError(KeyError):
    """Raised when a task_id does not exist."""


@dataclass
class TaskRecord:
    """Internal task state. API keys are kept only for in-memory redaction."""

    task_id: str
    original_input: str
    options: TaskOptions
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
    created_at: float = field(default_factory=time.monotonic)
    last_polled_at: float = field(default_factory=time.monotonic)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    worker_task: asyncio.Task | None = None


class TaskManager:
    """Manage task creation, polling, cancellation, logs and cleanup."""

    def __init__(self, temp_dirs: TaskTempDirManager | None = None) -> None:
        global_limit = int(os.getenv("GLOBAL_TASK_CONCURRENCY", "1"))
        self.global_concurrency = max(1, global_limit)
        self.audio_request_concurrency = max(1, int(os.getenv("AUDIO_REQUEST_CONCURRENCY", "2")))
        self.abandoned_after_seconds = max(
            60,
            int(os.getenv("TASK_ABANDONED_AFTER_SECONDS", "1800")),
        )
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = asyncio.Lock()
        self._global_semaphore = asyncio.Semaphore(self.global_concurrency)
        self._temp_dirs = temp_dirs or TaskTempDirManager()

    async def create_task(self, request: TaskCreateRequest) -> TaskStatusResponse:
        task_id = str(uuid.uuid4())
        options = request.options
        if options.max_audio_request_concurrency == 2:
            options.max_audio_request_concurrency = self.audio_request_concurrency

        record = TaskRecord(
            task_id=task_id,
            original_input=request.input,
            options=options,
            secret_values=[
                request.transcription_model_config.api_key,
                request.refine_model_config.api_key,
            ],
        )
        self._add_log(record, "info", "任务已创建，等待资源调度")
        self._add_log(record, "info", f"全局重任务并发上限：{self.global_concurrency}")
        self._add_log(
            record,
            "info",
            f"单任务音频片段请求并发上限：{record.options.max_audio_request_concurrency}",
        )

        async with self._lock:
            self._tasks[task_id] = record
            self._sweep_abandoned_locked()

        record.worker_task = asyncio.create_task(self._run_task(task_id))
        return self._to_response(record)

    async def get_task(self, task_id: str) -> TaskStatusResponse:
        async with self._lock:
            record = self._get_record_locked(task_id)
            record.last_polled_at = time.monotonic()
            self._sweep_abandoned_locked(exclude_task_id=task_id)
            return self._to_response(record)

    async def cancel_task(self, task_id: str) -> TaskStatusResponse:
        async with self._lock:
            record = self._get_record_locked(task_id)
            if record.status in TERMINAL_STATUSES:
                return self._to_response(record)

            record.cancel_event.set()
            self._add_log(record, "warning", "收到取消请求，正在停止任务")
            record.stage = TaskStage.CLEANUP_TEMP
            record.current_item = None
            if record.worker_task and not record.worker_task.done():
                record.worker_task.cancel()
            self._cleanup_record(record)
            self._clear_secrets(record)
            record.status = TaskStatus.CANCELED
            return self._to_response(record)

    async def _run_task(self, task_id: str) -> None:
        record = self._tasks[task_id]
        try:
            async with self._global_semaphore:
                await self._raise_if_cancelled(record)
                record.status = TaskStatus.RUNNING
                record.temp_dir = self._temp_dirs.create(record.task_id)
                self._add_log(record, "info", "已创建任务独立临时目录")

                await self._run_stage4_workflow(record)

                record.stage = TaskStage.CLEANUP_TEMP
                record.progress = 98
                self._add_log(record, "info", "开始清理临时文件")
                self._cleanup_record(record)

                record.stage = TaskStage.COMPLETED
                record.progress = 100
                record.current_item = None
                self._add_log(record, "info", "阶段 4：B站输入解析、视频信息与字幕处理已完成")
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
        except Exception as exc:  # pragma: no cover - defensive safety net
            record.stage = TaskStage.CLEANUP_TEMP
            record.error = redact_secrets(exc, record.secret_values)
            record.error_code = "internal_error"
            self._add_log(record, "error", f"任务失败：{record.error}")
            self._cleanup_record(record)
            self._clear_secrets(record)
            record.status = TaskStatus.FAILED

    async def _run_stage4_workflow(self, record: TaskRecord) -> None:
        await self._raise_if_cancelled(record)
        record.stage = TaskStage.PARSE_INPUT
        record.progress = 8
        record.current_item = "解析输入"
        self._add_log(record, "info", "开始解析用户输入")
        parsed = parse_bilibili_input(record.original_input)
        record.current_item = parsed.display
        self._add_log(record, "info", f"已识别 B站输入：{parsed.display}")

        await self._raise_if_cancelled(record)
        record.stage = TaskStage.FETCH_VIDEO_INFO
        record.progress = 16
        self._add_log(record, "info", "开始通过 yt-dlp 获取视频信息")
        video_info = await fetch_video_info(parsed)
        record.current_item = video_info.title
        self._log_video_info(record, video_info)

        await self._raise_if_cancelled(record)
        record.stage = TaskStage.FETCH_SUBTITLE
        record.progress = 24
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
            try:
                raw_subtitle = await download_subtitle(candidate)

                await self._raise_if_cancelled(record)
                record.stage = TaskStage.CLEAN_SUBTITLE
                record.progress = 32
                self._add_log(record, "info", "开始清理字幕正文")
                cleaned = clean_subtitle(raw_subtitle, candidate.ext)
                clean_text = cleaned.text
                self._add_log(
                    record,
                    "info",
                    f"字幕清理完成：格式 {cleaned.format}，正文 {cleaned.line_count} 行",
                )
            except (BilibiliError, SubtitleProcessingError) as exc:
                if not record.options.skip_subtitle_if_failed:
                    raise
                record.stage = TaskStage.CLEAN_SUBTITLE
                record.progress = 32
                subtitle_language = ""
                subtitle_source = ""
                self._add_log(record, "warning", f"字幕处理失败：{exc.message}；已按用户选择跳过字幕")

        await self._raise_if_cancelled(record)
        record.stage = TaskStage.DOWNLOAD_AUDIO
        record.progress = 42
        self._add_log(record, "info", "阶段 4 到此停止；音频下载、MP3 转换和切片将在阶段 5 实现")
        record.result = self._build_stage4_result(
            parsed_input=parsed.display,
            video_info=video_info,
            clean_subtitle=clean_text,
            subtitle_language=subtitle_language,
            subtitle_source=subtitle_source,
        )

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
        finally:
            record.temp_dir = None

    def _sweep_abandoned_locked(self, exclude_task_id: str | None = None) -> None:
        now = time.monotonic()
        for task_id, record in self._tasks.items():
            if task_id == exclude_task_id or record.status in TERMINAL_STATUSES:
                continue
            if now - record.last_polled_at < self.abandoned_after_seconds:
                continue

            record.cancel_event.set()
            record.stage = TaskStage.CLEANUP_TEMP
            record.error = "长时间没有前端轮询，任务已标记为 abandoned"
            self._add_log(record, "warning", record.error)
            if record.worker_task and not record.worker_task.done():
                record.worker_task.cancel()
            self._cleanup_record(record)
            self._clear_secrets(record)
            record.status = TaskStatus.ABANDONED

    def _get_record_locked(self, task_id: str) -> TaskRecord:
        record = self._tasks.get(task_id)
        if record is None:
            raise TaskNotFoundError(task_id)
        return record

    def _add_log(self, record: TaskRecord, level: str, message: object) -> None:
        record.logs.append(
            TaskLogEntry(
                time=utc_now_iso(),
                level=level,  # type: ignore[arg-type]
                message=redact_secrets(message, record.secret_values),
            ),
        )
        record.logs = record.logs[-500:]

    def _clear_secrets(self, record: TaskRecord) -> None:
        record.secret_values = []

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
        part_count = len(video_info.parts)
        self._add_log(record, "info", f"识别到 {part_count} 个分P或子任务候选")
        if part_count > 1:
            self._add_log(record, "warning", "本阶段仅处理当前分P；多P子任务结构已保留，后续阶段继续接入")

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
        return "UP 主上传字幕" if source == "uploaded" else "B站自动字幕"
