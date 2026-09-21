/** 模块加载冒烟（v0.3.4 对账补强）：全部非入口模块必须可被加载。
 *
 *  抓两件 `node --check` 与纯函数测试都覆盖不到的事：
 *  1. 模块**顶层访问 DOM/window**（在浏览器里没问题，但说明模块边界不干净，
 *     且会让这段冒烟失败——顶层副作用应当集中在 main.js 的 init）；
 *  2. **循环依赖在实际加载时炸掉**（ESM 循环里若顶层立即调用对方，会
 *     抛 "Cannot access before initialization"）。
 */

import { test } from "node:test";
import assert from "node:assert/strict";

const MODULES = [
  "api.js",
  "state.js",
  "theme.js",
  "shortcuts.js",
  "commands.js",
  "lib/format.js",
  "lib/table.js",
  "lib/audit-format.js",
  "lib/chart-math.js",
  "lib/port-ranges.js",
  "lib/command-text.js",
  "data/commands.js",
  "ui/dom.js",
  "ui/toast.js",
  "ui/modal.js",
  "ui/busy.js",
  "ui/icons.js",
  "ui/charts.js",
  "views/router.js",
  "views/login.js",
  "views/actions.js",
  "views/dashboard.js",
  "views/logs.js",
  "views/config.js",
  "views/audit.js",
  "views/services.js",
  "views/versions.js",
  "views/commands.js",
  "views/sessions.js",
];

test("全部非入口模块可加载（无顶层 DOM 访问 / 循环依赖异常）", async () => {
  for (const name of MODULES) {
    const url = new URL(`../../src/frpsctl/web/static/js/${name}`, import.meta.url);
    await assert.doesNotReject(import(url.href), `${name} 加载失败`);
  }
});
