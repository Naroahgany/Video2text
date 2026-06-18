const DB_NAME = "bilibili-transcription-workflow";
const DB_VERSION = 1;
const SETTINGS_STORE = "settings";
const HISTORY_STORE = "history";

const DEFAULT_MODEL_CONFIG = {
  baseUrl: "",
  apiKey: "",
  model: "",
  temperature: 0,
  stream: true,
};

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

function normalizeModelConfig(config = {}) {
  return {
    ...DEFAULT_MODEL_CONFIG,
    ...config,
    temperature: Number.isFinite(Number(config.temperature))
      ? Number(config.temperature)
      : DEFAULT_MODEL_CONFIG.temperature,
    stream: config.stream !== false,
  };
}

function sanitizeHistoryRecord(record = {}) {
  return {
    id: record.id || crypto.randomUUID(),
    title: record.title || "未命名任务",
    originalInput: record.originalInput || "",
    parsedInput: record.parsedInput || "",
    createdAt: record.createdAt || new Date().toISOString(),
    finalMarkdown: record.finalMarkdown || "",
    cleanSubtitle: record.cleanSubtitle || "",
    aiTranscript: record.aiTranscript || "",
    logs: Array.isArray(record.logs) ? record.logs : [],
    error: record.error || "",
    subTasks: Array.isArray(record.subTasks)
      ? record.subTasks.map((subTask) => ({
          id: subTask.id || crypto.randomUUID(),
          title: subTask.title || "",
          pIndex: subTask.pIndex ?? null,
          finalMarkdown: subTask.finalMarkdown || "",
          cleanSubtitle: subTask.cleanSubtitle || "",
          aiTranscript: subTask.aiTranscript || "",
          logs: Array.isArray(subTask.logs) ? subTask.logs : [],
          error: subTask.error || "",
        }))
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
  ]
    .join("\n")
    .toLowerCase();

  return haystack.includes(searchTerm.toLowerCase());
}

export async function initDatabase() {
  await openDatabase();
}

export async function getModelConfig(kind) {
  const db = await openDatabase();
  const transaction = db.transaction(SETTINGS_STORE, "readonly");
  const store = transaction.objectStore(SETTINGS_STORE);
  const record = await requestToPromise(store.get(settingKey(kind)));
  return normalizeModelConfig(record?.config);
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
  store.put({
    key: settingKey(kind),
    kind,
    config: normalizeModelConfig(config),
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

export async function clearHistoryRecords() {
  const db = await openDatabase();
  const transaction = db.transaction(HISTORY_STORE, "readwrite");
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
    if (record?.key && record?.config) {
      settingsStore.put({
        key: record.key,
        kind: record.kind || "",
        config: normalizeModelConfig(record.config),
        updatedAt: record.updatedAt || new Date().toISOString(),
      });
    }
  }

  for (const record of history) {
    historyStore.put(sanitizeHistoryRecord(record));
  }

  await transactionDone(transaction);
}
