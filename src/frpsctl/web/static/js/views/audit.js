/** 审计视图：插件审计 / Web 操作双视图 + 时间窗 + 过滤（只读）。 */

import { api } from "../api.js";
import { $, el, emptyState, tableOf } from "../ui/dom.js";
import { state } from "../state.js";
import { filterRows } from "../lib/table.js";
import {
  pluginAuditStatsItems,
  pluginAuditNotes,
  webAuditStatsItems,
  webAuditNotes,
  pluginAuditRows,
  webAuditRows,
} from "../lib/audit-format.js";

/** 加载审计数据（scope: plugin / web；since: ""/"1h"/"24h"/"7d"）。 */
export async function loadAudit(scope = state.auditScope) {
  state.auditScope = scope;
  state.auditLoaded = true;
  $("audit-scope-plugin").classList.toggle("primary", scope === "plugin");
  $("audit-scope-web").classList.toggle("primary", scope === "web");
  for (const btn of document.querySelectorAll("#audit-since button")) {
    btn.classList.toggle("primary", (btn.dataset.since || "") === state.auditSince);
  }
  $("audit-table").textContent = "加载中…";
  try {
    const params = new URLSearchParams();
    if (scope === "web") params.set("scope", "web");
    if (state.auditSince) params.set("since", state.auditSince);
    const suffix = params.toString() ? `?${params}` : "";
    state.lastAuditData = await api("/api/audit" + suffix);
    renderAudit(state.lastAuditData);
  } catch (err) {
    $("audit-table").textContent = "审计读取失败：" + err.message;
  }
}

function renderAudit(data) {
  const statsBox = $("audit-stats");
  const box = $("audit-table");
  if (data.scope === "web") {
    $("audit-path").textContent = data.path ? "· " + data.path : "";
    statsBox.replaceChildren(...webAuditStatsItems(data).map(([label, num]) => el("div", { class: "stat" }, [
      el("span", { class: "num", text: String(num) }),
      el("span", { class: "label", text: label }),
    ])));
    $("audit-note").textContent = webAuditNotes(data).join("\n");
    const all = webAuditRows(data.tail);
    const filtered = filterRows(all, $("audit-filter").value, ["action", "target", "source", "detail"]);
    const rows = filtered.map((r) => el("tr", {}, [
      el("td", { text: r.at }),
      el("td", {}, [el("span", { class: r.ok ? "tag online" : "tag", text: r.ok ? "成功" : "失败" })]),
      el("td", { text: r.action }),
      el("td", { text: r.target }),
      el("td", { text: r.source }),
      el("td", { text: r.detail }),
    ]));
    if (!rows.length) box.replaceChildren(emptyState(all.length ? "没有匹配的记录" : "没有记录"));
    else box.replaceChildren(tableOf(["时间", "结果", "动作", "目标", "来源", "参数"], rows));
    if (data.bad_lines) box.appendChild(el("div", { class: "muted", text: `（${data.bad_lines} 行无法解析）` }));
    return;
  }
  $("audit-path").textContent = data.path ? "· " + data.path : "";
  statsBox.replaceChildren(...pluginAuditStatsItems(data).map(([label, num]) => el("div", { class: "stat" }, [
    el("span", { class: "num", text: String(num) }),
    el("span", { class: "label", text: label }),
  ])));
  $("audit-note").textContent = pluginAuditNotes(data).join("\n");
  const all = pluginAuditRows((data.tail || []).slice().reverse());
  const filtered = filterRows(all, $("audit-filter").value, ["op", "user", "proxy_name", "reason", "decision"]);
  const rows = filtered.map((r) => el("tr", {}, [
    el("td", { text: r.at }),
    el("td", {}, [el("span", { class: r.decision === "allow" ? "tag online" : "tag", text: r.decision === "allow" ? "允许" : "拒绝" })]),
    el("td", { text: r.op }),
    el("td", { text: r.user }),
    el("td", { text: r.proxy_name }),
    el("td", { class: "num", text: r.remote_port }),
    el("td", { text: r.reason }),
  ]));
  if (!rows.length) box.replaceChildren(emptyState(all.length ? "没有匹配的记录" : "没有记录"));
  else box.replaceChildren(tableOf(["时间", "判定", "操作", "用户", "代理", "端口", "理由"], rows));
  if (data.bad_lines) box.appendChild(el("div", { class: "muted", text: `（${data.bad_lines} 行无法解析）` }));
}

/** 绑定审计控件（scope / 时间窗 / 刷新 / 过滤）。 */
export function initAudit() {
  $("audit-scope-plugin").addEventListener("click", () => loadAudit("plugin"));
  $("audit-scope-web").addEventListener("click", () => loadAudit("web"));
  $("audit-refresh").addEventListener("click", () => loadAudit());
  $("audit-filter").addEventListener("input", () => {
    if (state.auditLoaded && state.lastAuditData) renderAudit(state.lastAuditData);
  });
  for (const btn of document.querySelectorAll("#audit-since button")) {
    btn.addEventListener("click", () => {
      state.auditSince = btn.dataset.since || "";
      loadAudit();
    });
  }
}
