import {
  cancelTask,
  createTask,
  extractBilibiliCookieFromProfile,
  fetchHealth,
  fetchModelList,
  fetchTask,
  openBilibiliLoginWindow,
  retryTranscription,
} from "./api.js";
import {
  simplifyBilibiliCookieHeader,
  summarizeBilibiliCookie,
} from "./bilibiliCookie.js";
import {
  clearAllLocalData,
  deleteHistoryRecord,
  exportAllData,
  getAllModelConfigs,
  getBilibiliAccessSettings,
  getModelConfig,
  importAllData,
  initDatabase,
  listHistoryRecords,
  saveBilibiliAccessSettings,
  saveHistoryRecord,
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
  pageWarning: document.querySelector("[data-page-warning]"),
  refreshButton: document.querySelector("[data-refresh-health]"),
  workflowShell: document.querySelector("[data-workflow-shell]"),
  videoTitle: document.querySelector("[data-video-title]"),
  videoPartTitle: document.querySelector("[data-video-part-title]"),
  taskView: document.querySelector("[data-task-view]"),
  resultView: document.querySelector("[data-result-view]"),
  inputForm: document.querySelector("[data-input-form]"),
  taskInput: document.querySelector("[data-task-input]"),
  inputStatus: document.querySelector("[data-input-status]"),
  sendButton: document.querySelector(".send-button"),
  progressBar: document.querySelector("[data-progress-bar]"),
  progressLabel: document.querySelector("[data-progress-label]"),
  stageText: document.querySelector("[data-stage-text]"),
  taskElapsed: document.querySelector("[data-task-elapsed]"),
  resultDuration: document.querySelector("[data-result-duration]"),
  errorMessage: document.querySelector("[data-error-message]"),
  logPanel: document.querySelector("[data-log-panel]"),
  cancelTask: document.querySelector("[data-cancel-task]"),
  resetWorkflowButtons: Array.from(document.querySelectorAll("[data-reset-workflow]")),
  finalMarkdown: document.querySelector("[data-final-markdown]"),
  cleanSubtitle: document.querySelector("[data-clean-subtitle]"),
  aiTranscript: document.querySelector("[data-ai-transcript]"),
  fullLog: document.querySelector("[data-full-log]"),
  downloadMarkdown: document.querySelector("[data-download-markdown]"),
  copyMarkdown: document.querySelector("[data-copy-markdown]"),
  downloadCleanSubtitle: document.querySelector("[data-download-clean-subtitle]"),
  downloadAiTranscript: document.querySelector("[data-download-ai-transcript]"),
  downloadFullLog: document.querySelector("[data-download-full-log]"),
  intermediatePanel: document.querySelector("[data-intermediate-panel]"),
  openSettings: document.querySelector("[data-open-settings]"),
  closeSettingsButtons: Array.from(document.querySelectorAll("[data-close-settings]")),
  settingsOverlay: document.querySelector("[data-settings-overlay]"),
  settingsBack: document.querySelector("[data-settings-back]"),
  settingsTitle: document.querySelector("[data-settings-title]"),
  settingsPages: Array.from(document.querySelectorAll("[data-settings-page]")),
  settingsPageButtons: Array.from(document.querySelectorAll("[data-open-settings-page]")),
  configStatus: document.querySelector("[data-config-status]"),
  bilibiliAccessStatus: document.querySelector("[data-bilibili-access-status]"),
  bilibiliAccessMode: document.querySelector("[data-bilibili-access-mode]"),
  bilibiliCookieBrowser: document.querySelector("[data-bilibili-cookie-browser]"),
  bilibiliCookieHeader: document.querySelector("[data-bilibili-cookie-header]"),
  bilibiliCookieHeaderStatus: document.querySelector("[data-bilibili-cookie-header-status]"),
  bilibiliCookiesFile: document.querySelector("[data-bilibili-cookies-file]"),
  bilibiliCookiesFileStatus: document.querySelector("[data-bilibili-cookies-file-status]"),
  browserCookieField: document.querySelector("[data-browser-cookie-field]"),
  cookieHeaderField: document.querySelector("[data-cookie-header-field]"),
  cookiesFileField: document.querySelector("[data-cookies-file-field]"),
  profileLoginField: document.querySelector("[data-profile-login-field]"),
  openBilibiliLogin: document.querySelector("[data-open-bilibili-login]"),
  refreshBilibiliProfile: document.querySelector("[data-refresh-bilibili-profile]"),
  bilibiliProfileStatus: document.querySelector("[data-bilibili-profile-status]"),
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
  transcriptionRetryModal: document.querySelector("[data-transcription-retry-modal]"),
  transcriptionRetryErrorText: document.querySelector("[data-transcription-retry-error-text]"),
  retryTranscriptionButton: document.querySelector("[data-retry-transcription]"),
  changeTranscriptionModelButton: document.querySelector("[data-change-transcription-model]"),
  cancelTranscriptionTaskButton: document.querySelector("[data-cancel-transcription-task]"),
  dismissTranscriptionRetryButtons: Array.from(document.querySelectorAll("[data-dismiss-transcription-retry-modal]")),
  credentialModal: document.querySelector("[data-credential-modal]"),
  credentialStatus: document.querySelector("[data-credential-status]"),
  closeCredentialButtons: Array.from(document.querySelectorAll("[data-close-credential-guide]")),
  openBilibiliLoginGuide: document.querySelector("[data-open-bilibili-login-guide]"),
  extractBilibiliCookie: document.querySelector("[data-extract-bilibili-cookie]"),
  manualCookieGuide: document.querySelector("[data-manual-cookie-guide]"),
  saveManualCookieGuide: document.querySelector("[data-save-manual-cookie-guide]"),
  copyLocalCookie: document.querySelector("[data-copy-local-cookie]"),
  refreshLocalCookie: document.querySelector("[data-refresh-local-cookie]"),
  clearLocalCookie: document.querySelector("[data-clear-local-cookie]"),
};

const terminalTaskStatuses = new Set(["completed", "failed", "canceled", "abandoned", "waiting_model_retry"]);
const elapsedFreezeStatuses = new Set(["completed", "failed", "canceled", "abandoned", "waiting_model_retry"]);
const historyPersistStatuses = new Set(["completed", "failed", "canceled", "abandoned"]);
const recoverableSubtitleErrorCodes = new Set([
  "subtitle_not_found",
  "subtitle_timeout",
  "subtitle_fetch_failed",
  "subtitle_format_unrecognized",
  "subtitle_empty_after_cleaning",
  "subtitle_bilibili_returned_error",
]);
const credentialRefreshErrorCodes = new Set([
  "bilibili_http_412",
  "bilibili_http_403",
  "login_required",
  "cookie_invalid",
  "cookie_database_copy_failed",
]);
const transcriptionRetryErrorCodes = new Set([
  "transcription_base_url_invalid",
  "transcription_api_key_missing",
  "transcription_model_missing",
  "transcription_api_timeout",
  "transcription_network_error",
  "transcription_auth_failed",
  "transcription_rate_limited",
  "transcription_request_too_large",
  "transcription_gemini_request_invalid",
  "transcription_gemini_upload_failed",
  "transcription_provider_invalid",
  "transcription_provider_not_implemented",
  "transcription_audio_unsupported",
  "transcription_api_error",
  "transcription_invalid_response",
  "transcription_empty_response",
  "transcription_audio_read_failed",
]);
const workflowStateClasses = [
  "app-state-idle",
  "app-state-running",
  "app-state-completed",
  "app-state-failed",
];
const pageInstanceId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
let lastCollapsedIntermediateResultKey = "";
const pagePresenceChannelName = "bilibili-transcription-page-presence";
const activePageIds = new Set([pageInstanceId]);
let pagePresenceChannel = null;
let pagePresenceTimer = null;
let pollTimer = null;
let lastTaskInput = "";
let taskGeneration = 0;
let bilibiliAccessSaveTimer = null;
let bilibiliCredentialSessionToken = "";
let bilibiliCredentialReady = false;

function renderPageWarning() {
  if (!elements.pageWarning) {
    return;
  }

  elements.pageWarning.hidden = activePageIds.size < 2;
}

function announcePagePresence(type) {
  if (!pagePresenceChannel) {
    return;
  }

  pagePresenceChannel.postMessage({ type, pageInstanceId });
}

function handlePagePresenceMessage(event) {
  const message = event.data || {};
  if (!message.pageInstanceId || message.pageInstanceId === pageInstanceId) {
    return;
  }

  if (message.type === "hello") {
    activePageIds.add(message.pageInstanceId);
    announcePagePresence("pong");
  }

  if (message.type === "pong") {
    activePageIds.add(message.pageInstanceId);
  }

  if (message.type === "bye") {
    activePageIds.delete(message.pageInstanceId);
  }

  renderPageWarning();
}

function initPagePresenceWarning() {
  if (!("BroadcastChannel" in window)) {
    return;
  }

  pagePresenceChannel = new BroadcastChannel(pagePresenceChannelName);
  pagePresenceChannel.addEventListener("message", handlePagePresenceMessage);
  announcePagePresence("hello");
  pagePresenceTimer = window.setInterval(() => announcePagePresence("hello"), 5000);
  renderPageWarning();

  window.addEventListener("pagehide", () => {
    announcePagePresence("bye");
    if (pagePresenceTimer) {
      window.clearInterval(pagePresenceTimer);
    }
    pagePresenceChannel?.close();
  });
}

function getWorkflowState(task) {
  if (task.status === "idle") {
    return "idle";
  }

  if (task.status === "completed") {
    return "completed";
  }

  if (["failed", "canceled", "abandoned", "waiting_model_retry"].includes(task.status)) {
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
    .replace(/Bearer\s+[^\s,;]+/gi, "Bearer [REDACTED]")
    .replace(/(Authorization\s*[:=]\s*)(Bearer\s+)?[^\s,;]+/gi, "$1[REDACTED]")
    .replace(/(Cookie\s*[:=]\s*)[^\r\n]+/gi, "$1[REDACTED]")
    .replace(/(api[_-]?key\s*[:=]\s*)[^\s,;]+/gi, "$1[REDACTED]")
    .replace(/\b(SESSDATA|bili_jct|DedeUserID|DedeUserID__ckMd5|bili_ticket|bili_ticket_expires)\s*=\s*[^;\s]+/gi, "$1=[REDACTED]")
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

function formatElapsedDuration(milliseconds) {
  const totalSeconds = Math.max(0, Math.floor((Number(milliseconds) || 0) / 1000));
  const seconds = totalSeconds % 60;
  const totalMinutes = Math.floor(totalSeconds / 60);
  const minutes = totalMinutes % 60;
  const hours = Math.floor(totalMinutes / 60);

  if (hours > 0) {
    return `${hours}h ${minutes}m ${seconds}s`;
  }
  if (totalMinutes > 0) {
    return `${totalMinutes}m ${seconds}s`;
  }
  return `${seconds}s`;
}

function taskElapsedMilliseconds(task) {
  const startedAt = Number(task.taskStartedAt || 0);
  if (!startedAt) {
    return 0;
  }
  const finishedAt = Number(task.taskFinishedAt || 0);
  return Math.max(0, (finishedAt || Date.now()) - startedAt);
}

function taskElapsedSeconds(task) {
  return Math.max(0, Math.round(taskElapsedMilliseconds(task) / 1000));
}

function stripOuterTitleBrackets(value) {
  let title = String(value || "").trim();
  let previous = "";
  while (title !== previous) {
    previous = title;
    title = title.replace(/^【([\s\S]+)】$/, "$1").trim();
  }
  return title;
}

function readableTitle(value, fallback = "") {
  return stripOuterTitleBrackets(value) || fallback;
}

function currentSubTaskFrom(items = [], pIndex = null) {
  const subTasks = Array.isArray(items) ? items : [];
  if (!subTasks.length) {
    return null;
  }

  const currentPIndex = Number(pIndex);
  if (pIndex !== null && pIndex !== undefined && pIndex !== "" && Number.isFinite(currentPIndex)) {
    const matched = subTasks.find((subTask) => Number(subTask.pIndex) === currentPIndex);
    if (matched) {
      return matched;
    }
  }

  return subTasks.find((subTask) => subTask.finalMarkdown || subTask.aiTranscript || subTask.cleanSubtitle) || subTasks[0];
}

function formatPartHeading(subTask) {
  if (!subTask) {
    return "";
  }
  const title = readableTitle(subTask.title || "");
  const pIndex = Number(subTask.pIndex);
  const hasPIndex =
    subTask.pIndex !== null && subTask.pIndex !== undefined && subTask.pIndex !== "" && Number.isFinite(pIndex);

  if (title && hasPIndex) {
    return "P" + pIndex + "：" + title;
  }
  if (title) {
    return title;
  }
  return hasPIndex ? "P" + pIndex : "";
}

function currentPartHeading(recordOrTask) {
  const subTasks = Array.isArray(recordOrTask.subTasks) ? recordOrTask.subTasks : [];
  if (subTasks.length <= 1) {
    return "";
  }
  return formatPartHeading(currentSubTaskFrom(subTasks, recordOrTask.pIndex));
}

function formatHistoryDate(value) {
  const date = new Date(value || Date.now());
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.getFullYear() + "." + (date.getMonth() + 1) + "." + date.getDate();
}

function historyFinishedAt(createdAt, durationSeconds) {
  const duration = Number(durationSeconds);
  if (!Number.isFinite(duration) || duration <= 0) {
    return createdAt;
  }
  return createdAt + duration * 1000;
}

function isAbnormalHistoryRecord(record = {}) {
  return record.status !== "completed" || !String(record.finalMarkdown || "").trim();
}

function isSafeMarkdownLink(href) {
  try {
    const parsed = new URL(href, window.location.origin);
    return ["http:", "https:", "mailto:"].includes(parsed.protocol);
  } catch {
    return false;
  }
}

function appendInlineMarkdown(target, text) {
  const source = String(text || "");
  const inlinePattern = /(\[([^\]]+)\]\(([^)]+)\)|\*\*([^*]+)\*\*|\x60([^\x60]+)\x60|\*([^*]+)\*)/g;
  let cursor = 0;
  let match;

  while ((match = inlinePattern.exec(source)) !== null) {
    if (match.index > cursor) {
      target.append(document.createTextNode(source.slice(cursor, match.index)));
    }

    if (match[2] && match[3] && isSafeMarkdownLink(match[3])) {
      const link = document.createElement("a");
      link.href = match[3];
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = match[2];
      target.append(link);
    } else if (match[4]) {
      const strong = document.createElement("strong");
      strong.textContent = match[4];
      target.append(strong);
    } else if (match[5]) {
      const code = document.createElement("code");
      code.textContent = match[5];
      target.append(code);
    } else if (match[6]) {
      const emphasis = document.createElement("em");
      emphasis.textContent = match[6];
      target.append(emphasis);
    } else {
      target.append(document.createTextNode(match[0]));
    }

    cursor = inlinePattern.lastIndex;
  }

  if (cursor < source.length) {
    target.append(document.createTextNode(source.slice(cursor)));
  }
}

function appendInlineMarkdownWithBreaks(target, text) {
  String(text || "")
    .split("\n")
    .forEach((line, index) => {
      if (index > 0) {
        target.append(document.createElement("br"));
      }
      appendInlineMarkdown(target, line);
    });
}

function isMarkdownBlockStart(line) {
  return (
    /^#{1,6}\s+/.test(line) ||
    /^>\s?/.test(line) ||
    /^[-*+]\s+/.test(line) ||
    /^\d+[.)]\s+/.test(line) ||
    /^\x60\x60\x60/.test(line) ||
    /^-{3,}\s*$/.test(line)
  );
}

function renderMarkdownPreview(target, markdown) {
  target.replaceChildren();
  const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (!line.trim()) {
      index += 1;
      continue;
    }

    const codeFence = line.match(/^\x60\x60\x60\s*([^\x60]*)$/);
    if (codeFence) {
      const codeLines = [];
      index += 1;
      while (index < lines.length && !/^\x60\x60\x60\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      index += index < lines.length ? 1 : 0;
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      if (codeFence[1].trim()) {
        code.dataset.language = codeFence[1].trim();
      }
      code.textContent = codeLines.join("\n");
      pre.append(code);
      target.append(pre);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = Math.min(6, heading[1].length);
      const element = document.createElement("h" + level);
      appendInlineMarkdown(element, heading[2].trim());
      target.append(element);
      index += 1;
      continue;
    }

    if (/^-{3,}\s*$/.test(line)) {
      target.append(document.createElement("hr"));
      index += 1;
      continue;
    }

    if (/^>\s?/.test(line)) {
      const quoteLines = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        quoteLines.push(lines[index].replace(/^>\s?/, ""));
        index += 1;
      }
      const quote = document.createElement("blockquote");
      appendInlineMarkdownWithBreaks(quote, quoteLines.join("\n"));
      target.append(quote);
      continue;
    }

    const unorderedList = line.match(/^[-*+]\s+(.+)$/);
    const orderedList = line.match(/^\d+[.)]\s+(.+)$/);
    if (unorderedList || orderedList) {
      const isOrdered = Boolean(orderedList);
      const list = document.createElement(isOrdered ? "ol" : "ul");
      while (index < lines.length) {
        const itemMatch = lines[index].match(isOrdered ? /^\d+[.)]\s+(.+)$/ : /^[-*+]\s+(.+)$/);
        if (!itemMatch) {
          break;
        }
        const item = document.createElement("li");
        appendInlineMarkdown(item, itemMatch[1].trim());
        list.append(item);
        index += 1;
      }
      target.append(list);
      continue;
    }

    const paragraphLines = [];
    while (index < lines.length && lines[index].trim() && !isMarkdownBlockStart(lines[index])) {
      paragraphLines.push(lines[index]);
      index += 1;
    }
    const paragraph = document.createElement("p");
    appendInlineMarkdownWithBreaks(paragraph, paragraphLines.join("\n"));
    target.append(paragraph);
  }

  if (!target.childElementCount) {
    const empty = document.createElement("p");
    empty.className = "empty-text";
    empty.textContent = "暂无最终文稿";
    target.append(empty);
  }
}

function buildIntermediateCollapseKey(task) {
  const markdown = String(task.finalMarkdown || "");
  if (!markdown) {
    return "";
  }

  return [
    task.historyId || task.taskId || "local-result",
    task.filename || "final.md",
    task.taskFinishedAt || task.historyStartedAt || task.taskStartedAt || "",
    markdown.length,
  ].join("|");
}

function collapseIntermediatePanelForNewResult(task) {
  if (!elements.intermediatePanel) {
    return;
  }

  const resultKey = buildIntermediateCollapseKey(task);
  if (!resultKey || resultKey === lastCollapsedIntermediateResultKey) {
    return;
  }

  elements.intermediatePanel.open = false;
  lastCollapsedIntermediateResultKey = resultKey;
}

function parseBilibiliInput(input) {
  const text = input.trim();
  const bvMatch = text.match(/\bBV[0-9A-Za-z]{8,12}\b/);
  const urlMatch = text.match(/https?:\/\/[^\s，。"'<>]+/i);
  const matchedUrl = urlMatch?.[0] || "";
  const isBilibiliUrl = /(^https?:\/\/)?([^/]+\.)?(bilibili\.com|b23\.tv)\b/i.test(matchedUrl);
  const titleGuess = urlMatch
    ? text.slice(0, urlMatch.index).trim().replace(/[，,。:\s]+$/, "")
    : "";
  const videoTitle = titleGuess && titleGuess !== text ? titleGuess : "";

  if (bvMatch) {
    const bvId = bvMatch[0];
    const url = isBilibiliUrl
      ? matchedUrl
      : `https://www.bilibili.com/video/${bvId}/`;
    return { bvId, url, display: url, videoTitle };
  }

  if (isBilibiliUrl) {
    return { bvId: "", url: matchedUrl, display: matchedUrl, videoTitle };
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
  const canCancel = ["pending", "running", "waiting_model_retry"].includes(task.status);
  const hasFinalMarkdown = Boolean(task.finalMarkdown);
  const hasCleanSubtitle = Boolean(task.cleanSubtitle);
  const hasAiTranscript = Boolean(task.aiTranscript);
  const hasFullLog = task.logs.length > 0;
  const elapsedText = task.taskStartedAt ? formatElapsedDuration(taskElapsedMilliseconds(task)) : "";
  const displayTitle = readableTitle(
    task.videoTitle,
    workflowState === "idle" ? "等待视频标题" : "正在获取视频标题",
  );
  const displayPartTitle = currentPartHeading(task);
  const fallbackError =
    task.status === "canceled"
      ? "任务已取消，可以返回输入态或直接重新提交。"
      : task.status === "abandoned"
        ? "任务长时间未轮询，已被标记为 abandoned。"
        : task.status === "waiting_model_retry"
          ? "阶段 6 已暂停，可以重试或更换第一模型后再试。"
        : "";

  renderWorkflowState(task);
  elements.videoTitle.textContent = displayTitle;
  if (elements.videoPartTitle) {
    elements.videoPartTitle.textContent = displayPartTitle;
    elements.videoPartTitle.hidden = !displayPartTitle;
  }
  elements.progressBar.value = progress;
  elements.progressLabel.textContent = `${progress}%`;
  elements.stageText.textContent = task.currentItem ? `${task.stage}：${task.currentItem}` : task.stage;
  if (elements.taskElapsed) {
    elements.taskElapsed.textContent = elapsedText;
    elements.taskElapsed.hidden = !elapsedText || workflowState === "idle" || workflowState === "completed";
  }
  if (elements.resultDuration) {
    elements.resultDuration.textContent = elapsedText;
    elements.resultDuration.hidden = !elapsedText || workflowState !== "completed";
  }
  elements.errorMessage.textContent = task.error || fallbackError;
  const shouldScrollLogToLatest = elements.logPanel.textContent !== logText;
  if (shouldScrollLogToLatest) {
    elements.logPanel.textContent = logText;
  }
  elements.fullLog.textContent = logText;
  elements.cleanSubtitle.textContent = task.cleanSubtitle || "暂无内容";
  elements.aiTranscript.textContent = task.aiTranscript || "暂无内容";
  elements.cancelTask.disabled = !task.taskId || !canCancel;
  elements.taskInput.disabled = workflowState !== "idle";
  elements.sendButton.disabled = workflowState !== "idle" || canCancel;
  collapseIntermediatePanelForNewResult(task);
  renderMarkdownPreview(elements.finalMarkdown, task.finalMarkdown);
  elements.downloadMarkdown.disabled = !hasFinalMarkdown;
  if (elements.copyMarkdown) {
    elements.copyMarkdown.disabled = !hasFinalMarkdown;
  }
  if (elements.downloadCleanSubtitle) {
    elements.downloadCleanSubtitle.disabled = !hasCleanSubtitle;
  }
  if (elements.downloadAiTranscript) {
    elements.downloadAiTranscript.disabled = !hasAiTranscript;
  }
  if (elements.downloadFullLog) {
    elements.downloadFullLog.disabled = !hasFullLog;
  }

  if (shouldScrollLogToLatest) {
    elements.logPanel.scrollTop = elements.logPanel.scrollHeight;
  }
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

function showTranscriptionRetryModal(message) {
  if (!elements.transcriptionRetryModal) {
    return;
  }
  elements.transcriptionRetryErrorText.textContent =
    message || "第一模型音频转文字失败，可以重试或更换模型配置后重新尝试阶段 6。";
  elements.transcriptionRetryModal.hidden = false;
}

function hideTranscriptionRetryModal() {
  if (!elements.transcriptionRetryModal) {
    return;
  }
  elements.transcriptionRetryModal.hidden = true;
}

function shouldOfferSubtitleSkip(payload) {
  return payload.status === "failed" && recoverableSubtitleErrorCodes.has(payload.error_code || "");
}

function shouldPromptCredentialRefresh(payload) {
  return payload.status === "failed" && credentialRefreshErrorCodes.has(payload.error_code || "");
}

function shouldOfferTranscriptionRetry(payload) {
  return (
    payload.status === "waiting_model_retry" ||
    transcriptionRetryErrorCodes.has(payload.error_code || "")
  );
}

function setCredentialStatus(message) {
  if (elements.credentialStatus) {
    elements.credentialStatus.textContent = message;
  }
  if (elements.bilibiliProfileStatus) {
    elements.bilibiliProfileStatus.textContent = message;
  }
}

function showCredentialGuide(message = "请先初始化精简 B站 Cookie。") {
  bilibiliCredentialReady = false;
  if (elements.workflowShell) {
    elements.workflowShell.hidden = true;
  }
  if (elements.credentialModal) {
    elements.credentialModal.hidden = false;
  }
  setCredentialStatus(message);
}

function hideCredentialGuide() {
  if (elements.credentialModal) {
    elements.credentialModal.hidden = true;
  }
  if (elements.workflowShell) {
    elements.workflowShell.hidden = false;
  }
}

function unlockCredentialGate(message = "精简 B站 Cookie 已准备好。") {
  bilibiliCredentialReady = true;
  if (elements.credentialModal) {
    elements.credentialModal.hidden = true;
  }
  if (elements.workflowShell) {
    elements.workflowShell.hidden = false;
  }
  setCredentialStatus(message);
}

async function persistSimplifiedCookie(rawCookie, { reflectToUi = true } = {}) {
  const summary = summarizeBilibiliCookie(rawCookie);
  if (!summary.cookieHeader) {
    throw new Error("未找到 6 项白名单内的 B站 Cookie，请重新登录或粘贴包含 SESSDATA 的 Cookie。");
  }

  const browser = elements.bilibiliCookieBrowser?.value || "chrome";
  await saveBilibiliAccessSettings({
    mode: "cookie_header",
    browser,
    cookieHeader: summary.cookieHeader,
  });

  if (reflectToUi) {
    if (elements.bilibiliAccessMode) {
      elements.bilibiliAccessMode.value = "cookie_header";
    }
    if (elements.bilibiliCookieHeader) {
      elements.bilibiliCookieHeader.value = summary.cookieHeader;
    }
    if (elements.manualCookieGuide) {
      elements.manualCookieGuide.value = "";
    }
    updateBilibiliAccessUi();
  }

  return summary;
}

async function ensureBilibiliCredentialReady() {
  const settings = await getBilibiliAccessSettings();
  const cookieHeader = simplifyBilibiliCookieHeader(
    elements.bilibiliCookieHeader?.value || settings.cookieHeader || "",
  );
  if (!cookieHeader) {
    showCredentialGuide("未检测到精简 B站 Cookie，请先打开本地 B站登录窗口完成凭据初始化。");
    return;
  }

  if (cookieHeader !== settings.cookieHeader) {
    await saveBilibiliAccessSettings({ ...settings, mode: "cookie_header", cookieHeader });
    if (elements.bilibiliCookieHeader) {
      elements.bilibiliCookieHeader.value = cookieHeader;
    }
  }

  unlockCredentialGate("已检测到本地 Cookie。若后续任务提示失效，可在设置中点击“重新获取”。");
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
  const responseStatus = payload.status || "unknown";
  const taskStartedAt = currentTask.taskStartedAt || Date.now();
  const taskFinishedAt = elapsedFreezeStatuses.has(responseStatus)
    ? currentTask.taskFinishedAt || Date.now()
    : null;
  const hasUserVisibleTitle =
    currentTask.videoTitle && currentTask.videoTitle !== "正在获取视频标题";
  const nextTaskPatch = {
    taskId: payload.task_id || "",
    status: responseStatus,
    progress: payload.progress ?? 0,
    stage: payload.stage || "未知阶段",
    currentItem: payload.current_item || "",
    logs: Array.isArray(payload.logs) ? payload.logs : [],
    originalInput: currentTask.originalInput || lastTaskInput,
    recognizedInput: result.parsed_input || result.webpage_url || currentTask.recognizedInput,
    finalMarkdown: result.final_markdown || "",
    cleanSubtitle: result.clean_subtitle || "",
    aiTranscript: result.ai_transcript || "",
    videoTitle: hasUserVisibleTitle ? currentTask.videoTitle : result.title || currentTask.videoTitle,
    filename: result.filename || "final.md",
    pIndex: result.p_index ?? currentTask.pIndex ?? null,
    historyId: currentTask.historyId || payload.task_id || "",
    historyStartedAt: currentTask.historyStartedAt || currentTask.taskStartedAt || Date.now(),
    taskStartedAt,
    taskFinishedAt,
    error: payload.error || "",
    errorCode: payload.error_code || "",
  };
  setTaskState({
    ...nextTaskPatch,
    subTasks: buildSubTasksFromResult(result, nextTaskPatch, currentTask),
  });
  renderTask();

  if (shouldPromptCredentialRefresh(payload)) {
    showCredentialGuide(
      payload.error || "B站 Cookie 失效、HTTP 412、未登录或风控，请重新打开本地 B站登录窗口刷新 Cookie。",
    );
  } else if (shouldOfferSubtitleSkip(payload)) {
    showSubtitleFailureModal(payload.error);
  } else if (shouldOfferTranscriptionRetry(payload)) {
    showTranscriptionRetryModal(payload.error);
  }

  if (terminalTaskStatuses.has(payload.status)) {
    stopPolling();
  }
  if (historyPersistStatuses.has(responseStatus)) {
    persistCurrentTaskToHistory().catch((error) => {
      log("warning", error instanceof Error ? error.message : "历史记录保存失败");
    });
  }
}

function normalizeSubTaskMeta(subTask = {}, fallbackIndex = 1) {
  return {
    id: subTask.id || `p${subTask.pIndex ?? subTask.p_index ?? fallbackIndex}`,
    title: subTask.title || "",
    pIndex: subTask.pIndex ?? subTask.p_index ?? fallbackIndex,
    bvId: subTask.bvId || subTask.bv_id || "",
    url: subTask.url || "",
    durationSeconds: subTask.durationSeconds ?? subTask.duration_seconds ?? null,
  };
}

function buildSubTasksFromResult(result, taskPatch, currentTask) {
  const rawSubTasks = Array.isArray(result.sub_tasks) ? result.sub_tasks : [];
  const currentPIndex = Number(taskPatch.pIndex || 1);
  const currentResult = {
    finalMarkdown: taskPatch.finalMarkdown || "",
    cleanSubtitle: taskPatch.cleanSubtitle || "",
    aiTranscript: taskPatch.aiTranscript || "",
    logs: taskPatch.logs || [],
    error: taskPatch.error || "",
  };

  if (!rawSubTasks.length) {
    return [
      {
        id: `p${currentPIndex}`,
        title: taskPatch.videoTitle || currentTask.videoTitle || "当前分P",
        pIndex: currentPIndex,
        bvId: result.bv_id || "",
        url: result.webpage_url || taskPatch.recognizedInput || "",
        durationSeconds: result.duration_seconds ?? null,
        ...currentResult,
      },
    ];
  }

  return rawSubTasks.map((subTask, index) => {
    const meta = normalizeSubTaskMeta(subTask, index + 1);
    const isCurrentPart = Number(meta.pIndex || 1) === currentPIndex;
    return {
      ...meta,
      ...(isCurrentPart
        ? currentResult
        : {
            finalMarkdown: "",
            cleanSubtitle: "",
            aiTranscript: "",
            logs: [],
            error: "",
          }),
    };
  });
}

function historyTitleForTask(task) {
  const title = task.videoTitle && task.videoTitle !== "正在获取视频标题"
    ? task.videoTitle
    : task.recognizedInput || task.originalInput || "未命名任务";
  return readableTitle(title, "未命名任务");
}

function logsForHistory(task) {
  return task.logs.map((entry) => ({
    time: entry.time || new Date().toISOString(),
    level: entry.level || "info",
    message: redactSecrets(entry.message || ""),
  }));
}

async function persistCurrentTaskToHistory() {
  const task = getTaskState();
  if (!historyPersistStatuses.has(task.status)) {
    return;
  }

  await saveHistoryRecord({
    id: task.historyId || task.taskId || `local-${task.taskStartedAt || Date.now()}`,
    status: task.status,
    title: historyTitleForTask(task),
    pIndex: task.pIndex ?? null,
    durationSeconds: taskElapsedSeconds(task),
    originalInput: task.originalInput || lastTaskInput || elements.taskInput.value.trim(),
    parsedInput: task.recognizedInput || "",
    createdAt: new Date(task.historyStartedAt || task.taskStartedAt || Date.now()).toISOString(),
    finalMarkdown: task.finalMarkdown || "",
    cleanSubtitle: task.cleanSubtitle || "",
    aiTranscript: task.aiTranscript || "",
    logs: logsForHistory(task),
    error: task.error || "",
    subTasks: Array.isArray(task.subTasks) ? task.subTasks : [],
  });
  await renderHistory();
}

async function pollTask(taskId, generation) {
  try {
    const payload = await fetchTask(taskId);
    if (generation !== taskGeneration) {
      return;
    }
    applyTaskResponse(payload);
  } catch (error) {
    if (generation !== taskGeneration) {
      return;
    }
    log("error", error instanceof Error ? error.message : "任务状态读取失败");
    stopPolling();
  }
}

function startPolling(taskId, generation = taskGeneration) {
  stopPolling();
  pollTimer = window.setInterval(() => pollTask(taskId, generation), 1000);
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
  const config = {
    baseUrl: String(data.get("baseUrl") || "").trim(),
    apiKey: String(data.get("apiKey") || ""),
    model: String(data.get("model") || "").trim(),
    temperature: Number(data.get("temperature") || 0),
    stream: data.get("stream") === "on",
  };
  if (form.elements.provider) {
    config.provider = String(data.get("provider") || "openai_compatible_input_audio");
  }
  return config;
}

function updateTranscriptionProviderUi(form) {
  if (form.dataset.modelKind !== "transcription") {
    return;
  }

  const signupLink = form.querySelector("[data-openai-signup-link]");
  if (signupLink) {
    signupLink.hidden = form.elements.provider?.value !== "openai_compatible_input_audio";
  }
}

function writeModelForm(form, config) {
  form.elements.baseUrl.value = config.baseUrl || "";
  form.elements.apiKey.value = config.apiKey || "";
  form.elements.model.value = config.model || "";
  form.elements.temperature.value = String(config.temperature ?? 0);
  form.elements.stream.checked = config.stream !== false;
  if (form.elements.provider) {
    form.elements.provider.value = config.provider || "openai_compatible_input_audio";
  }
  updateTranscriptionProviderUi(form);
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
  const titles = {
    models: "模型配置",
    "bilibili-access": "本地Cookie值",
    history: "历史记录",
    menu: "设置",
  };
  elements.settingsTitle.textContent = titles[pageName] || "设置";
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

async function switchTranscriptionProvider(form) {
  const provider = form.elements.provider?.value || "";
  if (form.dataset.modelKind !== "transcription" || !provider) {
    return;
  }

  const config = await getModelConfig("transcription", provider);
  if (form.elements.provider.value !== provider) {
    return;
  }

  writeModelForm(form, config);
  populateModelOptions(form, []);
  elements.configStatus.textContent = "";
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

function normalizeBilibiliAccessMode(mode) {
  return mode === "cookie_header" ? "cookie_header" : "cookie_header";
}

function updateBilibiliAccessUi() {
  if (!elements.bilibiliCookieHeader) {
    return;
  }

  const summary = summarizeBilibiliCookie(elements.bilibiliCookieHeader.value || "");
  if (elements.bilibiliCookieHeader.value !== summary.cookieHeader) {
    elements.bilibiliCookieHeader.value = summary.cookieHeader;
  }
}

async function handleOpenBilibiliLogin() {
  if (elements.openBilibiliLogin) {
    elements.openBilibiliLogin.disabled = true;
  }
  if (elements.openBilibiliLoginGuide) {
    elements.openBilibiliLoginGuide.disabled = true;
  }
  if (elements.bilibiliProfileStatus) {
    elements.bilibiliProfileStatus.textContent = "正在打开本地专用B站登录窗口";
  }

  try {
    const payload = await openBilibiliLoginWindow();
    bilibiliCredentialSessionToken = payload.session_token || "";
    const message = payload.message || "已打开本地专用B站登录窗口";
    if (elements.bilibiliProfileStatus) {
      elements.bilibiliProfileStatus.textContent = message;
    }
    setCredentialStatus(
      "已打开本地专用B站登录窗口，请在该窗口中完成登录。\n" +
        "登录完成后请保持B站窗口打开，再回到这里点击“提取并保存Cookie”；\n" +
        "提取成功后程序会关闭登录窗口。",
    );
    log("info", message);
  } catch (error) {
    const message = error instanceof Error ? error.message : "打开B站登录窗口失败";
    if (elements.bilibiliProfileStatus) {
      elements.bilibiliProfileStatus.textContent = message;
    }
    setCredentialStatus(message);
    log("error", message);
  } finally {
    if (elements.openBilibiliLogin) {
      elements.openBilibiliLogin.disabled = false;
    }
    if (elements.openBilibiliLoginGuide) {
      elements.openBilibiliLoginGuide.disabled = false;
    }
  }
}

async function handleExtractBilibiliCookie() {
  if (elements.extractBilibiliCookie) {
    elements.extractBilibiliCookie.disabled = true;
  }
  setCredentialStatus("正在从当前打开的B站登录窗口提取并精简Cookie。");

  try {
    if (!bilibiliCredentialSessionToken) {
      throw new Error("请先点击“打开B站登录窗口”，完成登录后再提取Cookie。");
    }
    const payload = await extractBilibiliCookieFromProfile(bilibiliCredentialSessionToken);
    bilibiliCredentialSessionToken = "";
    await persistSimplifiedCookie(payload.cookie_header);
    unlockCredentialGate("精简 B站 Cookie 已提取并保存。");
    log("info", "精简 B站 Cookie 已保存到 IndexedDB");
  } catch (error) {
    const message = error instanceof Error ? error.message : "精简 Cookie 提取失败";
    showCredentialGuide(message);
    log("error", message);
  } finally {
    if (elements.extractBilibiliCookie) {
      elements.extractBilibiliCookie.disabled = false;
    }
  }
}

async function saveBilibiliAccessSettingsFromUi() {
  const currentSettings = await getBilibiliAccessSettings();
  const browser = elements.bilibiliCookieBrowser?.value || currentSettings.browser || "chrome";
  const cookieHeader = simplifyBilibiliCookieHeader(
    elements.bilibiliCookieHeader?.value || currentSettings.cookieHeader || "",
  );

  await saveBilibiliAccessSettings({
    mode: normalizeBilibiliAccessMode(currentSettings.mode),
    browser,
    cookieHeader,
  });
}

function scheduleBilibiliAccessSettingsSave() {
  if (bilibiliAccessSaveTimer) {
    window.clearTimeout(bilibiliAccessSaveTimer);
  }
  bilibiliAccessSaveTimer = window.setTimeout(() => {
    saveBilibiliAccessSettingsFromUi().catch(() => {});
  }, 300);
}

async function restoreBilibiliAccessSettings() {
  const settings = await getBilibiliAccessSettings();
  if (elements.bilibiliAccessMode) {
    elements.bilibiliAccessMode.value = normalizeBilibiliAccessMode(settings.mode);
  }
  if (elements.bilibiliCookieBrowser) {
    elements.bilibiliCookieBrowser.value = settings.browser || "chrome";
  }
  if (elements.bilibiliCookieHeader) {
    elements.bilibiliCookieHeader.value = settings.cookieHeader || "";
  }
  updateBilibiliAccessUi();
}

async function readBilibiliAccessOptions() {
  const settings = await getBilibiliAccessSettings();
  const mode = "cookie_header";
  const browser = elements.bilibiliCookieBrowser?.value || settings.browser || "chrome";
  const cookieHeader = simplifyBilibiliCookieHeader(
    elements.bilibiliCookieHeader?.value || settings.cookieHeader || "",
  );

  await saveBilibiliAccessSettingsFromUi();

  if (!cookieHeader) {
    throw new Error("当前需要精简 B站 Cookie，请先按新手引导打开本地 B站登录窗口刷新 Cookie。");
  }
  if (elements.bilibiliCookieHeader?.value !== cookieHeader) {
    elements.bilibiliCookieHeader.value = cookieHeader;
    updateBilibiliAccessUi();
  }

  return {
    bilibiliAccessMode: mode,
    bilibiliCookieBrowser: browser,
    bilibiliCookieHeader: cookieHeader,
    bilibiliCookiesFileContent: "",
  };
}

function restoreButtonText(button, text, delay = 1200) {
  if (!button) {
    return;
  }
  window.setTimeout(() => {
    button.textContent = text;
  }, delay);
}

async function copyTextToClipboard(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.inset = "0 auto auto 0";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

async function handleCopyMarkdown() {
  const button = elements.copyMarkdown;
  const defaultText = button?.textContent || "复制Markdown";
  const markdown = getTaskState().finalMarkdown || "";

  if (!markdown) {
    if (button) {
      button.textContent = "无内容";
      restoreButtonText(button, defaultText);
    }
    return;
  }

  try {
    await copyTextToClipboard(markdown);
    if (button) {
      button.textContent = "已复制";
      restoreButtonText(button, defaultText);
    }
  } catch (error) {
    if (button) {
      button.textContent = "复制失败";
      restoreButtonText(button, defaultText);
    }
    console.warn(error instanceof Error ? error.message : "复制 Markdown 失败");
  }
}

async function handleCopyLocalCookie() {
  const button = elements.copyLocalCookie;
  const defaultText = button?.textContent || "复制";
  const cookieHeader = simplifyBilibiliCookieHeader(
    elements.bilibiliCookieHeader?.value || (await getBilibiliAccessSettings()).cookieHeader || "",
  );

  if (!cookieHeader) {
    if (button) {
      button.textContent = "无内容";
      restoreButtonText(button, defaultText);
    }
    return;
  }

  try {
    await copyTextToClipboard(cookieHeader);
    if (button) {
      button.textContent = "已复制";
      restoreButtonText(button, defaultText);
    }
  } catch (error) {
    if (button) {
      button.textContent = "复制失败";
      restoreButtonText(button, defaultText);
    }
    log("error", error instanceof Error ? error.message : "复制本地 Cookie 失败");
  }
}

function handleRefreshLocalCookie() {
  bilibiliCredentialSessionToken = "";
  closeSettings();
  showCredentialGuide("请重新打开本地 B站登录窗口；登录完成后保持窗口打开，再点击“提取并保存Cookie”。");
}

async function handleClearLocalCookie() {
  const settings = await getBilibiliAccessSettings();
  await saveBilibiliAccessSettings({
    ...settings,
    mode: "cookie_header",
    cookieHeader: "",
  });
  if (elements.bilibiliCookieHeader) {
    elements.bilibiliCookieHeader.value = "";
  }
  updateBilibiliAccessUi();
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
    item.classList.toggle("is-abnormal", isAbnormalHistoryRecord(record));
    item.setAttribute("role", "button");
    item.tabIndex = 0;
    item.addEventListener("click", () => restoreHistoryRecord(record));
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        restoreHistoryRecord(record);
      }
    });

    const header = document.createElement("div");
    header.className = "history-item-header";

    const titleBlock = document.createElement("div");
    titleBlock.className = "history-title-block";
    const title = document.createElement("h3");
    title.textContent = readableTitle(record.title, "未命名任务");

    const partTitleText = currentPartHeading(record);
    const titleNodes = [title];
    if (partTitleText) {
      const partTitle = document.createElement("h4");
      partTitle.textContent = partTitleText;
      titleNodes.push(partTitle);
    }

    const meta = document.createElement("p");
    meta.textContent = [formatHistoryDate(record.createdAt), record.error ? "失败" : ""]
      .filter(Boolean)
      .join(" · ");

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "history-delete-button";
    deleteButton.setAttribute("aria-label", "删除这条历史记录");
    deleteButton.title = "删除";
    deleteButton.textContent = "×";
    deleteButton.addEventListener("click", async (event) => {
      event.stopPropagation();
      await handleDeleteHistoryRecord(record);
    });
    deleteButton.addEventListener("keydown", (event) => {
      event.stopPropagation();
    });

    const preview = document.createElement("p");
    preview.className = "history-preview";
    preview.textContent =
      record.finalMarkdown ||
      record.aiTranscript ||
      record.cleanSubtitle ||
      record.error ||
      record.parsedInput ||
      "暂无可预览正文";

    titleBlock.append(...titleNodes, meta);
    header.append(titleBlock, deleteButton);
    item.append(header, preview);
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

function downloadText(filename, text, type = "text/plain;charset=utf-8") {
  const blob = new Blob([text], {
    type,
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function sanitizeFilenamePart(value) {
  const cleaned = String(value || "")
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[. ]+$/g, "")
    .slice(0, 80)
    .trim();
  return cleaned || "未命名视频";
}

function localTimestampForFilename() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}_${pad(
    now.getHours(),
  )}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}

function buildMarkdownDownloadFilename(task) {
  const pLabel = `P${task.pIndex || 1}`;
  const title = sanitizeFilenamePart(task.videoTitle || "B站视频转文字");
  return `${pLabel}_${title}_${localTimestampForFilename()}.md`;
}

function buildTextDownloadFilename(task, suffix) {
  const pLabel = `P${task.pIndex || 1}`;
  const title = sanitizeFilenamePart(task.videoTitle || "B站视频转文字");
  return `${pLabel}_${title}_${localTimestampForFilename()}_${suffix}.txt`;
}

function restoreHistoryRecord(record) {
  stopPolling();
  taskGeneration += 1;
  hideSubtitleFailureModal();
  hideTranscriptionRetryModal();
  lastTaskInput = record.originalInput || "";
  const createdAt = Date.parse(record.createdAt || "") || Date.now();
  const finishedAt = historyFinishedAt(createdAt, record.durationSeconds);
  const status = ["failed", "canceled", "abandoned"].includes(record.status)
    ? record.status
    : record.status === "completed" && record.finalMarkdown
      ? "completed"
      : record.error
        ? "failed"
        : "completed";
  setTaskState({
    taskId: record.id || "",
    status,
    progress: status === "completed" ? 100 : 0,
    stage: "历史任务详情",
    currentItem: "",
    originalInput: record.originalInput || "",
    recognizedInput: record.parsedInput || "",
    videoTitle: record.title || "历史任务",
    logs: Array.isArray(record.logs) ? record.logs : [],
    finalMarkdown: record.finalMarkdown || "",
    cleanSubtitle: record.cleanSubtitle || "",
    aiTranscript: record.aiTranscript || "",
    filename: "final.md",
    pIndex: record.pIndex ?? record.subTasks?.[0]?.pIndex ?? null,
    subTasks: Array.isArray(record.subTasks) ? record.subTasks : [],
    historyId: record.id || "",
    historyStartedAt: createdAt,
    taskStartedAt: createdAt,
    taskFinishedAt: finishedAt,
    error: record.error || "",
    errorCode: "",
  });
  elements.taskInput.value = record.originalInput || "";
  closeSettings();
  renderTask();
  updateInputStatus();
}

function updateInputStatus() {
  const rawInput = elements.taskInput.value.trim();
  elements.inputForm.classList.toggle("has-content", rawInput.length > 0);
  elements.inputStatus.textContent = "";
}

async function resetWorkflow() {
  const currentTask = getTaskState();
  if (currentTask.status === "waiting_model_retry" && currentTask.taskId) {
    try {
      await cancelTask(currentTask.taskId);
    } catch (error) {
      log("warning", error instanceof Error ? error.message : "取消暂停任务失败，后端会在 abandoned 清理入口处理");
    }
  }

  stopPolling();
  taskGeneration += 1;
  hideSubtitleFailureModal();
  hideTranscriptionRetryModal();
  lastTaskInput = "";
  lastCollapsedIntermediateResultKey = "";
  setTaskState({
    taskId: "",
    status: "idle",
    progress: 0,
    stage: "等待创建任务",
    currentItem: "",
    originalInput: "",
    recognizedInput: "",
    videoTitle: "",
    logs: [],
    finalMarkdown: "",
    cleanSubtitle: "",
    aiTranscript: "",
    filename: "final.md",
    pIndex: null,
    subTasks: [],
    historyId: "",
    historyStartedAt: null,
    taskStartedAt: null,
    taskFinishedAt: null,
    error: "",
    errorCode: "",
  });
  elements.taskInput.value = "";
  elements.inputStatus.textContent = "";
  renderTask();
  updateInputStatus();
  elements.taskInput.focus({ preventScroll: true });
}

async function submitTask({
  rawInput = elements.taskInput.value.trim(),
  skipSubtitleIfFailed = false,
  historyIdOverride = "",
  historyStartedAtOverride = null,
} = {}) {
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
    const message = "用户输入无法解析：未识别到 B站视频链接或 BV 号";
    elements.inputStatus.textContent = message;
    log("error", message);
    return;
  }

  stopPolling();
  taskGeneration += 1;
  const currentGeneration = taskGeneration;
  hideSubtitleFailureModal();
  hideTranscriptionRetryModal();
  lastTaskInput = rawInput;
  lastCollapsedIntermediateResultKey = "";
  const taskStartedAt = Date.now();
  setTaskState({
    taskId: "",
    status: "pending",
    progress: 0,
    stage: "正在创建任务",
    currentItem: "",
    originalInput: rawInput,
    recognizedInput: parsed.display,
    videoTitle: parsed.videoTitle || "正在获取视频标题",
    logs: [],
    finalMarkdown: "",
    cleanSubtitle: "",
    aiTranscript: "",
    filename: "final.md",
    pIndex: null,
    subTasks: [],
    historyId: historyIdOverride || "",
    historyStartedAt: historyStartedAtOverride || taskStartedAt,
    taskStartedAt,
    taskFinishedAt: null,
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
    const bilibiliAccessOptions = await readBilibiliAccessOptions();
    const payload = await createTask({
      input: rawInput,
      transcriptionConfig: configs.transcription,
      refineConfig: configs.refine,
      options: {
        skipSubtitleIfFailed,
        ...bilibiliAccessOptions,
      },
    });
    if (currentGeneration !== taskGeneration) {
      return;
    }
    applyTaskResponse(payload);
    if (!terminalTaskStatuses.has(payload.status)) {
      startPolling(payload.task_id, currentGeneration);
    }
  } catch (error) {
    if (currentGeneration !== taskGeneration) {
      return;
    }
    setTaskState({
      status: "failed",
      progress: 0,
      stage: "创建任务失败",
      currentItem: "",
      taskFinishedAt: Date.now(),
      error: error instanceof Error ? error.message : "创建任务失败",
      errorCode: "",
    });
    log("error", error instanceof Error ? error.message : "创建任务失败");
    persistCurrentTaskToHistory().catch(() => {});
  }
}

async function handleStartTask(event) {
  event.preventDefault();
  await submitTask();
}

async function handleSkipSubtitle() {
  const currentTask = getTaskState();
  const rawInput = lastTaskInput || elements.taskInput.value.trim();
  if (!rawInput) {
    hideSubtitleFailureModal();
    return;
  }
  const historyId = currentTask.historyId || currentTask.taskId || "";
  const historyStartedAt = currentTask.historyStartedAt || currentTask.taskStartedAt || Date.now();
  await submitTask({
    rawInput,
    skipSubtitleIfFailed: true,
    historyIdOverride: historyId,
    historyStartedAtOverride: historyStartedAt,
  });
}

async function handleCancelTask() {
  const task = getTaskState();
  if (!task.taskId || !["pending", "running", "waiting_model_retry"].includes(task.status)) {
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

async function handleRetryTranscription() {
  const task = getTaskState();
  if (!task.taskId || task.status !== "waiting_model_retry") {
    return;
  }

  try {
    const config = await getModelConfig("transcription");
    hideTranscriptionRetryModal();
    taskGeneration += 1;
    const currentGeneration = taskGeneration;
    const payload = await retryTranscription(task.taskId, config);
    applyTaskResponse(payload);
    if (!terminalTaskStatuses.has(payload.status)) {
      startPolling(payload.task_id, currentGeneration);
    }
  } catch (error) {
    showTranscriptionRetryModal(error instanceof Error ? error.message : "阶段 6 重试失败");
    log("error", error instanceof Error ? error.message : "阶段 6 重试失败");
  }
}

function handleChangeTranscriptionModel() {
  openSettings();
  showSettingsPage("models");
  elements.modelForms.find((form) => form.dataset.modelKind === "transcription")?.scrollIntoView({
    block: "start",
    behavior: "smooth",
  });
}

async function handleCancelTranscriptionTask() {
  hideTranscriptionRetryModal();
  await handleCancelTask();
}

async function handleExport() {
  const data = await exportAllData();
  const date = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  downloadJson(`bilibili-transcription-data-${date}.json`, data);
  log("info", "已导出 IndexedDB 配置和历史数据");
}

async function handleImportFile(file) {
  try {
    if (!confirm("导入会覆盖当前 IndexedDB 中的模型配置、精简 B站 Cookie 和历史记录。导入文件请只使用你自己的本地备份，确认继续？")) {
      return;
    }
    const payload = JSON.parse(await file.text());
    await importAllData(payload);
    await restoreModelConfigs();
    await restoreBilibiliAccessSettings();
    await renderHistory();
    await ensureBilibiliCredentialReady();
    log("info", "已导入 IndexedDB 配置和历史数据");
  } catch (error) {
    log("error", error instanceof Error ? error.message : "导入失败");
  } finally {
    elements.importFile.value = "";
  }
}

async function handleClearHistory() {
  if (!confirm("确认清空本工具的全部本地数据？历史记录、模型配置和精简 B站 Cookie 都会删除。")) {
    return;
  }

  await clearAllLocalData();
  window.location.reload();
}

async function handleDeleteHistoryRecord(record) {
  if (!record?.id) {
    return;
  }
  if (!confirm("确认删除这条历史记录？")) {
    return;
  }
  await deleteHistoryRecord(record.id);
  await renderHistory();
  const currentTask = getTaskState();
  if ((currentTask.historyId || currentTask.taskId) === record.id) {
    await resetWorkflow();
  }
  log("warning", "已删除 1 条历史记录");
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
  elements.retryTranscriptionButton?.addEventListener("click", handleRetryTranscription);
  elements.changeTranscriptionModelButton?.addEventListener("click", handleChangeTranscriptionModel);
  elements.cancelTranscriptionTaskButton?.addEventListener("click", handleCancelTranscriptionTask);
  elements.dismissTranscriptionRetryButtons.forEach((button) =>
    button.addEventListener("click", hideTranscriptionRetryModal),
  );
  elements.resetWorkflowButtons.forEach((button) =>
    button.addEventListener("click", resetWorkflow),
  );
  elements.downloadMarkdown.addEventListener("click", () => {
    const task = getTaskState();
    downloadText(buildMarkdownDownloadFilename(task), task.finalMarkdown, "text/markdown;charset=utf-8");
  });
  elements.copyMarkdown?.addEventListener("click", handleCopyMarkdown);
  elements.downloadCleanSubtitle?.addEventListener("click", () => {
    const task = getTaskState();
    downloadText(buildTextDownloadFilename(task, "subtitle"), task.cleanSubtitle || "");
  });
  elements.downloadAiTranscript?.addEventListener("click", () => {
    const task = getTaskState();
    downloadText(buildTextDownloadFilename(task, "ai-transcript"), task.aiTranscript || "");
  });
  elements.downloadFullLog?.addEventListener("click", () => {
    const task = getTaskState();
    downloadText(buildTextDownloadFilename(task, "log"), task.logs.map(formatLog).join("\n"));
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
    if (event.key === "Escape" && elements.transcriptionRetryModal && !elements.transcriptionRetryModal.hidden) {
      hideTranscriptionRetryModal();
    }
    if (event.key === "Escape" && elements.credentialModal && !elements.credentialModal.hidden) {
      hideCredentialGuide();
    }
  });

  for (const form of elements.modelForms) {
    form.addEventListener("submit", (event) => event.preventDefault());
    form.querySelector("[data-save-config]").addEventListener("click", () => saveConfig(form));
    form.querySelector("[data-fetch-models]").addEventListener("click", () => loadModels(form));
    form.elements.provider?.addEventListener("change", () => {
      switchTranscriptionProvider(form).catch((error) => {
        elements.configStatus.textContent = "识别方式配置加载失败";
        log("error", error instanceof Error ? error.message : "识别方式配置加载失败");
      });
    });
    form.querySelector("[data-model-picker]").addEventListener("change", (event) => {
      if (!event.target.value) {
        return;
      }
      form.elements.model.value = event.target.value;
      event.target.value = "";
    });
  }

  elements.bilibiliAccessMode?.addEventListener("change", () => {
    updateBilibiliAccessUi();
    saveBilibiliAccessSettingsFromUi().catch(() => {});
  });
  elements.bilibiliCookieBrowser?.addEventListener("change", () => {
    updateBilibiliAccessUi();
    saveBilibiliAccessSettingsFromUi().catch(() => {});
  });
  elements.bilibiliCookieHeader?.addEventListener("change", () => {
    const simplified = simplifyBilibiliCookieHeader(elements.bilibiliCookieHeader.value);
    if (simplified) {
      elements.bilibiliCookieHeader.value = simplified;
    }
    updateBilibiliAccessUi();
    saveBilibiliAccessSettingsFromUi().catch(() => {});
  });
  elements.bilibiliCookieHeader?.addEventListener("input", () => {
    updateBilibiliAccessUi();
    scheduleBilibiliAccessSettingsSave();
  });
  elements.bilibiliCookiesFile?.addEventListener("change", updateBilibiliAccessUi);
  elements.openBilibiliLogin?.addEventListener("click", handleOpenBilibiliLogin);
  elements.openBilibiliLoginGuide?.addEventListener("click", handleOpenBilibiliLogin);
  elements.closeCredentialButtons.forEach((button) =>
    button.addEventListener("click", hideCredentialGuide),
  );
  elements.extractBilibiliCookie?.addEventListener("click", handleExtractBilibiliCookie);
  elements.copyLocalCookie?.addEventListener("click", handleCopyLocalCookie);
  elements.refreshLocalCookie?.addEventListener("click", handleRefreshLocalCookie);
  elements.clearLocalCookie?.addEventListener("click", handleClearLocalCookie);
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
  initPagePresenceWarning();
  renderHealth();
  renderTask();
  updateInputStatus();
  updateBilibiliAccessUi();

  try {
    await initDatabase();
    await restoreModelConfigs();
    await restoreBilibiliAccessSettings();
    await renderHistory();
    await ensureBilibiliCredentialReady();
  } catch (error) {
    log("error", error instanceof Error ? error.message : "IndexedDB 初始化失败");
  }

  refreshHealth();
}

init();
