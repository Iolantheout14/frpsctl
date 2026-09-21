/** 自绘确认弹层（替代原生 confirm：可样式化、Enter/Esc 可用）。
 *  以及通用弹层基座（详情抽屉 / 命令面板复用）。 */

import { $, el } from "./dom.js";

/** 确认框：返回 Promise<boolean>。danger 时确认键为危险配色。 */
export function confirmAsync(title, body = "", { danger = false } = {}) {
  return new Promise((resolve) => {
    const backdrop = $("modal-backdrop");
    $("modal-title").textContent = title;
    $("modal-body").textContent = body;
    const ok = $("modal-ok");
    ok.classList.toggle("danger", danger);
    ok.classList.toggle("primary", !danger);
    const done = (value) => {
      backdrop.classList.add("hidden");
      document.removeEventListener("keydown", onKey, true);
      ok.removeEventListener("click", onOk);
      $("modal-cancel").removeEventListener("click", onCancel);
      backdrop.removeEventListener("click", onBackdrop);
      resolve(value);
    };
    const onOk = () => done(true);
    const onCancel = () => done(false);
    const onBackdrop = (e) => { if (e.target === backdrop) done(false); };
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); done(false); }
      else if (e.key === "Enter") { e.stopPropagation(); done(true); }
    };
    ok.addEventListener("click", onOk);
    $("modal-cancel").addEventListener("click", onCancel);
    backdrop.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKey, true);
    backdrop.classList.remove("hidden");
    ok.focus();
  });
}

/** 侧滑抽屉：显示标题 + 内容节点；重复打开只替换内容（监听不重复注册）。
 *  内容由调用方构造（一律 textContent/createElement，不拼 HTML 字符串）。 */
let closeDrawer = null;

export function openDrawer(title, contentNode) {
  const box = $("drawer");
  const backdrop = $("drawer-backdrop");
  $("drawer-title").textContent = title;
  $("drawer-body").replaceChildren(contentNode);
  box.classList.remove("hidden");
  if (closeDrawer) return;   // 已打开：仅替换内容，ESC/遮罩监听保持有效
  const onKey = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
  };
  const onBackdrop = (e) => { if (e.target === backdrop) close(); };
  const close = () => {
    box.classList.add("hidden");
    document.removeEventListener("keydown", onKey, true);
    backdrop.removeEventListener("click", onBackdrop);
    $("drawer-close").removeEventListener("click", close);
    closeDrawer = null;
  };
  document.addEventListener("keydown", onKey, true);
  backdrop.addEventListener("click", onBackdrop);
  $("drawer-close").addEventListener("click", close);
  closeDrawer = close;
}
