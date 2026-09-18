"""Prometheus 指标输出（`web serve --metrics` 开启，0.3.0）。

只暴露**便宜**指标：实例状态、三层健康、dashboard 统计（一次 `server_info`
调用）。审计统计这类需要全文件扫描的指标不放在抓取路径上——Prometheus 的
抓取频率（通常 15s）不该把管理台的磁盘/CPU 拖起来。

整个文本经 `WebContext.cache` 缓存 5 秒：多副本抓取与高频抓取都不会放大
dashboard 负载；数字的 5 秒滞后对监控用途无感。
"""

from __future__ import annotations

from .api import WebContext, _admin
from .auth import AuthManager
from ..core.lifecycle import Lifecycle, State
from ..errors import FrpsctlError

__all__ = ["render", "render_cached"]

#: 指标文本的服务端缓存时间（秒）。
METRICS_TTL = 5.0


def _escape(value: str) -> str:
    """Prometheus label 值转义（反斜杠、引号、换行）。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(ctx: WebContext) -> str:
    instance = _escape(ctx.inst.name)
    lines: list[str] = [
        "# HELP frpsctl_up 管理台进程存活（恒为 1）",
        "# TYPE frpsctl_up gauge",
        f'frpsctl_up{{instance="{instance}"}} 1',
        "# HELP frpsctl_instance_state 实例状态（label state 标记当前值）",
        "# TYPE frpsctl_instance_state gauge",
    ]
    report = Lifecycle(ctx.inst).status()
    lines.append(f'frpsctl_instance_state{{instance="{instance}",state="{report.state.value}"}} 1')
    if report.health is not None:
        lines.append("# HELP frpsctl_health 三层健康（label layer/status）")
        lines.append("# TYPE frpsctl_health gauge")
        for layer, value in (
            ("l1", report.health.l1_process),
            ("l2", report.health.l2_control),
            ("l3", report.health.l3_plugin),
        ):
            lines.append(
                f'frpsctl_health{{instance="{instance}",layer="{layer}",status="{value.value}"}} 1'
            )

    if report.state in (State.RUNNING, State.SYSTEMD_ACTIVE):
        try:
            admin = _admin(ctx)
            with admin:
                info = admin.server_info()
        except FrpsctlError:
            info = None  # 统计暂时拿不到：指标缺一段，不影响其余部分
        if info is not None:
            lines += [
                "# HELP frpsctl_dashboard_clients 在线客户端数",
                "# TYPE frpsctl_dashboard_clients gauge",
                f'frpsctl_dashboard_clients{{instance="{instance}"}} {info.client_counts}',
                "# HELP frpsctl_dashboard_proxies 代理总数",
                "# TYPE frpsctl_dashboard_proxies gauge",
                f'frpsctl_dashboard_proxies{{instance="{instance}"}} {info.proxy_total}',
                "# HELP frpsctl_dashboard_conns 当前连接数",
                "# TYPE frpsctl_dashboard_conns gauge",
                f'frpsctl_dashboard_conns{{instance="{instance}"}} {info.cur_conns}',
                "# HELP frpsctl_dashboard_traffic_in_bytes_total 累计入站字节",
                "# TYPE frpsctl_dashboard_traffic_in_bytes_total counter",
                f'frpsctl_dashboard_traffic_in_bytes_total{{instance="{instance}"}} '
                f"{info.total_traffic_in}",
                "# HELP frpsctl_dashboard_traffic_out_bytes_total 累计出站字节",
                "# TYPE frpsctl_dashboard_traffic_out_bytes_total counter",
                f'frpsctl_dashboard_traffic_out_bytes_total{{instance="{instance}"}} '
                f"{info.total_traffic_out}",
            ]
    return "\n".join(lines) + "\n"


def render_cached(ctx: WebContext) -> str:
    """带 5 秒缓存的指标文本（Prometheus 高频抓取不打 dashboard）。"""
    cached = ctx.cache.get("metrics", METRICS_TTL)
    if cached is not None:
        return cached
    text = render(ctx)
    ctx.cache.put("metrics", text)
    return text


def check_basic_auth(auth: AuthManager, header: str, *, source: str = "") -> bool:
    """`Authorization: Basic` 校验：用户名任意，口令 = 管理台口令。

    Prometheus 的 `basic_auth` 抓取配置用得上；口令比较走常量时间。
    不建会话，但**失败与登录共用同一张来源限速表**（v0.3.0 review 修复：
    否则 `/metrics` 会成为绕开登录限速的第二条口令爆破通道）。
    """
    import base64

    if auth.is_throttled(source):
        return False
    if not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8", "replace")
    except (ValueError, UnicodeDecodeError):
        auth.note_failure(source)
        return False
    _user, _, password = decoded.partition(":")
    ok = auth.check_password(password)
    if not ok:
        auth.note_failure(source)
    return ok
