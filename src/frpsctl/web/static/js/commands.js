/** 命令面板（Ctrl+K）：视图切换 / 实例动作 / 界面命令的统一入口。 */

import { $, el } from "./ui/dom.js";
import { state } from "./state.js";
import { switchView } from "./views/router.js";
import { doAction } from "./views/actions.js";
import { refresh } from "./views/dashboard.js";
import { applyTheme, isLight } from "./theme.js";
import { copyText } from "./ui/toast.js";
import { COMMAND_SURFACE } from "./data/commands.js";
import { commandText } from "./lib/command-text.js";

let filtered = [];
let cursor = 0;

function commandList() {
  return [
    { label: "仪表盘", hint: "视图", run: () => switchView("dash") },
    { label: "配置编辑", hint: "视图", run: () => switchView("config") },
    { label: "审计", hint: "视图", run: () => switchView("audit") },
    { label: "服务", hint: "视图", run: () => switchView("services") },
    { label: "版本管理", hint: "视图", run: () => switchView("versions") },
    { label: "命令参考", hint: "视图", run: () => switchView("commands") },
    { label: "刷新数据", hint: "动作", run: () => refresh() },
    { label: "启动 frps", hint: "动作", run: () => doAction("start") },
    { label: "重启 frps", hint: "动作", run: () => doAction("restart") },
    { label: "停止 frps", hint: "动作", run: () => doAction("stop") },
    { label: "切换明暗主题", hint: "界面", run: () => applyTheme(!isLight()) },
    { label: "复制实例名", hint: "界面", run: () => copyText((state.lastStatus && state.lastStatus.instance) || "", "实例名") },
    // CLI 命令面（v0.3.5）：数据由代码派生（与 shell 补全同源），Enter = 复制命令。
    ...COMMAND_SURFACE.commands.map((entry) => ({
      label: `frpsctl ${entry.path}`,
      hint: entry.readonly ? "命令 · 只读" : "命令 · 变更",
      run: () => {
        switchView("commands");
        copyText(commandText(entry), " 命令");
      },
    })),
  ];
}

/** 面板是否可见（快捷键模块用它让出按键）。 */
export function paletteVisible() {
  return !$("cmdk").classList.contains("hidden");
}

function renderList() {
  const list = $("cmdk-list");
  list.replaceChildren();
  if (!filtered.length) {
    list.appendChild(el("li", { class: "cmdk-empty muted", text: "没有匹配的命令" }));
    return;
  }
  filtered.forEach((item, index) => {
    const li = el("li", {
      class: "cmdk-item" + (index === cursor ? " active" : ""),
      onclick: () => execute(index),
      onmousemove: () => { if (cursor !== index) { cursor = index; renderList(); } },
    }, [
      el("span", { class: "cmdk-label", text: item.label }),
      el("span", { class: "cmdk-hint muted", text: item.hint }),
    ]);
    list.appendChild(li);
  });
}

function execute(index) {
  const item = filtered[index];
  closeCommandPalette();
  if (item) item.run();
}

function applyFilter() {
  const query = $("cmdk-input").value.trim().toLowerCase();
  const all = commandList();
  filtered = !query ? all : all.filter((item) => (item.label + item.hint).toLowerCase().includes(query));
  cursor = 0;
  renderList();
}

/** 打开命令面板。 */
export function openCommandPalette() {
  $("cmdk").classList.remove("hidden");
  $("cmdk-input").value = "";
  filtered = commandList();
  cursor = 0;
  renderList();
  $("cmdk-input").focus();
}

/** 关闭命令面板。 */
export function closeCommandPalette() {
  $("cmdk").classList.add("hidden");
}

/** 绑定面板控件（输入过滤 / 键盘导航 / 遮罩关闭）。 */
export function initCommands() {
  $("cmdk-backdrop").addEventListener("click", closeCommandPalette);
  $("cmdk-input").addEventListener("input", applyFilter);
  $("cmdk-input").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); closeCommandPalette(); return; }
    if (e.key === "ArrowDown") { e.preventDefault(); cursor = Math.min(cursor + 1, filtered.length - 1); renderList(); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); cursor = Math.max(cursor - 1, 0); renderList(); return; }
    if (e.key === "Enter") { e.preventDefault(); execute(cursor); }
  });
}
