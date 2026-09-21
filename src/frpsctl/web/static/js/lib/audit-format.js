/** 审计视图的数据变换（纯函数：统计项 / 备注 / 行映射——node --test 直接断言）。 */

/** 插件审计统计卡片项。 */
export function pluginAuditStatsItems(data) {
  const stats = data.stats || {};
  return [
    ["记录", stats.total ?? "-"],
    ["允许", stats.allow ?? "-"],
    ["拒绝", stats.deny ?? "-"],
    ["限速抑制", stats.suppressed_total ?? "-"],
    ["坏行", data.bad_lines ?? 0],
    ["均耗时(ms)", stats.elapsed_count ? Number(stats.elapsed_avg_ms).toFixed(1) : "-"],
    ["峰耗时(ms)", stats.elapsed_count ? Number(stats.elapsed_max_ms).toFixed(1) : "-"],
  ];
}

/** 插件审计备注行（可用性 / 时间范围 / 分布）。 */
export function pluginAuditNotes(data) {
  const stats = data.stats || {};
  const notes = [];
  if (!data.available) notes.push("策略不可用：" + (data.reason || ""));
  else if (!data.enabled) notes.push("审计已关闭（策略 audit.enabled = false）");
  else if (!data.path) notes.push("审计仅内存（audit.path = null，进程退出即丢失）");
  notes.push("策略文件：" + data.policy_path);
  if (stats.first_at) notes.push("时间范围：" + new Date(stats.first_at * 1000).toLocaleString() + " → " + new Date((stats.last_at || stats.first_at) * 1000).toLocaleString());
  const byUser = stats.by_user || {};
  const byUserText = Object.entries(byUser)
    .map(([user, b]) => `${user || "(未声明)"} 允${b.allow}/拒${b.deny}`)
    .join("　");
  if (byUserText) notes.push("按用户：" + byUserText);
  const byOp = stats.by_op || {};
  const byOpText = Object.entries(byOp).map(([op, n]) => `${op}=${n}`).join("　");
  if (byOpText) notes.push("按操作：" + byOpText);
  return notes;
}

/** Web 操作审计统计卡片项。 */
export function webAuditStatsItems(data) {
  const stats = data.stats || {};
  return [
    ["操作", stats.total ?? "-"],
    ["成功", stats.ok ?? "-"],
    ["失败", stats.error ?? "-"],
    ["坏行", stats.bad_lines ?? data.bad_lines ?? 0],
  ];
}

/** Web 操作审计备注行。 */
export function webAuditNotes(data) {
  const stats = data.stats || {};
  const notes = [];
  if (!data.available) notes.push("尚无 Web 操作记录：" + (data.reason || ""));
  notes.push("审计文件：" + (data.path || ""));
  if (stats.first_at) notes.push("时间范围：" + new Date(stats.first_at * 1000).toLocaleString() + " → " + new Date((stats.last_at || stats.first_at) * 1000).toLocaleString());
  const byAction = stats.by_action || {};
  const actionText = Object.entries(byAction).map(([a, n]) => `${a}=${n}`).join("　");
  if (actionText) notes.push("按动作：" + actionText);
  const bySource = stats.by_source || {};
  const sourceText = Object.entries(bySource)
    .sort((a, b) => b[1] - a[1])
    .map(([s, n]) => `${s || "(未知)"}=${n}`)
    .join("　");
  if (sourceText) notes.push("按来源：" + sourceText);
  return notes;
}

/** 插件审计记录 → 表格行（新在前由调用方 slice().reverse() 保证）。 */
export function pluginAuditRows(records) {
  return (records || []).map((r) => ({
    at: r.at || "-",
    decision: r.decision || "-",
    op: r.op || "-",
    user: r.user || "-",
    proxy_name: r.proxy_name || "-",
    remote_port: r.remote_port ? String(r.remote_port) : "-",
    reason: r.reason || "",
  }));
}

/** Web 操作审计记录 → 表格行（本函数内置 reverse：新在前）。 */
export function webAuditRows(records) {
  return (records || []).slice().reverse().map((r) => ({
    at: r.at || "-",
    ok: r.result === "ok",
    action: r.action || "-",
    target: r.target || "-",
    source: r.source || "-",
    detail: Object.entries(r.params || {}).map(([k, v]) => `${k}=${v}`).join(" "),
  }));
}
