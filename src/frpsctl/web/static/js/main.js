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

/* 未保存修改的离页提示。 */
window.addEventListener("beforeunload", (event) => {
  if (state.dirty.size + state.deletes.size > 0) {
    event.preventDefault();
    event.returnValue = "";
  }
});

function init() {
  setAuthLostHandler(showLogin);
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
  initShortcuts();
  initLogin();
}

/* boot：有会话则直接进主界面（Cookie 还在、CSRF 从 /api/session 恢复）。 */
(async function boot() {
  init();
  try {
    await api("/api/status");
    await restoreSession();
    showApp();
  } catch (err) {
    showLogin();
  }
})();
