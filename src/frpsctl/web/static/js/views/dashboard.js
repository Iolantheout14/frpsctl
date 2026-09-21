/** 仪表盘视图：状态 / 横幅 / Hero / 列表 / 体检 / 详情抽屉。
 *
 *  与 actions.js 存在受控的循环 import（doAction ↔ refresh）：两侧都只在
 *  运行时调用对方导出，不在模块顶层求值，ESM 下安全。
 */

import { api, apiDownload, saveBlob } from "../api.js";
import { state, DETAIL_TTL } from "../state.js";
import { $, el, kvRow, animateNumber, emptyState, tableOf, sortHeader } from "../ui/dom.js";
import { toast, copyText } from "../ui/toast.js";
import { setBusy } from "../ui/busy.js";
import { confirmAsync, openDrawer } from "../ui/modal.js";
import { humanBytes, humanDuration, badgeClass } from "../lib/format.js";
import { filterRows, sortRows, clientRows, proxyRows } from "../lib/table.js";
import { refreshTraffic, bars7d, donut } from "../ui/charts.js";
import { refreshLogs } from "./logs.js";
import { doAction } from "./actions.js";

let refreshing = false;   // 防止慢请求叠加（5s 轮询 + 手动刷新）

/** 带 in-flight 守卫的刷新入口（轮询与手动刷新共用）。 */
export async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    await doRefresh();
  } finally {
    refreshing = false;
  }
}

async function doRefresh() {
  let status;
  try { status = await api("/api/status"); } catch (err) { toast("状态刷新失败：" + err.message, "err"); return; }
  state.lastStatus = status;
  $("inst-badge").textContent = `${status.instance} · ${status.owner}`;
  const dot = $("state-badge").querySelector(".dot");
  dot.className = "dot " + badgeClass(status.state);
  $("state-text").textContent = status.state;

  setBanners(buildBanners(status));

  $("kv-status").replaceChildren(...kvRow([
    ["状态", status.state + (status.pid ? `（pid ${status.pid}）` : "")],
    ["运行时长", humanDuration(status.uptime_seconds)],
    ["版本", (status.binary_version || "-") + (status.disk_version && status.disk_version !== status.binary_version ? ` → 磁盘 ${status.disk_version}（重启生效）` : "")],
    ["监听", status.listen ? `${status.listen.addr}:${status.listen.port}` : "-"],
    ["systemd", status.systemd_unit ? `${status.systemd_unit}（MainPID ${status.systemd_main_pid ?? "-"}）` : "-"],
  ]));
  const h = status.health;
  $("kv-health").replaceChildren(...kvRow([
    ["L1 进程", h ? h.l1_process : "-"],
    ["L2 控制面", h ? h.l2_control : "-"],
    ["L3 插件", h ? h.l3_plugin : "-"],
    ["gate", h ? (h.gate ? "通过" : "未通过") : "-"],
    ["详情", (h && h.detail) || "-"],
  ]));

  const dash = status.dashboard || {};
  renderHero(dash);

  if (status.state === "RUNNING" || status.state === "SYSTEMD_ACTIVE") {
    refreshLists();
    refreshTraffic();
    refreshLogs();
  }
}

/** 状态 → 横幅列表（含"配置待重启"的一键重启动作）。 */
function buildBanners(status) {
  const banners = [];
  if (status.state_corrupted) {
    banners.push({
      kind: "err",
      text: "⚠ 状态文件已损坏：无法判断进程归属，因此 stop / start / 配置变更都会被拒绝。\n"
        + "请确认没有 frps 在跑，然后删除实例目录下的 state.json。",
    });
  }
  if (status.version_hint) banners.push({ kind: "warn", text: "⚠ " + status.version_hint });
  if (status.systemd_probe_error) {
    banners.push({
      kind: "warn",
      text: "⚠ systemd 探测失败（所有权暂按 state.json 降级判定，变更操作会拒绝执行）：\n" + status.systemd_probe_error,
    });
  }
  const h = status.health;
  if (h && h.plugin_warning) banners.push({ kind: "warn", text: "⚠ " + h.plugin_warning });
  if (status.config_pending_restart) {
    banners.push({
      kind: "warn",
      text: "⚠ 配置文件已被修改但尚未生效（frps 没有热重载，需要重启）。",
      action: { label: "立即重启", onClick: () => doAction("restart") },
    });
  }
  return banners;
}

/** 渲染横幅（支持可选的操作按钮）。 */
function setBanners(items) {
  const box = $("banner");
  box.replaceChildren(...items.map((b) => {
    const node = el("div", { class: "banner " + b.kind, text: b.text });
    if (b.action) {
      node.appendChild(el("button", {
        class: "iconbtn banner-action",
        text: b.action.label,
        onclick: b.action.onClick,
      }));
    }
    return node;
  }));
  box.classList.toggle("hidden", items.length === 0);
}

function renderHero(dash) {
  const base = state.sessionBase;
  const sessionIn = base && dash.traffic_in !== null && dash.traffic_in !== undefined
    ? Math.max(0, dash.traffic_in - base.vin) : null;
  const sessionOut = base && dash.traffic_out !== null && dash.traffic_out !== undefined
    ? Math.max(0, dash.traffic_out - base.vout) : null;
  const items = [
    ["客户端", dash.clients],
    ["代理总数", dash.proxy_total],
    ["当前连接", dash.cur_conns],
    ["今日入站", humanBytes(dash.traffic_in)],
    ["今日出站", humanBytes(dash.traffic_out)],
    ["会话入站", humanBytes(sessionIn)],
    ["会话出站", humanBytes(sessionOut)],
    ["速率峰值", state.sessionPeak === null ? "-" : humanBytes(state.sessionPeak) + "/s"],
    ["速率均值", state.sessionAvg === null ? "-" : humanBytes(state.sessionAvg) + "/s"],
    ["TLS 强制", dash.tls_force === undefined ? "-" : (dash.tls_force ? "开" : "关")],
  ];
  const current = {};
  const frag = document.createDocumentFragment();
  for (const [label, value] of items) {
    const metric = el("div", { class: "metric" });
    const shown = value === null || value === undefined ? "-" : String(value);
    const num = el("span", { class: "num", text: "0" });
    if (!state.heroRendered && /^\d+$/.test(shown)) animateNumber(num, shown);
    else num.textContent = shown;
    metric.append(num);
    if (/^\d+$/.test(shown)) {
      current[label] = Number(shown);
      if (state.heroPrev && state.heroPrev[label] !== undefined) {
        const delta = Number(shown) - state.heroPrev[label];
        if (delta !== 0) {
          metric.append(el("span", {
            class: "delta",
            text: `${delta > 0 ? "▲" : "▼"} ${Math.abs(delta)}`,
            title: "与上一次刷新的变化",
          }));
        }
      }
    }
    metric.append(el("span", { class: "label", text: label }));
    frag.appendChild(metric);
  }
  state.heroPrev = current;
  $("hero").replaceChildren(frag);
  state.heroRendered = true;
  renderDonut(dash.proxy_type_counts);
}

/** 代理类型环图（无数据时显示空态）。 */
function renderDonut(typeCounts) {
  const box = $("chart-donut");
  if (!box) return;
  box.replaceChildren(donut(typeCounts));
}

/* ---------- 列表（客户端 / 代理） ---------- */

async function refreshLists() {
  try {
    const [clients, proxies, users] = await Promise.all([
      api("/api/clients"),
      api("/api/proxies"),
      api("/api/users"),
    ]);
    state.clientsCache = clients.clients || [];
    state.clientsTotal = clients.total || state.clientsCache.length;
    renderClients(state.clientsCache, state.clientsTotal, clients.total_known !== false);
    state.proxiesCache = proxies.proxies || [];
    state.proxiesTotal = proxies.total || state.proxiesCache.length;
    renderProxies(state.proxiesCache, proxies.total_known !== false);
    renderTopProxies(state.proxiesCache);
    renderUsers(users);
  } catch (err) {
    $("clients-table").textContent = "（dashboard 不可用：" + err.message + "）";
    $("proxies-table").textContent = "（dashboard 不可用：" + err.message + "）";
  }
}

/** 按用户聚合卡片（v0.3.5 F3）：客户端数 / 代理数（dashboard 的 users 口径）。 */
function renderUsers(data) {
  const box = $("users-table");
  const items = (data && data.users) || [];
  if (!items.length) {
    box.replaceChildren(emptyState("暂无按用户数据"));
    return;
  }
  const rows = items.map((item) =>
    el("tr", {}, [
      el("td", { text: item.user || "(未声明)" }),
      el("td", { class: "num", text: String(item.client_count) }),
      el("td", { class: "num", text: String(item.proxy_count) }),
    ]),
  );
  const nodes = [tableOf(["用户", "客户端", "代理"], rows)];
  if (data.truncated) {
    nodes.push(el("div", { class: "muted", text: `（仅显示前 ${data.limit} 个用户）` }));
  }
  box.replaceChildren(...nodes);
}

function renderClients(items, total, totalKnown = true) {
  const all = clientRows(items);
  const filtered = filterRows(all, $("clients-filter").value, ["name", "user", "hostname", "ip"]);
  const sorted = sortRows(filtered, state.clientSort.key, state.clientSort.dir);
  const rows = sorted.map((c) => el("tr", { class: "clickable", onclick: () => openClientDetail(c.name) }, [
    el("td", { text: c.name, title: "双击复制；单击查看详情", ondblclick: (e) => { e.stopPropagation(); copyText(c.name, "客户端名"); } }),
    el("td", { text: c.user }),
    el("td", { text: c.hostname }),
    el("td", { text: c.ip }),
    el("td", {}, [el("span", { class: c.online ? "tag online" : "tag", text: c.online ? "online" : "offline" })]),
    el("td", { text: c.version }),
  ]));
  const box = $("clients-table");
  const rerender = () => renderClients(state.clientsCache, state.clientsTotal);
  const headers = [
    sortHeader("name", "name", state.clientSort, rerender),
    sortHeader("user", "user", state.clientSort, rerender),
    sortHeader("hostname", "hostname", state.clientSort, rerender),
    sortHeader("ip", "ip", state.clientSort, rerender),
    "状态",
    sortHeader("版本", "version", state.clientSort, rerender),
  ];
  if (!rows.length) {
    box.replaceChildren(emptyState(all.length ? "没有匹配的客户端" : "暂无客户端"));
  } else {
    box.replaceChildren(tableOf(headers, rows));
  }
  $("clients-count").textContent = all.length ? `显示 ${rows.length} / ${all.length}` : "";
  if (typeof total === "number" && total !== items.length) {
    box.appendChild(el("div", {
      class: "muted",
      text: totalKnown
        ? `共 ${total} 条（已加载 ${items.length} 条）`
        : `至少 ${total} 条（服务端未提供总数；已加载 ${items.length} 条）`,
    }));
  }
}

function detailCell(name) {
  const detail = state.detailCache.get(name);
  if (!detail || detail.loading) return el("div", { class: "muted", text: "历史加载中…" });
  if (detail.error) return el("div", { class: "muted", text: "历史读取失败：" + detail.error });
  return bars7d(detail.history || []);
}

/* ---------- 今日流量 Top 5（v0.3.4 F2：排行与环图互补） ---------- */

function renderTopProxies(items) {
  const box = $("top-proxies");
  if (!box) return;
  const rows = proxyRows(items)
    .map((p) => ({ ...p, total_raw: p.in_raw + p.out_raw }))
    .filter((p) => p.total_raw > 0)
    .sort((a, b) => b.total_raw - a.total_raw)
    .slice(0, 5);
  if (!rows.length) {
    box.replaceChildren(emptyState("暂无流量数据"));
    return;
  }
  const table = el("table", {}, [
    el("thead", {}, [el("tr", {}, [
      el("th", { text: "#" }),
      el("th", { text: "代理" }),
      el("th", { text: "用户" }),
      el("th", { text: "今日(入/出)", class: "num" }),
    ])]),
    el("tbody", {}, rows.map((p, index) => el("tr", {}, [
      el("td", { class: "num", text: String(index + 1) }),
      el("td", { text: p.name, title: "双击复制", ondblclick: () => copyText(p.name, "代理名") }),
      el("td", { text: p.user }),
      el("td", { class: "num", text: p.traffic }),
    ]))),
  ]);
  box.replaceChildren(table);
}

function proxyRowsFor(items) {
  const all = proxyRows(items);
  const filtered = filterRows(all, $("proxies-filter").value, ["name", "user", "type", "port", "phase"]);
  return sortRows(filtered, state.proxySort.key, state.proxySort.dir);
}

function renderProxies(items, totalKnown = true) {
  const all = proxyRows(items);
  const sorted = proxyRowsFor(items);
  const rows = [];
  for (const p of sorted) {
    rows.push(el("tr", { class: "clickable", onclick: () => toggleProxy(p.name) }, [
      el("td", { text: p.name, title: "双击复制；单击展开曲线", ondblclick: (e) => { e.stopPropagation(); copyText(p.name, "代理名"); } }),
      el("td", { text: p.user }),
      el("td", { text: p.type }),
      el("td", {
        class: "num",
        text: p.port,
        title: p.port !== "-" ? "双击复制端口" : "",
        ondblclick: (e) => { e.stopPropagation(); if (p.port !== "-") copyText(p.port, "端口"); },
      }),
      el("td", {}, [el("span", { class: p.online ? "tag online" : "tag", text: p.phase })]),
      el("td", { class: "num", text: p.conns }),
      el("td", { class: "num", text: p.traffic }),
      el("td", {}, [
        el("button", {
          class: "iconbtn", text: "详情", title: "查看代理详情",
          onclick: (e) => { e.stopPropagation(); openProxyDetail(p.name); },
        }),
      ]),
    ]));
    if (state.expandedProxies.has(p.name)) {
      rows.push(el("tr", { class: "detail" }, [
        el("td", { colspan: "8" }, [detailCell(p.name)]),
      ]));
    }
  }
  const box = $("proxies-table");
  const rerender = () => renderProxies(state.proxiesCache);
  const headers = [
    sortHeader("name", "name", state.proxySort, rerender),
    sortHeader("user", "user", state.proxySort, rerender),
    sortHeader("类型", "type", state.proxySort, rerender),
    sortHeader("端口", "port", state.proxySort, rerender),
    "状态",
    sortHeader("连接", "conns", state.proxySort, rerender),
    "今日(入/出)",
    "",
  ];
  if (!rows.length) {
    box.replaceChildren(emptyState(all.length ? "没有匹配的代理" : "暂无代理记录"));
  } else {
    box.replaceChildren(tableOf(headers, rows));
  }
  $("proxies-count").textContent = all.length ? `显示 ${sorted.length} / ${all.length}` : "";
  if (state.proxiesTotal > items.length) {
    box.appendChild(el("div", {
      class: "muted",
      text: totalKnown
        ? `共 ${state.proxiesTotal} 条（已加载 ${items.length} 条）`
        : `至少 ${state.proxiesTotal} 条（服务端未提供总数；已加载 ${items.length} 条）`,
    }));
  }
}

function toggleProxy(name) {
  if (!name || name === "-") return;  // 无名代理没有可查的明细
  if (state.expandedProxies.has(name)) {
    state.expandedProxies.delete(name);
    renderProxies(state.proxiesCache);
    return;
  }
  state.expandedProxies.add(name);
  renderProxies(state.proxiesCache);
  loadProxyDetail(name);
}

async function loadProxyDetail(name) {
  const cached = state.detailCache.get(name);
  if (cached && (cached.loading || (cached.history && Date.now() - cached.at < DETAIL_TTL))) return;
  state.detailCache.set(name, { loading: true, at: Date.now() });
  try {
    const data = await api(`/api/traffic/${encodeURIComponent(name)}`);
    state.detailCache.set(name, { history: data.history || [], at: Date.now() });
  } catch (err) {
    state.detailCache.set(name, { error: err.message, at: Date.now() });
  }
  if (state.expandedProxies.has(name)) renderProxies(state.proxiesCache);
}

/* ---------- 详情抽屉（v0.3.4） ---------- */

/** 客户端详情：v2 `/api/v2/clients/{key}` 的只读透传。 */
export async function openClientDetail(key) {
  if (!key || key === "-") return;
  const content = el("div", { class: "muted", text: "加载中…" });
  openDrawer(`客户端：${key}`, content);
  try {
    const data = await api(`/api/clients/${encodeURIComponent(key)}`);
    const d = data.detail || {};
    content.replaceChildren(...kvRow([
      ["名称", d.key || key],
      ["用户", d.user || "-"],
      ["主机名", d.hostname || "-"],
      ["客户端 IP", d.clientIP || "-"],
      ["版本", d.version || "-"],
      ["在线", d.online ? "是" : "否"],
      ["连接数", String(d.conns ?? d.curConns ?? 0)],
      ["最后活跃", d.lastActive || d.last_active || "-"],
    ]));
  } catch (err) {
    content.textContent = "详情读取失败：" + err.message;
  }
}

/** 代理详情：v2 `/api/v2/proxies/{name}` 的只读透传。 */
export async function openProxyDetail(name) {
  if (!name || name === "-") return;
  const content = el("div", { class: "muted", text: "加载中…" });
  openDrawer(`代理：${name}`, content);
  try {
    const data = await api(`/api/proxies/${encodeURIComponent(name)}`);
    const d = data.detail || {};
    content.replaceChildren(...kvRow([
      ["名称", d.name || name],
      ["用户", d.user || "-"],
      ["类型", d.type || "-"],
      ["客户端", d.clientID || d.client_id || "-"],
      ["远端端口", d.remotePort !== undefined ? String(d.remotePort) : "-"],
      ["状态", d.status || d.phase || "-"],
      ["今日入站", humanBytes(d.todayTrafficIn)],
      ["今日出站", humanBytes(d.todayTrafficOut)],
      ["当前连接", String(d.curConns ?? 0)],
      ["最后启用", d.lastStartTime || d.lastStartAt || "-"],
    ]));
  } catch (err) {
    content.textContent = "详情读取失败：" + err.message;
  }
}

/* ---------- 动作：清理离线 / 体检 ---------- */

async function pruneOffline() {
  const yes = await confirmAsync(
    "清理离线代理记录？",
    "frp 不支持强制下线在线代理，这里只清理 dashboard 统计里的历史记录。",
    { danger: true },
  );
  if (!yes) return;
  setBusy(true, "清理中…");
  try {
    const data = await api("/api/actions/prune", { method: "POST", body: {} });
    const count = data.count ?? 0;
    toast(count ? `已清理 ${count} 条离线代理记录（清理前 ${data.before} 条）` : "没有可清理的离线代理记录", "ok");
  } catch (err) { toast("清理失败：\n" + err.message, "err"); }
  finally { setBusy(false); }
  refreshLists();
}

async function runDoctor() {
  if (state.doctorRunning) return;
  state.doctorRunning = true;
  setBusy(true, "体检中…");
  $("doctor-summary").textContent = "";
  try {
    const data = await api("/api/doctor");
    const counts = data.counts || {};
    $("doctor-summary").textContent = data.ok
      ? `通过（WARN ${counts.warn} / INFO ${counts.info}）`
      : `发现 ${counts.error} 个 ERROR（WARN ${counts.warn} / INFO ${counts.info}）`;
    const box = $("doctor-findings");
    box.replaceChildren();
    if (!data.findings.length) {
      box.appendChild(el("div", { class: "muted", text: "没有发现项" }));
      return;
    }
    for (const f of data.findings) {
      const cls = f.severity === "ERROR" ? "err" : (f.severity === "WARN" ? "warn" : "");
      const card = el("div", { class: "banner " + cls + " mb-8" });
      card.appendChild(el("div", { text: `[${f.severity}] ${f.check}：${f.message}` }));
      if (f.hint) card.appendChild(el("div", { class: "muted", text: "↳ " + f.hint }));
      box.appendChild(card);
    }
  } catch (err) {
    toast("体检失败：\n" + err.message, "err");
  } finally {
    state.doctorRunning = false;
    setBusy(false);
  }
}

/** 绑定仪表盘控件（按需导出诊断报告）。 */
export function initDashboard() {
  $("doctor-btn").addEventListener("click", runDoctor);
  $("prune-btn").addEventListener("click", pruneOffline);
  $("clients-filter").addEventListener("input", () => renderClients(state.clientsCache, state.clientsTotal));
  $("proxies-filter").addEventListener("input", () => renderProxies(state.proxiesCache));
  $("export-btn").addEventListener("click", async () => {
    try {
      const { blob, filename } = await apiDownload("/api/diagnostics");
      saveBlob(blob, filename);
      toast("诊断报告已下载（配置已打码；日志请自行检查敏感内容）", "ok");
    } catch (err) {
      toast("导出失败：\n" + err.message, "err");
    }
  });
}
