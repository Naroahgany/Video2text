const state = {
  health: {
    status: "checking",
    message: "正在检查...",
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
