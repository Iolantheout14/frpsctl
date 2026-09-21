/** 命令面 → 展示文本的纯函数（v0.3.5）。
 *
 *  无 DOM、无副作用：`node --test` 可直接 import 断言（与 `lib/port-ranges.js`
 *  同一纪律）。命令数据来自生成物 `data/commands.js`（与 CLI 命令面同源）。
 */

/** 是否位置参数（Click 的 Argument 也带 opts，故不能只看 kind）。 */
export function isPositional(param) {
  if (!param) return false;
  if (param.positional === true || param.kind === "arg") return true;
  return !(param.opts || []).some((opt) => String(opt).startsWith("-"));
}

/** 参数标签：flag 显示选项名；取值选项显示 `--opt VALUE`；位置参数显示大写名。 */
export function paramLabel(param) {
  if (!param) return "";
  if (isPositional(param)) {
    const name = String(param.name || "").toUpperCase();
    const nargs = Number(param.nargs || 1);
    return nargs > 1 ? `${name}…` : name;
  }
  const opts = param.opts || [];
  const long = opts.find((opt) => opt.startsWith("--")) || opts[0] || String(param.name || "");
  if (param.is_flag) return long;
  const value = String(param.name || "value").toUpperCase().replace(/-/g, "_");
  return param.multiple ? `${long} ${value}…` : `${long} ${value}`;
}

/** 默认值展示：flag 显示"开/关"，其余显示默认文本（无默认则 `-`）。 */
export function defaultLabel(param) {
  if (!param) return "-";
  if (param.is_flag) return param.default_text === "true" ? "开" : "关";
  if (param.default_text === undefined || param.default_text === null) return "-";
  return String(param.default_text);
}

/** 可复制的命令骨架：`frpsctl config set KEY [VALUE]`（可选位置参数带方括号）。 */
export function commandText(entry) {
  if (!entry) return "frpsctl";
  const args = (entry.params || [])
    .filter((param) => isPositional(param))
    .map((param) => {
      const name = String(param.name || "").toUpperCase();
      return param.required ? name : `[${name}]`;
    });
  return ["frpsctl", entry.path, ...args].join(" ").trim();
}

/** 搜索用的检索文本（路径 + 说明 + 参数名/选项/帮助，小写）。 */
export function commandHaystack(entry) {
  if (!entry) return "";
  const parts = [entry.path, entry.summary || "", entry.group || ""];
  for (const param of entry.params || []) {
    parts.push(param.name || "", (param.opts || []).join(" "), param.help || "");
  }
  return parts.join(" ").toLowerCase();
}

/** 徽章文本（只读/变更 + root/systemd 要求），供界面与测试共用。 */
export function badgesFor(entry) {
  if (!entry) return [];
  const out = [entry.readonly ? "只读" : "变更"];
  if (entry.needs_root) out.push("需 root");
  if (entry.needs_systemd) out.push("systemd");
  return out;
}
