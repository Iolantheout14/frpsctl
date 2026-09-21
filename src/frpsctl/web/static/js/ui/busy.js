/** 变更类操作的进行中状态（按钮禁用 + 状态徽章"操作中…"）。 */

import { $ } from "./dom.js";
import { state } from "../state.js";

/** 批量切换按钮可用性；on=true 时状态徽章显示 label。 */
export function setBusy(on, label = "") {
  state.busy = on;
  for (const id of ["act-start", "act-restart", "act-stop", "cfg-preview", "cfg-apply", "cfg-reset", "refresh-btn", "logout"]) {
    const node = $(id);
    if (node) node.disabled = on;
  }
  if (on) {
    const dot = $("state-badge").querySelector(".dot");
    dot.className = "dot busy";
    $("state-text").textContent = label || "操作中…";
  }
}
