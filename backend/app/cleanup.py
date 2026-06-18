"""Temporary directory management."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


class TaskTempDirManager:
    """Create and clean task-scoped temporary directories."""

    def __init__(self, root: Path | None = None) -> None:
        configured_root = os.getenv("TEMP_DIR")
        default_root = Path(tempfile.gettempdir()) / "bilibili-transcription"
        base_root = root or (Path(configured_root) if configured_root else default_root)
        self.root = base_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, task_id: str) -> Path:
        task_dir = self._task_path(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir

    def cleanup(self, task_id: str) -> None:
        task_dir = self._task_path(task_id)
        if task_dir.exists():
            shutil.rmtree(task_dir)

    def _task_path(self, task_id: str) -> Path:
        task_dir = (self.root / task_id).resolve()
        if self.root not in task_dir.parents:
            raise ValueError("任务临时目录越界")
        return task_dir
