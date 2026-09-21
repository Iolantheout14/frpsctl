/** 审计数据变换模块的真单测。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  pluginAuditStatsItems,
  pluginAuditNotes,
  webAuditStatsItems,
  webAuditNotes,
  pluginAuditRows,
  webAuditRows,
} from "../../src/frpsctl/web/static/js/lib/audit-format.js";

test("webAuditStatsItems 坏行口径（stats 优先，回退顶层）", () => {
  const a = webAuditStatsItems({ stats: { total: 7, ok: 6, error: 1, bad_lines: 5 }, bad_lines: 2 });
  assert.deepEqual(a, [["操作", 7], ["成功", 6], ["失败", 1], ["坏行", 5]]);
  const b = webAuditStatsItems({ bad_lines: 3 });
  assert.equal(b[3][1], 3);
});

test("webAuditNotes 空态与路径", () => {
  const notes = webAuditNotes({ available: false, reason: "x", path: "/p", stats: {} });
  assert.ok(notes.some((n) => n.includes("尚无 Web 操作记录")));
  assert.ok(notes.some((n) => n.includes("/p")));
});

test("webAuditRows 新在前 + detail 拼接", () => {
  const rows = webAuditRows([
    { at: "t1", result: "ok", action: "start", target: "a", source: "s", params: { k: 1 } },
    { at: "t2", result: "error:X", action: "stop" },
  ]);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].at, "t2");
  assert.equal(rows[0].ok, false);
  assert.equal(rows[1].detail, "k=1");
  assert.equal(rows[1].ok, true);
});

test("pluginAuditStatsItems 耗时字段", () => {
  const items = pluginAuditStatsItems({ stats: { total: 3, allow: 2, deny: 1, elapsed_count: 3, elapsed_avg_ms: 0.03, elapsed_max_ms: 0.11 } });
  assert.deepEqual(items[0], ["记录", 3]);
  assert.deepEqual(items[5], ["均耗时(ms)", "0.0"]);
  assert.deepEqual(items[6], ["峰耗时(ms)", "0.1"]);
  const empty = pluginAuditStatsItems({});
  assert.equal(empty[5][1], "-");
});

test("pluginAuditNotes 可用性三态", () => {
  assert.ok(pluginAuditNotes({ available: false, reason: "r", stats: {} })[0].includes("策略不可用"));
  assert.ok(pluginAuditNotes({ available: true, enabled: false, stats: {} })[0].includes("审计已关闭"));
  assert.ok(pluginAuditNotes({ available: true, enabled: true, path: null, stats: {} })[0].includes("仅内存"));
});

test("pluginAuditRows 默认值", () => {
  const [row] = pluginAuditRows([{ op: "NewProxy", decision: "deny", remote_port: 9999 }]);
  assert.equal(row.op, "NewProxy");
  assert.equal(row.remote_port, "9999");
  assert.equal(row.user, "-");
  assert.equal(row.reason, "");
});
