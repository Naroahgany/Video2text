import asyncio
from pathlib import Path

import httpx
import pytest

from backend.app.audio import AudioPart
from backend.app import llm_client, task_manager as task_manager_module
from backend.app.llm_client import (
    GeminiUploadedFile,
    TranscriptionProcessingError,
    _extract_gemini_text_parts,
    _gemini_stream_generate_content_endpoint,
    _ensure_refined_markdown,
    _extract_last_paragraph,
    _merge_refine_continuation,
    _normalize_refine_model_output,
    _validate_gemini_upload_response,
    _validate_mp3_audio_path,
    _upload_gemini_file,
    RefineProcessingError,
    build_audio_transcription_payload,
    build_gemini_file_payload,
    build_refine_chat_completion_payload,
    clean_refined_markdown_output,
    describe_transcription_route,
    encode_mp3_as_data_url,
    strip_thinking_content,
)
from backend.app.models import ModelConfig, TaskOptions
from backend.app.prompts import (
    REFINE_FINISH_INSTRUCTION,
    LOCAL_UPLOAD_NO_SUBTITLE_PLACEHOLDER,
    REFINE_FINISH_MARKER,
    NO_SUBTITLE_PLACEHOLDER,
    REFINE_TRANSCRIPT_TASK_PROMPT,
    build_refine_transcript_prompt,
)
from backend.app.task_manager import OVERLAP_RETRY_NOTE, TaskManager, TaskRecord


def test_audio_transcription_payload_uses_chat_completions_audio_data_url():
    payload = build_audio_transcription_payload(
        ModelConfig(
            base_url="https://api.example.com/v1",
            api_key="secret",
            model="audio-model",
            temperature=0.1,
            stream=True,
        ),
        "data:audio/mpeg;base64,YWJj",
        "请转写音频",
    )

    content = payload["messages"][0]["content"]

    assert payload["model"] == "audio-model"
    assert payload["temperature"] == 0.1
    assert payload["stream"] is True
    assert content[0] == {"type": "text", "text": "请转写音频"}
    assert content[1] == {
        "type": "audio_url",
        "audio_url": {"url": "data:audio/mpeg;base64,YWJj"},
    }
    assert content[2] == {"type": "text", "text": "请转写音频"}


def test_encode_mp3_as_data_url_uses_mpeg_prefix_and_base64(tmp_path):
    audio_path = tmp_path / "part_001.mp3"
    audio_path.write_bytes(b"abc")

    assert encode_mp3_as_data_url(audio_path) == "data:audio/mpeg;base64,YWJj"


def test_chat_completions_endpoint_is_appended_to_user_base_url():
    assert (
        llm_client._chat_completions_endpoint("https://api.siliconflow.cn/v1/")
        == "https://api.siliconflow.cn/v1/chat/completions"
    )


def test_openai_audio_parts_use_bounded_concurrency_and_merge_in_index_order(monkeypatch, tmp_path):
    manager = TaskManager()
    parts = []
    for index in range(1, 4):
        audio_path = tmp_path / f"part_{index:03d}.mp3"
        audio_path.write_bytes(b"mp3")
        parts.append(AudioPart(index, audio_path.name, audio_path, 0, 60, 60, 0))

    active = 0
    max_active = 0
    completion_order = []
    delays = {1: 0.06, 2: 0.01, 3: 0.01}
    transcripts = {1: "第一段", 2: "第二段", 3: "第三段"}

    async def fake_transcribe(audio_path, config, prompt=None, log=None):
        nonlocal active, max_active
        index = int(audio_path.stem.rsplit("_", 1)[-1])
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(delays[index])
            completion_order.append(index)
            return transcripts[index]
        finally:
            active -= 1

    monkeypatch.setattr(task_manager_module, "transcribe_mp3", fake_transcribe)
    record = TaskRecord(
        task_id="task-parallel-audio",
        original_input="BV-test",
        options=TaskOptions(max_audio_request_concurrency=2),
        transcription_model_config=ModelConfig(
            base_url="https://api.siliconflow.cn/v1",
            api_key="secret",
            model="Qwen/Qwen3-Omni-30B-A3B-Instruct",
            provider="openai_compatible_input_audio",
        ),
        refine_model_config=ModelConfig(),
        secret_values=["secret"],
        audio_parts=parts,
        progress=88,
    )

    transcript_by_index = asyncio.run(manager._transcribe_audio_parts(record, parts))

    assert max_active == 2
    assert completion_order == [2, 3, 1]
    assert list(transcript_by_index) == [1, 2, 3]
    assert manager._merge_audio_transcripts(parts, transcript_by_index) == "第一段\n\n第二段\n\n第三段"


def test_audio_transcription_payload_forces_streaming_even_if_config_stream_is_false():
    payload = build_audio_transcription_payload(
        ModelConfig(
            base_url="https://api.example.com/v1",
            api_key="secret",
            model="audio-model",
            temperature=0.1,
            stream=False,
        ),
        "YWJj",
        "请转写音频",
    )

    assert payload["stream"] is True


def test_refine_prompt_reuses_existing_four_segment_structure():
    prompt = build_refine_transcript_prompt("", "AI音频转写正文")

    assert prompt.count("<YourTask>") == 2
    assert prompt.count("</YourTask>") == 2
    assert prompt.count(REFINE_TRANSCRIPT_TASK_PROMPT) == 2
    assert f"<OriginalSubtitleContent>\n{NO_SUBTITLE_PLACEHOLDER}\n</OriginalSubtitleContent>" in prompt
    assert "<AIAudioTranscriptionResult>\nAI音频转写正文\n</AIAudioTranscriptionResult>" in prompt
    assert prompt.endswith(REFINE_FINISH_INSTRUCTION)
    assert REFINE_FINISH_MARKER in prompt


def test_refine_prompt_keeps_unified_subtitle_block_for_local_uploads():
    prompt = build_refine_transcript_prompt(
        LOCAL_UPLOAD_NO_SUBTITLE_PLACEHOLDER,
        "本地文件的AI音频转写正文",
    )

    assert f"<OriginalSubtitleContent>\n{LOCAL_UPLOAD_NO_SUBTITLE_PLACEHOLDER}\n</OriginalSubtitleContent>" in prompt
    assert "<AIAudioTranscriptionResult>\n本地文件的AI音频转写正文\n</AIAudioTranscriptionResult>" in prompt


def test_refine_continuation_prompt_includes_truncation_anchor_after_finish_instruction():
    prompt = build_refine_transcript_prompt("", "AI音频转写正文", continuation_anchor="最后一段话")

    assert REFINE_FINISH_INSTRUCTION in prompt
    assert "你刚刚的输出内容被截断了" in prompt
    assert "最后一句话是：“最后一段话”" in prompt
    assert prompt.index(REFINE_FINISH_INSTRUCTION) < prompt.index("你刚刚的输出内容被截断了")


def test_refine_chat_payload_defaults_to_streaming_chat_completions():
    payload = build_refine_chat_completion_payload(
        ModelConfig(
            base_url="https://api.example.com/v1",
            api_key="secret",
            model="text-model",
            temperature=0.2,
            stream=True,
        ),
        "阶段7 prompt",
    )

    assert payload == {
        "model": "text-model",
        "temperature": 0.2,
        "stream": True,
        "messages": [
            {
                "role": "user",
                "content": "阶段7 prompt",
            },
        ],
    }


def test_clean_refined_markdown_output_strips_protocol_artifacts():
    cleaned = clean_refined_markdown_output(
        "<think>内部推理</think>［KINGFALL MODE ENABLE］### 正文\n\n最终内容"
    )

    assert cleaned == "### 正文\n\n最终内容"


def test_normalize_refine_output_strips_finish_marker():
    cleaned, finished = _normalize_refine_model_output("### 正文\n\n最终内容\n\n[finish]")

    assert finished is True
    assert cleaned == "### 正文\n\n最终内容"


def test_normalize_refine_output_reports_missing_finish_without_dropping_text():
    cleaned, finished = _normalize_refine_model_output("### 正文\n\n最后一段")

    assert finished is False
    assert cleaned == "### 正文\n\n最后一段"


def test_extract_last_paragraph_uses_text_after_last_blank_line():
    assert _extract_last_paragraph("第一段\n\n第二段\n继续") == "第二段\n继续"


def test_merge_refine_continuation_removes_repeated_anchor_prefix():
    merged = _merge_refine_continuation("第一段\n\n最后一段话", "最后一段话，继续内容", "最后一段话")

    assert merged == "第一段\n\n最后一段话，继续内容"


def test_refine_output_with_input_xml_tags_is_rejected():
    with pytest.raises(RefineProcessingError) as exc_info:
        _ensure_refined_markdown("<OriginalSubtitleContent>字幕</OriginalSubtitleContent>\n\n[finish]")

    assert exc_info.value.code == "refine_output_invalid"


def test_refine_output_without_finish_is_rejected_by_single_response_guard():
    with pytest.raises(RefineProcessingError) as exc_info:
        _ensure_refined_markdown("正文但没有结束标识")

    assert exc_info.value.code == "refine_finish_missing"


def test_gemini_file_payload_uses_file_data_audio_part():
    payload = build_gemini_file_payload(
        "files/example",
        "请转写音频",
        temperature=0.1,
        mime_type="audio/mp3",
    )

    parts = payload["contents"][0]["parts"]

    assert payload["generationConfig"] == {
        "temperature": 0.1,
        "thinkingConfig": {
            "thinkingLevel": "high",
        },
    }
    assert parts[0] == {"text": "请转写音频"}
    assert parts[1] == {
        "fileData": {
            "mimeType": "audio/mp3",
            "fileUri": "files/example",
        },
    }
    assert parts[2] == {"text": "请转写音频"}


def test_gemini_file_payload_preserves_generation_config_and_uses_high_thinking():
    payload = build_gemini_file_payload(
        "files/example",
        "请转写音频",
        temperature=0.1,
        generation_config={
            "maxOutputTokens": 8192,
            "responseMimeType": "text/plain",
            "thinkingConfig": {
                "existingFlag": True,
            },
        },
    )

    assert payload["generationConfig"] == {
        "temperature": 0.1,
        "maxOutputTokens": 8192,
        "responseMimeType": "text/plain",
        "thinkingConfig": {
            "existingFlag": True,
            "thinkingLevel": "high",
        },
    }
    assert "thinkingBudget" not in str(payload)
    assert "thinking_budget" not in str(payload)


def test_gemini_file_payload_snake_fallback_still_uses_camel_generation_config():
    payload = build_gemini_file_payload(
        "files/example",
        "请转写音频",
        temperature=0.1,
        field_style="snake",
    )

    assert payload["generationConfig"] == {
        "temperature": 0.1,
        "thinkingConfig": {
            "thinkingLevel": "high",
        },
    }
    assert "thinkingBudget" not in str(payload)
    assert "thinking_budget" not in str(payload)
    assert payload["contents"][0]["parts"][0] == {"text": "请转写音频"}
    assert payload["contents"][0]["parts"][1] == {
        "file_data": {
            "mime_type": "audio/mp3",
            "file_uri": "files/example",
        },
    }
    assert payload["contents"][0]["parts"][2] == {"text": "请转写音频"}


def test_gemini_stream_generate_content_endpoint_uses_sse_streaming():
    endpoint = _gemini_stream_generate_content_endpoint(
        ModelConfig(
            base_url="https://api.example.com",
            api_key="secret",
            model="models/gemini-3.1-pro-preview",
        )
    )

    assert endpoint == "https://api.example.com/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent?alt=sse"


def test_extract_gemini_stream_text_parts_accepts_incremental_events():
    chunks = _extract_gemini_text_parts(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "第一段"},
                            {"text": "第二段"},
                        ]
                    }
                }
            ]
        }
    )

    assert chunks == ["第一段", "第二段"]


def test_openai_audio_payload_does_not_include_gemini_thinking_fields():
    payload = build_audio_transcription_payload(
        ModelConfig(
            base_url="https://api.example.com/v1",
            api_key="secret",
            model="audio-model",
            temperature=0.1,
            stream=False,
        ),
        "YWJj",
        "请转写音频",
    )

    serialized = str(payload)
    assert "generationConfig" not in serialized
    assert "thinkingConfig" not in serialized
    assert "thinkingLevel" not in serialized
    assert "thinkingBudget" not in serialized
    assert "thinking_budget" not in serialized


def test_strip_thinking_content_keeps_text_after_last_think_close():
    cleaned = strip_thinking_content(
        "前缀<think>第一轮</think>中间<think>第二轮</think>［KINGFALL MODE ENABLE］正式正文"
    )

    assert cleaned == "正式正文"


def test_merge_transcripts_strips_thinking_content():
    manager = TaskManager()
    parts = [
        AudioPart(1, "part_001.mp3", Path("part_001.mp3"), 0, 1200, 1200, 0),
    ]

    merged = manager._merge_audio_transcripts(
        parts,
        {
            1: "<think>这里是思考</think>这是正式转写",
        },
    )

    assert merged == "这是正式转写"


def test_legacy_gemini_providers_route_to_files_api(tmp_path):
    audio_path = tmp_path / "part_001.mp3"
    audio_path.write_bytes(b"mp3")

    for provider in ("aistudio_to_api_gemini_auto", "aistudio_to_api_gemini_inline"):
        route = describe_transcription_route(
            audio_path,
            ModelConfig(
                base_url="https://api.example.com",
                api_key="secret",
                model="gemini-model",
                provider=provider,
            ),
        )
        assert route == "AIStudioToAPI Gemini原生Files API"


def test_merge_transcripts_trims_reliable_overlap():
    manager = TaskManager()
    parts = [
        AudioPart(1, "part_001.mp3", Path("part_001.mp3"), 0, 1200, 1200, 0),
        AudioPart(2, "part_002.mp3", Path("part_002.mp3"), 1170, 1800, 630, 30),
    ]
    previous = "今天我们讲第一部分，然后进入第二部分，这里有一段需要被识别为重合内容"
    current = "这里有一段需要被识别为重合内容，接下来是新的内容"

    merged = manager._merge_audio_transcripts(
        parts,
        {
            1: previous,
            2: current,
        },
    )

    assert merged.count("这里有一段需要被识别为重合内容") == 1
    assert "接下来是新的内容" in merged


def test_merge_transcripts_adds_note_when_overlap_is_unclear():
    manager = TaskManager()
    parts = [
        AudioPart(1, "part_001.mp3", Path("part_001.mp3"), 0, 1200, 1200, 0),
        AudioPart(2, "part_002.mp3", Path("part_002.mp3"), 1170, 1800, 630, 30),
    ]

    merged = manager._merge_audio_transcripts(
        parts,
        {
            1: "上一段末尾内容",
            2: "下一段开头无法可靠匹配",
        },
    )

    assert OVERLAP_RETRY_NOTE in merged
    assert OVERLAP_RETRY_NOTE == (
        "（系统提示：本段开头的几句话与上一个文段的末尾内容是首尾相连的，有0.5分钟的转录重合。"
        "请AI把上一个文段的末尾以及本段的开头视为同一段话的重复转写，并且在生成最终文稿的过程中去掉本段系统提示词。）"
    )


def test_stage6_rejects_zero_byte_mp3(tmp_path):
    audio_path = tmp_path / "part_001.mp3"
    audio_path.write_bytes(b"")

    with pytest.raises(TranscriptionProcessingError) as exc_info:
        _validate_mp3_audio_path(audio_path)

    assert exc_info.value.code == "transcription_audio_file_empty"
    assert "文件大小为0" in exc_info.value.message


def test_gemini_upload_response_requires_file_uri():
    with pytest.raises(TranscriptionProcessingError) as exc_info:
        _validate_gemini_upload_response(
            {"file": {"state": "ACTIVE", "sizeBytes": "3", "mimeType": "audio/mp3"}},
            3,
            "audio/mp3",
        )

    assert exc_info.value.code == "transcription_gemini_upload_failed"
    assert "缺少file.uri" in exc_info.value.message


def test_gemini_upload_response_requires_active_state():
    with pytest.raises(TranscriptionProcessingError) as exc_info:
        _validate_gemini_upload_response(
            {
                "file": {
                    "uri": "files/example",
                    "state": "PROCESSING",
                    "sizeBytes": "3",
                    "mimeType": "audio/mp3",
                }
            },
            3,
            "audio/mp3",
        )

    assert exc_info.value.code == "transcription_gemini_upload_failed"
    assert "不是ACTIVE" in exc_info.value.message


def test_gemini_upload_response_requires_matching_size_bytes():
    with pytest.raises(TranscriptionProcessingError) as exc_info:
        _validate_gemini_upload_response(
            {
                "file": {
                    "uri": "files/example",
                    "state": "ACTIVE",
                    "sizeBytes": "9000",
                    "mimeType": "audio/mp3",
                }
            },
            3,
            "audio/mp3",
        )

    assert exc_info.value.code == "transcription_gemini_upload_failed"
    assert "大小不一致" in exc_info.value.message


def test_gemini_upload_response_requires_reasonable_mime_type():
    with pytest.raises(TranscriptionProcessingError) as exc_info:
        _validate_gemini_upload_response(
            {
                "file": {
                    "uri": "files/example",
                    "state": "ACTIVE",
                    "sizeBytes": "3",
                    "mimeType": "application/octet-stream",
                }
            },
            3,
            "audio/mp3",
        )

    assert exc_info.value.code == "transcription_gemini_upload_failed"
    assert "mimeType不合理" in exc_info.value.message


def test_gemini_upload_response_accepts_small_size_tolerance():
    uploaded = _validate_gemini_upload_response(
        {
            "file": {
                "uri": "files/example",
                "state": "ACTIVE",
                "sizeBytes": "1001",
                "mimeType": "audio/mp3",
            }
        },
        1000,
        "audio/mp3",
    )

    assert uploaded == GeminiUploadedFile(
        uri="files/example",
        mime_type="audio/mp3",
        state="ACTIVE",
        size_bytes=1001,
    )


def test_gemini_file_upload_stream_path_and_strong_validation(tmp_path):
    audio_path = tmp_path / "part_001.mp3"
    audio_path.write_bytes(b"mp3-bytes")
    requests = []

    async def handler(request):
        body = await request.aread()
        requests.append((str(request.url), body))
        if str(request.url).endswith("/upload/v1beta/files"):
            return httpx.Response(200, headers={"x-goog-upload-url": "https://api.example.com/upload/session"})
        if str(request.url).endswith("/upload/session"):
            return httpx.Response(
                200,
                json={
                    "file": {
                        "uri": "files/mock",
                        "state": "ACTIVE",
                        "sizeBytes": str(len(body)),
                        "mimeType": "audio/mp3",
                    }
                },
            )
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await _upload_gemini_file(
                client,
                audio_path,
                ModelConfig(base_url="https://api.example.com", api_key="secret", model="gemini-model"),
                "audio/mp3",
                file_size=audio_path.stat().st_size,
            )

    uploaded = asyncio.run(run())

    assert uploaded == GeminiUploadedFile("files/mock", "audio/mp3", "ACTIVE", len(b"mp3-bytes"))
    assert len(requests) == 2
    assert requests[1][1] == b"mp3-bytes"


def test_gemini_audio_not_received_falls_back_through_mime_and_field_styles(monkeypatch, tmp_path):
    audio_path = tmp_path / "part_001.mp3"
    audio_path.write_bytes(b"mp3")
    upload_calls = []
    generate_calls = []

    async def fake_upload(client, path, config, mime_type, file_size, log=None):
        upload_calls.append(mime_type)
        return GeminiUploadedFile(
            uri=f"files/{mime_type.replace('/', '-')}",
            mime_type=mime_type,
            state="ACTIVE",
            size_bytes=file_size,
        )

    async def fake_generate(client, endpoint, headers, payload, api_key):
        part = payload["contents"][0]["parts"][1]
        if "fileData" in part:
            field_name = "fileData"
            mime_type = part["fileData"]["mimeType"]
        else:
            field_name = "file_data"
            mime_type = part["file_data"]["mime_type"]
        generate_calls.append((mime_type, field_name))
        if len(generate_calls) < 4:
            return "我没有收到音频，无法听取音频。"
        return "这是最终转写文本"

    monkeypatch.setattr(llm_client, "_upload_gemini_file", fake_upload)
    monkeypatch.setattr(llm_client, "_post_streaming_gemini_generate_content", fake_generate)

    text = asyncio.run(
        llm_client.transcribe_mp3_with_aistudio_gemini(
            audio_path,
            ModelConfig(
                base_url="https://api.example.com",
                api_key="secret",
                model="gemini-model",
                provider="aistudio_to_api_gemini_file",
            ),
        )
    )

    assert text == "这是最终转写文本"
    assert upload_calls == ["audio/mp3", "audio/mpeg"]
    assert generate_calls == [
        ("audio/mp3", "fileData"),
        ("audio/mp3", "file_data"),
        ("audio/mpeg", "fileData"),
        ("audio/mpeg", "file_data"),
    ]


def test_gemini_generation_reuses_uploaded_file_uri_after_transient_failure(monkeypatch, tmp_path):
    audio_path = tmp_path / "part_001.mp3"
    audio_path.write_bytes(b"mp3")
    upload_calls = []
    file_uris = []

    async def fake_upload(client, path, config, mime_type, file_size, log=None):
        upload_calls.append(mime_type)
        return GeminiUploadedFile(
            uri="files/reusable",
            mime_type=mime_type,
            state="ACTIVE",
            size_bytes=file_size,
        )

    async def fake_generate(client, endpoint, headers, payload, api_key):
        file_uris.append(payload["contents"][0]["parts"][1]["fileData"]["fileUri"])
        if len(file_uris) == 1:
            raise TranscriptionProcessingError("transcription_network_error", "临时网络抖动")
        return "复用上传文件后的转写文本"

    monkeypatch.setattr(llm_client, "_upload_gemini_file", fake_upload)
    monkeypatch.setattr(llm_client, "_post_streaming_gemini_generate_content", fake_generate)

    text = asyncio.run(
        llm_client.transcribe_mp3_with_aistudio_gemini(
            audio_path,
            ModelConfig(
                base_url="https://api.example.com",
                api_key="secret",
                model="gemini-model",
                provider="aistudio_to_api_gemini_file",
            ),
        )
    )

    assert text == "复用上传文件后的转写文本"
    assert upload_calls == ["audio/mp3"]
    assert file_uris == ["files/reusable", "files/reusable"]
