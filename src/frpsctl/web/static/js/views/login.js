/** 登录 / 登出 / 会话恢复。 */

import { api } from "../api.js";
import { $ } from "../ui/dom.js";
import { state } from "../state.js";
import { startPolling, stopPolling } from "./actions.js";
import { refresh } from "./dashboard.js";

/** 回到登录页（清空 CSRF、停轮询）。 */
export function showLogin() {
  state.csrf = null;
  stopPolling();
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
}

/** 进入主界面（隐藏登录页、启动轮询并立即刷新）。 */
export function showApp() {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  startPolling();
  refresh();
}

async function doLogin() {
  $("login-error").textContent = "";
  try {
    const data = await api("/api/login", { method: "POST", body: { password: $("password").value } });
    state.csrf = data.csrf;
    $("password").value = "";
    showApp();
  } catch (err) {
    $("login-error").textContent = err.message;
  }
}

/** 刷新页面后 Cookie 还在、但内存里的 CSRF 丢了：从服务端取回。 */
export async function restoreSession() {
  const data = await api("/api/session");
  state.csrf = data.csrf;
}

/** 绑定登录页控件。 */
export function initLogin() {
  $("login-btn").addEventListener("click", doLogin);
  $("password").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
  $("logout").addEventListener("click", async () => {
    try { await api("/api/logout", { method: "POST", body: {} }); } catch (e) { /* 忽略 */ }
    showLogin();
  });
}
