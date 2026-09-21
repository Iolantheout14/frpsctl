/** JSON API 客户端（fetch 封装 + CSRF + 统一错误）。
 *
 *  401 的"会话失效"通过注册回调通知登录模块——避免 api ↔ 视图的循环依赖。
 */

import { state } from "./state.js";

let authLostHandler = () => {};

/** 注册会话失效处理器（main 在启动时传入 showLogin）。 */
export function setAuthLostHandler(fn) {
  authLostHandler = fn;
}

/** 发起一次 JSON API 调用；非 2xx 抛带 message/hint 的 Error。 */
export async function api(path, { method = "GET", body = null } = {}) {
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  if (method !== "GET" && state.csrf) headers["X-CSRF-Token"] = state.csrf;
  const resp = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : null });
  let data = {};
  try { data = await resp.json(); } catch (e) { /* 空响应体 */ }
  if (resp.status === 401 && !path.endsWith("/login")) {
    authLostHandler();
    throw new Error("登录已过期，请重新登录");
  }
  if (!resp.ok) throw new Error(data.error ? (data.error + (data.hint ? "\n" + data.hint : "")) : `HTTP ${resp.status}`);
  return data;
}

/** 下载接口（诊断导出）：返回 {blob, filename}。 */
export async function apiDownload(path) {
  const resp = await fetch(path);
  if (!resp.ok) {
    let data = {};
    try { data = await resp.json(); } catch (e) { /* 非 JSON 错误体 */ }
    throw new Error(data.error ? (data.error + (data.hint ? "\n" + data.hint : "")) : `HTTP ${resp.status}`);
  }
  const blob = await resp.blob();
  const disposition = resp.headers.get("Content-Disposition") || "";
  const match = /filename="?([^";]+)"?/.exec(disposition);
  return { blob, filename: match ? match[1] : "frpsctl-diagnostics.txt" };
}

/** 触发浏览器下载。 */
export function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
