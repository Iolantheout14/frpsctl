"""运维观测命令（doctor/prune/clients/proxies/traffic/instances）。"""

from __future__ import annotations

import contextlib
import time
import typer
from ... import capabilities as capabilities_module
from ...core import doctor as doc
from ...core.admin import (
    PROXY_TYPES,
    TRAFFIC_MAX_PROXIES,
    aggregate_days,
    fetch_histories,
    traffic_total,
)
from ...core.instance import list_instances
from ...core.lifecycle import Lifecycle, State
from ...errors import (
    FrpsctlError,
    UsageError,
)
from .. import ui

from ..app import app
from .. import runtime
from ... import report as report_mod
from .lifecycle import _status_payload


@app.command()
def doctor(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """体检：二进制 / 配置 / 权限 / 暴露面 / 端口 / 所有权 / 插件。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    report = doc.run_doctor(app_ctx.instance, binary=app_ctx.binary)

    if app_ctx.json:
        # 与 Web 体检卡片同一份形状（frpsctl.report.doctor_payload）
        ui.emit_json(report_mod.doctor_payload(report))
    else:
        for finding in report.sorted_findings():
            ui.emit(f"[{finding.severity.value:5}] {finding.check}: {finding.message}")
            if finding.hint:
                ui.emit(f"        ↳ {finding.hint}")
        counts = report.counts
        suffix = f"（WARN {counts['warn']} / INFO {counts['info']}）"
        ui.emit("")
        if report.ok:
            ui.emit(f"体检通过{suffix}")
        else:
            ui.emit(f"发现 {counts['error']} 个 ERROR{suffix}")

    if not report.ok:
        raise typer.Exit(1)


@app.command()
def prune(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """清理 dashboard 统计里的离线代理记录。

    ⚠️ frp **没有**强制下线在线代理的 API：`DELETE /api/proxies` 的实际语义是
    `ClearOfflineProxies()`（只接受 `?status=offline`），真机实测确认。要断开
    某个客户端请停掉它的 frpc（或在其侧下线代理）。

    此前存在的 `kick` 命令基于对该端点的误读，从未真正工作过，已由本命令取代。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    admin = runtime._require_admin(app_ctx, feature="清理离线代理记录")
    with admin:
        outcome = admin.prune_offline_proxies()
    if app_ctx.json:
        ui.emit_json(
            {
                "cleared": True,
                "count": outcome.cleared,
                "before": outcome.before,
                "exact": outcome.exact,
            }
        )
        return
    if outcome.exact:
        ui.emit(f"已清理 {outcome.cleared} 条离线代理记录（清理前 {outcome.before} 条）")
    else:
        # 列表翻页被上限截断时差值只是下界——降级必须可见
        ui.warn("⚠ 代理列表被截断，清理条数只是下界")
        ui.emit(f"已清理（至少 {outcome.cleared} 条；清理前至少 {outcome.before} 条）")


@app.command()
def clients(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """列出在线客户端（v2 Admin API，自动翻页取全量）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    admin = runtime._require_admin(app_ctx, feature="列出客户端")
    with admin:
        page = admin.page_clients()
    items = page.items

    if app_ctx.json:
        ui.emit_json({"clients": items, "total": page.total, "truncated": page.truncated})
        return
    if not items:
        ui.emit("没有在线客户端")
        return
    if page.truncated:
        ui.warn(f"⚠ 客户端列表被截断：服务端声明共 {page.total} 条，仅取回 {len(items)} 条")
    ui.emit(f"{'name':<28} {'user':<10} {'hostname':<20} {'online':<7} {'ip':<16} version")
    for item in items:
        ui.emit(
            f"{_text(item.get('key')):<28} {_text(item.get('user')):<10} "
            f"{_text(item.get('hostname')):<20} {str(bool(item.get('online'))):<7} "
            f"{_text(item.get('clientIP')):<16} {_text(item.get('version'))}"
        )
    ui.emit(f"共 {page.total} 条")


@app.command()
def proxies(
    ctx: typer.Context,
    ptype: str = typer.Option("", "--type", help="只看某类型（tcp/udp/http/https/stcp/xtcp/tcpmux/sudp）"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """列出代理（v2 Admin API，自动翻页取全量）。

    `--type` 的取值必须在 frp 支持的类型集合内：拼错类型名过去会静默返回空
    列表，用户以为"没有代理"——用法错误(2) 比一个空表诚实（ADR-7）。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    if ptype and ptype not in PROXY_TYPES:
        raise UsageError(
            f"未知代理类型：{ptype!r}",
            hint=f"合法类型：{'/'.join(sorted(PROXY_TYPES))}",
        )
    admin = runtime._require_admin(app_ctx, feature="列出代理")
    with admin:
        page = admin.page_proxies()
    items = page.items
    if ptype:
        items = [item for item in items if item.type == ptype]

    if app_ctx.json:
        ui.emit_json(
            {
                "proxies": [
                    {
                        "name": item.name,
                        "user": item.user,
                        "type": item.type,
                        "remote_port": item.remote_port,
                        "phase": item.phase,
                        "client_id": item.client_id,
                        "cur_conns": item.cur_conns,
                        "today_traffic_in": item.today_traffic_in,
                        "today_traffic_out": item.today_traffic_out,
                        "last_start_at": item.last_start_at,
                    }
                    for item in items
                ],
                "total": page.total,
                "truncated": page.truncated,
            }
        )
        return
    if not items:
        ui.emit("没有代理")
        return
    if page.truncated:
        ui.warn(f"⚠ 代理列表被截断：服务端声明共 {page.total} 条，仅取回 {len(page.items)} 条")
    header = (
        f"{'name':<28} {'user':<10} {'type':<7} {'port':<6} "
        f"{'phase':<8} {'up':<8} {'conns':<6} traffic(in/out)"
    )
    ui.emit(header)
    now = time.time()
    for item in items:
        port = str(item.remote_port) if item.remote_port else "-"
        uptime = (
            ui.human_duration(max(0.0, now - item.last_start_at)) if item.last_start_at else "-"
        )
        traffic = f"{ui.human_bytes(item.today_traffic_in)} / {ui.human_bytes(item.today_traffic_out)}"
        ui.emit(
            f"{item.name:<28} {item.user:<10} {item.type:<7} {port:<6} "
            f"{item.phase:<8} {uptime:<8} {item.cur_conns:<6} {traffic}"
        )


@app.command()
def traffic(
    ctx: typer.Context,
    name: str = typer.Argument("", help="只看某个代理（留空 = 全部代理逐日汇总）"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """代理流量历史（近 7 天，日粒度）。

    数据源与 Web 趋势图相同（v2 traffic 端点）。离线/已删除的代理按"无数据"
    处理（真机语义是 404），单个代理查询失败也不会拖垮整体。默认输出全部代理的
    **逐日汇总**；给出代理名则输出该代理明细。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    admin = runtime._require_admin(app_ctx, feature="读取流量历史")

    if name:
        with admin:
            history = admin.proxy_traffic(name)
        total = traffic_total(history)
        if app_ctx.json:
            ui.emit_json({"name": name, "history": history, "total": total})
            return
        if not history:
            ui.emit(f"没有 {name} 的流量记录（代理不存在、已离线或从未产生流量）")
            return
        ui.emit(f"代理 {name} 的流量（近 7 天）：")
        _render_traffic_rows(history)
        _render_traffic_total(total)
        return

    with admin:
        page = admin.page_proxies()
        truncated = page.total > TRAFFIC_MAX_PROXIES
        names = [item.name for item in page.items[:TRAFFIC_MAX_PROXIES]]
        fetched = fetch_histories(admin, names)

    series = fetched.series
    days = aggregate_days(series)
    if app_ctx.json:
        ui.emit_json(
            {
                "days": days,
                "proxies": len(series),
                "total": page.total,
                "truncated": truncated,
                # v0.3.1：整体预算内未取全时如实标记（与 Web 同字段）
                "partial": fetched.partial,
            }
        )
        return
    if not days:
        ui.emit("没有流量数据（没有代理，或全部代理都没有历史）")
        return
    if truncated:
        ui.warn(f"⚠ 代理数超过 {TRAFFIC_MAX_PROXIES}，仅统计前 {TRAFFIC_MAX_PROXIES} 个")
    if fetched.partial:
        ui.warn("⚠ 部分代理的流量历史未在预算内返回（汇总可能偏低）")
    ui.emit("全部代理的逐日流量（近 7 天）：")
    _render_traffic_rows(days)
    _render_traffic_total(traffic_total(days))


def _render_traffic_rows(points: list[dict]) -> None:
    ui.emit(f"{'date':<12}{'in':>12}{'out':>12}")
    for point in points:
        ui.emit(
            f"{point['date']:<12}"
            f"{ui.human_bytes(int(point['in'])):>12}"
            f"{ui.human_bytes(int(point['out'])):>12}"
        )


def _render_traffic_total(total: dict[str, int]) -> None:
    ui.emit(f"{'合计':<12}{ui.human_bytes(total['in']):>12}{ui.human_bytes(total['out']):>12}")


@app.command()
def instances(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    health: bool = typer.Option(
        False, "--health", help="同时做三层健康探测（每个运行中的实例一次网络往返）"
    ),
) -> None:
    """列出全部实例的一行式概览（多实例运维入口）。

    默认只读本地状态（owner / state / pid / 版本），不做网络探测；
    `--health` 会额外跑三层健康检查。逐实例用各自的配置与软链，
    不继承 `--binary`（巡检关心的是"每个实例现在什么情况"）。
    """
    from dataclasses import replace

    app_ctx = runtime._ctx(ctx).with_json(json_output)
    root = app_ctx.instance.instances_root
    found = list_instances(root, data_home=app_ctx.instance.data_home)

    def inspect(inst) -> object:
        lc = Lifecycle(inst)
        report = lc.status()
        if health and report.state in (State.RUNNING, State.SYSTEMD_ACTIVE):
            with contextlib.suppress(FrpsctlError):
                report = replace(
                    report, health=lc.check_health(expect_pid=report.systemd_main_pid)
                )
        return report

    # `--health` 是逐实例的网络探测：多实例巡检时串行会让总耗时线性叠加，
    # 结果顺序与输入顺序保持一致（`pool.map` 保证），输出因此稳定可比。
    if health and len(found) > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(4, len(found))) as pool:
            reports = list(pool.map(inspect, found))
    else:
        reports = [inspect(inst) for inst in found]

    if app_ctx.json:
        ui.emit_json({"instances": [_status_payload(report) for report in reports]})
        return
    if not reports:
        ui.emit(f"未找到任何实例（{root}）")
        ui.emit("用 `frpsctl init` 创建第一个实例。")
        return
    for report in reports:
        if report.state is State.RUNNING:
            state_text = f"RUNNING (pid {report.pid}, up {ui.human_duration(report.uptime_seconds)})"
        elif report.state is State.SYSTEMD_ACTIVE:
            state_text = f"SYSTEMD_ACTIVE (pid {report.systemd_main_pid})"
        else:
            state_text = report.state.value
        version_text = f"  frps {report.binary_version}" if report.binary_version else ""
        health_text = f"  {report.health.render()}" if report.health is not None else ""
        ui.emit(f"{report.instance:<16} {report.owner.value:<8} {state_text}{version_text}{health_text}")


def _text(value: object) -> str:
    """渲染用：None 显示为 "-"，其余 str()。"""
    return "-" if value is None or value == "" else str(value)


@app.command()
def capabilities(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """输出能力清单：命令 / 退出码 / 环境变量 / 版本门槛。

    `--json` 的形状是**从代码派生**的（命令树、`ExitCode`、`env.ENV_VARS`），
    供脚本与文档生成消费——README 的对应表格由同一份数据生成并对账。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    data = capabilities_module.payload()
    if app_ctx.json:
        ui.emit_json(data)
        return
    ui.emit(f"frpsctl {data['version']}（Python {data['python_requires']}，仅 Linux）")
    ui.emit(f"支持的 frps：最低 {data['frps_minimum']}，推荐 {data['frps_reckoned']}")
    ui.emit("")
    ui.emit(f"命令（{len(data['commands'])} 条）：")
    for path in data["commands"]:
        ui.emit(f"  {path}")
    ui.emit("")
    ui.emit("退出码：")
    for name, code in sorted(data["exit_codes"].items(), key=lambda item: item[1]):
        ui.emit(f"  {code:>2}  {name}")
    ui.emit("")
    ui.emit("环境变量：")
    for name, text in data["env_vars"].items():
        ui.emit(f"  {name:<24} {text}")
