const state = {
  health: {
    status: "checking",
    message: "正在检查...",
  },
  task: {
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
  };
}

export function setTaskState(nextTask) {
  state.task = {
    ...state.task,
    ...nextTask,
    logs: nextTask.logs ? [...nextTask.logs] : state.task.logs,
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
