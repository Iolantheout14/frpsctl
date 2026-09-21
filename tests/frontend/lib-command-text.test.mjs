/** 命令面展示纯逻辑单测（node --test 直接 import 生产模块 + 生成物数据）。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { COMMAND_SURFACE } from "../../src/frpsctl/web/static/js/data/commands.js";
import {
  badgesFor,
  commandHaystack,
  commandText,
  defaultLabel,
  paramLabel,
} from "../../src/frpsctl/web/static/js/lib/command-text.js";

test("生成物包含全部 63 条命令且字段齐备", () => {
  const commands = COMMAND_SURFACE.commands;
  assert.equal(commands.length, 63);
  assert.ok(COMMAND_SURFACE.version);
  assert.ok(COMMAND_SURFACE.frps_minimum);
  assert.ok(Object.keys(COMMAND_SURFACE.exit_codes).length >= 13);
  for (const entry of commands) {
    assert.equal(typeof entry.path, "string");
    assert.equal(typeof entry.readonly, "boolean");
    assert.ok(Array.isArray(entry.params));
  }
});

test("paramLabel：flag / 取值 / 位置参数", () => {
  assert.equal(paramLabel({ kind: "opt", name: "json", opts: ["--json"], is_flag: true }), "--json");
  assert.equal(paramLabel({ kind: "opt", name: "lines", opts: ["--lines", "-n"] }), "--lines LINES");
  assert.equal(
    paramLabel({ kind: "opt", name: "mirror", opts: ["--mirror"], multiple: true }),
    "--mirror MIRROR…",
  );
  assert.equal(paramLabel({ kind: "arg", name: "key" }), "KEY");
  assert.equal(paramLabel({ kind: "arg", name: "steps", nargs: 2 }), "STEPS…");
  assert.equal(paramLabel(null), "");
});

test("defaultLabel：flag 显示开关，取值显示默认文本", () => {
  assert.equal(defaultLabel({ kind: "opt", name: "json", opts: ["--json"], is_flag: true, default_text: "false" }), "关");
  assert.equal(defaultLabel({ kind: "opt", name: "json", opts: ["--json"], is_flag: true, default_text: "true" }), "开");
  assert.equal(defaultLabel({ kind: "opt", name: "lines", opts: ["--lines"], default_text: "100" }), "100");
  assert.equal(defaultLabel({ kind: "opt", name: "x", opts: ["--x"], default_text: null }), "-");
  assert.equal(defaultLabel(null), "-");
});

test("commandText：骨架含位置参数（可选参数加方括号）", () => {
  const setCmd = COMMAND_SURFACE.commands.find((c) => c.path === "config set");
  assert.equal(commandText(setCmd), "frpsctl config set KEY [VALUE]");
  const statusCmd = COMMAND_SURFACE.commands.find((c) => c.path === "status");
  assert.equal(commandText(statusCmd), "frpsctl status");
  assert.equal(commandText(null), "frpsctl");
});

test("每条命令的 commandText 都以 frpsctl 开头且不重复路径", () => {
  for (const entry of COMMAND_SURFACE.commands) {
    const text = commandText(entry);
    assert.ok(text.startsWith("frpsctl "), `${entry.path} 骨架异常：${text}`);
    assert.ok(text.includes(entry.path), `${entry.path} 骨架缺少路径：${text}`);
    assert.ok(!text.includes("undefined") && !text.includes("null"), `${entry.path} 骨架含空值：${text}`);
  }
});

test("commandHaystack：覆盖路径、说明与参数", () => {
  const mirror = COMMAND_SURFACE.commands.find((c) => c.path === "install");
  const hay = commandHaystack(mirror);
  assert.ok(hay.includes("install"));
  assert.ok(hay.includes("--mirror"));
  assert.ok(hay.includes("--with-frpc"));
  assert.equal(commandHaystack(null), "");
  // 反例：不存在的 token 不应命中
  assert.ok(!hay.includes("not-a-real-flag"));
});

test("badgesFor：只读/变更 + root/systemd", () => {
  assert.deepEqual(badgesFor({ readonly: true }), ["只读"]);
  assert.deepEqual(badgesFor({ readonly: false, needs_root: true, needs_systemd: true }), [
    "变更",
    "需 root",
    "systemd",
  ]);
  assert.deepEqual(badgesFor(null), []);
});

test("分类与现实一致：service install 需 root，web service status 只读", () => {
  const byPath = new Map(COMMAND_SURFACE.commands.map((c) => [c.path, c]));
  const install = byPath.get("service install");
  assert.equal(install.readonly, false);
  assert.equal(install.needs_root, true);
  assert.equal(install.needs_systemd, true);
  const status = byPath.get("web service status");
  assert.equal(status.readonly, true);
  assert.equal(status.web_view, "services");
  // 全部命令的 web_view 必须是前端真实存在的视图
  const known = new Set(["dash", "config", "audit", "services", "versions", "commands"]);
  for (const entry of COMMAND_SURFACE.commands) {
    if (entry.web_view !== null) assert.ok(known.has(entry.web_view), `${entry.path} 视图非法`);
  }
});
