import test from "node:test";
import assert from "node:assert/strict";

import {
  mergeStoredModelConfig,
  resolveStoredModelConfig,
} from "../src/db.js";

const GEMINI_PROVIDER = "aistudio_to_api_gemini_file";
const OPENAI_PROVIDER = "openai_compatible_input_audio";
const OPENAI_DEFAULTS = {
  provider: OPENAI_PROVIDER,
  baseUrl: "https://api.siliconflow.cn/v1",
  apiKey: "",
  model: "Qwen/Qwen3-Omni-30B-A3B-Thinking",
  temperature: 0.1,
  stream: true,
};

const geminiConfig = {
  provider: GEMINI_PROVIDER,
  baseUrl: "https://gemini.example.test",
  apiKey: "gemini-test-key",
  model: "gemini-test-model",
  temperature: 0.2,
  stream: true,
};

const openAiConfig = {
  provider: OPENAI_PROVIDER,
  baseUrl: "https://openai.example.test/v1",
  apiKey: "openai-test-key",
  model: "openai-audio-test-model",
  temperature: 0.7,
  stream: true,
};

test("\u672a\u4fdd\u5b58\u7684 OpenAI Compatible \u914d\u7f6e\u4f7f\u7528\u7845\u57fa\u6d41\u52a8\u9ed8\u8ba4\u503c", () => {
  assert.deepEqual(
    resolveStoredModelConfig({}, "transcription", OPENAI_PROVIDER),
    OPENAI_DEFAULTS,
  );
});

test("首次打开音频转文字配置时默认使用 OpenAI Compatible", () => {
  assert.deepEqual(
    resolveStoredModelConfig({}, "transcription"),
    OPENAI_DEFAULTS,
  );
});

test("已取消的音频识别方式回落到 OpenAI Compatible 默认配置", () => {
  for (const provider of ["unsupported_provider"]) {
    assert.deepEqual(
      resolveStoredModelConfig(
        {
          provider,
          baseUrl: "https://removed-provider.example.test",
          apiKey: "removed-provider-key",
          model: "removed-provider-model",
          temperature: 0.8,
          stream: false,
        },
        "transcription",
      ),
      OPENAI_DEFAULTS,
    );
  }
});

test("旧版单份 Gemini 配置不会填入 OpenAI Compatible 表单", () => {
  const openAiResolved = resolveStoredModelConfig(
    geminiConfig,
    "transcription",
    OPENAI_PROVIDER,
  );

  assert.deepEqual(openAiResolved, OPENAI_DEFAULTS);
});

test("Gemini 与 OpenAI Compatible 配置分别保存且可分别恢复", () => {
  const storedGemini = mergeStoredModelConfig({}, geminiConfig, "transcription");
  const storedBoth = mergeStoredModelConfig(storedGemini, openAiConfig, "transcription");

  assert.deepEqual(
    resolveStoredModelConfig(storedBoth, "transcription", GEMINI_PROVIDER),
    geminiConfig,
  );
  assert.deepEqual(
    resolveStoredModelConfig(storedBoth, "transcription", OPENAI_PROVIDER),
    openAiConfig,
  );
});

test("刷新时恢复最后保存的识别方式及其配置", () => {
  const storedGemini = mergeStoredModelConfig({}, geminiConfig, "transcription");
  const storedBoth = mergeStoredModelConfig(storedGemini, openAiConfig, "transcription");

  assert.deepEqual(
    resolveStoredModelConfig(storedBoth, "transcription"),
    openAiConfig,
  );
});
