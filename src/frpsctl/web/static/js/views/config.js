/** 配置编辑视图：逐字段表单 → 预览 diff → 应用（一次事务）→ 历史与回滚。
 *
 *  配置原文绝不下发浏览器（值已打码）；敏感键留空表示不改；删除键回落默认。
 *  `allowPorts` 使用结构化表格编辑器（v0.3.4 F8）。
 */

import { api } from "../api.js";
import { $, el, renderDiff, tableOf } from "../ui/dom.js";
import { state } from "../state.js";
import { toast } from "../ui/toast.js";
import { setBusy } from "../ui/busy.js";
import { confirmAsync } from "../ui/modal.js";
import { looksBalanced } from "../lib/format.js";
import { parsePortRanges, parsePortText, serializePortRanges, validatePortRangeRow } from "../lib/port-ranges.js";
import { registerView } from "./router.js";
import { refresh } from "./dashboard.js";

function entryValue(entry) {
  return entry.masked ? "" : (typeof entry.value === "object" ? JSON.stringify(entry.value) : String(entry.value ?? ""));
}

/** 重新加载配置（丢弃草稿）。 */
export async function loadConfig() {
  state.dirty.clear();
  state.deletes.clear();
  state.addedKeys = [];
  updateDirtyCount();
  $("cfg-preview-card").classList.add("hidden");
  loadHistory();
  try {
    const data = await api("/api/config");
    state.configEntries = data.entries || [];
    state.configLoaded = true;
    renderConfigForm();
  } catch (err) {
    $("config-form").replaceChildren(el("div", { class: "muted", text: "配置读取失败：" + err.message }));
  }
}

function makeField(entry, input) {
  const delBtn = el("button", {
    class: "danger row-del", text: "删除", title: "删除该键（回落 frp 默认值）",
    onclick: () => toggleDelete(entry.key),
  });
  const field = el("div", { class: "field", "data-key": entry.key }, [el("label", { text: entry.key }), input, delBtn]);
  return field;
}

/* ---------- allowPorts 结构化编辑器（F8） ---------- */

function makePortEditor(entry) {
  // 表单重建（搜索过滤/切视图）时保留未预览的修改：草稿存的是序列化文本，
  // 优先于磁盘值解析（v0.3.4 review 修复：此前重建会静默丢弃编辑器里的改动）
  const draft = state.dirty.get(entry.key);
  const diskRows = parsePortRanges(Array.isArray(entry.value) ? entry.value : []);
  const rows = typeof draft === "string" ? parsePortText(draft) : diskRows;
  // 草稿清除基准必须是"磁盘值的**同形态**序列化文本"：entryValue(entry)
  // 对数组返回 JSON 字符串，与 TOML 文本永不相等（改回原值也不消计数）
  const baseline = serializePortRanges(diskRows);
  const box = el("div", { class: "port-editor" });
  const commit = () => {
    const text = serializePortRanges(rows);
    if (text === baseline) state.dirty.delete(entry.key);
    else state.dirty.set(entry.key, text);
    const field = box.closest(".field");
    if (field) field.classList.toggle("dirty", state.dirty.has(entry.key));
    updateDirtyCount();
  };
  const rebuild = () => {
    box.replaceChildren();
    rows.forEach((row, index) => {
      const startInput = el("input", {
        class: "port-input", value: row.start, placeholder: "起始", inputmode: "numeric",
        "aria-label": "起始端口",
      });
      const endInput = el("input", {
        class: "port-input", value: row.end, placeholder: "结束", inputmode: "numeric",
        "aria-label": "结束端口",
      });
      const modeSelect = el("select", { class: "port-mode", "aria-label": "类型" }, [
        el("option", { value: "range", text: "区间" }),
        el("option", { value: "single", text: "单端口" }),
      ]);
      modeSelect.value = row.mode;
      const applyValidity = () => {
        const problem = validatePortRangeRow(row);
        startInput.classList.toggle("invalid", Boolean(problem));
        endInput.classList.toggle("invalid", row.mode === "range" && Boolean(problem));
        box.title = problem;
      };
      startInput.addEventListener("input", () => { row.start = startInput.value; applyValidity(); commit(); });
      endInput.addEventListener("input", () => { row.end = endInput.value; applyValidity(); commit(); });
      modeSelect.addEventListener("change", () => { row.mode = modeSelect.value; rebuild(); commit(); });
      const remove = el("button", {
        class: "iconbtn", text: "移除", title: "移除该行",
        onclick: () => { rows.splice(index, 1); rebuild(); commit(); },
      });
      const line = el("div", { class: "port-row" }, [modeSelect, startInput, endInput, remove]);
      if (row.mode === "single") endInput.disabled = true;
      applyValidity();
      box.appendChild(line);
    });
    box.appendChild(el("div", { class: "port-actions" }, [
      el("button", { class: "iconbtn", text: "+ 单端口", onclick: () => { rows.push({ mode: "single", start: "", end: "" }); rebuild(); commit(); } }),
      el("button", { class: "iconbtn", text: "+ 区间", onclick: () => { rows.push({ mode: "range", start: "", end: "" }); rebuild(); commit(); } }),
    ]));
  };
  rebuild();
  return box;
}

/* ---------- 表单渲染 ---------- */

function looksBalancedInput(input) {
  const ok = looksBalanced(input.value);
  input.classList.toggle("invalid", !ok);
  input.title = ok ? "" : "括号或引号不配对（预览时将由服务端做权威校验）";
}

function bindDirtyInput(input, entry) {
  input.addEventListener("input", () => {
    const raw = input.value;
    if (raw === entryValue(entry)) state.dirty.delete(entry.key);
    else state.dirty.set(entry.key, raw);
    input.closest(".field").classList.toggle("dirty", state.dirty.has(entry.key));
    looksBalancedInput(input);
    updateDirtyCount();
  });
  input.addEventListener("blur", () => looksBalancedInput(input));
}

/** 按当前配置与草稿渲染表单（搜索过滤只影响显示，草稿不丢）。 */
export function renderConfigForm() {
  const box = $("config-form");
  box.replaceChildren();
  const query = $("cfg-search") ? $("cfg-search").value.trim().toLowerCase() : "";
  const visible = (key) =>
    !query || key.toLowerCase().includes(query) || state.dirty.has(key) || state.deletes.has(key);
  let currentGroup = null;
  for (const entry of state.configEntries) {
    if (!visible(entry.key)) continue;
    const parts = entry.key.split(".");
    const group = parts.length > 1 ? parts.slice(0, -1).join(".") : "(顶层)";
    if (group !== currentGroup) {
      currentGroup = group;
      box.appendChild(el("div", { class: "group-title", text: group === "(顶层)" ? "顶层" : `[${group}]` }));
    }
    let control;
    if (entry.key === "allowPorts" && !entry.masked && Array.isArray(entry.value)) {
      control = makePortEditor(entry);
    } else {
      // 重建时保留未预览的修改（搜索过滤会触发重建；否则输入框会"回退"到磁盘值）
      const current = state.dirty.has(entry.key) ? state.dirty.get(entry.key) : entryValue(entry);
      const input = el("input", {
        "data-key": entry.key,
        value: current,
        placeholder: entry.masked ? `${entry.value}（已设置；留空不改）` : "",
      });
      bindDirtyInput(input, entry);
      looksBalancedInput(input);
      control = input;
    }
    const field = makeField(entry, control);
    field.classList.toggle("dirty", state.dirty.has(entry.key));
    if (state.deletes.has(entry.key)) markDeleted(field, control, entry.key, true);
    box.appendChild(field);
  }
  // 新增的键（不在配置里的）
  const addedVisible = state.addedKeys.filter(visible);
  if (addedVisible.length) {
    box.appendChild(el("div", { class: "group-title", text: "新增" }));
    for (const key of addedVisible) {
      const input = el("input", { value: state.dirty.get(key) ?? "", "data-key": key });
      input.addEventListener("input", () => { state.dirty.set(key, input.value); looksBalancedInput(input); updateDirtyCount(); });
      input.addEventListener("blur", () => looksBalancedInput(input));
      looksBalancedInput(input);
      box.appendChild(el("div", { class: "field", "data-key": key }, [
        el("label", { text: key }),
        input,
        el("button", { class: "danger row-del", text: "移除", onclick: () => { state.addedKeys = state.addedKeys.filter((k) => k !== key); state.dirty.delete(key); renderConfigForm(); updateDirtyCount(); } }),
      ]));
    }
  }
}

function markDeleted(field, control, key, on) {
  field.classList.toggle("deleted", on);
  const input = control.tagName === "INPUT" ? control : field.querySelector("input");
  if (input) input.disabled = on;
  const editor = field.querySelector(".port-editor");
  if (editor) editor.classList.toggle("disabled", on);
  const btn = field.querySelector(".row-del");
  if (btn) { btn.textContent = on ? "恢复" : "删除"; btn.classList.toggle("primary", on); }
}

function toggleDelete(key) {
  const field = document.querySelector(`.field[data-key="${CSS.escape(key)}"]`);
  const entry = state.configEntries.find((e) => e.key === key);
  const isEditor = Boolean(field && field.querySelector(".port-editor"));
  if (isEditor) {
    // 结构化编辑器：删除/恢复都走整段重建——把 JSON 文本塞进 input 会把
    // 行编辑器撑坏（v0.3.4 review 修复）
    if (state.deletes.has(key)) state.deletes.delete(key);
    else {
      state.deletes.add(key);
      state.dirty.delete(key);
    }
    renderConfigForm();
    updateDirtyCount();
    return;
  }
  if (state.deletes.has(key)) {
    state.deletes.delete(key);
    if (field && entry) {
      // 恢复：把控件还原成"当前值"（否则它停在删除前被清空的状态）
      const input = field.querySelector("input");
      if (input) input.value = entryValue(entry);
      markDeleted(field, input, key, false);
    }
  } else {
    state.deletes.add(key);
    state.dirty.delete(key);
    if (field) {
      const input = field.querySelector("input");
      if (input) input.value = "";
      markDeleted(field, input, key, true);
    }
  }
  updateDirtyCount();
}

function updateDirtyCount() {
  const total = state.dirty.size + state.deletes.size;
  const node = $("cfg-count");
  node.textContent = total ? `${total} 项待应用（改 ${state.dirty.size} / 删 ${state.deletes.size}）` : "无修改";
  node.classList.toggle("clickable", total > 0);
}

function addKey() {
  const key = $("add-key").value.trim();
  const value = $("add-value").value;
  if (!key) { toast("请填写键名", "err"); return; }
  if (value === "") { toast('请填写值（清空字符串请写 ""）', "err"); return; }
  if (state.configEntries.some((e) => e.key === key)) { toast(`${key} 已存在，请直接修改对应字段`, "err"); return; }
  if (state.addedKeys.includes(key)) { toast(`${key} 已在新增列表里`, "err"); return; }
  state.addedKeys.push(key);
  state.dirty.set(key, value);
  $("add-key").value = "";
  $("add-value").value = "";
  renderConfigForm();
  updateDirtyCount();
  $("config-form").lastElementChild?.scrollIntoView({ behavior: "smooth", block: "center" });
}

/* ---------- 预览 / 应用 ---------- */

async function previewChanges() {
  const changes = [...state.dirty.entries()].filter(([key]) => !state.deletes.has(key));
  const unsets = [...state.deletes];
  if (!changes.length && !unsets.length) { toast("没有修改"); return; }
  setBusy(true, "预览中…");
  try {
    const data = await api("/api/config/preview", { method: "POST", body: { changes, unsets } });
    if (data.noop) { toast("这些值与当前配置相同，无需变更"); return; }
    renderDiff($("cfg-diff"), data.diff);
    $("cfg-preview-card").classList.remove("hidden");
    $("cfg-preview-card").dataset.previewId = data.preview_id;
  } catch (err) { toast("预览失败：\n" + err.message, "err"); }
  finally { setBusy(false); }
}

async function applyPreview() {
  const id = $("cfg-preview-card").dataset.previewId;
  if (!id) return;
  const yes = await confirmAsync(
    "应用配置并重启服务？",
    "会重启实例；健康检查失败会自动回滚并如实告知。",
    { danger: true },
  );
  if (!yes) return;
  setBusy(true, "应用中…");
  try {
    const data = await api("/api/config/apply", { method: "POST", body: { preview_id: id } });
    toast(data.noop ? "无需变更" : "配置已应用并重启" + (data.plugin_warning ? "\n⚠ " + data.plugin_warning : ""), "ok");
    $("cfg-preview-card").classList.add("hidden");
    state.configLoaded = true;
    loadConfig();
    refresh();
  } catch (err) { toast("应用失败：\n" + err.message, "err"); }
  finally { setBusy(false); }
}

/* ---------- 历史与回滚 ---------- */

async function loadHistory() {
  try {
    const data = await api("/api/config/history");
    const rows = [];
    for (const entry of data.entries || []) {
      rows.push(el("tr", {}, [
        el("td", { text: entry.name }),
        el("td", { text: entry.action || "-" }),
        el("td", { text: entry.at || "-" }),
        el("td", { class: "num" }, [
          el("button", { text: "查看差异", onclick: () => toggleHistoryDiff(entry) }),
          " ",
          el("button", { class: "danger", text: "回滚到此份", onclick: () => rollbackTo(entry.steps, entry.name) }),
        ]),
      ]));
    }
    $("history-table").replaceChildren(tableOf(["快照", "操作", "时间", ""], rows));
  } catch (err) {
    $("history-table").textContent = "历史读取失败：" + err.message;
  }
}

async function toggleHistoryDiff(entry) {
  const table = $("history-table").querySelector("table");
  if (!table) return;
  const existing = table.querySelector("tr.diffrow");
  if (existing) {
    const wasSame = existing.dataset.steps === String(entry.steps);
    existing.remove();
    if (wasSame) return;
  }
  const pre = el("pre", { class: "diffbox", text: "读取中…" });
  const row = el("tr", { class: "detail diffrow", "data-steps": String(entry.steps) }, [
    el("td", { colspan: "4" }, [pre]),
  ]);
  table.querySelector("tbody").appendChild(row);
  try {
    const data = await api(`/api/config/history/${entry.steps}/diff`);
    renderDiff(pre, data.diff);
  } catch (err) {
    pre.textContent = "差异读取失败：" + err.message;
  }
}

async function rollbackTo(steps, name) {
  const yes = await confirmAsync(
    `回滚到快照 ${name}？`,
    `${steps} 步前的配置会替换当前配置并重启服务。建议先用"查看差异"确认会改什么。`,
    { danger: true },
  );
  if (!yes) return;
  setBusy(true, "回滚中…");
  try {
    await api("/api/actions/rollback", { method: "POST", body: { steps } });
    toast("已回滚并重启", "ok");
    state.configLoaded = true;
    loadConfig();
    refresh();
  } catch (err) { toast("回滚失败：\n" + err.message, "err"); }
  finally { setBusy(false); }
}

/* ---------- 初始化 ---------- */

/** 绑定配置页控件（首展钩子：已加载过不重载，草稿不丢）。 */
export function initConfig() {
  registerView("config", () => { if (!state.configLoaded) loadConfig(); });
  $("cfg-search").addEventListener("input", () => renderConfigForm());
  $("cfg-count").addEventListener("click", () => {
    const target = document.querySelector(".field.dirty, .field.deleted");
    if (target) target.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  $("add-btn").addEventListener("click", addKey);
  $("add-key").addEventListener("keydown", (e) => { if (e.key === "Enter") $("add-value").focus(); });
  $("add-value").addEventListener("keydown", (e) => { if (e.key === "Enter") addKey(); });
  $("cfg-preview").addEventListener("click", previewChanges);
  $("cfg-apply").addEventListener("click", applyPreview);
  $("cfg-cancel").addEventListener("click", () => $("cfg-preview-card").classList.add("hidden"));
  $("cfg-reset").addEventListener("click", async () => {
    const total = state.dirty.size + state.deletes.size;
    if (total) {
      const yes = await confirmAsync(
        "丢弃未保存的修改？",
        `将丢弃 ${total} 项未保存的修改并重新加载配置。`,
        { danger: true },
      );
      if (!yes) return;
    }
    loadConfig();
  });
}
