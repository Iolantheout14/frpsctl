/** 纯格式化与校验函数（零 DOM、零状态——由 `node --test` 直接 import 断言）。
 *  原 index.html 单文件时代的 humanBytes / humanDuration / looksBalanced /
 *  classifyLogLine / badgeClass 在此集中，行为逐字保持。 */

/** 字节数 → 人类可读（B / KiB / MiB / GiB / TiB）。 */
export function humanBytes(n) {
  if (n === null || n === undefined) return "-";
  let v = Number(n);
  for (const unit of ["B", "KiB", "MiB", "GiB", "TiB"]) {
    if (Math.abs(v) < 1024 || unit === "TiB") return unit === "B" ? `${Math.round(v)} B` : `${v.toFixed(1)} ${unit}`;
    v /= 1024;
  }
}

/** 秒数 → 人类可读时长（d/h/m/s 两段式）。 */
export function humanDuration(s) {
  if (s === null || s === undefined) return "-";
  s = Math.floor(s);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d${h}h`; if (h) return `${h}h${m}m`; if (m) return `${m}m${s % 60}s`; return `${s}s`;
}

/** 实例状态 → 徽章颜色 class（badgeClass("RUNNING") === "ok"）。 */
export function badgeClass(stateName) {
  if (stateName === "RUNNING" || stateName === "SYSTEMD_ACTIVE") return "ok";
  if (stateName === "FOREIGN") return "fail";
  if (stateName === "STALE") return "warn";
  return "";
}

/** 日志行 → 着色 class（ERROR/WARN 关键字，大小写不敏感）。 */
export function classifyLogLine(line) {
  if (/\b(error|fatal|panic)\b/i.test(line)) return "lv-error";
  if (/\bwarn(ing)?\b/i.test(line)) return "lv-warn";
  return "";
}

/** 轻量启发式：只检查"结构化值"的括号/引号配对（真正的校验在服务端预览时）。
 *  用类型栈而不是深度计数：深度计数检测不到混合括号不匹配。 */
export function looksBalanced(raw) {
  const text = raw.trim();
  if (!text || !" [{\"'}]".includes(text[0])) return true;
  const stack = [];
  let quote = null;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quote) {
      if (ch === quote && text[i - 1] !== "\\") quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") { quote = ch; continue; }
    if (ch === "[" || ch === "{") stack.push(ch === "[" ? "]" : "}");
    else if (ch === "]" || ch === "}") {
      if (stack.pop() !== ch) return false;
    }
  }
  return stack.length === 0 && quote === null;
}
