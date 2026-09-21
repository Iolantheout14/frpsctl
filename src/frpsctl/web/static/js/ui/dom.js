/** DOM 构造与通用渲染工具（零业务状态）。 */

/** id 查询的短别名（全项目统一入口）。 */
export const $ = (id) => document.getElementById(id);

/** 元素工厂：attrs 支持 class / text / on* / 其余 setAttribute。 */
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) {
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

/** SVG 元素工厂（命名空间正确，属性一律 setAttribute）。 */
export function svgEl(tag, attrs, children = []) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  for (const c of children) node.appendChild(c);
  return node;
}

/** 骨架屏（加载占位）。 */
export function skeletonRows(rows = 3, cols = 5) {
  const frag = document.createDocumentFragment();
  for (let i = 0; i < rows; i++) {
    const row = el("div", { class: "row" });
    for (let c = 0; c < cols; c++) row.appendChild(el("div", { class: "skeleton flex-1" }));
    frag.appendChild(row);
  }
  return frag;
}

/** 空态（内嵌 SVG 图标 + 文本）。 */
export function emptyState(text) {
  const box = el("div", { class: "empty" });
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("width", "40"); svg.setAttribute("height", "40"); svg.setAttribute("viewBox", "0 0 24 24");
  const path = document.createElementNS(NS, "path");
  path.setAttribute("d", "M4 7h16v12H4z M4 7l3-3h10l3 3");
  path.setAttribute("fill", "none"); path.setAttribute("stroke", "currentColor"); path.setAttribute("stroke-width", "1.4");
  svg.appendChild(path);
  box.append(svg, el("div", { text }));
  return box;
}

/** 键值对 → kv 网格的子节点数组（调用方 replaceChildren(...kvRow(pairs))）。 */
export function kvRow(pairs) {
  const rows = [];
  for (const [k, v] of pairs) rows.push(el("div", { text: k }), el("div", { text: v }));
  return rows;
}

/** 数字滚动（首次进入的微动效；仅更新文本节点）。 */
export function animateNumber(node, target) {
  const value = Number(target);
  if (!Number.isFinite(value) || value === 0) { node.textContent = String(target); return; }
  const from = 0;
  const start = performance.now();
  const dur = 500;
  function frame(now) {
    const k = Math.min(1, (now - start) / dur);
    const eased = 1 - Math.pow(1 - k, 3);
    node.textContent = String(Math.round(from + (value - from) * eased));
    if (k < 1) requestAnimationFrame(frame);
    else node.textContent = String(target);
  }
  requestAnimationFrame(frame);
}

/** 表格骨架：headers 里的字符串转 th（数字列右对齐），节点原样使用。 */
export function tableOf(headers, rows) {
  if (!rows.length) return el("div", { class: "muted", text: "（空）" });
  const head = headers.map((h) => {
    if (typeof h !== "string") return h;
    return el("th", { text: h, class: h === "端口" || h === "连接" || h === "今日(入/出)" ? "num" : "" });
  });
  const table = el("table", {}, [
    el("thead", {}, [el("tr", {}, head)]),
    el("tbody", {}, rows),
  ]);
  return table;
}

/** 可排序表头（点击在 asc/desc 间切换并触发 rerender；sortState 是 {key, dir}）。 */
export function sortHeader(label, key, sortState, rerender) {
  const arrow = sortState.key === key ? (sortState.dir === "asc" ? " ↑" : " ↓") : "";
  const th = el("th", { text: label + arrow, title: "点击排序" });
  th.addEventListener("click", () => {
    if (sortState.key === key) sortState.dir = sortState.dir === "asc" ? "desc" : "asc";
    else { sortState.key = key; sortState.dir = "asc"; }
    rerender();
  });
  return th;
}

/** diff 文本 → 着色片段（.diffline.add/.del/.hunk）写入 pre。 */
export function renderDiff(pre, text) {
  const frag = document.createDocumentFragment();
  const body = text && text.length ? text : "(无文本差异)";
  for (const line of body.replace(/\n$/, "").split("\n")) {
    const cls = line.startsWith("+++") || line.startsWith("---") ? "hunk"
      : line.startsWith("+") ? "add"
      : line.startsWith("-") ? "del"
      : line.startsWith("@@") ? "hunk" : "";
    frag.appendChild(el("div", { class: "diffline " + cls, text: line }));
  }
  pre.replaceChildren(frag);
}

/** 轻量相对时间（审计/任务列表用）。 */
export function relativeTime(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return "-";
  const delta = Math.max(0, Date.now() / 1000 - Number(seconds));
  if (delta < 60) return `${Math.floor(delta)} 秒前`;
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  return `${Math.floor(delta / 86400)} 天前`;
}
