import { simplifyBilibiliCookieHeader } from "./bilibiliCookie.js";

const DB_NAME = "bilibili-transcription-workflow";
const DB_VERSION = 1;
const SETTINGS_STORE = "settings";
const HISTORY_STORE = "history";

const OPENAI_COMPATIBLE_TRANSCRIPTION_PROVIDER = "openai_compatible_input_audio";
const DEFAULT_MODEL_CONFIG = {
  baseUrl: "",
  apiKey: "",
  model: "",
  temperature: 0,
  stream: true,
  provider: OPENAI_COMPATIBLE_TRANSCRIPTION_PROVIDER,
};
const DEFAULT_OPENAI_COMPATIBLE_TRANSCRIPTION_CONFIG = {
  baseUrl: "https://api.siliconflow.cn/v1",
  apiKey: "",
  model: "Qwen/Qwen3-Omni-30B-A3B-Thinking",
  temperature: 0.1,
};
const DEFAULT_TRANSCRIPTION_PROVIDER = OPENAI_COMPATIBLE_TRANSCRIPTION_PROVIDER;
const LEGACY_MODEL_PROVIDER_ALIASES = {
  aistudio_to_api_gemini_auto: "aistudio_to_api_gemini_file",
  aistudio_to_api_gemini_inline: "aistudio_to_api_gemini_file",
};

const MODEL_PROVIDER_VALUES = new Set([
  "openai_compatible_input_audio",
  "aistudio_to_api_gemini_file",
  "openai_audio_transcriptions",
]);

const BILIBILI_ACCESS_SETTINGS_KEY = "bilibiliAccessSettings";
const DEFAULT_BILIBILI_ACCESS_SETTINGS = {
  mode: "cookie_header",
  browser: "chrome",
  cookieHeader: "",
};
const HISTORY_STATUSES = new Set(["completed", "failed", "canceled", "abandoned", "waiting_model_retry"]);

let databasePromise;

function requestToPromise(request) {
  return new Promise((resolve, reject) => {
    request.addEventListener("success", () => resolve(request.result));
    request.addEventListener("error", () => reject(request.error));
  });
}

function transactionDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.addEventListener("complete", () => resolve());
    transaction.addEventListener("error", () => reject(transaction.error));
    transaction.addEventListener("abort", () => reject(transaction.error));
  });
}

function openDatabase() {
  if (databasePromise) {
    return databasePromise;
  }

  databasePromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);

    request.addEventListener("upgradeneeded", () => {
      const db = request.result;

      if (!db.objectStoreNames.contains(SETTINGS_STORE)) {
        db.createObjectStore(SETTINGS_STORE, { keyPath: "key" });
      }

      if (!db.objectStoreNames.contains(HISTORY_STORE)) {
        const historyStore = db.createObjectStore(HISTORY_STORE, { keyPath: "id" });
        historyStore.createIndex("createdAt", "createdAt", { unique: false });
        historyStore.createIndex("title", "title", { unique: false });
      }
    });

    request.addEventListener("success", () => resolve(request.result));
    request.addEventListener("error", () => reject(request.error));
  });

  return databasePromise;
}

function settingKey(kind) {
  return `${kind}ModelConfig`;
}

function kindFromSettingRecord(record = {}) {
  if (record.kind === "transcription" || record.kind === "refine") {
    return record.kind;
  }
  if (record.key === settingKey("transcription")) {
    return "transcription";
  }
  if (record.key === settingKey("refine")) {
    return "refine";
  }
  return "";
}

function normalizeModelConfig(config = {}, kind = "", providerOverride = "") {
  const defaultProvider =
    kind === "transcription" ? DEFAULT_TRANSCRIPTION_PROVIDER : DEFAULT_MODEL_CONFIG.provider;
  const requestedProvider = providerOverride || config.provider;
  const rawProvider = LEGACY_MODEL_PROVIDER_ALIASES[requestedProvider] || requestedProvider;
  const isKnownProvider = MODEL_PROVIDER_VALUES.has(rawProvider);
  const provider = isKnownProvider ? rawProvider : defaultProvider;
  const { providerConfigs: _providerConfigs, ...flatConfig } = config;
  const configValues = requestedProvider && !isKnownProvider ? {} : flatConfig;
  const defaultConfig =
    kind === "transcription" && provider === OPENAI_COMPATIBLE_TRANSCRIPTION_PROVIDER
      ? { ...DEFAULT_MODEL_CONFIG, ...DEFAULT_OPENAI_COMPATIBLE_TRANSCRIPTION_CONFIG }
      : DEFAULT_MODEL_CONFIG;

  return {
    ...defaultConfig,
    ...configValues,
    temperature: Number.isFinite(Number(configValues.temperature))
      ? Number(configValues.temperature)
      : defaultConfig.temperature,
    stream: configValues.stream !== false,
    provider,
  };
}

function normalizeStoredModelConfig(config = {}, kind = "") {
  const normalizedConfig = normalizeModelConfig(config, kind);
  if (kind !== "transcription") {
    return normalizedConfig;
  }

  const providerConfigs = {};
  const storedProviderConfigs = config?.providerConfigs;
  if (storedProviderConfigs && typeof storedProviderConfigs === "object") {
    for (const [storedProvider, storedConfig] of Object.entries(storedProviderConfigs)) {
      if (!storedConfig || typeof storedConfig !== "object") {
        continue;
      }
      const provider = LEGACY_MODEL_PROVIDER_ALIASES[storedProvider] || storedProvider;
      if (!MODEL_PROVIDER_VALUES.has(provider)) {
        continue;
      }
      providerConfigs[provider] = normalizeModelConfig(storedConfig, kind, provider);
    }
  }

  if (!providerConfigs[normalizedConfig.provider]) {
    providerConfigs[normalizedConfig.provider] = normalizedConfig;
  }

  return {
    ...providerConfigs[normalizedConfig.provider],
    provider: normalizedConfig.provider,
    providerConfigs,
  };
}

export function resolveStoredModelConfig(config = {}, kind = "", provider = "") {
  const storedConfig = normalizeStoredModelConfig(config, kind);
  if (kind !== "transcription") {
    return storedConfig;
  }

  const resolvedProvider = normalizeModelConfig(
    {},
    kind,
    provider || storedConfig.provider,
  ).provider;
  return storedConfig.providerConfigs[resolvedProvider]
    || normalizeModelConfig({}, kind, resolvedProvider);
}

export function mergeStoredModelConfig(currentConfig = {}, nextConfig = {}, kind = "") {
  const normalizedNextConfig = normalizeModelConfig(nextConfig, kind);
  if (kind !== "transcription") {
    return normalizedNextConfig;
  }

  const storedConfig = normalizeStoredModelConfig(currentConfig, kind);
  return {
    ...normalizedNextConfig,
    providerConfigs: {
      ...storedConfig.providerConfigs,
      [normalizedNextConfig.provider]: normalizedNextConfig,
    },
  };
}

function normalizeBilibiliAccessSettings(config = {}) {
  const rawMode = String(config.mode || DEFAULT_BILIBILI_ACCESS_SETTINGS.mode);
  const mode = [
    "anonymous",
    "enhanced_headers",
    "bilibili_api",
    "impersonate",
    "browser_cookie",
    "cookies_file",
  ].includes(rawMode)
    ? "cookie_header"
    : rawMode;

  return {
    ...DEFAULT_BILIBILI_ACCESS_SETTINGS,
    ...config,
    mode,
    browser: String(config.browser || DEFAULT_BILIBILI_ACCESS_SETTINGS.browser),
    cookieHeader: simplifyBilibiliCookieHeader(config.cookieHeader || ""),
  };
}

function redactSensitiveText(value = "") {
  return String(value || "")
    .replace(/Bearer\s+[^\s,;]+/gi, "Bearer [REDACTED]")
    .replace(/(Authorization\s*[:=]\s*)(Bearer\s+)?[^\s,;]+/gi, "$1[REDACTED]")
    .replace(/(Cookie\s*[:=]\s*)[^\r\n]+/gi, "$1[REDACTED]")
    .replace(/\b(SESSDATA|bili_jct|DedeUserID|DedeUserID__ckMd5|bili_ticket|bili_ticket_expires)\s*=\s*[^;\s]+/gi, "$1=[REDACTED]")
    .replace(/(api[_-]?key\s*[:=]\s*)[^\s,;]+/gi, "$1[REDACTED]")
    .replace(/sk-[A-Za-z0-9_-]{8,}/g, "[REDACTED]")
    .replace(/[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g, "[REDACTED]");
}

function sanitizeLogEntry(log = {}) {
  return {
    time: log.time || new Date().toISOString(),
    level: ["info", "warning", "error"].includes(log.level) ? log.level : "info",
    message: redactSensitiveText(log.message || ""),
  };
}

function finiteNumberOrNull(value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function sanitizeSubTask(subTask = {}) {
  return {
    id: subTask.id || crypto.randomUUID(),
    title: redactSensitiveText(subTask.title || ""),
    pIndex: finiteNumberOrNull(subTask.pIndex),
    bvId: subTask.bvId || "",
    url: redactSensitiveText(subTask.url || ""),
    durationSeconds: finiteNumberOrNull(subTask.durationSeconds),
    finalMarkdown: subTask.finalMarkdown || "",
    cleanSubtitle: subTask.cleanSubtitle || "",
    aiTranscript: subTask.aiTranscript || "",
    logs: Array.isArray(subTask.logs) ? subTask.logs.map(sanitizeLogEntry) : [],
    error: redactSensitiveText(subTask.error || ""),
  };
}

function sanitizeHistoryRecord(record = {}) {
  const status = HISTORY_STATUSES.has(record.status) ? record.status : record.error ? "failed" : "completed";
  return {
    id: record.id || crypto.randomUUID(),
    status,
    title: redactSensitiveText(record.title || "未命名任务"),
    pIndex: finiteNumberOrNull(record.pIndex),
    durationSeconds: finiteNumberOrNull(record.durationSeconds),
    originalInput: redactSensitiveText(record.originalInput || ""),
    parsedInput: redactSensitiveText(record.parsedInput || ""),
    createdAt: record.createdAt || new Date().toISOString(),
    finalMarkdown: record.finalMarkdown || "",
    cleanSubtitle: record.cleanSubtitle || "",
    aiTranscript: record.aiTranscript || "",
    logs: Array.isArray(record.logs) ? record.logs.map(sanitizeLogEntry) : [],
    error: redactSensitiveText(record.error || ""),
    subTasks: Array.isArray(record.subTasks)
      ? record.subTasks.map(sanitizeSubTask)
      : [],
  };
}

function recordMatchesSearch(record, searchTerm) {
  if (!searchTerm) {
    return true;
  }

  const haystack = [
    record.title,
    record.originalInput,
    record.parsedInput,
    record.finalMarkdown,
    record.cleanSubtitle,
    record.aiTranscript,
    record.error,
    ...record.subTasks.flatMap((subTask) => [
      subTask.title,
      subTask.url,
      subTask.finalMarkdown,
      subTask.cleanSubtitle,
      subTask.aiTranscript,
      subTask.error,
    ]),
  ]
    .join("\n")
    .toLowerCase();

  return haystack.includes(searchTerm.toLowerCase());
}

export async function initDatabase() {
  await openDatabase();
}

export async function getModelConfig(kind, provider = "") {
  const db = await openDatabase();
  const transaction = db.transaction(SETTINGS_STORE, "readonly");
  const store = transaction.objectStore(SETTINGS_STORE);
  const record = await requestToPromise(store.get(settingKey(kind)));
  return resolveStoredModelConfig(record?.config, kind, provider);
}

export async function getAllModelConfigs() {
  const [transcription, refine] = await Promise.all([
    getModelConfig("transcription"),
    getModelConfig("refine"),
  ]);

  return { transcription, refine };
}

export async function saveModelConfig(kind, config) {
  const db = await openDatabase();
  const transaction = db.transaction(SETTINGS_STORE, "readwrite");
  const store = transaction.objectStore(SETTINGS_STORE);
  const currentRecord = await requestToPromise(store.get(settingKey(kind)));
  store.put({
    key: settingKey(kind),
    kind,
    config: mergeStoredModelConfig(currentRecord?.config, config, kind),
    updatedAt: new Date().toISOString(),
  });
  await transactionDone(transaction);
}

export async function getBilibiliAccessSettings() {
  const db = await openDatabase();
  const transaction = db.transaction(SETTINGS_STORE, "readonly");
  const store = transaction.objectStore(SETTINGS_STORE);
  const record = await requestToPromise(store.get(BILIBILI_ACCESS_SETTINGS_KEY));
  return normalizeBilibiliAccessSettings(record?.config);
}

export async function saveBilibiliAccessSettings(config) {
  const db = await openDatabase();
  const transaction = db.transaction(SETTINGS_STORE, "readwrite");
  const store = transaction.objectStore(SETTINGS_STORE);
  store.put({
    key: BILIBILI_ACCESS_SETTINGS_KEY,
    kind: "bilibiliAccess",
    config: normalizeBilibiliAccessSettings(config),
    updatedAt: new Date().toISOString(),
  });
  await transactionDone(transaction);
}

export async function saveHistoryRecord(record) {
  const db = await openDatabase();
  const transaction = db.transaction(HISTORY_STORE, "readwrite");
  const store = transaction.objectStore(HISTORY_STORE);
  store.put(sanitizeHistoryRecord(record));
  await transactionDone(transaction);
}

export async function deleteHistoryRecord(id) {
  if (!id) {
    return;
  }
  const db = await openDatabase();
  const transaction = db.transaction(HISTORY_STORE, "readwrite");
  transaction.objectStore(HISTORY_STORE).delete(id);
  await transactionDone(transaction);
}

export async function listHistoryRecords(searchTerm = "") {
  const db = await openDatabase();
  const transaction = db.transaction(HISTORY_STORE, "readonly");
  const store = transaction.objectStore(HISTORY_STORE);
  const records = await requestToPromise(store.getAll());

  return records
    .map(sanitizeHistoryRecord)
    .filter((record) => recordMatchesSearch(record, searchTerm.trim()))
    .sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime());
}

export async function clearAllLocalData() {
  const db = await openDatabase();
  const transaction = db.transaction([SETTINGS_STORE, HISTORY_STORE], "readwrite");
  transaction.objectStore(SETTINGS_STORE).clear();
  transaction.objectStore(HISTORY_STORE).clear();
  await transactionDone(transaction);
}

export async function exportAllData() {
  const db = await openDatabase();
  const settingsTransaction = db.transaction(SETTINGS_STORE, "readonly");
  const historyTransaction = db.transaction(HISTORY_STORE, "readonly");

  const [settings, history] = await Promise.all([
    requestToPromise(settingsTransaction.objectStore(SETTINGS_STORE).getAll()),
    requestToPromise(historyTransaction.objectStore(HISTORY_STORE).getAll()),
  ]);

  return {
    schemaVersion: DB_VERSION,
    exportedAt: new Date().toISOString(),
    settings,
    history: history.map(sanitizeHistoryRecord),
  };
}

export async function importAllData(payload) {
  const db = await openDatabase();
  const settings = Array.isArray(payload?.settings) ? payload.settings : [];
  const history = Array.isArray(payload?.history) ? payload.history : [];
  const transaction = db.transaction([SETTINGS_STORE, HISTORY_STORE], "readwrite");
  const settingsStore = transaction.objectStore(SETTINGS_STORE);
  const historyStore = transaction.objectStore(HISTORY_STORE);

  settingsStore.clear();
  historyStore.clear();

  for (const record of settings) {
    if (record?.key === BILIBILI_ACCESS_SETTINGS_KEY && record?.config) {
      settingsStore.put({
        key: BILIBILI_ACCESS_SETTINGS_KEY,
        kind: "bilibiliAccess",
        config: normalizeBilibiliAccessSettings(record.config),
        updatedAt: record.updatedAt || new Date().toISOString(),
      });
      continue;
    }

    if (record?.key && record?.config) {
      const kind = kindFromSettingRecord(record);
      settingsStore.put({
        key: record.key,
        kind,
        config: normalizeStoredModelConfig(record.config, kind),
        updatedAt: record.updatedAt || new Date().toISOString(),
      });
    }
  }

  for (const record of history) {
    historyStore.put(sanitizeHistoryRecord(record));
  }

  await transactionDone(transaction);
}
