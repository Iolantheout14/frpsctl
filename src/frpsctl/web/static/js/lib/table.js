/** 列表数据变换（纯函数：过滤 / 排序 / 行映射——node --test 直接断言）。 */

import { humanBytes } from "./format.js";

/** 本地过滤：query 对 fields 指定的字段做子串匹配（大小写不敏感）。 */
export function filterRows(rows, query, fields) {
  const q = String(query || "").trim().toLowerCase();
  if (!q) return rows;
  return rows.filter((row) => fields.some((key) => String(row[key] ?? "").toLowerCase().includes(q)));
}

/** 排序：两侧都能转数字时按数值比较，否则按中文字典序。 */
export function sortRows(rows, key, dir) {
  const sign = dir === "desc" ? -1 : 1;
  return rows.slice().sort((a, b) => {
    const an = Number(a[key]);
    const bn = Number(b[key]);
    if (!Number.isNaN(an) && !Number.isNaN(bn)) return (an - bn) * sign;
    return String(a[key] ?? "").localeCompare(String(b[key] ?? ""), "zh") * sign;
  });
}

/** v2 客户端条目 → 表格行。 */
export function clientRows(items) {
  return (items || []).map((c) => ({
    key: c.key || "-",
    name: c.key || "-",
    user: c.user || "-",
    hostname: c.hostname || "-",
    ip: c.clientIP || "-",
    online: Boolean(c.online),
    version: c.version || "-",
  }));
}

/** v2 代理条目 → 表格行。 */
export function proxyRows(items) {
  return (items || []).map((p) => ({
    name: p.name || "-",
    user: p.user || "-",
    type: p.type || "-",
    port: p.remote_port ? String(p.remote_port) : "-",
    online: p.phase === "online",
    phase: p.phase || "-",
    conns: String(p.cur_conns ?? 0),
    traffic: `${humanBytes(p.today_traffic_in)} / ${humanBytes(p.today_traffic_out)}`,
    // 原始数值：Top 流量排行/排序用（展示字符串不可比较）
    in_raw: Number(p.today_traffic_in) || 0,
    out_raw: Number(p.today_traffic_out) || 0,
  }));
}
