/** 装配与启动（唯一入口：index.html 只引用本模块）。 */

import { api, setAuthLostHandler } from "./api.js";
import { state } from "./state.js";
import { initTheme } from "./theme.js";
import { initCommands } from "./commands.js";
import { initShortcuts } from "./shortcuts.js";
import { initLogin, showLogin, showApp, restoreSession } from "./views/login.js";
import { initRouter } from "./views/router.js";
import { initActions } from "./views/actions.js";
import { initDashboard } from "./views/dashboard.js";
import { initLogs } from "./views/logs.js";
import { initConfig } from "./views/config.js";
import { initAudit } from "./views/audit.js";
import { initServices } from "./views/services.js";
import { initVersions } from "./views/versions.js";
import { initCommandsView } from "./views/commands.js";
import { initSessions } from "./views/sessions.js";

/* 未保存修改的离页提示。 */
window.addEventListener("beforeunload", (event) => {
  if (state.dirty.size + state.deletes.size > 0) {
    event.preventDefault();
    event.returnValue = "";
  }
});

/** 首次启动完成前不把 401 说成"会话已过期"（首访无会话是正常状态）。 */
let booted = false;

function init() {
  setAuthLostHandler(() => showLogin(booted ? "会话已过期，请重新登录。" : ""));
  initTheme();
  initCommands();
  initRouter();
  initActions();
  initDashboard();
  initLogs();
  initConfig();
  initAudit();
  initServices();
  initVersions();
  initCommandsView();
  initSessions();
  initShortcuts();
  initLogin();
}

/* boot：有会话则直接进主界面（Cookie 还在、CSRF 从 /api/session 恢复）。 */
(async function boot() {
  try {
    init();
  } catch (err) {
    // 初始化失败（例如某个 id 打错导致 $() 返回 null）不能留一个全 hidden 的
    // 空白页：让登录页可见并给出可行动的提示（v0.3.5 review：此前 init() 在
    // try 之外，一处 typo 就是整页空白且只在控制台可见）。
    console.error("frpsctl: 界面初始化失败", err);
    showLogin("界面初始化失败，请刷新页面；若持续出现请查看浏览器控制台。");
    return;
  }
  try {
    await api("/api/status");
    await restoreSession();
    showApp();
  } catch (err) {
    showLogin();
  }
  booted = true;
})();
