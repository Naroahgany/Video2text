const state = {
  health: {
    status: "checking",
    message: "正在检查...",
  },
  task: {
    sourceType: "bilibili",
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
  },
};

export function getHealthState() {
  return { ...state.health };
}

export function setHealthState(nextHealth) {
  state.health = {
    ...state.health,
    ...nextHealth,
  };
}

export function getTaskState() {
  return {
    ...state.task,
    logs: [...state.task.logs],
    subTasks: Array.isArray(state.task.subTasks) ? [...state.task.subTasks] : [],
  };
}

export function setTaskState(nextTask) {
  state.task = {
    ...state.task,
    ...nextTask,
    logs: nextTask.logs ? [...nextTask.logs] : state.task.logs,
    subTasks: nextTask.subTasks ? [...nextTask.subTasks] : state.task.subTasks,
  };
}

export function appendTaskLog(level, message) {
  state.task.logs = [
    ...state.task.logs,
    {
      time: new Date().toISOString(),
      level,
      message,
    },
  ].slice(-500);
}
