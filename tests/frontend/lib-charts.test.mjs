/** 图表几何与端口区间编辑的真单测。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { smoothPath, speedSeries, trimSamples, donutSlices } from "../../src/frpsctl/web/static/js/lib/chart-math.js";
import { parsePortRanges, parsePortText, serializePortRanges, validatePortRangeRow } from "../../src/frpsctl/web/static/js/lib/port-ranges.js";

test("smoothPath 单点返回空、多点输出贝塞尔", () => {
  assert.equal(smoothPath([1], [1]), "");
  assert.equal(smoothPath([], []), "");
  const d = smoothPath([0, 10, 20], [0, 10, 0]);
  assert.ok(d.startsWith("M0.0,0.0"));
  assert.ok(d.includes(" C"));
});

test("speedSeries 差分与负值钳制", () => {
  const speeds = speedSeries([
    { t: 0, vin: 0, vout: 0 },
    { t: 1000, vin: 1000, vout: 2000 },
    { t: 3000, vin: 900, vout: 4000 },   // 负增量（重置）按 0
  ]);
  assert.equal(speeds.length, 2);
  assert.equal(speeds[0].in, 1000);
  assert.equal(speeds[0].out, 2000);
  assert.equal(speeds[1].in, 0);
  assert.equal(speeds[1].out, 1000);
});

test("speedSeries 忽略非正时间差", () => {
  const speeds = speedSeries([{ t: 5, vin: 0, vout: 0 }, { t: 5, vin: 100, vout: 0 }]);
  assert.equal(speeds.length, 0);
});

test("trimSamples 窗口裁剪与节流", () => {
  const now = 10_000_000;
  const old = { t: now - 2 * 3600 * 1000, vin: 0, vout: 0 };
  const fresh = { t: now - 10_000, vin: 1, vout: 1 };
  const tooClose = { t: now - 1000, vin: 2, vout: 2 };
  const r1 = trimSamples([old, fresh], now);
  assert.equal(r1.samples.length, 1);
  assert.equal(r1.append, true);
  const r2 = trimSamples([fresh, tooClose], now);
  assert.equal(r2.append, false);
  assert.equal(r2.samples.length, 2);
});

test("trimSamples 上限 720 条", () => {
  const now = 10_000_000;
  const samples = Array.from({ length: 800 }, (_, i) => ({ t: now - (800 - i) * 4000, vin: i, vout: i }));
  const r = trimSamples(samples, now);
  assert.equal(r.samples.length, 720);
});

test("donutSlices 比例与总量", () => {
  const slices = donutSlices([{ name: "tcp", value: 3 }, { name: "http", value: 1 }], 4);
  assert.equal(slices.length, 2);
  assert.equal(slices[0].ratio, 0.75);
  assert.equal(slices[1].ratio, 0.25);
  assert.ok(Math.abs(slices[1].end - slices[0].end - Math.PI / 2) < 1e-9);
  assert.equal(donutSlices([], 0).length, 0);
});

test("parsePortRanges 两种形态", () => {
  const rows = parsePortRanges([{ single: 6000 }, { start: 7000, end: 7100 }, { start: 8000 }]);
  assert.deepEqual(rows, [
    { mode: "single", start: "6000", end: "" },
    { mode: "range", start: "7000", end: "7100" },
    { mode: "range", start: "8000", end: "" },
  ]);
  assert.deepEqual(parsePortRanges("not-array"), []);
});

test("serializePortRanges 往返与跳过非法行", () => {
  const text = serializePortRanges([
    { mode: "single", start: "6000", end: "" },
    { mode: "range", start: "7000", end: "7100" },
  ]);
  assert.equal(text, "[ { single = 6000 }, { start = 7000, end = 7100 } ]");
  assert.equal(serializePortRanges([{ mode: "range", start: "7100", end: "7000" }]), "[  ]");
  assert.equal(
    serializePortRanges([{ mode: "range", start: "7000", end: "7000" }]),
    "[ { single = 7000 } ]",
  );
});

test("validatePortRangeRow 边界", () => {
  assert.equal(validatePortRangeRow({ mode: "single", start: "1", end: "" }), "");
  assert.equal(validatePortRangeRow({ mode: "single", start: "65535", end: "" }), "");
  assert.match(validatePortRangeRow({ mode: "single", start: "0", end: "" }), /1-65535/);
  assert.match(validatePortRangeRow({ mode: "single", start: "65536", end: "" }), /1-65535/);
  assert.match(validatePortRangeRow({ mode: "single", start: "abc", end: "" }), /1-65535/);
  assert.match(validatePortRangeRow({ mode: "range", start: "7100", end: "7000" }), /不能小于/);
  assert.match(validatePortRangeRow({ mode: "range", start: "7000", end: "" }), /1-65535/);
});

test("parsePortText 与 serializePortRanges 往返一致（草稿恢复用）", () => {
  const rows = [
    { mode: "single", start: "6000", end: "" },
    { mode: "range", start: "7000", end: "7100" },
  ];
  const text = serializePortRanges(rows);
  assert.deepEqual(parsePortText(text), rows);
  assert.deepEqual(parsePortText(""), []);
  assert.deepEqual(parsePortText("[ { start = 8000, end = 8000 } ]"), [
    { mode: "range", start: "8000", end: "8000" },
  ]);
});
