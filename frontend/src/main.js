import { fetchHealth } from "./api.js";
import { getHealthState, setHealthState } from "./state.js";

const statusText = document.querySelector("[data-health-status]");
const statusDot = document.querySelector("[data-health-dot]");
const refreshButton = document.querySelector("[data-refresh-health]");

function renderHealth() {
  const state = getHealthState();
  statusText.textContent = state.message;
  statusDot.dataset.state = state.status;
}

async function refreshHealth() {
  setHealthState({ status: "checking", message: "正在检查..." });
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
      message: error instanceof Error ? error.message : "健康检查失败",
    });
  }

  renderHealth();
}

refreshButton.addEventListener("click", refreshHealth);
refreshHealth();
