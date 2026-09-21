/** 图表的纯几何计算（零 DOM——node --test 直接断言）。 */

/** Catmull-Rom → 三次贝塞尔：视觉平滑且不引入外部依赖。 */
export function smoothPath(xs, ys) {
  if (xs.length < 2) return "";
  let d = `M${xs[0].toFixed(1)},${ys[0].toFixed(1)}`;
  for (let i = 0; i < xs.length - 1; i++) {
    const x0 = xs[Math.max(0, i - 1)], y0 = ys[Math.max(0, i - 1)];
    const x1 = xs[i], y1 = ys[i], x2 = xs[i + 1], y2 = ys[i + 1];
    const x3 = xs[Math.min(xs.length - 1, i + 2)], y3 = ys[Math.min(ys.length - 1, i + 2)];
    const c1x = x1 + (x2 - x0) / 6, c1y = y1 + (y2 - y0) / 6;
    const c2x = x2 - (x3 - x1) / 6, c2y = y2 - (y3 - y1) / 6;
    d += ` C${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}`;
  }
  return d;
}

/** 累计流量采样序列 → 区间速率序列（负增量按 0 钳制）。 */
export function speedSeries(samples) {
  const speeds = [];
  for (let i = 1; i < samples.length; i++) {
    const dt = (samples[i].t - samples[i - 1].t) / 1000;
    if (dt <= 0) continue;
    speeds.push({ t: samples[i].t, in: Math.max(0, (samples[i].vin - samples[i - 1].vin) / dt), out: Math.max(0, (samples[i].vout - samples[i - 1].vout) / dt) });
  }
  return speeds;
}

/** 采样序列裁剪：只保留最近一小时、且距上一条至少 3 秒。 */
export function trimSamples(samples, now, maxAgeMs = 3600 * 1000, minGapMs = 3000) {
  const kept = (samples || []).filter((s) => now - s.t < maxAgeMs).slice(-720);
  const last = kept[kept.length - 1];
  if (last && now - last.t < minGapMs) return { samples: kept, append: false };
  return { samples: kept, append: true };
}

/** 环图分段：把计数映射为弧度区间（从 -90° 起顺时针）。 */
export function donutSlices(items, total) {
  const sum = total > 0 ? total : items.reduce((acc, item) => acc + item.value, 0);
  if (!sum) return [];
  const slices = [];
  let start = -Math.PI / 2;
  for (const item of items) {
    const angle = (item.value / sum) * Math.PI * 2;
    slices.push({ ...item, start, end: start + angle, ratio: item.value / sum });
    start += angle;
  }
  return slices;
}
