/** 键盘快捷键（g d/c/a/s/v 导航、r 刷新、/ 聚焦过滤、Ctrl+K 命令面板）。 */

import { $ } from "./ui/dom.js";
import { state } from "./state.js";
import { switchView } from "./views/router.js";
import { refresh } from "./views/dashboard.js";
import { openCommandPalette, paletteVisible } from "./commands.js";

/** 全局键位绑定（在输入框中只保留 Escape）。 */
export function initShortcuts() {
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      openCommandPalette();
      return;
    }
    if (paletteVisible()) return;   // 面板自己处理按键
    const tag = (e.target && e.target.tagName) || "";
    const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if (typing && e.key !== "Escape") return;
    if (!$("modal-backdrop").classList.contains("hidden")) return;  // 弹层自己处理
    if (e.key === "g") { state.gPrefix = Date.now(); return; }
    if (Date.now() - state.gPrefix < 1200) {
      const nav = { d: "dash", c: "config", a: "audit", s: "services", v: "versions", k: "commands" }[e.key];
      if (nav) { switchView(nav); state.gPrefix = 0; return; }
    }
    if (e.key === "r" && !e.metaKey && !e.ctrlKey) { refresh(); }
    if (e.key === "/") {
      const target = !$("view-config").classList.contains("hidden") ? $("cfg-search")
        : (!$("view-audit").classList.contains("hidden") ? $("audit-filter")
        : (!$("view-commands").classList.contains("hidden") ? $("cmd-search")
        : $("proxies-filter")));
      e.preventDefault();
      if (target) target.focus();
    }
  });
}
