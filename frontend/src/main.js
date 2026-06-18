import { cancelTask, createTask, fetchHealth, fetchModelList, fetchTask } from "./api.js";
import {
  clearHistoryRecords,
  exportAllData,
  getAllModelConfigs,
  getModelConfig,
  importAllData,
  initDatabase,
  listHistoryRecords,
  saveModelConfig,
} from "./db.js";
import {
  appendTaskLog,
  getHealthState,
  getTaskState,
  setHealthState,
  setTaskState,
} from "./state.js";

const modelLabels = {
  transcription: "音频转文字模型",
  refine: "文稿优化模型",
};

const elements = {
  appShell: document.querySelector(".app-shell"),
  statusText: document.querySelector("[data-health-status]"),
  statusDot: document.querySelector("[data-health-dot]"),
  refreshButton: document.querySelector("[data-refresh-health]"),
  workflowShell: document.querySelector("[data-workflow-shell]"),
  videoTitle: document.querySelector("[data-video-title]"),
  taskView: document.querySelector("[data-task-view]"),
  resultView: document.querySelector("[data-result-view]"),
  inputForm: document.querySelector("[data-input-form]"),
  taskInput: document.querySelector("[data-task-input]"),
  inputStatus: document.querySelector("[data-input-status]"),
  sendButton: document.querySelector(".send-button"),
  progressBar: document.querySelector("[data-progress-bar]"),
  progressLabel: document.querySelector("[data-progress-label]"),
  stageText: document.querySelector("[data-stage-text]"),
  recognizedInput: document.querySelector("[data-recognized-input]"),
  errorMessage: document.querySelector("[data-error-message]"),
  logPanel: document.querySelector("[data-log-panel]"),
  cancelTask: document.querySelector("[data-cancel-task]"),
  resetWorkflowButtons: Array.from(document.querySelectorAll("[data-reset-workflow]")),
  finalMarkdown: document.querySelector("[data-final-markdown]"),
  cleanSubtitle: document.querySelector("[data-clean-subtitle]"),
  aiTranscript: document.querySelector("[data-ai-transcript]"),
  fullLog: document.querySelector("[data-full-log]"),
  downloadMarkdown: document.querySelector("[data-download-markdown]"),
  openSettings: document.querySelector("[data-open-settings]"),
  closeSettingsButtons: Array.from(document.querySelectorAll("[data-close-settings]")),
  settingsOverlay: document.querySelector("[data-settings-overlay]"),
  settingsBack: document.querySelector("[data-settings-back]"),
  settingsTitle: document.querySelector("[data-settings-title]"),
  settingsPages: Array.from(document.querySelectorAll("[data-settings-page]")),
  settingsPageButtons: Array.from(document.querySelectorAll("[data-open-settings-page]")),
  configStatus: document.querySelector("[data-config-status]"),
  historySearch: document.querySelector("[data-history-search]"),
  historyCount: document.querySelector("[data-history-count]"),
  historyList: document.querySelector("[data-history-list]"),
  exportData: document.querySelector("[data-export-data]"),
  importData: document.querySelector("[data-import-data]"),
  importFile: document.querySelector("[data-import-file]"),
  clearHistory: document.querySelector("[data-clear-history]"),
  modelForms: Array.from(document.querySelectorAll("[data-model-kind]")),
  subtitleModal: document.querySelector("[data-subtitle-modal]"),
  subtitleErrorText: document.querySelector("[data-subtitle-error-text]"),
  skipSubtitleButton: document.querySelector("[data-skip-subtitle]"),
  dismissSubtitleButtons: Array.from(document.querySelectorAll("[data-dismiss-subtitle-modal]")),
};

const terminalTaskStatuses = new Set(["completed", "failed", "canceled", "abandoned"]);
const recoverableSubtitleErrorCodes = new Set([
  "subtitle_not_found",
  "subtitle_timeout",
  "subtitle_fetch_failed",
  "subtitle_format_unrecognized",
  "subtitle_empty_after_cleaning",
  "subtitle_bilibili_returned_error",
]);
const workflowStateClasses = [
  "app-state-idle",
  "app-state-running",
  "app-state-completed",
  "app-state-failed",
];
let pollTimer = null;
let lastTaskInput = "";

function getWorkflowState(task) {
  if (task.status === "idle") {
    return "idle";
  }

  if (task.status === "completed") {
    return "completed";
  }

  if (["failed", "canceled", "abandoned"].includes(task.status)) {
    return "failed";
  }

  return "running";
}

function renderWorkflowState(task) {
  const workflowState = getWorkflowState(task);
  elements.appShell.classList.remove(...workflowStateClasses);
  elements.appShell.classList.add(`app-state-${workflowState}`);
  elements.workflowShell.dataset.workflowState = workflowState;
  elements.taskView.setAttribute(
    "aria-hidden",
    workflowState === "idle" || workflowState === "completed" ? "true" : "false",
  );
  elements.resultView.setAttribute("aria-hidden", workflowState === "completed" ? "false" : "true");
}

function redactSecrets(value) {
  return String(value)
    .replace(/Bearer\s+[^\s]+/gi, "Bearer [REDACTED]")
    .replace(/Authorization\s*:\s*[^\s]+/gi, "Authorization: [REDACTED]")
    .replace(/sk-[A-Za-z0-9_-]{8,}/g, "[REDACTED]")
    .replace(/[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}/g, "[REDACTED]");
}

function localTime(isoTime) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(isoTime));
}

function parseBilibiliInput(input) {
  const text = input.trim();
  const bvMatch = text.match(/\bBV[0-9A-Za-z]{8,12}\b/);
  const urlMatch = text.match(/https?:\/\/[^\s，。"'<>]+/i);
  const titleGuess = urlMatch
    ? text.slice(0, urlMatch.index).trim().replace(/[，,。:\s]+$/, "")
    : "";
  const videoTitle = titleGuess && titleGuess !== text ? titleGuess : "";

  if (bvMatch) {
    const bvId = bvMatch[0];
    const url = urlMatch?.[0]?.includes("bilibili.com/video/")
      ? urlMatch[0]
      : `https://www.bilibili.com/video/${bvId}/`;
    return { bvId, url, display: url, videoTitle };
  }

  if (urlMatch?.[0]?.includes("bilibili.com/video/")) {
    return { bvId: "", url: urlMatch[0], display: urlMatch[0], videoTitle };
  }

  return null;
}

function renderHealth() {
  const state = getHealthState();
  elements.statusText.textContent = state.message;
  elements.statusDot.dataset.state = state.status;
}

function formatLog(log) {
  return `[${localTime(log.time)}] ${log.level.toUpperCase()} ${log.message}`;
}

function renderTask() {
  const task = getTaskState();
  const workflowState = getWorkflowState(task);
  const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
  const logText = task.logs.length ? task.logs.map(formatLog).join("\n") : "暂无日志";
  const canCancel = ["pending", "running"].includes(task.status);
  const hasFinalMarkdown = Boolean(task.finalMarkdown);
  const displayTitle =
    task.videoTitle || (workflowState === "idle" ? "等待视频标题" : "正在获取视频标题");
  const fallbackError =
    task.status === "canceled"
      ? "任务已取消，可以返回输入态或直接重新提交。"
      : task.status === "abandoned"
        ? "任务长时间未轮询，已被标记为 abandoned。"
        : "";

  renderWorkflowState(task);
  elements.videoTitle.textContent = displayTitle;
  elements.progressBar.value = progress;
  elements.progressLabel.textContent = `${progress}%`;
  elements.stageText.textContent = task.stage;
  elements.recognizedInput.textContent = task.recognizedInput
    ? `识别结果：${task.recognizedInput}`
    : "";
  elements.errorMessage.textContent = task.error || fallbackError;
  elements.logPanel.textContent = logText;
  elements.fullLog.textContent = logText;
  elements.cleanSubtitle.textContent = task.cleanSubtitle || "暂无内容";
  elements.aiTranscript.textContent = task.aiTranscript || "暂无内容";
  elements.cancelTask.disabled = !task.taskId || !canCancel;
  elements.taskInput.disabled = workflowState !== "idle";
  elements.sendButton.disabled = workflowState !== "idle" || canCancel;
  elements.finalMarkdown.textContent = task.finalMarkdown;
  elements.downloadMarkdown.disabled = !hasFinalMarkdown;

  elements.logPanel.scrollTop = elements.logPanel.scrollHeight;
}

function showSubtitleFailureModal(message) {
  if (!elements.subtitleModal) {
    return;
  }
  elements.subtitleErrorText.textContent =
    message || "字幕处理失败，可以选择跳过字幕继续后续流程。";
  elements.subtitleModal.hidden = false;
}

function hideSubtitleFailureModal() {
  if (!elements.subtitleModal) {
    return;
  }
  elements.subtitleModal.hidden = true;
}

function shouldOfferSubtitleSkip(payload) {
  return payload.status === "failed" && recoverableSubtitleErrorCodes.has(payload.error_code || "");
}

function log(level, message) {
  appendTaskLog(level, redactSecrets(message));
  renderTask();
}

function stopPolling() {
  if (pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

function applyTaskResponse(payload) {
  const currentTask = getTaskState();
  const result = payload.result || {};
  const hasUserVisibleTitle =
    currentTask.videoTitle && currentTask.videoTitle !== "正在获取视频标题";
  setTaskState({
    taskId: payload.task_id || "",
    status: payload.status || "unknown",
    progress: payload.progress ?? 0,
    stage: payload.stage || "未知阶段",
    logs: Array.isArray(payload.logs) ? payload.logs : [],
    recognizedInput: result.parsed_input || result.webpage_url || currentTask.recognizedInput,
    finalMarkdown: result.final_markdown || "",
    cleanSubtitle: result.clean_subtitle || "",
    aiTranscript: result.ai_transcript || "",
    videoTitle: hasUserVisibleTitle ? currentTask.videoTitle : result.title || currentTask.videoTitle,
    filename: result.filename || "final.md",
    error: payload.error || "",
    errorCode: payload.error_code || "",
  });
  renderTask();

  if (shouldOfferSubtitleSkip(payload)) {
    showSubtitleFailureModal(payload.error);
  }

  if (terminalTaskStatuses.has(payload.status)) {
    stopPolling();
  }
}

async function pollTask(taskId) {
  try {
    const payload = await fetchTask(taskId);
    applyTaskResponse(payload);
  } catch (error) {
    log("error", error instanceof Error ? error.message : "任务状态读取失败");
    stopPolling();
  }
}

function startPolling(taskId) {
  stopPolling();
  pollTimer = window.setInterval(() => pollTask(taskId), 1000);
}

async function refreshHealth() {
  setHealthState({ status: "checking", message: "正在连接" });
  renderHealth();

  try {
    const result = await fetchHealth();
    setHealthState({
      status: "ok",
      message: `${result.service} 已连接`,
    });
  } catch (error) {
    setHealthState({
      status: "error",
      message: "后端未连接",
    });
  }

  renderHealth();
}

function readModelForm(form) {
  const data = new FormData(form);
  return {
    baseUrl: String(data.get("baseUrl") || "").trim(),
    apiKey: String(data.get("apiKey") || ""),
    model: String(data.get("model") || "").trim(),
    temperature: Number(data.get("temperature") || 0),
    stream: data.get("stream") === "on",
  };
}

function writeModelForm(form, config) {
  form.elements.baseUrl.value = config.baseUrl || "";
  form.elements.apiKey.value = config.apiKey || "";
  form.elements.model.value = config.model || "";
  form.elements.temperature.value = String(config.temperature ?? 0);
  form.elements.stream.checked = config.stream !== false;
}

function populateModelOptions(form, models) {
  const options = form.querySelector("[data-model-options]");
  const picker = form.querySelector("[data-model-picker]");
  const pickerPlaceholder = document.createElement("option");
  pickerPlaceholder.value = "";
  pickerPlaceholder.textContent = models.length ? "选择模型" : "未返回模型";

  options.replaceChildren();
  picker.replaceChildren(pickerPlaceholder);

  for (const model of models) {
    const listOption = document.createElement("option");
    listOption.value = model;
    options.append(listOption);

    const pickerOption = document.createElement("option");
    pickerOption.value = model;
    pickerOption.textContent = model;
    picker.append(pickerOption);
  }

  picker.disabled = models.length === 0;
  form.classList.toggle("has-model-options", models.length > 0);
}

function showSettingsPage(pageName) {
  for (const page of elements.settingsPages) {
    page.classList.toggle("is-active", page.dataset.settingsPage === pageName);
  }

  const isMenu = pageName === "menu";
  elements.settingsBack.hidden = isMenu;
  elements.settingsTitle.textContent =
    pageName === "models" ? "模型配置" : pageName === "history" ? "历史记录" : "设置";
}

function openSettings() {
  elements.settingsOverlay.hidden = false;
  document.body.classList.add("settings-open");
  showSettingsPage("menu");
}

function closeSettings() {
  elements.settingsOverlay.hidden = true;
  document.body.classList.remove("settings-open");
}

async function restoreModelConfigs() {
  await Promise.all(
    elements.modelForms.map(async (form) => {
      const kind = form.dataset.modelKind;
      const config = await getModelConfig(kind);
      writeModelForm(form, config);
    }),
  );
  elements.configStatus.textContent = "已恢复本地配置";
}

async function saveConfig(form) {
  const kind = form.dataset.modelKind;
  const config = readModelForm(form);
  await saveModelConfig(kind, config);
  elements.configStatus.textContent = "已保存";
  log("info", `${modelLabels[kind]}配置已保存到 IndexedDB`);
}

async function loadModels(form) {
  const kind = form.dataset.modelKind;
  const config = readModelForm(form);

  if (!config.baseUrl || !config.apiKey) {
    elements.configStatus.textContent = "请先填写 API Base URL 和 API Key";
    log("warning", `${modelLabels[kind]}需要先填写 API Base URL 和 API Key`);
    return;
  }

  await saveModelConfig(kind, config);
  elements.configStatus.textContent = "正在获取模型";
  log("info", `正在获取${modelLabels[kind]}列表`);

  try {
    const models = await fetchModelList(config);
    populateModelOptions(form, models);
    elements.configStatus.textContent = `已加载 ${models.length} 个模型`;
    log("info", `${modelLabels[kind]}返回 ${models.length} 个模型`);
  } catch (error) {
    elements.configStatus.textContent = "模型列表获取失败";
    log("error", error instanceof Error ? error.message : "模型列表获取失败");
  }
}

async function renderHistory() {
  const searchTerm = elements.historySearch.value;
  const records = await listHistoryRecords(searchTerm);
  elements.historyCount.textContent = `${records.length} 条`;
  elements.historyList.replaceChildren();

  if (!records.length) {
    const empty = document.createElement("p");
    empty.className = "empty-text";
    empty.textContent = "暂无历史记录";
    elements.historyList.append(empty);
    return;
  }

  for (const record of records) {
    const item = document.createElement("article");
    item.className = "history-item";

    const title = document.createElement("h3");
    title.textContent = record.title;

    const meta = document.createElement("p");
    meta.textContent = `${new Date(record.createdAt).toLocaleString("zh-CN")} · ${
      record.error ? "失败" : "已保存"
    }`;

    item.append(title, meta);
    elements.historyList.append(item);
  }
}

function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], {
    type: "application/json;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function downloadText(filename, text) {
  const blob = new Blob([text], {
    type: "text/markdown;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function updateInputStatus() {
  const rawInput = elements.taskInput.value.trim();
  elements.inputForm.classList.toggle("has-content", rawInput.length > 0);
  elements.inputStatus.textContent = "";
}

function resetWorkflow() {
  stopPolling();
  hideSubtitleFailureModal();
  lastTaskInput = "";
  setTaskState({
    taskId: "",
    status: "idle",
    progress: 0,
    stage: "等待创建任务",
    recognizedInput: "",
    videoTitle: "",
    logs: [],
    finalMarkdown: "",
    cleanSubtitle: "",
    aiTranscript: "",
    filename: "final.md",
    error: "",
    errorCode: "",
  });
  elements.taskInput.value = "";
  elements.inputStatus.textContent = "";
  renderTask();
  updateInputStatus();
  elements.taskInput.focus({ preventScroll: true });
}

async function submitTask({ rawInput = elements.taskInput.value.trim(), skipSubtitleIfFailed = false } = {}) {
  const currentTask = getTaskState();
  if (["pending", "running"].includes(currentTask.status)) {
    return;
  }

  rawInput = rawInput.trim();
  const parsed = parseBilibiliInput(rawInput);

  if (!rawInput) {
    elements.inputStatus.textContent = "";
    return;
  }

  if (!parsed) {
    elements.inputStatus.textContent = "未识别到 B站视频链接或 BV 号";
    return;
  }

  stopPolling();
  hideSubtitleFailureModal();
  lastTaskInput = rawInput;
  setTaskState({
    taskId: "",
    status: "pending",
    progress: 0,
    stage: "正在创建任务",
    recognizedInput: parsed.display,
    videoTitle: parsed.videoTitle || "正在获取视频标题",
    logs: [],
    finalMarkdown: "",
    cleanSubtitle: "",
    aiTranscript: "",
    filename: "final.md",
    error: "",
    errorCode: "",
  });
  elements.inputStatus.textContent = "";
  renderTask();
  log("info", `已识别输入：${parsed.display}`);
  if (skipSubtitleIfFailed) {
    log("warning", "已选择跳过 B站字幕，后续流程将仅依赖 AI 音频转文字稿");
  }

  try {
    const configs = await getAllModelConfigs();
    const payload = await createTask({
      input: rawInput,
      transcriptionConfig: configs.transcription,
      refineConfig: configs.refine,
      options: {
        skipSubtitleIfFailed,
      },
    });
    applyTaskResponse(payload);
    if (!terminalTaskStatuses.has(payload.status)) {
      startPolling(payload.task_id);
    }
  } catch (error) {
    setTaskState({
      status: "failed",
      progress: 0,
      stage: "创建任务失败",
      error: error instanceof Error ? error.message : "创建任务失败",
      errorCode: "",
    });
    log("error", error instanceof Error ? error.message : "创建任务失败");
  }
}

async function handleStartTask(event) {
  event.preventDefault();
  await submitTask();
}

async function handleSkipSubtitle() {
  const rawInput = lastTaskInput || elements.taskInput.value.trim();
  if (!rawInput) {
    hideSubtitleFailureModal();
    return;
  }
  await submitTask({ rawInput, skipSubtitleIfFailed: true });
}

async function handleCancelTask() {
  const task = getTaskState();
  if (!task.taskId || !["pending", "running"].includes(task.status)) {
    return;
  }

  elements.cancelTask.disabled = true;
  try {
    const payload = await cancelTask(task.taskId);
    applyTaskResponse(payload);
  } catch (error) {
    log("error", error instanceof Error ? error.message : "取消任务失败");
  }
}

async function handleExport() {
  const data = await exportAllData();
  const date = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  downloadJson(`bilibili-transcription-data-${date}.json`, data);
  log("info", "已导出 IndexedDB 配置和历史数据");
}

async function handleImportFile(file) {
  try {
    const payload = JSON.parse(await file.text());
    await importAllData(payload);
    await restoreModelConfigs();
    await renderHistory();
    log("info", "已导入 IndexedDB 配置和历史数据");
  } catch (error) {
    log("error", error instanceof Error ? error.message : "导入失败");
  } finally {
    elements.importFile.value = "";
  }
}

async function handleClearHistory() {
  if (!confirm("确认清空历史记录？模型配置会保留。")) {
    return;
  }

  await clearHistoryRecords();
  await renderHistory();
  log("warning", "历史记录已清空");
}

function bindEvents() {
  elements.refreshButton.addEventListener("click", refreshHealth);
  elements.taskInput.addEventListener("input", updateInputStatus);
  elements.inputForm.addEventListener("submit", handleStartTask);
  elements.cancelTask.addEventListener("click", handleCancelTask);
  elements.skipSubtitleButton.addEventListener("click", handleSkipSubtitle);
  elements.dismissSubtitleButtons.forEach((button) =>
    button.addEventListener("click", hideSubtitleFailureModal),
  );
  elements.resetWorkflowButtons.forEach((button) =>
    button.addEventListener("click", resetWorkflow),
  );
  elements.downloadMarkdown.addEventListener("click", () => {
    const task = getTaskState();
    downloadText(task.filename || "final.md", task.finalMarkdown);
  });
  elements.openSettings.addEventListener("click", openSettings);
  elements.closeSettingsButtons.forEach((button) => button.addEventListener("click", closeSettings));
  elements.settingsBack.addEventListener("click", () => showSettingsPage("menu"));
  elements.settingsPageButtons.forEach((button) => {
    button.addEventListener("click", () => showSettingsPage(button.dataset.openSettingsPage));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !elements.settingsOverlay.hidden) {
      closeSettings();
    }
    if (event.key === "Escape" && elements.subtitleModal && !elements.subtitleModal.hidden) {
      hideSubtitleFailureModal();
    }
  });

  for (const form of elements.modelForms) {
    form.addEventListener("submit", (event) => event.preventDefault());
    form.querySelector("[data-save-config]").addEventListener("click", () => saveConfig(form));
    form.querySelector("[data-fetch-models]").addEventListener("click", () => loadModels(form));
    form.querySelector("[data-model-picker]").addEventListener("change", (event) => {
      if (!event.target.value) {
        return;
      }
      form.elements.model.value = event.target.value;
      event.target.value = "";
    });
  }

  elements.historySearch.addEventListener("input", renderHistory);
  elements.exportData.addEventListener("click", handleExport);
  elements.importData.addEventListener("click", () => elements.importFile.click());
  elements.importFile.addEventListener("change", () => {
    const [file] = elements.importFile.files;
    if (file) {
      handleImportFile(file);
    }
  });
  elements.clearHistory.addEventListener("click", handleClearHistory);
}

async function init() {
  bindEvents();
  renderHealth();
  renderTask();
  updateInputStatus();

  try {
    await initDatabase();
    await restoreModelConfigs();
    await renderHistory();
  } catch (error) {
    log("error", error instanceof Error ? error.message : "IndexedDB 初始化失败");
  }

  refreshHealth();
}

init();
