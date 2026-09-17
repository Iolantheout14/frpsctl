"""Web 管理台的 JSON API（设计文档 §18.2）。

所有 handler 只调 `core/`，返回 `(status, payload)`；异常由 `dispatch` 统一
映射为 HTTP 状态码——错误消息与 CLI 走**同一套** `FrpsctlError.render()`，
机密不会出现在响应里（core 层已保证）。

变更类操作的语义与 CLI 完全一致（同一套 core 函数）：

| 操作 | core 入口 | 与 CLI 的对应 |
|------|-----------|--------------|
| start / stop / restart | `Lifecycle` | 同名命令（含健康门控） |
| 配置预览 / 应用 | `plan_set_many` + `apply_sets`（CAS） | `config set` 的多键形态 |
| 回滚 | `rollback_to` | `config rollback` |
| 清理离线记录 | `AdminClient.clear_offline_proxies` | `prune` |

**配置编辑的两段式交互**（预览 → 应用）是 Web 特有的安全设计：预览时服务端
在锁内取"当前文件"的快照与 diff（打码后下发），应用时以 `expected_current`
做 CAS——页面开着的时候配置文件被 CLI 改过，应用会被拒绝而不是覆盖。
未打码的配置原文从不离开服务端。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from ..core import config as cfg
from ..core.admin import AdminClient
from ..core.healthcheck import parse_dashboard
from ..core.instance import Instance
from ..core.lifecycle import HealthReport, Lifecycle, State
from ..core.lock import instance_lock
from ..core.logs import resolve_log_target, tail_lines
from ..core.transaction import apply_sets, rollback_to
from ..errors import (
    AdminUnreachable,
    ConfigError,
    ExitCode,
    FrpsctlError,
    UsageError,
)
from .auth import AuthManager

__all__ = ["WebContext", "dispatch", "http_status_for"]

#: 配置预览的存活时间：超过后必须重新预览（防"看着旧 diff 点应用"）。
PREVIEW_TTL = 600.0

#: 未消费的预览条目上限：已登录用户正常交互远小于它；上限防内存堆积。
MAX_PENDING_PREVIEWS = 32

#: 趋势端点最多查多少个代理（每个一次 dashboard 请求）。
TRAFFIC_MAX_PROXIES = 50

#: 日志接口一次最多返回的行数（防止把大日志一次性拉爆浏览器）。
MAX_LOG_LINES = 2000


@dataclass
class _Preview:
    expected_current: str
    changes: tuple[tuple[str, str], ...]
    created_at: float


@dataclass
class WebContext:
    """一个 Web 服务进程的共享上下文（可注入 clock 供测试控制 TTL）。"""

    inst: Instance
    auth: AuthManager
    clock: Callable[[], float] = time.monotonic
    previews: dict[str, _Preview] = field(default_factory=dict)
    preview_lock: threading.Lock = field(default_factory=threading.Lock)

    def prune_previews(self) -> None:
        """清理过期预览；超过上限时丢弃最旧的（防内存堆积）。"""
        now = self.clock()
        with self.preview_lock:
            for key in [
                key for key, item in self.previews.items() if now - item.created_at > PREVIEW_TTL
            ]:
                self.previews.pop(key, None)
            while len(self.previews) > MAX_PENDING_PREVIEWS:
                oldest = min(self.previews, key=lambda key: self.previews[key].created_at)
                self.previews.pop(oldest, None)


# ---------------------------------------------------------------------------
# 路由分发（server 只负责 HTTP 细节）
# ---------------------------------------------------------------------------


def dispatch(
    ctx: WebContext,
    *,
    method: str,
    path: str,
    query: dict[str, str],
    body: dict[str, Any],
    authenticated: bool,
) -> tuple[int, dict]:
    """把一次 API 调用分发给对应 handler，并统一映射异常。

    认证/CSRF 的**强制**由 server 层在调用本函数前完成（它管 HTTP 细节），
    这里只按路径分发（`POST /api/login` 是唯一免认证的入口）。
    """
    try:
        return _route(ctx, method=method, path=path, query=query, body=body, authenticated=authenticated)
    except FrpsctlError as exc:
        return http_status_for(exc), {
            "error": exc.message,
            "hint": exc.hint or "",
        }
    except (ValueError, TypeError) as exc:  # 请求体形状错误（如 changes 不是数组）
        return 400, {"error": f"请求格式错误：{exc}", "hint": ""}
    except Exception as exc:  # noqa: BLE001 - 兜底：不外泄细节，只报类型
        return 500, {"error": f"服务内部错误：{type(exc).__name__}", "hint": "查看 web 服务日志"}


def http_status_for(exc: FrpsctlError) -> int:
    """`FrpsctlError` → HTTP 状态码（语义对齐，脚本与前端都据此分支）。"""
    if isinstance(exc, AdminUnreachable):
        return 502  # 上游 dashboard 不可达/未鉴权
    code = exc.exit_code
    if code in (ExitCode.USAGE, ExitCode.CONFIG_INVALID, ExitCode.BINARY):
        return 400
    if code in (
        ExitCode.NOT_RUNNING,
        ExitCode.ALREADY_RUNNING,
        ExitCode.OWNERSHIP_CONFLICT,
    ):
        return 409
    if code == ExitCode.PERMISSION:
        return 403
    if code in (ExitCode.UNHEALTHY, ExitCode.ROLLED_BACK, ExitCode.STARTUP_FAILED):
        return 502
    if code == ExitCode.ADMIN_UNREACHABLE:
        return 502
    return 500


def _route(
    ctx: WebContext,
    *,
    method: str,
    path: str,
    query: dict[str, str],
    body: dict[str, Any],
    authenticated: bool,
) -> tuple[int, dict]:
    if method == "POST" and path == "/api/login":
        # 登录/登出由 server 层直接处理（涉及 Set-Cookie 的 HTTP 细节），
        # 走到这里说明 server 的路由与 api 的定义漂移了。
        return 500, {"error": "登录应由 server 层处理（路由漂移）"}
    if not authenticated:
        return 401, {"error": "未登录", "hint": "请先登录"}

    if method == "GET":
        if path == "/api/status":
            return 200, status_payload(ctx)
        if path == "/api/clients":
            return 200, clients_payload(ctx)
        if path == "/api/proxies":
            return 200, proxies_payload(ctx)
        if path == "/api/traffic":
            return 200, traffic_payload(ctx)
        if path == "/api/config":
            return 200, config_payload(ctx)
        if path == "/api/logs":
            return 200, logs_payload(ctx, query.get("lines"))
        return 404, {"error": f"未知接口：GET {path}"}

    if method == "POST":
        if path.startswith("/api/actions/"):
            return 200, action_payload(ctx, path.rsplit("/", 1)[-1], body)
        if path == "/api/config/preview":
            return 200, config_preview(ctx, body)
        if path == "/api/config/apply":
            return 200, config_apply(ctx, body)
        return 404, {"error": f"未知接口：POST {path}"}

    return 405, {"error": f"不支持的请求方法：{method}"}


# ---------------------------------------------------------------------------
# 只读
# ---------------------------------------------------------------------------


def _admin(ctx: WebContext) -> AdminClient:
    dash = parse_dashboard(ctx.inst.config)
    if not dash.enabled:
        raise AdminUnreachable(
            "dashboard 未启用（webServer.port = 0），无法读取统计",
            hint="在配置里设置 webServer.port",
        )
    return AdminClient(dash.base_url, dash.user, dash.password)


def _health(report: HealthReport | None) -> dict | None:
    if report is None:
        return None
    return {
        "l1_process": report.l1_process.value,
        "l2_control": report.l2_control.value,
        "l3_plugin": report.l3_plugin.value,
        "detail": report.detail,
        "gate": report.gate,
    }


def status_payload(ctx: WebContext) -> dict:
    """进程状态 + dashboard 统计（统计不可得时为 None，不影响状态本身）。"""
    report = Lifecycle(ctx.inst).status()
    payload: dict[str, Any] = {
        "instance": report.instance,
        "owner": report.owner.value,
        "state": report.state.value,
        "state_corrupted": report.state_corrupted,
        "pid": report.pid,
        "uptime_seconds": report.uptime_seconds,
        "binary_version": report.binary_version,
        "disk_version": report.disk_version,
        "listen": None
        if report.listen is None
        else {"addr": report.listen.addr, "port": report.listen.port},
        "systemd_unit": report.systemd_unit,
        "systemd_main_pid": report.systemd_main_pid,
        "health": _health(report.health),
        "version_hint": report.version_hint,
        "dashboard": None,
    }
    if report.state in (State.RUNNING, State.SYSTEMD_ACTIVE):
        try:
            admin = _admin(ctx)
            with admin:
                info = admin.server_info()
        except FrpsctlError:
            pass  # 统计拿不到不影响状态展示
        else:
            payload["dashboard"] = {
                "clients": info.client_counts,
                "proxy_type_counts": info.proxy_type_counts,
                "proxy_total": info.proxy_total,
                "cur_conns": info.cur_conns,
                "traffic_in": info.total_traffic_in,
                "traffic_out": info.total_traffic_out,
                "tls_force": info.tls_force,
                "version": info.version,
            }
    return payload


def clients_payload(ctx: WebContext) -> dict:
    admin = _admin(ctx)
    with admin:
        items = admin.list_clients()
    return {"clients": items}


def proxies_payload(ctx: WebContext) -> dict:
    admin = _admin(ctx)
    with admin:
        items = admin.list_proxies()
    return {"proxies": [asdict(item) for item in items]}


def traffic_payload(ctx: WebContext) -> dict:
    """逐代理的 7 天日粒度流量（趋势图数据源）。

    单个代理查询失败**不拖垮整张图**（记为无数据）——dashboard 半死不活时
    让整个趋势接口 502 没有意义。
    """
    admin = _admin(ctx)
    with admin:
        proxies = admin.list_proxies()[:TRAFFIC_MAX_PROXIES]
        series = []
        for item in proxies:
            try:
                history = admin.proxy_traffic(item.name)
            except FrpsctlError:
                history = []
            series.append({"name": item.name, "history": history})
    return {"granularity": "day", "proxies": series}


def _plain(value: Any) -> Any:
    """tomlkit 包装类型 → 可 JSON 化的原生值。"""
    return value.unwrap() if hasattr(value, "unwrap") else value


def config_payload(ctx: WebContext) -> dict:
    """配置树（打码后的值 + `masked` 标记）。**原文永不下发浏览器。**"""
    doc = cfg.load_config(ctx.inst.config)
    entries = []
    for key, value in cfg.flatten_tree(doc):
        secret = cfg.is_secret_key(key)
        entries.append(
            {
                "key": key,
                "value": cfg.mask_value(_plain(value)) if secret else _plain(value),
                "masked": secret,
            }
        )
    return {"entries": entries}


def logs_payload(ctx: WebContext, lines_raw: str | None) -> dict:
    try:
        lines = int(lines_raw) if lines_raw else 200
    except ValueError:
        lines = 200
    lines = max(1, min(lines, MAX_LOG_LINES))
    target = resolve_log_target(ctx.inst)
    return {"path": str(target), "lines": tail_lines(target, lines)}


# ---------------------------------------------------------------------------
# 变更
# ---------------------------------------------------------------------------


def action_payload(ctx: WebContext, action: str, body: dict[str, Any]) -> dict:
    lc = Lifecycle(ctx.inst)
    health_timeout = _float(body.get("health_timeout"), 10.0)

    if action == "start":
        report = lc.start(health_timeout=health_timeout)
        return {"pid": report.pid, "healthy": report.healthy, "health": _health(report.health)}
    if action == "stop":
        lc.stop(timeout=_float(body.get("timeout"), 10.0))
        return {"stopped": True}
    if action == "restart":
        report = lc.restart(health_timeout=health_timeout)
        return {"pid": report.pid, "healthy": report.healthy, "health": _health(report.health)}
    if action == "prune":
        # frp 没有强制下线在线代理的 API（DELETE /api/proxies 实为清理离线
        # 记录）——UI 的"清理离线记录"按钮走这里。
        admin = _admin(ctx)
        with admin:
            admin.clear_offline_proxies()
        return {"cleared": True}
    if action == "rollback":
        outcome = rollback_to(
            ctx.inst,
            steps=_int(body.get("steps"), 1),
            lifecycle=lc,
            restart=True,
            health_timeout=health_timeout,
        )
        return {
            "target": str(outcome.after),
            "restarted": outcome.restarted,
            "diff": cfg.mask_diff(outcome.diff),
        }
    raise UsageError(f"未知操作：{action!r}")


def _normalize_changes(raw: Any) -> list[tuple[str, str]]:
    """接受 `[[key, value], ...]` 或 `[{key, value}, ...]`，其余形状直接拒绝。"""
    if not isinstance(raw, list) or not raw:
        raise UsageError("changes 必须是非空数组")
    changes: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            changes.append((str(item[0]), str(item[1])))
        elif isinstance(item, dict) and "key" in item and "value" in item:
            changes.append((str(item["key"]), str(item["value"])))
        else:
            raise UsageError(f"无法识别的变更条目：{item!r}（应为 [key, value] 或 {{key, value}}）")
    return changes


def config_preview(ctx: WebContext, body: dict[str, Any]) -> dict:
    """锁内取当前快照 + 生成打码 diff，并登记一次待应用的预览。"""
    changes = _normalize_changes(body.get("changes"))
    with instance_lock(ctx.inst.lock):
        current = cfg.read_config_text(ctx.inst.config)
        plan = cfg.plan_set_many(ctx.inst.config, changes)

    preview_id = secrets.token_urlsafe(16)
    ctx.prune_previews()
    with ctx.preview_lock:
        ctx.previews[preview_id] = _Preview(
            expected_current=current,
            changes=tuple(changes),
            created_at=ctx.clock(),
        )

    keys = []
    for key, _raw in changes:
        secret = cfg.is_secret_key(key)
        before = plan.before.get(key)
        after = plan.after.get(key)
        keys.append(
            {
                "key": key,
                "before": cfg.mask_value(before) if secret else before,
                "after": cfg.mask_value(after) if secret else after,
            }
        )
    return {
        "preview_id": preview_id,
        "noop": plan.is_noop,
        "diff": cfg.mask_diff(plan.diff),
        "keys": keys,
    }


def config_apply(ctx: WebContext, body: dict[str, Any]) -> dict:
    """按预览应用（CAS）：预览之后文件被改过 → 拒绝而不是覆盖。"""
    preview_id = str(body.get("preview_id") or "")
    ctx.prune_previews()
    with ctx.preview_lock:
        preview = ctx.previews.pop(preview_id, None)
    if preview is None:
        raise ConfigError("预览已过期或不存在", hint="请重新提交变更并预览")

    outcome = apply_sets(
        ctx.inst,
        changes=preview.changes,
        expected_current=preview.expected_current,
        lifecycle=Lifecycle(ctx.inst),
        restart=True,
        health_timeout=10.0,
    )
    return {
        "applied": outcome.applied,
        "restarted": outcome.restarted,
        "noop": outcome.noop,
        "diff": cfg.mask_diff(outcome.diff),
        "plugin_warning": outcome.plugin_warning,
        "note": outcome.note,
    }


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
