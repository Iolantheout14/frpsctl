/** 服务视图（v0.3.4 F1）：frps / Web 管理台 / 服务端插件的托管状态与插件启停。
 *
 *  插件是登录单点（fail-closed，挂掉全员登不上）——本视图是它的状态与
 *  启停入口。Web 管理台自身不做启停（会断开当前会话），只给 CLI 提示。
 */

import { api } from "../api.js";
import { $, el, kvRow } from "../ui/dom.js";
import { toast } from "../ui/toast.js";
import { confirmAsync } from "../ui/modal.js";
import { humanDuration } from "../lib/format.js";
import { icon } from "../ui/icons.js";
import { registerView } from "./router.js";

const LABELS = { frps: "frps 实例", web: "Web 管理台", plugin: "服务端插件" };
const ACTIONS = {
  frps: null,   // frps 的启停走仪表盘/CLI（所有权语义复杂，不在本页重复）
  web: null,    // 启停 Web 自身会断开当前会话——只读 + CLI 提示
  plugin: ["start", "stop", "restart"],
};

/** 拉取并渲染三个服务的托管状态。 */
export async function loadServices() {
  const box = $("services-grid");
  box.replaceChildren(el("div", { class: "muted", text: "加载中…" }));
  try {
    const data = await api("/api/services");
    renderServices(data);
  } catch (err) {
    box.replaceChildren(el("div", { class: "muted", text: "服务状态读取失败：" + err.message }));
  }
}

function ownerTag(info) {
  const owner = info.owner || "none";
  const cls = info.active ? "tag online" : "tag";
  return el("span", { class: cls, text: `${owner} · ${info.active ? "运行中" : "未运行"}` });
}

function serviceCard(name, info) {
  const card = el("section", { class: "card service-card" });
  const head = el("div", { class: "service-head" }, [
    icon(name === "frps" ? "activity" : (name === "web" ? "chip" : "shield"), 18),
    el("h2", { text: LABELS[name] }),
    ownerTag(info),
  ]);
  card.appendChild(head);
  const pairs = [
    ["状态", info.state || (info.active ? "运行中" : "未运行")],
    ["pid", info.pid === null || info.pid === undefined ? "-" : String(info.pid)],
    ["运行时长", humanDuration(info.uptime_seconds)],
  ];
  if (name === "frps") {
    pairs.push(["监听", info.listen ? `${info.listen.addr}:${info.listen.port}` : "-"]);
    pairs.push(["systemd", info.systemd_unit || "-"]);
  }
  if (name !== "frps") {
    pairs.push(["监听", info.bind || "-"]);
    pairs.push(["日志", info.log || "-"]);
  }
  if (info.detail) pairs.push(["说明", info.detail]);
  card.appendChild(el("div", { class: "kv" }, kvRow(pairs)));

  const actions = ACTIONS[name];
  if (actions) {
    const row = el("div", { class: "row mt-12" });
    for (const action of actions) {
      const label = { start: "启动", stop: "停止", restart: "重启" }[action];
      const disabled = (action === "start" && info.active)
        || ((action === "stop" || action === "restart") && !info.active);
      const btn = el("button", {
        class: action === "stop" ? "danger" : (action === "start" ? "primary" : ""),
        text: label,
        disabled,
        onclick: () => pluginAction(action),
      });
      row.appendChild(btn);
    }
    card.appendChild(row);
  } else if (name === "web") {
    card.appendChild(el("p", { class: "muted small mb-0", text: info.active
      ? "本页面由该服务提供：停止/重启请用 CLI（frpsctl web restart）——从这里操作会断开当前会话。"
      : "启动请用 CLI：frpsctl web start（direct）或 frpsctl web service start（systemd）。" }));
  } else {
    card.appendChild(el("p", { class: "muted small mb-0", text: "启停请用仪表盘按钮或 CLI（frpsctl start|stop|restart）。" }));
  }
  return card;
}

function renderServices(data) {
  const services = data.services || {};
  const box = $("services-grid");
  box.replaceChildren();
  for (const name of ["frps", "web", "plugin"]) {
    const info = services[name] || { owner: "none", active: false };
    box.appendChild(serviceCard(name, info));
  }
}

async function pluginAction(action) {
  const label = { start: "启动", stop: "停止", restart: "重启" }[action];
  const yes = await confirmAsync(`确认${label}插件服务？`, "插件是登录单点（fail-closed）：停止后所有客户端都无法登录。", { danger: action !== "start" });
  if (!yes) return;
  try {
    const data = await api(`/api/actions/plugin-${action}`, { method: "POST", body: {} });
    toast(`插件服务已${label}` + (data.pid ? `（pid ${data.pid}）` : ""), "ok");
  } catch (err) {
    toast(`${label}失败：\n` + err.message, "err");
  }
  loadServices();
}

/** 注册服务视图（每次进入都刷新状态）。 */
export function initServices() {
  registerView("services", () => loadServices());
  $("services-refresh").addEventListener("click", () => loadServices());
}
