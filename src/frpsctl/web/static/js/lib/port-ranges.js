/** 端口区间数组的结构化编辑（纯函数：解析 / 序列化 / 校验——node --test 断言）。
 *
 *  v0.3.4（F8）：`allowPorts` 从"JSON 文本手编"升级为表格行编辑；
 *  其余数组键仍走文本编辑（记账：等真实痛点再逐个结构化）。
 */

/** 服务端下发的 allowPorts 数组 → 编辑器行。 */
export function parsePortRanges(value) {
  if (!Array.isArray(value)) return [];
  const rows = [];
  for (const item of value) {
    if (item && typeof item === "object") {
      if (item.single !== undefined) rows.push({ mode: "single", start: String(item.single), end: "" });
      else if (item.start !== undefined || item.end !== undefined) {
        rows.push({ mode: "range", start: String(item.start ?? ""), end: String(item.end ?? "") });
      }
    }
  }
  return rows;
}

/** 校验一行；返回空串表示合法。 */
export function validatePortRangeRow(row) {
  const start = Number(row.start);
  if (!Number.isInteger(start) || start < 1 || start > 65535) return "端口必须是 1-65535 的整数";
  if (row.mode === "range") {
    const end = Number(row.end);
    if (!Number.isInteger(end) || end < 1 || end > 65535) return "结束端口必须是 1-65535 的整数";
    if (end < start) return "结束端口不能小于起始端口";
  }
  return "";
}

/** 编辑器行 → TOML 内联数组文本（与 CLI `parse_scalar` 兼容）；非法行跳过。 */
export function serializePortRanges(rows) {
  const parts = [];
  for (const row of rows) {
    if (validatePortRangeRow(row)) continue;
    const start = Number(row.start);
    if (row.mode === "single") {
      parts.push(`{ single = ${start} }`);
      continue;
    }
    const end = Number(row.end);
    parts.push(start === end ? `{ single = ${start} }` : `{ start = ${start}, end = ${end} }`);
  }
  return `[ ${parts.join(", ")} ]`;
}

/** 序列化文本 → 编辑器行（表单重建时从草稿恢复；与 `serializePortRanges` 往返一致）。 */
export function parsePortText(text) {
  const rows = [];
  for (const match of String(text || "").matchAll(/\{([^}]*)\}/g)) {
    const body = match[1];
    const single = /single\s*=\s*(\d+)/.exec(body);
    const start = /start\s*=\s*(\d+)/.exec(body);
    const end = /end\s*=\s*(\d+)/.exec(body);
    if (single) rows.push({ mode: "single", start: single[1], end: "" });
    else if (start || end) {
      rows.push({ mode: "range", start: start ? start[1] : "", end: end ? end[1] : "" });
    }
  }
  return rows;
}
