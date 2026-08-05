"""Local media upload validation and workflow tests."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import task_manager as task_manager_module
from backend.app.audio import AudioPart, AudioProcessingError, _require_binary, _run_subprocess
from backend.app.cleanup import TaskTempDirManager
from backend.app.local_media import (
    LocalMediaValidationError,
    classify_local_media,
    sanitize_local_media_filename,
    store_local_media_upload,
)
from backend.app.llm_client import TranscriptionProcessingError
from backend.app.models import ModelConfig, TaskOptions, TaskStatus
from backend.app.prompts import LOCAL_UPLOAD_NO_SUBTITLE_PLACEHOLDER
from backend.app.task_manager import TaskManager, TaskRecord


class FakeUpload:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    async def read(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    async def close(self) -> None:
        self.closed = True


class LocalMediaValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_common_video_and_audio_extensions_are_supported(self) -> None:
        for filename in ("demo.mp4", "demo.mkv", "demo.mov", "voice.mp3", "voice.flac", "voice.opus"):
            expected = "video" if filename.rsplit(".", 1)[-1] in {"mp4", "mkv", "mov"} else "audio"
            self.assertEqual(classify_local_media(filename), expected)

    def test_m4s_supports_audio_video_and_unknown_mime_types(self) -> None:
        self.assertEqual(classify_local_media("audio.m4s", "audio/mp4"), "audio")
        self.assertEqual(classify_local_media("video.m4s", "video/iso.segment"), "video")
        self.assertEqual(classify_local_media("download.m4s", "application/octet-stream"), "media")

    def test_media_mime_type_supports_extensionless_files(self) -> None:
        self.assertEqual(classify_local_media("recording", "audio/wav"), "audio")
        self.assertEqual(classify_local_media("camera-export", "video/mp4"), "video")

    def test_non_media_upload_is_rejected(self) -> None:
        with self.assertRaises(LocalMediaValidationError):
            classify_local_media("notes.txt", "text/plain")

    def test_filename_is_reduced_to_a_safe_local_name(self) -> None:
        self.assertEqual(
            sanitize_local_media_filename("../../课程：第一讲?.mp4", "video"),
            "课程：第一讲.mp4",
        )

    async def test_upload_is_streamed_and_empty_upload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "upload" / "voice.wav"
            upload = FakeUpload([b"abc", b"def"])
            written = await store_local_media_upload(upload, output_path)
            self.assertEqual(written, 6)
            self.assertEqual(output_path.read_bytes(), b"abcdef")
            self.assertTrue(upload.closed)

            empty_path = Path(temp_dir) / "empty.wav"
            with self.assertRaises(LocalMediaValidationError):
                await store_local_media_upload(FakeUpload([]), empty_path)
            self.assertFalse(empty_path.exists())

    def test_imageio_ffmpeg_binary_is_used_when_command_name_is_missing(self) -> None:
        with patch("backend.app.audio.shutil.which", return_value=None):
            executable = _require_binary("ffmpeg", "ffmpeg_unavailable", "FFmpeg 不可用")

        self.assertTrue(Path(executable).is_file())

    def test_ffmpeg_missing_audio_stream_has_stable_error_code(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ffmpeg"],
            returncode=1,
            stdout="",
            stderr="Stream map '0:a:0' matches no streams. Output file does not contain any stream",
        )
        with patch("backend.app.audio.subprocess.run", return_value=completed):
            with self.assertRaises(AudioProcessingError) as captured:
                _run_subprocess(
                    ["ffmpeg"],
                    60,
                    "mp3_conversion_failed",
                    "MP3 转换失败",
                )

        self.assertEqual(captured.exception.code, "audio_stream_missing")
        self.assertIn("未检测到可用的音频轨", captured.exception.message)


class LocalMediaWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_upload_skips_bilibili_and_prepares_mp3_parts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = TaskManager(TaskTempDirManager(Path(temp_dir) / "tasks"))
            record = TaskRecord(
                task_id="local-task",
                original_input="访谈.mp4",
                options=TaskOptions(),
                transcription_model_config=ModelConfig(),
                refine_model_config=ModelConfig(),
                secret_values=[],
                source_type="local_upload",
                local_media_kind="video",
                local_media_filename="访谈.mp4",
            )
            record.temp_dir = manager._temp_dirs.create(record.task_id)
            record.local_media_path = record.temp_dir / "local-upload" / "访谈.mp4"
            record.local_media_path.parent.mkdir(parents=True)
            record.local_media_path.write_bytes(b"video")

            async def fake_convert(_input_path: Path, output_path: Path, _duration=None) -> float:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"mp3")
                return 61.0

            async def fake_split(
                _mp3_path: Path,
                output_dir: Path,
                _duration: float,
                **_options,
            ) -> list[AudioPart]:
                part_path = output_dir / "part_001.mp3"
                part_path.parent.mkdir(parents=True, exist_ok=True)
                part_path.write_bytes(b"mp3")
                return [AudioPart(1, part_path.name, part_path, 0, 61, 61, 0)]

            with (
                patch.object(task_manager_module, "convert_audio_to_mp3", side_effect=fake_convert),
                patch.object(task_manager_module, "split_mp3_by_rule", side_effect=fake_split),
            ):
                await manager._run_local_media_workflow(record)

            self.assertEqual(record.progress, 20)
            self.assertEqual(len(record.audio_parts), 1)
            self.assertIsNotNone(record.result)
            assert record.result is not None
            self.assertEqual(record.result.source_type, "local_upload")
            self.assertEqual(record.result.clean_subtitle, LOCAL_UPLOAD_NO_SUBTITLE_PLACEHOLDER)
            self.assertEqual(record.result.subtitle_source, "local_upload_placeholder")
            self.assertIn("跳过字幕提取", record.result.final_markdown)

    async def test_local_files_without_audio_have_the_same_actionable_chinese_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = TaskManager(TaskTempDirManager(Path(temp_dir) / "tasks"))
            with patch.object(
                task_manager_module,
                "convert_audio_to_mp3",
                side_effect=AudioProcessingError(
                    "audio_stream_missing",
                    "输入文件中未检测到可用的音频轨，无法转换为 MP3。",
                ),
            ):
                for filename, media_kind in (
                    ("video-only.m4s", "media"),
                    ("video-only.mp4", "video"),
                    ("video-only.mkv", "video"),
                ):
                    with self.subTest(filename=filename):
                        record = TaskRecord(
                            task_id=f"video-only-{Path(filename).suffix.lstrip('.')}",
                            original_input=filename,
                            options=TaskOptions(),
                            transcription_model_config=ModelConfig(),
                            refine_model_config=ModelConfig(),
                            secret_values=[],
                            source_type="local_upload",
                            local_media_kind=media_kind,
                            local_media_filename=filename,
                        )
                        record.temp_dir = manager._temp_dirs.create(record.task_id)
                        record.local_media_path = record.temp_dir / "local-upload" / filename
                        record.local_media_path.parent.mkdir(parents=True)
                        record.local_media_path.write_bytes(b"video-only")

                        with self.assertRaises(AudioProcessingError) as captured:
                            await manager._run_local_media_workflow(record)

                        self.assertEqual(captured.exception.code, "local_upload_audio_stream_missing")
                        self.assertEqual(
                            captured.exception.message,
                            "FFmpeg 已成功读取文件，但其中没有可用的音频轨，无法进行转文字处理。请检查上传的文件是否包含音频轨。",
                        )

    def test_local_transcription_pause_preserves_upload_for_direct_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = TaskManager(TaskTempDirManager(Path(temp_dir) / "tasks"))
            record = TaskRecord(
                task_id="local-retry",
                original_input="访谈.wav",
                options=TaskOptions(),
                transcription_model_config=ModelConfig(),
                refine_model_config=ModelConfig(),
                secret_values=[],
                source_type="local_upload",
            )
            record.temp_dir = manager._temp_dirs.create(record.task_id)
            part_path = record.temp_dir / "stage5" / "parts" / "part_001.mp3"
            part_path.parent.mkdir(parents=True)
            part_path.write_bytes(b"mp3")
            record.audio_parts = [AudioPart(1, part_path.name, part_path, 0, 1, 1, 0)]

            manager._pause_stage6_for_retry(
                record,
                TranscriptionProcessingError("model_failed", "model failed"),
            )

            self.assertEqual(record.status, TaskStatus.WAITING_MODEL_RETRY)
            self.assertIsNotNone(record.temp_dir)
            self.assertEqual(len(record.audio_parts), 1)
            self.assertTrue(part_path.exists())
            manager._cleanup_record(record)

