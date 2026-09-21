"""CLI 与 Web 的**共享表示层**（0.3.0 新增）。

同一份 core 结果（状态、健康、体检、审计统计）此前在 CLI 与 Web 各自拼一遍
JSON dict——`cli._status_payload` 与 `web.api.status_payload`、两处
doctor payload、两处 health dict、两处 start payload。两处维护迟早漂移
（字段名/默认值/键集），而它们都是对脚本与前端承诺的形状。

本模块只做纯数据组装：不打印、不做 I/O、不依赖 typer/httpx。`core/` 的
分层约束（core 不依赖任何前端）不受影响——它位于 `cli/` 与 `web/` 之前，
被两者共享。
"""

from __future__ import annotations

from typing import Any

from .core.admin import sum_proxy_types
from .core.doctor import DoctorReport
from .core.lifecycle import HealthReport, StartReport, StatusReport
from .errors import ExitCode

__all__ = [
    "ExitCode",
    "audit_summary_payload",
    "doctor_payload",
    "health_payload",
    "start_payload",
    "status_payload",
]


def health_payload(health: HealthReport | None) -> dict[str, Any] | None:
    """三层健康（含 gate 与 L3 告警）。CLI 的启动/重启输出与 Web 的状态卡共用。

    `gate` 与 `plugin_warning` 此前只有 Web 侧在下发：脚本消费 CLI `--json`
    时同样需要"启动是否算成功"与"插件是否拖住了登录"这两个判断，统一后
    CLI 输出是**向后兼容的增字段**。
    """
    if health is None:
        return None
    return {
        "l1_process": health.l1_process.value,
        "l2_control": health.l2_control.value,
        "l3_plugin": health.l3_plugin.value,
        "detail": health.detail,
        "gate": health.gate,
        "plugin_warning": health.plugin_warning,
    }


def start_payload(report: StartReport) -> dict[str, Any]:
    """start / restart / Web actions 的统一形状（含 version，向后兼容增字段）。"""
    return {
        "pid": report.pid,
        "version": str(report.version),
        "healthy": report.healthy,
        "health": health_payload(report.health),
    }


def status_payload(
    report: StatusReport,
    *,
    dashboard: dict[str, Any] | None = None,
    clients: int | None = None,
    proxy_counts: dict[str, int] | None = None,
    include_paths: bool = True,
) -> dict[str, Any]:
    """`status` / `instances` / Web 仪表盘共用的状态形状。

    `include_paths=False` 是 Web 的形状：它不下发 binary/config 路径（页面
    不需要，且路径属于本机信息）。**键集与各自的既有契约一致**——统一的是
    实现，不是形状。
    """
    counts = proxy_counts or {}
    payload: dict[str, Any] = {
        "instance": report.instance,
        "owner": report.owner.value,
        "state": report.state.value,
        "state_corrupted": report.state_corrupted,
        # systemd 探测失败时的如实标记（v0.3.1）：owner 是"按 state.json 降级
        # 判定"的结果，脚本据此区分"真的没有 systemd"与"systemd 没应答"。
        "systemd_probe_error": report.systemd_probe_error,
        "pid": report.pid,
        "uptime_seconds": report.uptime_seconds,
    }
    if include_paths:
        payload["binary"] = str(report.binary) if report.binary else None
    payload["binary_version"] = report.binary_version
    payload["disk_version"] = report.disk_version
    if include_paths:
        payload["config"] = str(report.config) if report.config else None
        payload["config_mode"] = report.config_mode
    payload["listen"] = (
        None
        if report.listen is None
        else {"addr": report.listen.addr, "port": report.listen.port}
    )
    payload["systemd_unit"] = report.systemd_unit
    payload["systemd_main_pid"] = report.systemd_main_pid
    # v0.3.4：配置已改但未重启（CLI status 与 Web 横幅共用同一判据）
    payload["config_pending_restart"] = report.config_pending_restart
    payload["health"] = health_payload(report.health)
    if include_paths:
        payload["clients"] = clients
        payload["proxy_type_counts"] = counts
        payload["proxy_total"] = sum_proxy_types(counts) if counts else None
    payload["version_hint"] = report.version_hint
    if not include_paths:
        payload["dashboard"] = dashboard
    return payload


def doctor_payload(report: DoctorReport) -> dict[str, Any]:
    """`doctor --json` 与 Web 体检卡片的唯一形状。"""
    return {
        "instance": report.instance,
        "ok": report.ok,
        "counts": report.counts,
        "findings": [
            {
                "check": finding.check,
                "severity": finding.severity.value,
                "message": finding.message,
                "hint": finding.hint,
            }
            for finding in report.sorted_findings()
        ],
    }


def audit_summary_payload(summary: Any) -> dict[str, Any]:
    """审计统计形状（CLI `plugin audit stats` 与 Web 审计视图共用）。"""
    return {
        "total": summary.total,
        "allow": summary.allow,
        "deny": summary.deny,
        "suppressed_total": summary.suppressed_total,
        "bad_lines": summary.bad_lines,
        "first_at": summary.first_at,
        "last_at": summary.last_at,
        "by_user": summary.by_user,
        "by_op": summary.by_op,
        "elapsed_avg_ms": summary.elapsed_avg_ms,
        "elapsed_max_ms": summary.elapsed_max_ms,
        "elapsed_count": summary.elapsed_count,
    }
