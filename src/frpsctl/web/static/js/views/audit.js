/** 审计视图：插件审计 / Web 操作双视图 + 时间窗 + 服务端过滤 + 分页 + 导出（只读）。
 *
 *  v0.3.5：服务端过滤（`action/result/source` 或 `op/decision/user/source`）、
 *  "加载更多"（按 offset 向更旧翻页）与导出（JSONL / CSV 附件）。
 *
 *  两套过滤并存是刻意的：**服务端过滤**缩小查询范围（影响匹配数与分页），
 *  **当前页过滤**（`audit-filter`）只是本地高亮/筛选已加载的行。界面上用
 *  `audit-page` 一行说清"匹配 N 条、本页 M 条、统计未应用过滤"。
 */

import { api, apiDownload, saveBlob } from "../api.js";
import { $, el, emptyState, tableOf } from "../ui/dom.js";
import { state } from "../state.js";
import { toast } from "../ui/toast.js";
import { filterRows } from "../lib/table.js";
import {
  pluginAuditStatsItems,
  pluginAuditNotes,
  webAuditStatsItems,
  webAuditNotes,
  pluginAuditRows,
  webAuditRows,
} from "../lib/audit-format.js";

/** 每页记录数（"加载更多"每次追加一页）。 */
const PAGE_SIZE = 50;

/** 各 scope 可用的服务端过滤字段（与 `web/api.py` 的白名单一致）。 */
const FILTER_FIELDS = {
  plugin: [["", "不过滤"], ["op", "操作"], ["decision", "判定"], ["user", "用户"], ["source", "来源"]],
  web: [["", "不过滤"], ["action", "动作"], ["result", "结果"], ["source", "来源"]],
};

const PAGE_FILTER_KEYS = {
  plugin: ["op", "user", "proxy_name", "reason", "decision"],
  web: ["action", "target", "source", "detail"],
};

/** 重建过滤字段选项（仅在 scope 变化时调用，避免清掉用户的选择）。 */
function syncFilterFields() {
  const options = FILTER_FIELDS[state.auditScope] || FILTER_FIELDS.plugin;
  $("audit-field").replaceChildren(
    ...options.map(([value, label]) => el("option", { value, text: label })),
  );
}

/** 时间窗参数：自定义起止优先于预设按钮（两者互斥，见 markPresetButtons）。 */
function windowParams() {
  const params = new URLSearchParams();
  const from = $("audit-from").value.trim();
  const to = $("audit-to").value.trim();
  const since = from || state.auditSince;
  if (since) params.set("since", since);
  if (to) params.set("until", to);
  return params;
}

/** 预设按钮的高亮：只有"用预设且未填自定义起止"时才点亮。 */
function markPresetButtons() {
  const custom = $("audit-from").value.trim() || $("audit-to").value.trim();
  for (const btn of document.querySelectorAll("#audit-since button")) {
    btn.classList.toggle(
      "primary",
      !custom && (btn.dataset.since || "") === state.auditSince,
    );
  }
}

/** 当前生效的服务端过滤（字段, 值）；未填返回 null。 */
function activeFilter() {
  const field = $("audit-field").value;
  const value = $("audit-value").value.trim();
  return field && value ? [field, value] : null;
}

function statsNode([label, num]) {
  return [
    el("span", { class: "num", text: String(num) }),
    el("span", { class: "label", text: label }),
  ];
}

/** 请求序号：只有最后一次发起的请求可以写状态（防 append/切 scope 的乱序竞态）。 */
let auditSeq = 0;

/** 加载审计数据；`append=true` 时向更旧的方向追加一页。 */
export async function loadAudit(scope = state.auditScope, { append = false } = {}) {
  // 首次加载还没回来就点"加载更多"：没有已加载的记录可追加 → 退回首次加载。
  if (append && state.auditRecords.length === 0) append = false;
  const seq = ++auditSeq;
  state.auditScope = scope;
  state.auditLoaded = true;
  if (state.auditFilterScope !== scope) {
    syncFilterFields();
    // 切 scope 视为重置过滤条件：字段选项已被重建，残留的值会让用户以为在过滤
    $("audit-value").value = "";
    $("audit-from").value = "";
    $("audit-to").value = "";
    state.auditSince = "";
    state.auditFilterScope = scope;
  }
  $("audit-scope-plugin").classList.toggle("primary", scope === "plugin");
  $("audit-scope-web").classList.toggle("primary", scope === "web");
  $("audit-scope-plugin").setAttribute("aria-pressed", scope === "plugin" ? "true" : "false");
  $("audit-scope-web").setAttribute("aria-pressed", scope === "web" ? "true" : "false");
  markPresetButtons();
  const offset = append ? state.auditRecords.length : 0;
  if (!append) {
    $("audit-table").textContent = "加载中…";
    state.auditRecords = [];
  }
  try {
    const params = windowParams();
    if (scope === "web") params.set("scope", "web");
    const filter = activeFilter();
    if (filter) params.set(filter[0], filter[1]);
    params.set("limit", String(PAGE_SIZE));
    if (offset) params.set("offset", String(offset));
    const data = await api("/api/audit?" + params.toString());
    if (seq !== auditSeq) return;  // 已被更新的请求取代：丢弃本次结果
    const newestFirst = (data.tail || []).slice().reverse();
    state.auditRecords = append ? state.auditRecords.concat(newestFirst) : newestFirst;
    state.lastAuditData = data;
    renderAudit(data);
  } catch (err) {
    if (seq !== auditSeq) return;
    state.lastAuditData = null;
    state.auditRecords = [];
    $("audit-table").textContent = "审计读取失败：" + err.message;
  }
}

function renderAudit(data) {
  const isWeb = data.scope === "web";
  const statsBox = $("audit-stats");
  const box = $("audit-table");
  const items = isWeb ? webAuditStatsItems(data) : pluginAuditStatsItems(data);
  statsBox.replaceChildren(...items.map((item) => el("div", { class: "stat" }, statsNode(item))));
  $("audit-note").textContent = (isWeb ? webAuditNotes(data) : pluginAuditNotes(data)).join("\n");
  $("audit-path").textContent = data.path ? "· " + data.path : "";

  const page = data.page || {};
  const notes = [];
  if (page.matched !== undefined) {
    notes.push(`匹配 ${page.matched} 条 · 已加载 ${state.auditRecords.length} 条`);
  }
  const filters = page.filters || {};
  if (Object.keys(filters).length) {
    notes.push(
      "服务端过滤：" +
        Object.entries(filters).map(([key, value]) => `${key}~${value}`).join(" ") +
        "（上方统计为时间窗内全量，未应用过滤）",
    );
  }
  if (page.truncated) notes.push("匹配数超过扫描上限，已截断（请缩小时间窗或加过滤）");
  $("audit-page").textContent = notes.join("　·　");

  // 行数据：webAuditRows 内部会 reverse（期望时间升序输入），pluginAuditRows
  // 期望"新 → 旧"。state.auditRecords 统一保存"新 → 旧"，这里按各自契约适配。
  const rowsAll = isWeb
    ? webAuditRows([...state.auditRecords].reverse())
    : pluginAuditRows(state.auditRecords);
  const filtered = filterRows(rowsAll, $("audit-filter").value, PAGE_FILTER_KEYS[isWeb ? "web" : "plugin"]);
  const rows = isWeb
    ? filtered.map((row) =>
        el("tr", {}, [
          el("td", { text: row.at }),
          el("td", {}, [el("span", { class: row.ok ? "tag online" : "tag", text: row.ok ? "成功" : "失败" })]),
          el("td", { text: row.action }),
          el("td", { text: row.target }),
          el("td", { text: row.source }),
          el("td", { text: row.detail }),
        ]),
      )
    : filtered.map((row) =>
        el("tr", {}, [
          el("td", { text: row.at }),
          el("td", {}, [
            el("span", {
              class: row.decision === "allow" ? "tag online" : "tag",
              text: row.decision === "allow" ? "允许" : "拒绝",
            }),
          ]),
          el("td", { text: row.op }),
          el("td", { text: row.user }),
          el("td", { text: row.proxy_name }),
          el("td", { class: "num", text: row.remote_port }),
          el("td", { text: row.reason }),
        ]),
      );
  if (!rows.length) {
    box.replaceChildren(emptyState(rowsAll.length ? "没有匹配的记录" : "没有记录"));
  } else {
    box.replaceChildren(
      tableOf(
        isWeb
          ? ["时间", "结果", "动作", "目标", "来源", "参数"]
          : ["时间", "判定", "操作", "用户", "代理", "端口", "理由"],
        rows,
      ),
    );
  }
  if (data.bad_lines) {
    box.appendChild(el("div", { class: "muted", text: `（${data.bad_lines} 行无法解析）` }));
  }
  $("audit-more").hidden = !page.has_more;
}

/** 导出当前过滤条件下的审计（JSONL / CSV 附件）。 */
async function exportAudit() {
  const params = windowParams();
  if (state.auditScope === "web") params.set("scope", "web");
  const filter = activeFilter();
  if (filter) params.set(filter[0], filter[1]);
  params.set("format", $("audit-format").value);
  try {
    const { blob, filename, headers } = await apiDownload("/api/audit/export?" + params.toString());
    saveBlob(blob, filename);
    const truncated = headers.get("X-Export-Truncated") === "true";
    const records = headers.get("X-Export-Records");
    if (truncated) {
      toast(`导出已截断：仅含最近 ${records} 条匹配记录（缩小时间窗或加过滤条件）`, "err");
    } else {
      toast(`审计导出已开始下载（${records} 条）`, "ok");
    }
  } catch (err) {
    toast("导出失败：" + err.message, "err");
  }
}

/** 绑定审计控件（scope / 时间窗 / 服务端过滤 / 分页 / 导出 / 当前页过滤）。 */
export function initAudit() {
  $("audit-scope-plugin").addEventListener("click", () => loadAudit("plugin"));
  $("audit-scope-web").addEventListener("click", () => loadAudit("web"));
  $("audit-refresh").addEventListener("click", () => loadAudit());
  $("audit-field").addEventListener("change", () => loadAudit());
  $("audit-value").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadAudit();
  });
  $("audit-clear").addEventListener("click", () => {
    $("audit-value").value = "";
    $("audit-field").value = "";
    loadAudit();
  });
  $("audit-more").addEventListener("click", () => loadAudit(state.auditScope, { append: true }));
  // 自定义起止与预设互斥（双向）：填了自定义就清掉预设高亮，反之亦然
  // 起止两个输入行为对称：回车或失焦都重新查询（此前 from 失焦无反应、to 会查询）
  $("audit-from").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadAudit();
  });
  $("audit-from").addEventListener("change", () => {
    markPresetButtons();
    loadAudit();
  });
  $("audit-to").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadAudit();
  });
  $("audit-to").addEventListener("change", () => {
    state.auditSince = "";
    markPresetButtons();
    loadAudit();
  });
  $("audit-export").addEventListener("click", exportAudit);
  $("audit-filter").addEventListener("input", () => {
    if (state.auditLoaded && state.lastAuditData) renderAudit(state.lastAuditData);
  });
  for (const btn of document.querySelectorAll("#audit-since button")) {
    btn.addEventListener("click", () => {
      state.auditSince = btn.dataset.since || "";
      // 预设与自定义起止互斥：点预设即清空自定义输入（否则用户看不到谁在生效）
      $("audit-from").value = "";
      $("audit-to").value = "";
      loadAudit();
    });
  }
}
