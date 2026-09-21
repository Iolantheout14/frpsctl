/** 日志视图：增量 tail（since 偏移）+ 跟随/暂停 + 着色 + 复制。
 *
 *  v0.3.4：轮询改为增量追加（服务端返回 reset 时全量替换），并裁剪 DOM
 *  节点数——此前每 5 秒整段替换，长日志页会持续抖动与丢滚动位置。
 */

import { api } from "../api.js";
import { $, el } from "../ui/dom.js";
import { state } from "../state.js";
import { copyText, toast } from "../ui/toast.js";
import { classifyLogLine } from "../lib/format.js";

/** DOM 节点上限（约等于显示的日志行数上限，超出丢最旧）。 */
const MAX_LOG_NODES = 4000;

function renderLogLines(lines) {
  const frag = document.createDocumentFragment();
  for (const line of lines) {
    // 全量响应的行可能自带换行符（历史契约），增量响应不带——两种都正确渲染
    const text = line.endsWith("\n") ? line : line + "\n";
    frag.appendChild(el("span", { class: "logline " + classifyLogLine(line), text }));
  }
  return frag;
}

/** 拉取日志（增量：state.logOffset 为空或服务端 reset 时全量）。 */
export async function refreshLogs() {
  if (!state.logStick) return;   // 暂停期间不拉取（恢复时由滚动监听补拉）
  const lines = $("log-lines").value;
  const since = state.logOffset;
  const query = `lines=${lines}` + (since === null || since === undefined ? "" : `&since=${since}`);
  try {
    const data = await api(`/api/logs?${query}`);
    $("log-path").textContent = "· " + (data.path || "");
    const pre = $("logs");
    const reset = data.reset || since === null || since === undefined;
    if (reset) {
      pre.replaceChildren(renderLogLines(data.lines || []));
    } else if ((data.lines || []).length) {
      pre.appendChild(renderLogLines(data.lines || []));
      while (pre.childNodes.length > MAX_LOG_NODES) pre.removeChild(pre.firstChild);
    }
    if (data.offset !== undefined) state.logOffset = data.offset;
    if (state.logStick) pre.scrollTop = pre.scrollHeight;
  } catch (err) {
    // 不清屏（保留现场）；错误走 toast；offset 置空让下次请求全量重来
    // ——此前把错误文本写进日志体会永久混入 DOM（v0.3.4 review 修复）
    toast("日志读取失败：" + err.message, "err");
    state.logOffset = null;
  }
}

/** 绑定日志控件（滚动跟随、行数、复制）。 */
export function initLogs() {
  $("logs").addEventListener("scroll", () => {
    const pre = $("logs");
    const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    const wasStuck = state.logStick;
    state.logStick = stick;
    $("log-follow").textContent = state.logStick ? "跟随中" : "已暂停（滚到底部恢复）";
    if (state.logStick && !wasStuck) refreshLogs();   // 恢复跟随：立即补一次
  });
  $("log-lines").addEventListener("change", () => {
    // 切换行数是显式意图：恢复跟随并全量重拉
    state.logStick = true;
    state.logOffset = null;
    $("log-follow").textContent = "跟随中";
    refreshLogs();
  });
  $("log-copy").addEventListener("click", () => copyText($("logs").textContent, " 日志"));
}
