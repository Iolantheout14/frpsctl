"""实例生命周期与观测命令（start/stop/restart/status/log）。"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
import typer
from ...core import config as cfg
from ...core import healthcheck
from ...core.admin import (
    sum_proxy_types,
)
from ...core.lifecycle import StartReport, State
from ...errors import (
    ConfigError,
    FrpsctlError,
    UnhealthyAfterStart,
)
from .. import ui
from ..context import AppContext

from ..app import app
from .. import runtime
from ... import report as report_mod


@app.command()
def start(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    foreground: bool = typer.Option(
        False,
        "--foreground",
        help="前台运行（调试用：不写 state、脱离本工具的托管，stop 管不到它）",
    ),
    health_timeout: float = typer.Option(
        10.0, "--health-timeout", min=0, help="健康检查等待秒数"
    ),
) -> None:
    """启动实例：verify → 加锁 → 派生 → 早退检测 → 写 state → 健康检查。

    健康 gate（L1 ∧ L2）未通过时退出码为 12，但进程仍被托管（`status`/`stop` 可用）。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    lc = runtime._lifecycle(app_ctx)

    # 落盘前先跑权威校验（§9 第 5 步的理念：把 verify 请到最前面）
    text = cfg.read_config_text(app_ctx.config_path)  # 不存在 → ConfigError(3) 而非 FileNotFoundError(1)
    cfg.validate_text(
        text,
        binary=lc.binary(),
        workdir=app_ctx.instance.dir,
        uses_unsafe=cfg.needs_unsafe_flag(text),
    )

    if foreground:
        ui.emit(f"前台运行 frps -c {app_ctx.config_path}（Ctrl-C 结束）")
        raise typer.Exit(
            subprocess.call([str(lc.binary()), "-c", str(app_ctx.config_path)], cwd=app_ctx.instance.dir)
        )

    tick = _health_tick(app_ctx)
    if tick is not None:
        ui.note(f"等待健康检查（最多 {health_timeout:g}s）…")
    try:
        report = lc.start(health_timeout=health_timeout, on_health_tick=tick)
    finally:
        ui.end_progress()
    if app_ctx.json:
        ui.emit_json(_start_payload(report))
    else:
        ui.emit(f"已启动：pid {report.pid}，frps {report.version}")
        ui.emit(f"health   : {report.health.render()}")
    _emit_health_warnings(report.health)
    _require_healthy(report)


@app.command()
def stop(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    force: bool = typer.Option(False, "--force", help="直接 SIGKILL（不做 SIGTERM 等待）"),
    timeout: float = typer.Option(10.0, "--timeout", min=0, help="SIGTERM 后等待秒数"),
) -> None:
    """停止实例。身份校验不通过时**拒绝**（退出码 11），绝不冒险 kill。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    report = runtime._lifecycle(app_ctx).stop(timeout=timeout, force=force)
    if app_ctx.json:
        ui.emit_json({"stopped": report.stopped})
    else:
        ui.emit("已停止")


@app.command()
def restart(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    timeout: float = typer.Option(10.0, "--timeout", min=0, help="停止等待秒数"),
    health_timeout: float = typer.Option(
        10.0, "--health-timeout", min=0, help="健康检查等待秒数"
    ),
) -> None:
    """重启实例（stop → start）。配置变更请用 `config set`，它会自动回滚。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    lc = runtime._lifecycle(app_ctx)
    tick = _health_tick(app_ctx)
    if tick is not None:
        ui.note(f"等待健康检查（最多 {health_timeout:g}s）…")
    try:
        report = lc.restart(timeout=timeout, health_timeout=health_timeout, on_health_tick=tick)
    finally:
        ui.end_progress()
    if app_ctx.json:
        ui.emit_json(_start_payload(report))
    else:
        ui.emit(f"已重启：pid {report.pid}，frps {report.version}")
        ui.emit(f"health   : {report.health.render()}")
    _emit_health_warnings(report.health)
    _require_healthy(report)


def _start_payload(report) -> dict:
    """start/restart 的 JSON 形状（0.3.0 起由 `frpsctl.report` 单点生成：

    CLI 与 Web 的 actions 返回同一个函数，health 里也因此统一带上了
    `gate` / `plugin_warning`——向后兼容的增字段）。"""
    return report_mod.start_payload(report)


def _health_tick(app_ctx: AppContext):
    """健康等待期的逐轮进度回调（`--json` 时返回 None）。

    进度只进 stderr：stdout 是机器可读契约；`--json` 下连进度都关掉——脚本
    可能把 stderr 一并收进日志，节奏性输出只会造成噪声。
    """
    if app_ctx.json:
        return None

    def tick(elapsed: float, report) -> None:
        ui.progress(
            f"等待健康检查 {elapsed:.0f}s："
            f"L1 {report.l1_process.value}  L2 {report.l2_control.value}  L3 {report.l3_plugin.value}"
        )

    return tick


def _emit_health_warnings(health) -> None:
    """L3 失败不改变退出码，但必须显著提示（§3.7）。

    只接收 health：告警文案与 --json 无关（即使 --json 也要走 stderr 告警，
    否则脚本会把"客户端登录不了"当成成功）。
    """
    warning = health.plugin_warning
    if warning:
        ui.warn(f"⚠ {warning}")


def _require_healthy(report: StartReport) -> None:
    """健康 gate（L1 ∧ L2）未通过时以退出码 12 收场（§3.7）。

    为什么必须非零：`start` 的语义是"服务可用"，而 gate 失败意味着控制面
    （dashboard / Admin API）不可达——status 的统计、prune、以及依赖它的
    运维动作全都取不到数。此前这条路径**完全静默**（退出码 0、stderr 无
    告警），脚本会把"dashboard 起不来"当成成功；这正是"降级必须可见"要防的。

    不适用于 L3：插件是独立进程、独立风险面，它的抖动不改变 gate 的定义，
    由 `_emit_health_warnings` 单独告警。

    进程**不会被清理**：未过 gate 的实例仍由本工具托管，`status` 可见、
    `stop` 可停。错误信息由 `UnhealthyAfterStart.render()` 统一渲染（stderr），
    stdout 的 --json 契约不受影响。
    """
    if report.healthy:
        return
    raise UnhealthyAfterStart(report.pid, report.health.render())


@app.command()
def status(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    watch: bool = typer.Option(False, "--watch", help="持续刷新"),
    # 负数会让 time.sleep 抛 ValueError → "未分类错误(1)"；0 则是忙循环。
    # 用 Click 的数值范围校验把它归入**用法错误(2)**（脚本据此区分"参数写错"）。
    interval: float = typer.Option(2.0, "--interval", min=0.1, help="--watch 的刷新间隔秒数"),
) -> None:
    """状态聚合：owner / 状态 / pid / 版本 / 运行时长 / 客户端 / 代理 / 流量 / 健康。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    if not watch:
        _print_status(app_ctx)
        return

    # `--watch` 是**持续刷新**，Ctrl-C 是它的正常退出方式，不是错误。
    # 这里刻意只在循环内渲染：以前写成"循环 + 循环后无条件再渲染一次"，于是
    # Ctrl-C 会多刷一屏（`except` 分支里的 `return` 让循环后的调用变成"退出前
    # 再打一次"），用户看到的是按了中断却又冒出一份状态。
    watch_lc = runtime._lifecycle(app_ctx)  # 复用实例 → owner 探测缓存生效
    try:
        while True:
            # 清屏只对**终端**有意义：重定向到文件/管道时写 ANSI 转义码会污染
            # 输出（`frpsctl status --watch > state.txt` 拿到一堆 \x1b[2J）。
            if not app_ctx.json and sys.stdout.isatty():
                ui.emit("\033[2J\033[H")
            # --watch --json 用**单行** JSON（NDJSON）：多行缩进格式在连续
            # 输出时无法被逐行消费（脚本会拿到一串无法解析的片段）。
            _print_status(app_ctx, compact=app_ctx.json, lc=watch_lc)
            # 持续刷新必须**逐轮 flush**：stdout 重定向到管道/文件时是块缓冲，
            # 不冲刷的话 `status --watch --json | jq` 会攒满 4KB 才吐数据——
            # "watch" 失去意义（v0.2.6 回归 review 与 audit tail 一并修复）。
            with contextlib.suppress(OSError):
                sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return


def _status_payload(
    report, *, client_count: int | None = None, proxy_counts: dict[str, int] | None = None
) -> dict:
    """`status` / `instances` 共用的 JSON 形状（含 `--watch` 的 NDJSON 行）。

    实现在 `frpsctl.report.status_payload`（CLI 与 Web 仪表盘共享同一份，
    消除"两处各拼一遍"的漂移面）。
    """
    return report_mod.status_payload(
        report, clients=client_count, proxy_counts=proxy_counts
    )


def _print_status(app_ctx: AppContext, *, compact: bool = False, lc=None) -> None:
    """渲染一次状态。`lc` 可由 `--watch` 循环传入以复用 owner 探测缓存
    （v0.3.0 review：每轮新建实例会让 2 秒 TTL 缓存永不命中）。"""
    lc = lc or runtime._lifecycle(app_ctx)
    report = lc.status()

    info = None
    client_count = None
    proxy_counts: dict[str, int] = {}
    if report.state is State.RUNNING:
        # 先取对象再判 None：`with None` 会直接 TypeError，而 dashboard 未启用
        # （webServer.port = 0）是**合法配置**，不是异常情况。status 必须永远能
        # 回答"现在什么情况"，不能因为没开 dashboard 就崩。
        client = runtime._admin(app_ctx)
        if client is not None:
            try:
                with client:
                    info = client.server_info()
                    client_count = info.client_counts
                    proxy_counts = info.proxy_type_counts
            except FrpsctlError:
                info = None

    if app_ctx.json:
        ui.emit_json(
            _status_payload(report, client_count=client_count, proxy_counts=proxy_counts),
            compact=compact,
        )
        return

    if report.state_corrupted:
        ui.warn(
            f"⚠ 状态文件已损坏：{app_ctx.instance.state}\n"
            "  无法判断进程归属，因此 stop/start/config set 都会拒绝执行。\n"
            "  请确认没有 frps 在跑，然后删除该文件。"
        )
    ui.emit(f"instance : {report.instance:<18} owner : {report.owner.value}")
    if report.systemd_unit:
        ui.emit(f"unit     : {report.systemd_unit} (MainPID {report.systemd_main_pid})")
    if report.state is State.RUNNING:
        uptime = ui.human_duration(report.uptime_seconds)
        ui.emit(f"state    : RUNNING (pid {report.pid}, up {uptime})")
    else:
        ui.emit(f"state    : {report.state.value}")

    if report.binary_version:
        shown = report.binary_version
        if report.disk_version and report.disk_version != report.binary_version:
            shown = f"{report.binary_version} (running) → {report.disk_version} (on disk, restart to apply)"
        ui.emit(f"binary   : frps {shown}")
    if report.config:
        ui.emit(f"config   : {report.config} ({report.config_mode})")
    if report.listen is not None:
        ui.emit(f"listen   : {report.listen.display}")

    dash = healthcheck.parse_dashboard(app_ctx.config_path)
    if dash.enabled:
        auth = "on" if dash.auth_enabled else "OFF"
        ui.emit(f"dashboard: {dash.addr}:{dash.port} (auth: {auth})")

    if report.health is not None:
        ui.emit(f"health   : {report.health.render()}")
    if report.version_hint:
        ui.warn(f"⚠ {report.version_hint}")
    if report.health is not None and report.health.plugin_warning:
        ui.warn(f"⚠ {report.health.plugin_warning}")

    if client_count is not None:
        ui.emit(f"clients  : {client_count} online")
    if proxy_counts:
        detail = "  ".join(f"{k}={v}" for k, v in sorted(proxy_counts.items()))
        ui.emit(f"proxies  : {detail}        total {sum_proxy_types(proxy_counts)}")
    if info is not None:
        ui.emit(
            f"traffic  : in {ui.human_bytes(info.total_traffic_in)} / "
            f"out {ui.human_bytes(info.total_traffic_out)}  (conns now {info.cur_conns})"
        )


@app.command()
def log(
    ctx: typer.Context,
    follow: bool = typer.Option(False, "--follow", "-f", help="持续跟踪"),
    lines: int = typer.Option(100, "--lines", "-n", min=0, help="显示行数（0 = 不显示历史）"),
) -> None:
    """看日志。优先 `log.to` 指向的文件；缺失时回退到 startup 日志（ADR-5）。

    **不提供 `--json`**：日志是流式文本，把它塞进 JSON 只会让 `-f` 失去意义。
    需要结构化日志请让 frp 自己输出（`log.to` 指向文件后用工具解析）。

    实现是纯 Python 的 tail（不依赖外部 `tail` 命令）：最小化容器里可能没有
    它，而缺失时的裸 `FileNotFoundError` 会被误报成"工具内部错误"。
    """
    app_ctx = runtime._ctx(ctx)
    inst = app_ctx.instance
    from ...core.logs import resolve_log_target

    target = resolve_log_target(inst)

    if not target.exists():
        # 不是"实例未运行"——实例可能在跑，只是配置里的 log.to 指向了别处。
        # 用 ConfigurationError 并**明确给出**解析出来的路径，否则用户不知道
        # 去哪儿找日志（G6：失败可诊断）。
        raise ConfigError(
            f"日志文件不存在：{target}",
            hint=("该路径来自配置的 log.to；若实例在运行，检查 log.to 是否是绝对路径或相对于实例目录"),
        )

    _tail_file(target, lines=lines, follow=follow)


def _tail_file(path: Path, *, lines: int, follow: bool) -> None:
    """纯 Python 的 tail：显示文件尾部的 `lines` 行，可选持续跟踪。"""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        if lines > 0:
            ring: deque[str] = deque(maxlen=lines)
            for line in handle:
                ring.append(line)
            sys.stdout.write("".join(ring))
            sys.stdout.flush()
        if not follow:
            return
        _follow_file(path, handle)


def _follow_file(path: Path, initial) -> None:
    """持续输出追加内容；文件被轮转（inode 变化）时自动重开。

    frp 的日志按天轮转（rename + 新建），`tail -f` 的语义就是"跟住路径"，
    而不是"跟住旧 inode"。这里用 stat 对比实现同样的语义。
    """
    handle = initial
    try:
        while True:
            chunk = handle.readline()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
                continue
            time.sleep(runtime.FOLLOW_INTERVAL)
            handle = runtime._reopen_if_rotated(path, handle)
    finally:
        with contextlib.suppress(OSError):
            handle.close()


