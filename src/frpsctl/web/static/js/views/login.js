/** 登录 / 登出 / 会话恢复（v0.3.5 登录页 2.0）。
 *
 *  三件事与安全/可用性直接相关：
 *
 *  - **提交互斥**：连点"登录"或狂按回车只发一次请求。这不只是体验问题——
 *    登录失败限速是 5 次/60 秒（`web/auth.py`），重复提交会消耗**用户自己**
 *    的失败配额，把正常用户锁在门外 60 秒。
 *  - **错误原因同形**：服务端刻意让"口令错误"与"被限速"的响应完全一致
 *    （不给爆破者信号），前端不得添加能区分二者的提示；只提供"连续失败会
 *    被暂时限制"的通用说明。
 *  - **会话过期提示**：`api.js` 的 401 回调带原因（首访不提示，避免误报）。
 *
 *  登录页元信息（实例名 / 版本 / 门槛）来自**免认证**的 `/api/login-info`，
 *  只含非敏感静态字段（边界见 `web/api.py::login_info_payload`）。
 */

import { api } from "../api.js";
import { $ } from "../ui/dom.js";
import { state } from "../state.js";
import { startPolling, stopPolling } from "./actions.js";
import { refresh } from "./dashboard.js";

let submitting = false;

/** 回到登录页（清空 CSRF、停轮询）；notice 用于"会话已过期"这类原因提示。 */
export function showLogin(notice = "") {
  state.csrf = null;
  stopPolling();
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
  $("login-error").textContent = notice;
  $("login-caps").hidden = true;
  submitting = false;
  const button = $("login-btn");
  button.disabled = false;
  button.textContent = "登录";
  $("password").value = "";
  // 口令可见性必须复位：否则上一个使用者点过"显示"后，下一个人的口令默认明文
  // 可见（安全相关的体验缺陷，v0.3.5 review 修复）。
  resetPasswordVisibility();
  $("password").focus();
}

/** 口令框复位为掩码态（并同步切换按钮的文案与 aria）。 */
function resetPasswordVisibility() {
  $("password").type = "password";
  const toggle = $("password-toggle");
  toggle.textContent = "显示";
  toggle.setAttribute("aria-pressed", "false");
}

/** 进入主界面（隐藏登录页、启动轮询并立即刷新）。 */
export function showApp() {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  startPolling();
  refresh();
}

function shake() {
  const card = $("login").querySelector(".login-card");
  if (!card) return;
  card.classList.remove("login-shake");
  void card.offsetWidth;  // 重排以重播动画
  card.classList.add("login-shake");
}

async function doLogin() {
  if (submitting) return;  // ★ 提交互斥：并发提交会消耗自己的失败配额
  submitting = true;
  const button = $("login-btn");
  button.disabled = true;
  button.textContent = "登录中…";
  $("login-error").textContent = "";
  try {
    const data = await api("/api/login", { method: "POST", body: { password: $("password").value } });
    state.csrf = data.csrf;
    $("password").value = "";
    submitting = false;
    button.disabled = false;
    button.textContent = "登录";
    showApp();
  } catch (err) {
    $("login-error").textContent = err.message;
    shake();
    submitting = false;
    button.disabled = false;
    button.textContent = "登录";
    $("password").select();
  }
}

function togglePassword() {
  const input = $("password");
  const shown = input.type === "text";
  input.type = shown ? "password" : "text";
  const button = $("password-toggle");
  button.textContent = shown ? "显示" : "隐藏";
  button.setAttribute("aria-pressed", shown ? "false" : "true");
  input.focus();
}

/** CapsLock 提示：老浏览器没有 getModifierState 时静默跳过。 */
function watchCaps(event) {
  if (typeof event.getModifierState !== "function") return;
  $("login-caps").hidden = !event.getModifierState("CapsLock");
}

/** 免认证的登录页元信息（失败静默——它只是补充信息，不该挡住登录）。 */
async function loadLoginInfo() {
  try {
    const info = await api("/api/login-info");
    const parts = [`实例 ${info.instance}`, `frpsctl ${info.frpsctl}`, `frps ≥ ${info.frps_minimum}`];
    if (info.non_loopback) parts.push("⚠ 非回环绑定");
    $("login-meta").textContent = parts.join("  ·  ");
  } catch (err) {
    $("login-meta").textContent = "";
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
  $("password").addEventListener("keydown", (event) => {
    if (event.key === "Enter") doLogin();
    watchCaps(event);
  });
  $("password").addEventListener("keyup", watchCaps);
  $("password-toggle").addEventListener("click", togglePassword);
  $("logout").addEventListener("click", async () => {
    try { await api("/api/logout", { method: "POST", body: {} }); } catch (e) { /* 忽略：本地状态照常清 */ }
    showLogin();
  });
  loadLoginInfo();
}
