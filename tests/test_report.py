"""响应形状的**完整键集基线**（0.3.0 验收标准）。

CLI 与 Web 的状态 / 启动 / 健康 / 体检 / 审计形状都由 `frpsctl.report` 单点
生成——形状在这里锁定一次，两个前端同时受保护：

- **完整键集**断言（不是子集）：脚本消费 `--json` 时对键名敏感，"少一个键"
  与"多一个键"都必须是有意变更；
- 键集变化时同步更新基线，并在 CHANGELOG 记录（新增字段是向后兼容的，
  删除/改名是破坏性的）；
- 两个分支（CLI 的 `include_paths=True` 与 Web 的 `False`）都要锁。
"""

from __future__ import annotations

from frpsctl.core.doctor import DoctorReport, Finding, Severity
from frpsctl.core.health import HealthLayer, HealthReport
from frpsctl.core.healthcheck import ListenInfo
from frpsctl.core.lifecycle import Owner, StartReport, State, StatusReport
from frpsctl.core.auditlog import AuditSummary
from frpsctl.core.version import Version
from frpsctl.report import (
    audit_summary_payload,
    doctor_payload,
    health_payload,
    start_payload,
    status_payload,
)

HEALTH_KEYS = {"l1_process", "l2_control", "l3_plugin", "detail", "gate", "plugin_warning"}
START_KEYS = {"pid", "version", "healthy", "health"}
STATUS_CLI_KEYS = {
    "instance",
    "owner",
    "state",
    "state_corrupted",
    "pid",
    "uptime_seconds",
    "binary",
    "binary_version",
    "disk_version",
    "config",
    "config_mode",
    "listen",
    "systemd_unit",
    "systemd_main_pid",
    "health",
    "clients",
    "proxy_type_counts",
    "proxy_total",
    "version_hint",
}
STATUS_WEB_KEYS = {
    "instance",
    "owner",
    "state",
    "state_corrupted",
    "pid",
    "uptime_seconds",
    "binary_version",
    "disk_version",
    "listen",
    "systemd_unit",
    "systemd_main_pid",
    "health",
    "version_hint",
    "dashboard",
}
DOCTOR_KEYS = {"instance", "ok", "counts", "findings"}
AUDIT_KEYS = {
    "total",
    "allow",
    "deny",
    "suppressed_total",
    "bad_lines",
    "first_at",
    "last_at",
    "by_user",
    "by_op",
    "elapsed_avg_ms",
    "elapsed_max_ms",
    "elapsed_count",
}


def _health() -> HealthReport:
    return HealthReport(
        l1_process=HealthLayer.OK,
        l2_control=HealthLayer.FAIL,
        l3_plugin=HealthLayer.FAIL,
        detail="x",
        ms=1.0,
    )


def _status(**overrides) -> StatusReport:
    base = {
        "instance": "t",
        "owner": Owner.DIRECT,
        "state": State.RUNNING,
        "pid": 42,
        "uptime_seconds": 1.5,
        "binary_version": "0.71.0",
        "disk_version": "0.71.0",
        "listen": ListenInfo(addr="0.0.0.0", port=7000),
        "systemd_unit": None,
        "systemd_main_pid": None,
        "health": _health(),
        "version_hint": None,
        "config_mode": "0600",
    }
    base.update(overrides)
    return StatusReport(**base)


def test_health_shape() -> None:
    payload = health_payload(_health())
    assert set(payload) == HEALTH_KEYS
    assert isinstance(payload["gate"], bool)
    assert payload["plugin_warning"] == "插件不可达：x —— 客户端将无法登录（fail-closed），请先恢复插件服务"
    assert health_payload(None) is None


def test_start_shape() -> None:
    report = StartReport(pid=7, version=Version(0, 71, 0), health=_health())
    payload = start_payload(report)
    assert set(payload) == START_KEYS
    assert payload["pid"] == 7 and payload["version"] == "0.71.0"
    assert set(payload["health"]) == HEALTH_KEYS


def test_status_cli_shape() -> None:
    """CLI / `instances` 分支：含 binary/config 路径与列表计数。"""
    payload = status_payload(_status(), clients=3, proxy_counts={"tcp": 2})
    assert set(payload) == STATUS_CLI_KEYS
    assert payload["proxy_total"] == 2
    assert payload["listen"] == {"addr": "0.0.0.0", "port": 7000}
    # 无计数时 proxy_total 为 None（而不是 0——"未知"与"零"不能混）
    assert status_payload(_status())["proxy_total"] is None


def test_status_web_shape() -> None:
    """Web 分支：不下发本机路径，改为 dashboard 统计块。"""
    dashboard = {"clients": 1, "proxy_total": 2}
    payload = status_payload(_status(), dashboard=dashboard, include_paths=False)
    assert set(payload) == STATUS_WEB_KEYS
    assert payload["dashboard"] == dashboard
    assert status_payload(_status(), include_paths=False)["dashboard"] is None


def test_doctor_shape() -> None:
    report = DoctorReport(
        instance="t", findings=[Finding("检查", Severity.WARN, "消息", "提示")]
    )
    payload = doctor_payload(report)
    assert set(payload) == DOCTOR_KEYS
    assert payload["counts"] == {"error": 0, "warn": 1, "info": 0}
    assert payload["ok"] is True
    assert set(payload["findings"][0]) == {"check", "severity", "message", "hint"}


def test_audit_shape() -> None:
    payload = audit_summary_payload(AuditSummary(total=2, allow=1, deny=1))
    assert set(payload) == AUDIT_KEYS
    assert payload["by_user"] == {} and payload["by_op"] == {}
    assert payload["elapsed_avg_ms"] == 0.0
