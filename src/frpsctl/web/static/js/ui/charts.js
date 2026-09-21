/** 图表渲染（7 天柱状 / 实时速率曲线 / 代理类型环图）。
 *
 *  颜色一律走 CSS 变量（主题切换即时生效）；提示框用 SVG 属性定位
 *  （禁止内联 style——CSP 无 unsafe-inline）。
 */

import { api } from "../api.js";
import { state, SAMPLES_KEY } from "../state.js";
import { $, el, emptyState, svgEl } from "./dom.js";
import { humanBytes } from "../lib/format.js";
import { smoothPath, speedSeries, trimSamples, donutSlices } from "../lib/chart-math.js";

/** 定义入/出两组纵向渐变（stop 类由 CSS 变量着色）。 */
function chartGradients(defs, idIn, idOut) {
  defs.append(
    svgEl("linearGradient", { id: idIn, x1: "0", y1: "0", x2: "0", y2: "1" }, [
      svgEl("stop", { offset: "0", class: "grad-in-stop", "stop-opacity": "0.9" }),
      svgEl("stop", { offset: "1", class: "grad-in-stop", "stop-opacity": "0.22" }),
    ]),
    svgEl("linearGradient", { id: idOut, x1: "0", y1: "0", x2: "0", y2: "1" }, [
      svgEl("stop", { offset: "0", class: "grad-out-stop", "stop-opacity": "0.9" }),
      svgEl("stop", { offset: "1", class: "grad-out-stop", "stop-opacity": "0.22" }),
    ]),
  );
}

/** SVG 内浮动提示：返回 show(x, y, 行1, 行2) 函数。 */
function hoverTip(svg, W, H) {
  const tip = svgEl("g", { class: "tip", visibility: "hidden" });
  const rect = svgEl("rect", { class: "tip-box", x: 0, y: 0, width: 156, height: 40, rx: 6 });
  const line1 = svgEl("text", { class: "tip-text", x: 8, y: 16 });
  const line2 = svgEl("text", { class: "tip-text", x: 8, y: 31 });
  tip.append(rect, line1, line2);
  svg.appendChild(tip);
  return (x, y, a, b) => {
    const clampedX = Math.min(Math.max(x + 12, 4), W - 160);
    const clampedY = Math.min(Math.max(y - 44, 4), H - 46);
    rect.setAttribute("x", clampedX);
    rect.setAttribute("y", clampedY);
    line1.setAttribute("x", clampedX + 8);
    line1.setAttribute("y", clampedY + 16);
    line2.setAttribute("x", clampedX + 8);
    line2.setAttribute("y", clampedY + 31);
    line1.textContent = a;
    line2.textContent = b;
    tip.setAttribute("visibility", "visible");
  };
}

/** 7 天流量柱状图（points: [{date, in, out}]）。 */
export function bars7d(points) {
  if (!points.length) return emptyState("暂无流量数据");
  const max = Math.max(1, ...points.map((v) => Math.max(v.in, v.out)));
  const W = 560, H = 170, pad = 26;
  const bw = Math.max(1.5, (W - pad * 2) / points.length / 2 - 4);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H });
  const defs = svgEl("defs", {});
  chartGradients(defs, "gradBarIn", "gradBarOut");
  svg.appendChild(defs);
  for (let g = 0; g <= 4; g++) {
    const y = pad + ((H - pad * 2) / 4) * g;
    svg.appendChild(svgEl("line", { x1: pad, y1: y, x2: W - pad, y2: y, class: "grid-line" }));
  }
  const showTip = hoverTip(svg, W, H);
  let totalIn = 0, totalOut = 0;
  points.forEach((v, i) => {
    totalIn += v.in; totalOut += v.out;
    const step = (W - pad * 2) / points.length;
    const x0 = pad + i * step;
    const hIn = (v.in / max) * (H - pad * 2);
    const hOut = (v.out / max) * (H - pad * 2);
    svg.appendChild(svgEl("rect", { x: x0, y: H - pad - hIn, width: bw, height: Math.max(0, hIn), rx: 2, class: "bar-in", fill: "url(#gradBarIn)" }));
    svg.appendChild(svgEl("rect", { x: x0 + bw + 3, y: H - pad - hOut, width: bw, height: Math.max(0, hOut), rx: 2, class: "bar-out", fill: "url(#gradBarOut)" }));
    const hit = svgEl("rect", { x: x0 - 2, y: pad, width: step, height: H - pad * 2, class: "bar-hit" });
    hit.addEventListener("mousemove", (e) => {
      const box = svg.getBoundingClientRect();
      const px = ((e.clientX - box.left) / box.width) * W;
      const py = ((e.clientY - box.top) / box.height) * H;
      showTip(px, py, `${v.date} 入 ${humanBytes(v.in)}`, `出 ${humanBytes(v.out)}`);
    });
    hit.addEventListener("mouseleave", () => svg.querySelector(".tip").setAttribute("visibility", "hidden"));
    svg.appendChild(hit);
    const label = svgEl("text", { x: x0, y: H - 8, class: "axis", "font-size": 10 });
    label.textContent = String(v.date).slice(5);
    svg.appendChild(label);
  });
  const legend = svgEl("text", { x: pad, y: 13, class: "axis", "font-size": 11 });
  legend.textContent = `蓝=入站 绿=出站　合计 入 ${humanBytes(totalIn)} / 出 ${humanBytes(totalOut)}`;
  svg.appendChild(legend);
  return svg;
}

/** 代理类型分布环图（v0.3.4：proxy_type_counts 的零成本可视化）。
 *  颜色走 CSS 类（`.donut-c0..7` → CSS 变量），主题切换即时生效。 */
export function donut(typeCounts) {
  const entries = Object.entries(typeCounts || {}).filter(([, v]) => Number(v) > 0);
  if (!entries.length) return emptyState("暂无代理数据");
  const items = entries.map(([name, value]) => ({ name, value: Number(value) }));
  const total = items.reduce((acc, item) => acc + item.value, 0);
  const W = 220, H = 170, cx = 85, cy = H / 2, outer = 62, inner = 38;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H });
  const slices = donutSlices(items, total);
  const sliceClass = (index) => "donut-c" + (index % 8);
  slices.forEach((slice, index) => {
    if (slice.ratio >= 0.9999) {
      svg.appendChild(svgEl("circle", { cx, cy, r: (outer + inner) / 2, class: "donut-slice ring " + sliceClass(index) }));
      return;
    }
    const large = slice.end - slice.start > Math.PI ? 1 : 0;
    const x1 = cx + outer * Math.cos(slice.start), y1 = cy + outer * Math.sin(slice.start);
    const x2 = cx + outer * Math.cos(slice.end), y2 = cy + outer * Math.sin(slice.end);
    const x3 = cx + inner * Math.cos(slice.end), y3 = cy + inner * Math.sin(slice.end);
    const x4 = cx + inner * Math.cos(slice.start), y4 = cy + inner * Math.sin(slice.start);
    const d = `M${x1.toFixed(2)},${y1.toFixed(2)} A${outer},${outer} 0 ${large} 1 ${x2.toFixed(2)},${y2.toFixed(2)} L${x3.toFixed(2)},${y3.toFixed(2)} A${inner},${inner} 0 ${large} 0 ${x4.toFixed(2)},${y4.toFixed(2)} Z`;
    const path = svgEl("path", { d, class: "donut-slice " + sliceClass(index) });
    const tip = svgEl("title", {});
    tip.textContent = `${slice.name}：${slice.value}（${(slice.ratio * 100).toFixed(1)}%）`;
    path.appendChild(tip);
    svg.appendChild(path);
  });
  const totalText = svgEl("text", { x: cx, y: cy + 2, class: "donut-total", "text-anchor": "middle" });
  totalText.textContent = String(total);
  const totalLabel = svgEl("text", { x: cx, y: cy + 16, class: "axis", "text-anchor": "middle", "font-size": 10 });
  totalLabel.textContent = "总数";
  svg.append(totalText, totalLabel);
  let legendY = 20;
  slices.slice(0, 6).forEach((slice, index) => {
    const swatch = svgEl("rect", { x: 158, y: legendY - 8, width: 10, height: 10, rx: 2, class: sliceClass(index) });
    const label = svgEl("text", { x: 172, y: legendY, class: "axis", "font-size": 10.5 });
    label.textContent = `${slice.name} ${slice.value}`;
    svg.append(swatch, label);
    legendY += 20;
  });
  return svg;
}

function readSamples() {
  try { return JSON.parse(localStorage.getItem(SAMPLES_KEY) || "[]"); } catch (e) { return []; }
}

/** 采样一次累计流量（写入本地存储；返回全部样本）。 */
export function recordSample(trafficIn, trafficOut) {
  if (trafficIn === null || trafficIn === undefined) return readSamples();
  const now = Date.now();
  // 会话基线 = **本次页面加载的第一次采样**（内存态，刷新即重新开始，与
  // "本次会话"语义一致）。不能用 `samples.length === 0` 判断：samples 持久化
  // 在 localStorage，刷新后非空 → 基线永远不设、会话流量永远显示 "-"
  // （v0.3.4 交付前对账发现的缺陷）。
  if (state.sessionBase === null) state.sessionBase = { vin: trafficIn, vout: trafficOut ?? 0 };
  const { samples, append } = trimSamples(readSamples(), now);
  if (!append) return samples;
  samples.push({ t: now, vin: trafficIn, vout: trafficOut });
  try {
    localStorage.setItem(SAMPLES_KEY, JSON.stringify(samples.slice(-720)));
  } catch (e) {
    /* 隐私模式/配额满：采样仅在内存中 */
  }
  return samples;
}

/** 实时速率面积图（平滑曲线 + 面积填充 + 十字线提示）。 */
export function sparkline(samples) {
  const speeds = speedSeries(samples);
  if (speeds.length < 2) return el("div", { class: "muted", text: "（采样不足，稍候）" });
  const max = Math.max(1, ...speeds.map((s) => Math.max(s.in, s.out)));
  const W = 560, H = 150, pad = 22;
  const xs = speeds.map((_, i) => pad + (i / Math.max(1, speeds.length - 1)) * (W - pad * 2));
  const yOf = (v) => H - pad - (v / max) * (H - pad * 2);
  const yIn = speeds.map((s) => yOf(s.in));
  const yOut = speeds.map((s) => yOf(s.out));
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H });
  const defs = svgEl("defs", {});
  chartGradients(defs, "gradAreaIn", "gradAreaOut");
  svg.appendChild(defs);
  for (let g = 0; g <= 3; g++) {
    const y = pad + ((H - pad * 2) / 3) * g;
    svg.appendChild(svgEl("line", { x1: pad, y1: y, x2: W - pad, y2: y, class: "grid-line" }));
  }
  const areaOf = (ys) => `${smoothPath(xs, ys)} L${xs[xs.length - 1].toFixed(1)},${H - pad} L${xs[0].toFixed(1)},${H - pad} Z`;
  svg.appendChild(svgEl("path", { d: areaOf(yIn), fill: "url(#gradAreaIn)", stroke: "none" }));
  svg.appendChild(svgEl("path", { d: areaOf(yOut), fill: "url(#gradAreaOut)", stroke: "none" }));
  svg.appendChild(svgEl("path", { d: smoothPath(xs, yIn) || "M0,0", fill: "none", class: "line-in", "stroke-width": 1.8 }));
  svg.appendChild(svgEl("path", { d: smoothPath(xs, yOut) || "M0,0", fill: "none", class: "line-out", "stroke-width": 1.8 }));
  const cursor = svgEl("line", { x1: pad, y1: pad, x2: pad, y2: H - pad, class: "crosshair", visibility: "hidden" });
  svg.appendChild(cursor);
  const showTip = hoverTip(svg, W, H);
  const overlay = svgEl("rect", { x: pad, y: pad, width: W - pad * 2, height: H - pad * 2, class: "bar-hit" });
  overlay.addEventListener("mousemove", (e) => {
    const box = svg.getBoundingClientRect();
    const px = ((e.clientX - box.left) / box.width) * W;
    let idx = Math.round(((px - pad) / (W - pad * 2)) * (speeds.length - 1));
    idx = Math.min(Math.max(idx, 0), speeds.length - 1);
    const s = speeds[idx];
    cursor.setAttribute("x1", xs[idx]);
    cursor.setAttribute("x2", xs[idx]);
    cursor.setAttribute("visibility", "visible");
    showTip(xs[idx], yOf(s.in), `入 ${humanBytes(s.in)}/s`, `出 ${humanBytes(s.out)}/s`);
  });
  overlay.addEventListener("mouseleave", () => {
    cursor.setAttribute("visibility", "hidden");
    svg.querySelector(".tip").setAttribute("visibility", "hidden");
  });
  svg.appendChild(overlay);
  const last = speeds[speeds.length - 1];
  const label = svgEl("text", { x: pad, y: 12, class: "axis", "font-size": 11 });
  label.textContent = `当前 入 ${humanBytes(last.in)}/s 出 ${humanBytes(last.out)}/s　峰值 ${humanBytes(max)}/s`;
  svg.appendChild(label);
  return svg;
}

/** 刷新 7 天趋势与实时曲线（趋势保留旧图直到新数据到达）。 */
export async function refreshTraffic() {
  if (state.trafficLoading) return;   // 慢 dashboard 下防止请求叠加
  state.trafficLoading = true;
  try {
    const data = await api("/api/traffic");
    $("chart-7d").replaceChildren(bars7d(data.days || []));
    const hints = [];
    if (data.truncated) hints.push(`已截断：仅统计前 ${data.limit} 个代理（共 ${data.total} 个）`);
    if (data.partial) hints.push("部分代理的流量历史未在预算内返回，汇总可能偏低");
    $("chart-7d-hint").textContent = hints.join("；");
  } catch (err) { /* dashboard 不可用时保留旧图 */ }
  finally { state.trafficLoading = false; }
  const dash = (state.lastStatus && state.lastStatus.dashboard) || {};
  const samples = recordSample(dash.traffic_in, dash.traffic_out);
  const speeds = speedSeries(samples);
  const peaks = speeds.map((s) => Math.max(s.in, s.out));
  state.sessionPeak = peaks.length ? Math.max(...peaks) : null;
  state.sessionAvg = peaks.length ? peaks.reduce((a, b) => a + b, 0) / peaks.length : null;
  $("chart-live").replaceChildren(sparkline(samples));
  if (samples.length === 1) $("live-hint").textContent = "已采样 1 次，等待下一次刷新后绘出速率曲线。";
}
