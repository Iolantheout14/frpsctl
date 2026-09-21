/** 通知（toast）与剪贴板复制。 */

import { $, el } from "./dom.js";

/** 弹出一条通知。kind：""（信息）/ "ok" / "err"（err 常驻，需手动关闭）。 */
export function toast(msg, kind = "") {
  const icon = kind === "err" ? "✕" : (kind === "ok" ? "✓" : "ℹ");
  const box = el("div", { class: "item " + kind });
  const close = el("button", { class: "close", text: "×", title: "关闭", onclick: () => box.remove() });
  box.append(
    el("span", { class: "ticon", text: icon, "aria-hidden": "true" }),
    el("div", { class: "msg", text: msg }),
    close,
  );
  $("toast").appendChild(box);
  if (kind !== "err") setTimeout(() => box.remove(), 4000);
}

/** 复制文本到剪贴板（失败降级为错误通知）。 */
export async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`已复制${label || ""}`, "ok");
  } catch (e) {
    toast("复制失败（浏览器未授权剪贴板）", "err");
  }
}
