function apiUrl(path) {
  const isLocalDev = ["127.0.0.1", "localhost"].includes(window.location.hostname);
  const isBackendPort = ["8000", "8001"].includes(window.location.port);
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

export async function fetchModelList({ baseUrl, apiKey }) {
  const response = await fetch(apiUrl("/api/models/list"), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      base_url: baseUrl,
      api_key: apiKey,
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
    body: JSON.stringify({
      input,
      transcription_model_config: toBackendModelConfig(transcriptionConfig),
      refine_model_config: toBackendModelConfig(refineConfig),
      options: {
        skip_subtitle_if_failed: Boolean(options.skipSubtitleIfFailed),
        audio_part_interval_seconds: Number(options.audioPartIntervalSeconds ?? 10),
        target_chunk_minutes: Number(options.targetChunkMinutes ?? 20),
        max_audio_request_concurrency: Number(options.maxAudioRequestConcurrency ?? 2),
      },
    }),
  });

  return parseTaskResponse(response, "Task create request failed");
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

function toBackendModelConfig(config = {}) {
  return {
    base_url: config.baseUrl || "",
    api_key: config.apiKey || "",
    model: config.model || "",
    temperature: Number(config.temperature ?? 0),
    stream: config.stream !== false,
  };
}

async function parseTaskResponse(response, fallbackMessage) {
  const payload = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(payload.detail || `${fallbackMessage}: ${response.status}`);
  }

  return payload;
}
