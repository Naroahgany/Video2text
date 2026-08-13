"""Task artifact cleanup lifecycle tests."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import MethodType

from backend.app.audio import AudioPart
from backend.app.cleanup import TaskTempDirManager
from backend.app.llm_client import RefineProcessingError, TranscriptionProcessingError
from backend.app.models import (
    ModelConfig,
    RefineRetryRequest,
    TaskOptions,
    TaskResult,
    TaskStage,
    TaskStatus,
    TranscriptionRetryRequest,
)
from backend.app.task_manager import TaskManager, TaskRecord


def build_record(task_id: str) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        original_input="BV1test",
        options=TaskOptions(),
        transcription_model_config=ModelConfig(api_key="transcription-secret"),
        refine_model_config=ModelConfig(api_key="refine-secret"),
        secret_values=["transcription-secret", "refine-secret"],
        retry_bilibili_cookie_header="SESSDATA=test",
    )


class TaskTempDirManagerTests(unittest.TestCase):
    def test_cleanup_removes_task_and_empty_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "tasks"
            manager = TaskTempDirManager(root)
            task_dir = manager.create("task-1")
            (task_dir / "audio.mp3").write_bytes(b"audio")

            manager.cleanup("task-1")

            self.assertFalse(task_dir.exists())
            self.assertFalse(root.exists())

    def test_cleanup_all_removes_stale_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "tasks"
            manager = TaskTempDirManager(root)
            first = manager.create("task-1")
            second = manager.create("task-2")
            (first / "download.m4s").write_bytes(b"first")
            (second / "part.mp3").write_bytes(b"second")
            (root / "orphan.tmp").write_text("orphan", encoding="utf-8")

            manager.cleanup_all()

            self.assertFalse(root.exists())


class TaskManagerCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcription_pause_preserves_audio_and_previous_results_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "tasks"
            manager = TaskManager(TaskTempDirManager(root))
            record = build_record("task-pause")
            record.temp_dir = manager._temp_dirs.create(record.task_id)
            part_path = record.temp_dir / "stage5" / "parts" / "part_001.mp3"
            part_path.parent.mkdir(parents=True)
            part_path.write_bytes(b"audio")
            record.audio_parts = [AudioPart(1, part_path.name, part_path, 0, 1, 1, 0)]
            record.result = TaskResult(clean_subtitle="已完成的字幕识别结果")

            manager._pause_stage6_for_retry(
                record,
                TranscriptionProcessingError("model_failed", "model failed"),
            )

            self.assertEqual(record.status, TaskStatus.WAITING_MODEL_RETRY)
            self.assertEqual(record.stage, TaskStage.TRANSCRIBE_AUDIO)
            self.assertIsNotNone(record.temp_dir)
            self.assertEqual(len(record.audio_parts), 1)
            self.assertTrue(part_path.exists())
            self.assertEqual(record.result.clean_subtitle, "已完成的字幕识别结果")
            self.assertEqual(record.retry_bilibili_cookie_header, "SESSDATA=test")

    async def test_refine_pause_preserves_subtitle_and_transcript_but_cleans_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "tasks"
            manager = TaskManager(TaskTempDirManager(root))
            record = build_record("task-refine-pause")
            record.temp_dir = manager._temp_dirs.create(record.task_id)
            part_path = record.temp_dir / "stage5" / "parts" / "part_001.mp3"
            part_path.parent.mkdir(parents=True)
            part_path.write_bytes(b"audio")
            record.audio_parts = [AudioPart(1, part_path.name, part_path, 0, 1, 1, 0)]
            record.result = TaskResult(
                clean_subtitle="已完成的字幕识别结果",
                ai_transcript="已完成的音频转文字结果",
            )

            manager._pause_stage7_for_retry(
                record,
                RefineProcessingError("refine_api_error", "文稿优化模型暂时不可用"),
            )

            self.assertEqual(record.status, TaskStatus.WAITING_MODEL_RETRY)
            self.assertEqual(record.stage, TaskStage.REFINE_MARKDOWN)
            self.assertEqual(record.result.clean_subtitle, "已完成的字幕识别结果")
            self.assertEqual(record.result.ai_transcript, "已完成的音频转文字结果")
            self.assertIsNone(record.temp_dir)
            self.assertEqual(record.audio_parts, [])
            self.assertFalse(root.exists())
            self.assertEqual(record.secret_values, [])

    async def test_model_retry_endpoints_do_not_cross_stage_boundaries(self) -> None:
        manager = TaskManager()
        transcription_record = build_record("task-stage6-boundary")
        transcription_record.status = TaskStatus.WAITING_MODEL_RETRY
        transcription_record.stage = TaskStage.TRANSCRIBE_AUDIO
        refine_record = build_record("task-stage7-boundary")
        refine_record.status = TaskStatus.WAITING_MODEL_RETRY
        refine_record.stage = TaskStage.REFINE_MARKDOWN
        manager._tasks = {
            transcription_record.task_id: transcription_record,
            refine_record.task_id: refine_record,
        }

        transcription_response = await manager.retry_refine(
            transcription_record.task_id,
            RefineRetryRequest(refine_model_config=ModelConfig(api_key="new-refine-secret")),
        )
        refine_response = await manager.retry_transcription(
            refine_record.task_id,
            TranscriptionRetryRequest(transcription_model_config=ModelConfig(api_key="new-audio-secret")),
        )

        self.assertEqual(transcription_response.status, TaskStatus.WAITING_MODEL_RETRY)
        self.assertEqual(refine_response.status, TaskStatus.WAITING_MODEL_RETRY)
        self.assertIsNone(transcription_record.worker_task)
        self.assertIsNone(refine_record.worker_task)

    async def test_cancel_waits_for_worker_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "tasks"
            manager = TaskManager(TaskTempDirManager(root))
            record = build_record("task-cancel")
            record.status = TaskStatus.RUNNING
            record.temp_dir = manager._temp_dirs.create(record.task_id)
            task_dir = record.temp_dir
            manager._tasks[record.task_id] = record

            async def worker() -> None:
                try:
                    await asyncio.Event().wait()
                finally:
                    (task_dir / "worker-finalized.tmp").write_text("done", encoding="utf-8")

            record.worker_task = asyncio.create_task(worker())
            await asyncio.sleep(0)

            response = await manager.cancel_task(record.task_id)

            self.assertEqual(response.status, TaskStatus.CANCELED)
            self.assertTrue(record.worker_task.done())
            self.assertFalse(root.exists())
            self.assertEqual(record.secret_values, [])
            self.assertEqual(record.retry_bilibili_cookie_header, "")

    async def test_abandoned_sweep_waits_then_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "tasks"
            manager = TaskManager(TaskTempDirManager(root))
            manager.abandoned_after_seconds = 60
            record = build_record("task-abandoned")
            record.status = TaskStatus.RUNNING
            record.last_polled_at = time.monotonic() - 61
            record.temp_dir = manager._temp_dirs.create(record.task_id)
            manager._tasks[record.task_id] = record

            async def worker() -> None:
                try:
                    await asyncio.Event().wait()
                finally:
                    assert record.temp_dir is not None
                    (record.temp_dir / "worker-finalized.tmp").write_text("done", encoding="utf-8")

            record.worker_task = asyncio.create_task(worker())
            await asyncio.sleep(0)

            await manager.sweep_abandoned_tasks()

            self.assertEqual(record.status, TaskStatus.ABANDONED)
            self.assertTrue(record.worker_task.done())
            self.assertFalse(root.exists())

    async def test_transcription_retry_reuses_audio_without_rerunning_stage4(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "tasks"
            manager = TaskManager(TaskTempDirManager(root))
            record = build_record("task-retry")
            record.status = TaskStatus.RUNNING
            record.temp_dir = manager._temp_dirs.create(record.task_id)
            part_path = record.temp_dir / "stage5" / "parts" / "part_001.mp3"
            part_path.parent.mkdir(parents=True)
            part_path.write_bytes(b"audio")
            record.audio_parts = [AudioPart(1, part_path.name, part_path, 0, 1, 1, 0)]
            manager._tasks[record.task_id] = record
            calls: list[str] = []

            async def fake_stage4(_manager: TaskManager, current: TaskRecord) -> None:
                self.fail("音频转文字重做不应重新执行阶段4")

            async def fake_stage6(_manager: TaskManager, current: TaskRecord) -> None:
                calls.append("stage6")
                self.assertTrue(current.audio_parts)

            async def fake_stage7(self: TaskManager, current: TaskRecord) -> None:
                calls.append("stage7")

            manager._run_stage4_workflow = MethodType(fake_stage4, manager)
            manager._run_stage6_workflow = MethodType(fake_stage6, manager)
            manager._run_stage7_workflow = MethodType(fake_stage7, manager)

            await manager._resume_stage6_task(record.task_id)

            self.assertEqual(calls, ["stage6", "stage7"])
            self.assertEqual(record.status, TaskStatus.COMPLETED)
            self.assertFalse(root.exists())

    async def test_refine_retry_runs_only_stage7_with_preserved_text_results(self) -> None:
        manager = TaskManager()
        record = build_record("task-refine-retry")
        record.status = TaskStatus.RUNNING
        record.stage = TaskStage.REFINE_MARKDOWN
        record.result = TaskResult(
            clean_subtitle="已完成的字幕识别结果",
            ai_transcript="已完成的音频转文字结果",
        )
        manager._tasks[record.task_id] = record
        calls: list[str] = []

        async def fail_stage6(_manager: TaskManager, _current: TaskRecord) -> None:
            self.fail("文稿优化重做不应重新执行阶段6")

        async def fake_stage7(_manager: TaskManager, current: TaskRecord) -> None:
            calls.append("stage7")
            self.assertEqual(current.result.clean_subtitle, "已完成的字幕识别结果")
            self.assertEqual(current.result.ai_transcript, "已完成的音频转文字结果")
            current.result.final_markdown = "# 重做完成"

        manager._run_stage6_workflow = MethodType(fail_stage6, manager)
        manager._run_stage7_workflow = MethodType(fake_stage7, manager)

        await manager._resume_stage7_task(record.task_id)

        self.assertEqual(calls, ["stage7"])
        self.assertEqual(record.result.final_markdown, "# 重做完成")
        self.assertEqual(record.status, TaskStatus.COMPLETED)

    async def test_shutdown_cleans_active_and_stale_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "tasks"
            manager = TaskManager(TaskTempDirManager(root))
            record = build_record("task-shutdown")
            record.status = TaskStatus.WAITING_MODEL_RETRY
            record.temp_dir = manager._temp_dirs.create(record.task_id)
            (record.temp_dir / "part.mp3").write_bytes(b"audio")
            manager._tasks[record.task_id] = record
            stale_dir = root / "stale-task"
            stale_dir.mkdir()
            (stale_dir / "download.m4s").write_bytes(b"stale")

            await manager.shutdown()

            self.assertEqual(record.status, TaskStatus.CANCELED)
            self.assertFalse(root.exists())
