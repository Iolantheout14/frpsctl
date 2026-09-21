/** 变更类动作（启动/停止/重启）与 5 秒轮询调度。 */

import { api } from "../api.js";
import { $ } from "../ui/dom.js";
import { state } from "../state.js";
import { setBusy } from "../ui/busy.js";
import { confirmAsync } from "../ui/modal.js";
import { toast } from "../ui/toast.js";
import { refresh } from "./dashboard.js";

/** 执行一次实例动作（start/stop/restart/rollback），带确认与结果通知。 */
export async function doAction(name, body = {}) {
  const label = { start: "启动", stop: "停止", restart: "重启" }[name] || name;
  if (name !== "start") {
    const yes = await confirmAsync(`确认${label}实例？`, "", { danger: name === "stop" });
    if (!yes) return;
  }
  setBusy(true, `${label}中…`);
  try {
    const data = await api(`/api/actions/${name}`, { method: "POST", body });
    toast(`${label}完成` + (data.pid ? `（pid ${data.pid}）` : "") + (data.healthy === false ? "，但健康检查未通过" : ""), data.healthy === false ? "err" : "ok");
  } catch (err) {
    toast(`${label}失败：\n` + err.message, "err");
  } finally {
    setBusy(false);
  }
  refresh();
}

/** 启动自动轮询（已启动则幂等）。 */
export function startPolling() {
  if (!state.timer) state.timer = setInterval(tick, 5000);
}

/** 停止自动轮询。 */
export function stopPolling() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
}

async function tick() {
  if (document.hidden || !$("autopoll").checked || state.busy) return;
  if (!$("view-dash").classList.contains("hidden")) refresh();
}

/** 绑定动作按钮与自动刷新开关。 */
export function initActions() {
  $("act-start").addEventListener("click", () => doAction("start"));
  $("act-stop").addEventListener("click", () => doAction("stop"));
  $("act-restart").addEventListener("click", () => doAction("restart"));
  $("refresh-btn").addEventListener("click", () => refresh());
  $("autopoll").addEventListener("change", (e) => {
    stopPolling();
    if (e.target.checked) { startPolling(); refresh(); }
  });
}
