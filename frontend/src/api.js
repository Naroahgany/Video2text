function apiUrl(path) {
  const isLocalDev = ["127.0.0.1", "localhost"].includes(window.location.hostname);
  const port = Number(window.location.port);
  const isBackendPort = Number.isInteger(port) && port >= 8000 && port <= 8099;
  const baseUrl = isLocalDev && !isBackendPort ? "http://127.0.0.1:8000" : "";
  return `${baseUrl}${path}`;
}

export async function fetchHealth() {
  const response = await fetch(apiUrl("/api/health"), {
    headers: {
      Accept: "application/json",
    },
  });

  if (!response.ok) {
    throw new Error(`Health check failed: ${response.status}`);
  }

  return response.json();
}

export async function fetchModelList({ baseUrl, apiKey, provider }) {
  const response = await fetch(apiUrl("/api/models/list"), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      base_url: baseUrl,
      api_key: apiKey,
      provider: provider || "openai_compatible_input_audio",
    }),
  });

  const payload = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(payload.detail || `Model list request failed: ${response.status}`);
  }

  return Array.isArray(payload.models) ? payload.models : [];
}

export async function createTask({ input, transcriptionConfig, refineConfig, options = {} }) {
  const response = await fetch(apiUrl("/api/tasks"), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(toBackendTaskRequest(input, transcriptionConfig, refineConfig, options)),
  });

  return parseTaskResponse(response, "Task create request failed");
}

export function createLocalMediaTask({
  file,
  transcriptionConfig,
  refineConfig,
  options = {},
  onUploadProgress,
}) {
  const formData = new FormData();
  formData.append(
    "task_request",
    JSON.stringify(toBackendTaskRequest(file.name, transcriptionConfig, refineConfig, options)),
  );
  formData.append("file", file, file.name);

  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", apiUrl("/api/tasks/local-upload"));
    request.setRequestHeader("Accept", "application/json");
    request.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable || typeof onUploadProgress !== "function") {
        return;
      }
      onUploadProgress(Math.max(0, Math.min(1, event.loaded / event.total)));
    });
    request.addEventListener("load", () => {
      let payload = {};
      try {
        payload = JSON.parse(request.responseText || "{}");
      } catch {
        payload = {};
      }
      if (request.status >= 200 && request.status < 300) {
        resolve(payload);
        return;
      }
      reject(new Error(payload.detail || `Local media upload failed: ${request.status}`));
    });
    request.addEventListener("error", () => {
      reject(new Error("本地文件上传失败，请确认后端服务仍在运行。"));
    });
    request.addEventListener("abort", () => {
      reject(new Error("本地文件上传已取消。"));
    });
    request.send(formData);
  });
}

export async function fetchTask(taskId) {
  const response = await fetch(apiUrl(`/api/tasks/${encodeURIComponent(taskId)}`), {
    headers: {
      Accept: "application/json",
    },
  });

  return parseTaskResponse(response, "Task status request failed");
}

export async function cancelTask(taskId) {
  const response = await fetch(apiUrl(`/api/tasks/${encodeURIComponent(taskId)}/cancel`), {
    method: "POST",
    headers: {
      Accept: "application/json",
    },
  });

  return parseTaskResponse(response, "Task cancel request failed");
}

export async function retryTranscription(taskId, transcriptionConfig) {
  const response = await fetch(apiUrl(`/api/tasks/${encodeURIComponent(taskId)}/retry-transcription`), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      transcription_model_config: toBackendModelConfig(transcriptionConfig),
    }),
  });

  return parseTaskResponse(response, "Task transcription retry request failed");
}

export async function retryRefine(taskId, refineConfig) {
  const response = await fetch(apiUrl(`/api/tasks/${encodeURIComponent(taskId)}/retry-refine`), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      refine_model_config: toBackendModelConfig(refineConfig),
    }),
  });

  return parseTaskResponse(response, "Task refine retry request failed");
}

export async function openBilibiliLoginWindow() {
  const response = await fetch(apiUrl("/api/bilibili/profile/open-login"), {
    method: "POST",
    headers: {
      Accept: "application/json",
    },
  });

  return parseJsonResponse(response, "Bilibili login window request failed");
}

export async function fetchBilibiliProfileStatus() {
  const response = await fetch(apiUrl("/api/bilibili/profile/status"), {
    headers: {
      Accept: "application/json",
    },
  });

  return parseJsonResponse(response, "Bilibili profile status request failed");
}

export async function extractBilibiliCookieFromProfile(sessionToken) {
  const response = await fetch(apiUrl("/api/bilibili/profile/extract-cookie"), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      session_token: sessionToken || "",
    }),
  });

  return parseJsonResponse(response, "Bilibili cookie extraction request failed");
}

export async function validateBilibiliCookie(cookieHeader) {
  const response = await fetch(apiUrl("/api/bilibili/cookie/validate"), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      cookie_header: cookieHeader || "",
    }),
  });

  return parseJsonResponse(response, "Bilibili cookie validation request failed");
}

function toBackendTaskRequest(input, transcriptionConfig, refineConfig, options = {}) {
  return {
    input,
    transcription_model_config: toBackendModelConfig(transcriptionConfig),
    refine_model_config: toBackendModelConfig(refineConfig),
    options: {
      skip_subtitle_if_failed: Boolean(options.skipSubtitleIfFailed),
      bilibili_access_mode: options.bilibiliAccessMode || "cookie_header",
      bilibili_cookie_browser: options.bilibiliCookieBrowser || "chrome",
      bilibili_cookie_header: options.bilibiliCookieHeader || "",
      bilibili_cookies_file_content: options.bilibiliCookiesFileContent || "",
      audio_part_interval_seconds: Number(options.audioPartIntervalSeconds ?? 20),
      no_slice_max_minutes: Number(options.noSliceMaxMinutes ?? 15),
      target_chunk_minutes: Number(options.targetChunkMinutes ?? 15),
      chunk_overlap_minutes: Number(options.chunkOverlapMinutes ?? 0.5),
      max_audio_request_concurrency: Number(options.maxAudioRequestConcurrency ?? 2),
    },
  };
}

function toBackendModelConfig(config = {}) {
  return {
    base_url: config.baseUrl || "",
    api_key: config.apiKey || "",
    model: config.model || "",
    temperature: Number(config.temperature ?? 0),
    stream: config.stream !== false,
    provider: config.provider || "openai_compatible_input_audio",
  };
}

async function parseTaskResponse(response, fallbackMessage) {
  return parseJsonResponse(response, fallbackMessage);
}

async function parseJsonResponse(response, fallbackMessage) {
  const payload = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(payload.detail || `${fallbackMessage}: ${response.status}`);
  }

  return payload;
}
