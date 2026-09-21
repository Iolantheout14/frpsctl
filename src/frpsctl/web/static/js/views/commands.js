/** 命令参考视图（v0.3.5）：把 CLI 的全部命令与参数搬进界面。
 *
 *  数据来自生成物 `data/commands.js`（`cli/introspect.py` 从 Typer app 反射，
 *  与命令面契约快照、shell 补全同源）。CI 跑 `introspect --check` 断言生成物
 *  无漂移——所以这里展示的**永远**是当前 CLI 的真实命令面，不会漏、不会旧。
 */

import { $, el, kvRow } from "../ui/dom.js";
import { COMMAND_SURFACE } from "../data/commands.js";
import { copyText } from "../ui/toast.js";
import { icon } from "../ui/icons.js";
import { badgesFor, commandHaystack, commandText, defaultLabel, paramLabel } from "../lib/command-text.js";
import { registerView, switchView } from "./router.js";

const GROUP_ORDER = ["", "config", "service", "plugin", "web"];

const GROUP_LABELS = {
  "": "顶层命令",
  config: "配置（config）",
  service: "frps systemd（service）",
  plugin: "服务端插件（plugin）",
  web: "Web 管理台（web）",
};

const VIEW_LABELS = {
  dash: "仪表盘",
  config: "配置",
  audit: "审计",
  services: "服务",
  versions: "版本",
  commands: "命令",
};

let built = false;

function badgeClass(label) {
  if (label === "只读") return "tag tag-ro";
  if (label === "变更") return "tag tag-mut";
  if (label === "需 root") return "tag tag-root";
  return "tag tag-sys";
}

function makeItem(entry) {
  const badges = el(
    "div",
    { class: "cmd-badges" },
    badgesFor(entry).map((label) => el("span", { class: badgeClass(label), text: label })),
  );
  const copy = el(
    "button",
    {
      class: "iconbtn",
      title: "复制这条命令",
      onclick: (event) => {
        event.stopPropagation();
        copyText(commandText(entry), " 命令");
      },
    },
    [icon("terminal", 14)],
  );
  const buttons = [copy];
  if (entry.web_view) {
    buttons.push(
      el(
        "button",
        {
          class: "iconbtn",
          title: `在界面中查看（${VIEW_LABELS[entry.web_view] || entry.web_view}）`,
          onclick: (event) => {
            event.stopPropagation();
            switchView(entry.web_view);
          },
        },
        [icon("activity", 14)],
      ),
    );
  }

  // 头部是可展开控件：键盘必须可达（Enter/Space），并维护 aria-expanded
  const head = el(
    "div",
    {
      class: "cmd-item-head",
      role: "button",
      tabindex: "0",
      "aria-expanded": "false",
      onclick: () => node.classList.toggle("open"),
      onkeydown: (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          node.classList.toggle("open");
        }
      },
    },
    [el("code", { class: "cmd-code", text: commandText(entry) }), badges, el("div", { class: "cmd-buttons" }, buttons)],
  );

  const params = entry.params || [];
  const body = el("div", { class: "cmd-item-body" }, [
    params.length
      ? el("div", { class: "tablewrap" }, [
          el("table", {}, [
            el("thead", {}, [
              el("tr", {}, [
                el("th", { text: "选项" }),
                el("th", { text: "说明" }),
                el("th", { text: "默认" }),
                el("th", { text: "要求" }),
              ]),
            ]),
            el(
              "tbody",
              {},
              params.map((param) =>
                el("tr", {}, [
                  el("td", {}, [el("code", { text: paramLabel(param) })]),
                  el("td", { class: "muted", text: param.help || "" }),
                  el("td", { class: "mono", text: defaultLabel(param) }),
                  el("td", { class: "muted", text: param.required ? "必填" : "" }),
                ]),
              ),
            ),
          ]),
        ])
      : el("div", { class: "muted", text: "（无参数）" }),
  ]);

  const node = el("div", { class: "cmd-item" }, [head, body]);
  node.dataset.search = commandHaystack(entry);
  // 展开状态同步到 aria（键盘用户与屏幕阅读器都要知道当前是否展开）
  const syncExpanded = () => head.setAttribute("aria-expanded", node.classList.contains("open") ? "true" : "false");
  head.addEventListener("click", syncExpanded);
  head.addEventListener("keydown", syncExpanded);
  return node;
}

function buildInfo() {
  $("cmd-info").replaceChildren(
    ...kvRow([
      ["frpsctl", COMMAND_SURFACE.version],
      ["命令总数", String(COMMAND_SURFACE.commands.length)],
      ["只读 / 变更", `${COMMAND_SURFACE.commands.filter((c) => c.readonly).length} / ${COMMAND_SURFACE.commands.filter((c) => !c.readonly).length}`],
      ["frps 最低版本", COMMAND_SURFACE.frps_minimum],
      ["frps 建议版本", COMMAND_SURFACE.frps_reckoned],
    ]),
  );
}

function buildTree() {
  const grouped = new Map();
  for (const entry of COMMAND_SURFACE.commands) {
    const key = entry.group || "";
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(entry);
  }
  const nodes = [];
  for (const key of GROUP_ORDER) {
    const entries = grouped.get(key) || [];
    if (!entries.length) continue;
    nodes.push(
      el("div", { class: "cmd-group" }, [
        el("div", { class: "group-title", text: `${GROUP_LABELS[key] || key} · ${entries.length} 条` }),
        ...entries.map(makeItem),
      ]),
    );
  }
  $("cmd-tree").replaceChildren(...nodes);
}

function buildTables() {
  $("cmd-exit-codes").replaceChildren(
    el("table", {}, [
      el("thead", {}, [el("tr", {}, [el("th", { class: "num", text: "码" }), el("th", { text: "枚举名" })])]),
      el(
        "tbody",
        {},
        Object.entries(COMMAND_SURFACE.exit_codes).map(([name, code]) =>
          el("tr", {}, [el("td", { class: "num", text: String(code) }), el("td", { class: "mono", text: name })]),
        ),
      ),
    ]),
  );
  $("cmd-env-vars").replaceChildren(
    el("table", {}, [
      el("thead", {}, [el("tr", {}, [el("th", { text: "变量" }), el("th", { text: "作用" })])]),
      el(
        "tbody",
        {},
        Object.entries(COMMAND_SURFACE.env_vars).map(([name, desc]) =>
          el("tr", {}, [el("td", {}, [el("code", { text: name })]), el("td", { class: "muted", text: desc })]),
        ),
      ),
    ]),
  );
}

function applyFilter() {
  const query = $("cmd-search").value.trim().toLowerCase();
  let visible = 0;
  for (const group of $("cmd-tree").querySelectorAll(".cmd-group")) {
    let groupVisible = 0;
    for (const item of group.querySelectorAll(".cmd-item")) {
      const hit = !query || (item.dataset.search || "").includes(query);
      item.classList.toggle("hidden", !hit);
      if (hit) groupVisible += 1;
    }
    group.classList.toggle("hidden", groupVisible === 0);
    visible += groupVisible;
  }
  $("cmd-count").textContent = query
    ? `匹配 ${visible} / ${COMMAND_SURFACE.commands.length} 条`
    : `共 ${COMMAND_SURFACE.commands.length} 条命令`;
}

function toggleExpand() {
  const items = [...$("cmd-tree").querySelectorAll(".cmd-item")];
  const anyClosed = items.some((node) => !node.classList.contains("open"));
  for (const node of items) {
    node.classList.toggle("open", anyClosed);
    const head = node.querySelector(".cmd-item-head");
    if (head) head.setAttribute("aria-expanded", anyClosed ? "true" : "false");
  }
  $("cmd-expand").textContent = anyClosed ? "全部折叠" : "全部展开";
}

/** 绑定视图（首展时构建一次；重复进入保留搜索与展开状态）。 */
export function initCommandsView() {
  registerView("commands", () => {
    if (!built) {
      buildInfo();
      buildTree();
      buildTables();
      built = true;
    }
    applyFilter();
  });
  $("cmd-search").addEventListener("input", applyFilter);
  $("cmd-expand").addEventListener("click", toggleExpand);
}
