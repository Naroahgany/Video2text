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
from backend.app.llm_client import TranscriptionProcessingError
from backend.app.models import ModelConfig, TaskOptions, TaskStatus
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
    async def test_transcription_pause_cleans_audio_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "tasks"
            manager = TaskManager(TaskTempDirManager(root))
            record = build_record("task-pause")
            record.temp_dir = manager._temp_dirs.create(record.task_id)
            part_path = record.temp_dir / "stage5" / "parts" / "part_001.mp3"
            part_path.parent.mkdir(parents=True)
            part_path.write_bytes(b"audio")
            record.audio_parts = [AudioPart(1, part_path.name, part_path, 0, 1, 1, 0)]

            manager._pause_stage6_for_retry(
                record,
                TranscriptionProcessingError("model_failed", "model failed"),
            )

            self.assertEqual(record.status, TaskStatus.WAITING_MODEL_RETRY)
            self.assertIsNone(record.temp_dir)
            self.assertEqual(record.audio_parts, [])
            self.assertFalse(root.exists())
            self.assertEqual(record.retry_bilibili_cookie_header, "SESSDATA=test")

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

    async def test_retry_recreates_audio_then_cleans_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "tasks"
            manager = TaskManager(TaskTempDirManager(root))
            record = build_record("task-retry")
            record.status = TaskStatus.RUNNING
            manager._tasks[record.task_id] = record
            calls: list[str] = []

            async def fake_stage4(_manager: TaskManager, current: TaskRecord) -> None:
                calls.append("stage4")
                assert current.temp_dir is not None
                part_path = current.temp_dir / "stage5" / "parts" / "part_001.mp3"
                part_path.parent.mkdir(parents=True)
                part_path.write_bytes(b"audio")
                current.audio_parts = [AudioPart(1, part_path.name, part_path, 0, 1, 1, 0)]

            async def fake_stage6(_manager: TaskManager, current: TaskRecord) -> None:
                calls.append("stage6")
                self.assertTrue(current.audio_parts)

            async def fake_stage7(self: TaskManager, current: TaskRecord) -> None:
                calls.append("stage7")

            manager._run_stage4_workflow = MethodType(fake_stage4, manager)
            manager._run_stage6_workflow = MethodType(fake_stage6, manager)
            manager._run_stage7_workflow = MethodType(fake_stage7, manager)

            await manager._resume_stage6_task(record.task_id)

            self.assertEqual(calls, ["stage4", "stage6", "stage7"])
            self.assertEqual(record.status, TaskStatus.COMPLETED)
            self.assertFalse(root.exists())

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
