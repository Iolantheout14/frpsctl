/** 服务视图（v0.3.4 F1；v0.3.5 F4）：三服务托管状态、插件启停与 Web 自重启。
 *
 *  插件是登录单点（fail-closed，挂掉全员登不上）——本视图是它的状态与启停入口。
 *  Web 管理台自身**只在 systemd 托管时**可自重启（后端"先应答后动作"，前端
 *  轮询免认证端点等待恢复）；direct 后台模式由后端拒绝并给 CLI 指引。
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
  frps: null,          // frps 的启停走仪表盘/CLI（所有权语义复杂，不在本页重复）
  web: ["restart"],    // v0.3.5 F4：systemd 托管时可自重启（direct 由后端拒绝）
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
        onclick: () => serviceAction(name, action),
      });
      // ⚠️ 布尔属性必须用**属性赋值**：把 disabled 通过 el() 的 attrs 传下去会
      // setAttribute("disabled", "false")——属性存在即禁用，按钮永远点不动。
      // 这是 v0.3.4 的真实缺陷（插件启停按钮不可用，2026-09-21 发现并修复），
      // 守卫见 tests/test_web_frontend.py::test_no_boolean_attr_shorthand_in_el。
      if (disabled) btn.disabled = true;
      row.appendChild(btn);
    }
    card.appendChild(row);
  }
  if (name === "web") {
    card.appendChild(el("p", { class: "muted small mb-0", text: info.active
      ? "重启仅 systemd 托管可用（先应答后动作，页面会在恢复前轮询等待）；direct 后台请用 CLI：frpsctl web restart。"
      : "启动请用 CLI：frpsctl web start（direct）或 frpsctl web service start（systemd）。" }));
  } else if (!actions) {
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

/** 服务动作分派（web 走自重启；插件走 plugin-* 动作）。 */
async function serviceAction(name, action) {
  if (name === "web") {
    await restartWeb();
    return;
  }
  await pluginAction(action);
}

/** 自重启 Web 管理台（v0.3.5 F4）：轮询免认证端点直到恢复。 */
async function restartWeb() {
  const yes = await confirmAsync(
    "重启 Web 管理台？",
    "当前页面会短暂断开；重启完成后需要重新登录（direct 后台模式请改用 CLI）。",
    { danger: true },
  );
  if (!yes) return;
  try {
    await api("/api/actions/web-restart", { method: "POST", body: {} });
  } catch (err) {
    toast("重启失败：\n" + err.message, "err");
    return;
  }
  // 覆盖层（而不是只弹 toast）：重启会断开当前页面的所有请求，用户需要一个
  // 明确的"正在进行中"状态，并在恢复后自动回到界面（会话已随重启失效，会回到登录页）。
  $("restart-overlay").classList.remove("hidden");
  $("restart-note").textContent = "等待服务恢复，恢复后将自动重新加载";
  // 后端是"先应答后动作"，POST 200 不代表进程已下线：必须**先观察到至少一次
  // 连接失败**（进程确实停了）再接受成功探测，否则会在旧进程上立刻 reload，
  // 新页面撞上下线窗口（v0.3.5 review）。`attempt >= 3` 兜底"极快重启"——
  // 3 秒足够覆盖下线窗口，避免永远看不到失败而等到超时。
  let sawFailure = false;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    $("restart-note").textContent = `等待服务恢复…（已等待 ${attempt + 1} 秒 / 最多 30 秒）`;
    try {
      const resp = await fetch("/api/login-info", { cache: "no-store" });
      if (resp.ok && (sawFailure || attempt >= 3)) {
        $("restart-note").textContent = "管理台已恢复，正在重新加载…";
        location.reload();
        return;
      }
    } catch (e) {
      sawFailure = true;  // 观察到下线：之后的成功才是真正的恢复
    }
  }
  $("restart-overlay").classList.add("hidden");
  toast("等待管理台恢复超时，请手动刷新页面", "err");
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
