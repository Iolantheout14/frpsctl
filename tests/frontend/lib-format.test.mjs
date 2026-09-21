/** 纯逻辑模块的真单测（node --test 直接 import 生产模块——v0.3.4 拆分的核心收益）。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { humanBytes, humanDuration, badgeClass, classifyLogLine, looksBalanced } from "../../src/frpsctl/web/static/js/lib/format.js";
import { filterRows, sortRows, clientRows, proxyRows } from "../../src/frpsctl/web/static/js/lib/table.js";

test("humanBytes 边界", () => {
  assert.equal(humanBytes(0), "0 B");
  assert.equal(humanBytes(1024), "1.0 KiB");
  assert.equal(humanBytes(1536), "1.5 KiB");
  assert.equal(humanBytes(1048576), "1.0 MiB");
  assert.equal(humanBytes(null), "-");
  assert.equal(humanBytes(undefined), "-");
  assert.equal(humanBytes(512), "512 B");
});

test("humanDuration 边界", () => {
  assert.equal(humanDuration(90), "1m30s");
  assert.equal(humanDuration(3600), "1h0m");
  assert.equal(humanDuration(90061), "1d1h");
  assert.equal(humanDuration(null), "-");
  assert.equal(humanDuration(0), "0s");
});

test("badgeClass 状态映射", () => {
  assert.equal(badgeClass("RUNNING"), "ok");
  assert.equal(badgeClass("SYSTEMD_ACTIVE"), "ok");
  assert.equal(badgeClass("FOREIGN"), "fail");
  assert.equal(badgeClass("STALE"), "warn");
  assert.equal(badgeClass("STOPPED"), "");
});

test("classifyLogLine 关键字着色", () => {
  assert.equal(classifyLogLine("level=error msg=x"), "lv-error");
  assert.equal(classifyLogLine("FATAL boom"), "lv-error");
  assert.equal(classifyLogLine("warn: slow"), "lv-warn");
  assert.equal(classifyLogLine("warning slow"), "lv-warn");
  assert.equal(classifyLogLine("info ok"), "");
});

test("looksBalanced 类型栈边界", () => {
  const cases = [
    ["7000", true],
    ["true", true],
    ['"text"', true],
    ["[1, 2, 3]", true],
    ["[{ single = 6000 }, { start = 7000, end = 7100 }]", true],
    ['{ token = "with ] bracket" }', true],
    ["[1, 2", false],
    ["{ a = 1 ]", false],
    ['"unclosed', false],
    ["]", false],
    ["", true],
    ["   ", true],
  ];
  for (const [value, expected] of cases) {
    assert.equal(looksBalanced(value), expected, `looksBalanced(${JSON.stringify(value)})`);
  }
});

test("filterRows 子串匹配（大小写不敏感 / 多字段）", () => {
  const rows = [
    { name: "alice-ssh", user: "alice", ip: "10.0.0.1" },
    { name: "bob-web", user: "bob", ip: "10.0.0.2" },
  ];
  assert.equal(filterRows(rows, "", ["name"]).length, 2);
  assert.equal(filterRows(rows, "ALICE", ["name", "user"]).length, 1);
  assert.equal(filterRows(rows, "10.0.0.2", ["ip"]).length, 1);
  assert.equal(filterRows(rows, "nope", ["name"]).length, 0);
});

test("sortRows 数值优先、中文兜底、方向", () => {
  // 非数值项与数值项混排：`Number("-")` 为 NaN → 走 localeCompare（"-" 排前）
  const asc = sortRows([{ v: "10" }, { v: "2" }, { v: "-" }], "v", "asc");
  assert.deepEqual(asc.map((r) => r.v), ["-", "2", "10"]);
  const nums = sortRows([{ v: "10" }, { v: "2" }], "v", "asc");
  assert.deepEqual(nums.map((r) => r.v), ["2", "10"]);
  const desc = sortRows([{ v: "10" }, { v: "2" }], "v", "desc");
  assert.deepEqual(desc.map((r) => r.v), ["10", "2"]);
});

test("clientRows 归一化", () => {
  const rows = clientRows([
    { key: "k", user: "u", hostname: "h", clientIP: "1.2.3.4", online: true, version: "0.71.0" },
    { online: 0 },
  ]);
  assert.equal(rows[0].name, "k");
  assert.equal(rows[0].ip, "1.2.3.4");
  assert.equal(rows[0].online, true);
  assert.equal(rows[1].online, false);   // 非布尔归一化
  assert.equal(rows[1].name, "-");
});

test("proxyRows 端口与流量", () => {
  const [row] = proxyRows([
    { name: "n", type: "tcp", remote_port: 6000, phase: "online", cur_conns: 3, today_traffic_in: 2048, today_traffic_out: 0 },
  ]);
  assert.equal(row.port, "6000");
  assert.equal(row.online, true);
  assert.equal(row.traffic, "2.0 KiB / 0 B");
  const [offline] = proxyRows([{ name: "x" }]);
  assert.equal(offline.port, "-");
  assert.equal(offline.online, false);
});
