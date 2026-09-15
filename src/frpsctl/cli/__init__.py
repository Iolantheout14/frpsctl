"""frpsctl 命令层（设计文档 §5、§7）。

**薄层约束**：`cli/` 不直接调用 `subprocess` 或 `httpx`，只调用 `core/`；
`core/` 不打印任何东西。这条约束让 `--json` 与人读输出共享同一份逻辑，
也让"命令的行为"可以脱离终端被测试。
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import typer

from .. import __version__
from ..core import config as cfg
from ..core import doctor as doc
from ..core import healthcheck, release
from ..core import platform as plat
from ..core.admin import AdminClient, sum_proxy_types
from ..core.instance import Instance
from ..core.lifecycle import Lifecycle, Owner, State
from ..core.systemd import Systemd
from ..core.transaction import apply_change, rollback_to
from ..core.version import parse_version, upgrade_hint
from ..errors import (
    AlreadyRunning,
    ConfigError,
    FrpsctlError,
    NotRunning,
)
from . import ui
from .context import AppContext, build_context, run_cli

app = typer.Typer(
    add_completion=False,
    # 关掉 Click 的自动帮助：缺子命令属于**用法错误**，应当走退出码 2（§7.3），
    # 而不是打印帮助后退出 0 —— 那会让脚本误判为成功。
    no_args_is_help=False,
    help="把 frp 服务端（frps）包装成命令行工具：配置翻译器 + 进程保镖 + 状态聚合器。",
)
config_app = typer.Typer(no_args_is_help=True, help="配置读写与变更闭环。")
service_app = typer.Typer(no_args_is_help=True, help="systemd 集成。")
app.add_typer(config_app, name="config")
app.add_typer(service_app, name="service")


# ---------------------------------------------------------------------------
# 全局选项与上下文
# ---------------------------------------------------------------------------


def _show_version(value: bool) -> bool:
    """`--version` 的 eager 回调。

    `is_eager=True` 让它在 Click 校验"必须给出子命令"**之前**执行——否则
    `frpsctl --version` 会被当成缺子命令而报用法错误（退出码 2），
    一个纯查询动作却失败了。
    """
    if value:
        ui.emit(f"frpsctl {__version__}")
        raise typer.Exit(0)
    return value


@app.callback()
def _root(
    ctx: typer.Context,
    instance: str = typer.Option(
        None, "--instance", "-i", help="实例名（默认 default，可用 FRPSCTL_INSTANCE 覆盖）"
    ),
    root: Path = typer.Option(None, "--root", help="实例根目录（默认 ~/.local/share/frpsctl/instances）"),
    config: Path = typer.Option(None, "--config", help="直接指定配置文件（覆盖实例默认）"),
    binary: Path = typer.Option(None, "--binary", help="直接指定 frps 二进制"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过交互确认"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="详细输出"),
    version: bool = typer.Option(
        False, "--version", help="显示 frpsctl 版本", is_eager=True, callback=_show_version
    ),
) -> None:
    """解析全局选项。平台检查在这里做一次硬拒绝（§3.4）。"""
    plat.assert_supported()
    ctx.obj = build_context(
        instance=instance,
        root=root,
        config=config,
        binary=binary,
        json_output=json_output,
        yes=yes,
        verbose=verbose,
    )


def _ctx(ctx: typer.Context) -> AppContext:
    obj = ctx.find_root().obj
    assert isinstance(obj, AppContext)
    return obj


def _lifecycle(app_ctx: AppContext) -> Lifecycle:
    return Lifecycle(app_ctx.instance, binary=app_ctx.binary)


def _admin(app_ctx: AppContext) -> AdminClient | None:
    """构造 Admin 客户端；未启用 dashboard 时返回 None。"""
    dash = healthcheck.parse_dashboard(app_ctx.config_path)
    if not dash.enabled:
        return None
    password = app_ctx.admin_password or dash.password
    return AdminClient(dash.base_url, dash.user, password)


# ---------------------------------------------------------------------------
# install / init / verify
# ---------------------------------------------------------------------------


@app.command()
def install(
    ctx: typer.Context,
    version: str = typer.Option("0.71.0", "--version", help="要安装的 frps 版本"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    force: bool = typer.Option(False, "--force", help="已存在同版本时重新下载"),
    only_download: bool = typer.Option(False, "--only-download", help="只落盘，不切换软链（§8.6.1）"),
    insecure: bool = typer.Option(False, "--insecure", help="拿不到官方校验和时仍继续（风险自负）"),
) -> None:
    """下载官方 frps 二进制并强校验 sha256。

    低于 0.70.0 的版本会被直接拒绝——它们没有 v2 Admin API（§3.6）。
    """
    app_ctx = _ctx(ctx).with_json(json_output)
    app_ctx.instance.ensure_dirs()
    result = release.install(
        bin_dir=app_ctx.instance.bin_dir,
        version=version,
        insecure=insecure,
        force=force,
        switch=not only_download,
    )

    if app_ctx.json:
        ui.emit_json(
            {
                "version": result.version,
                "binary": str(result.binary),
                "downloaded": result.downloaded,
                "switched": result.switched,
                "active": str(app_ctx.instance.bin_link),
            }
        )
        return

    ui.emit(f"frps {result.version} → {result.binary}")
    if result.switched:
        ui.emit(f"当前版本软链 → {app_ctx.instance.bin_link}")
        ui.emit("")
        ui.emit("注意：换链**不影响正在运行的进程**（Linux 上可执行映像已绑定 inode），")
        ui.emit("      只影响下一次 start。运行 `frpsctl status` 可对比两个版本。")
        if Systemd(app_ctx.instance).is_active():
            ui.emit("      该实例由 systemd 托管：unit 的 ExecStart 写的是具体路径，需 systemctl restart。")
    else:
        ui.emit("已按 --only-download 落盘，未切换软链。")


@app.command()
def init(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的配置（会先备份）"),
    bind_port: int = typer.Option(7000, "--bind-port", help="控制端口 bindPort"),
    dashboard_port: int = typer.Option(7500, "--dashboard-port", help="dashboard 端口（0 = 不启用）"),
    allow_ports: str = typer.Option(
        "", "--allow-ports", help="端口白名单，如 6000-6100 或 6000,6001（留空 = 不限，不推荐）"
    ),
    no_input: bool = typer.Option(False, "--no-input", help="全部使用默认值，不交互"),
) -> None:
    """生成安全基线的入门配置（§10）。

    默认即为安全侧：强制随机口令、`tls.force = true`、引导设置 allowPorts。
    """
    app_ctx = _ctx(ctx).with_json(json_output)
    inst = app_ctx.instance
    target = app_ctx.config_path

    if target.exists() and not force:
        raise ConfigError(
            f"配置已存在：{target}",
            hint="确认要覆盖请加 --force（会先把当前配置备份到 config-history/）",
        )

    if not no_input and not app_ctx.yes:
        ui.emit("将生成一份安全基线的 frps 配置。直接回车使用方括号中的默认值。")
        ui.emit("")
        bind_port = typer.prompt("控制端口 bindPort", default=bind_port, type=int)
        dashboard_port = typer.prompt(
            "dashboard 端口（0 = 不启用，将失去状态聚合能力）", default=dashboard_port, type=int
        )
        allow_ports = typer.prompt(
            "允许客户端申请的端口段（如 6000-6100；留空 = 不限，不推荐）",
            default=allow_ports,
        )

    inst.ensure_dirs()
    if target.exists():
        from ..core.transaction import config_snapshot

        config_snapshot(inst, action="init --force", detail="覆盖前备份")

    token = secrets.token_urlsafe(32)[:32]
    dash_user = "admin"
    dash_password = secrets.token_urlsafe(24)[:24]
    ranges = _parse_port_ranges(allow_ports)

    text = _render_init_config(
        bind_port=bind_port,
        dashboard_port=dashboard_port,
        token=token,
        dash_user=dash_user,
        dash_password=dash_password,
        ranges=ranges,
    )
    cfg.atomic_write(target, text, mode=0o600)

    if app_ctx.json:
        ui.emit_json(
            {
                "config": str(target),
                "mode": "0600",
                "bindPort": bind_port,
                "webServer": {"port": dashboard_port, "user": dash_user},
                "auth": {"method": "token", "token": ui.mask_secret(token)},
            }
        )
        return

    ui.emit(f"已生成 {target}（权限 0600）")
    ui.emit("")
    ui.emit(f"  auth.token          {token}")
    ui.emit(f"  webServer.user      {dash_user}")
    ui.emit(f"  webServer.password  {dash_password}")
    ui.emit("")
    ui.emit("⚠ 上面三项只会显示这一次，配置里已写入（文件权限 0600）。")
    if not ranges:
        ui.emit("⚠ allowPorts 未设置：任何持有 token 的客户端都能申请任意端口。")
    ui.emit("")
    ui.emit(f"下一步：frpsctl verify && frpsctl start")


def _parse_port_ranges(spec: str) -> list[tuple[str, int, int]]:
    """把 `6000-6100,7000` 解析成 `[("range",6000,6100), ("single",7000,7000)]`。"""
    out: list[tuple[str, int, int]] = []
    for chunk in spec.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            start_text, _, end_text = chunk.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError:
                raise ConfigError(f"无法解析端口段：{chunk!r}") from None
            if not (1 <= start <= end <= 65535):
                raise ConfigError(f"端口段非法：{chunk!r}（需 1 ≤ start ≤ end ≤ 65535）")
            out.append(("range", start, end))
        else:
            try:
                port = int(chunk)
            except ValueError:
                raise ConfigError(f"无法解析端口：{chunk!r}") from None
            if not (1 <= port <= 65535):
                raise ConfigError(f"端口非法：{chunk!r}")
            out.append(("single", port, port))
    return out


def _render_init_config(
    *,
    bind_port: int,
    dashboard_port: int,
    token: str,
    dash_user: str,
    dash_password: str,
    ranges: list[tuple[str, int, int]],
) -> str:
    """渲染入门配置。每条安全项旁边写明它为什么在那儿（§10）。

    ⚠️ **TOML 布局纪律**：`allowPorts` 是顶层键，必须写在**任何 `[table]` 之前**。
    一旦落到某个表头之后，它就变成那张表的子键（`log.allowPorts`），而 frps 的
    报错会是极具误导性的 `unknown field "allowPorts"`——看起来像键名写错，
    实际是位置错了。因此这里先输出全部顶层键，再输出各表。
    """
    lines = [
        "# frps 配置 —— 由 frpsctl init 生成",
        "# 权限已设为 0600：文件内含 auth.token 与 dashboard 口令。",
        "",
        "# ── 顶层键（必须写在任何 [table] 之前）─────────────────────",
        'bindAddr = "0.0.0.0"',
        f"bindPort = {bind_port}",
        "",
        "# 单客户端可申请的端口数上限。frp 默认为 0（不限），",
        "# 不设的话单个客户端就能把端口耗尽。",
        "maxPortsPerClient = 20",
    ]

    if ranges:
        lines += [
            "",
            "# 端口白名单：只允许客户端申请这些端口 / 端口段。",
            "# 不设的话，任何持有 token 的客户端都能申请任意端口。",
            "allowPorts = [",
        ]
        for kind, start, end in ranges:
            if kind == "single":
                lines.append(f"  {{ single = {start} }},")
            else:
                lines.append(f"  {{ start = {start}, end = {end} }},")
        lines.append("]")

    lines += [
        "",
        "[auth]",
        "# 为空表示不校验客户端 token（等于没有客户端鉴权）。",
        f'token = "{token}"',
        "",
        "[webServer]",
        "# dashboard 只有 Basic Auth 一层防护，因此默认只监听本机。",
        "# 改成 0.0.0.0 之前请确认口令已设。",
        'addr = "127.0.0.1"',
        f"port = {dashboard_port}",
        f'user = "{dash_user}"',
        f'password = "{dash_password}"',
        "",
        "[transport.tls]",
        "# 拒绝明文 frpc 连接（frp 默认是 false）。",
        "force = true",
        "",
        "[log]",
        'to = "./frps.log"',
        'level = "info"',
        "maxDays = 7",
    ]
    return "\n".join(lines) + "\n"


@app.command()
def verify(
    ctx: typer.Context,
    file: Path = typer.Option(None, "--file", help="要校验的文件（默认实例配置）"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """双保险校验：pydantic 语义 + 官方 `frps verify`。

    用临时副本，**不动线上文件**。
    """
    app_ctx = _ctx(ctx).with_json(json_output)
    target = file or app_ctx.config_path
    lc = _lifecycle(app_ctx)
    version = lc.binary_version()
    binary = lc.binary()

    text = target.read_text("utf-8")
    uses_unsafe = _config_uses_unsafe(text)
    cfg.validate_text(
        text, binary=binary, version=version, workdir=app_ctx.instance.dir, uses_unsafe=uses_unsafe
    )
    flags = " ".join(cfg.config_flags(version, uses_exec_token_source=uses_unsafe))
    if app_ctx.json:
        ui.emit_json({"file": str(target), "ok": True, "flags": flags})
    else:
        ui.emit(f"{target} 校验通过（frps {version}，标志：{flags}）")


def _config_uses_unsafe(text: str) -> bool:
    import tomllib

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    source = (data.get("auth") or {}).get("tokenSource") or {}
    return str(source.get("type", "")).lower() == "exec"


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


@app.command()
def start(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    foreground: bool = typer.Option(False, "--foreground", help="前台运行（不派生）"),
    health_timeout: float = typer.Option(10.0, "--health-timeout", help="健康检查等待秒数"),
) -> None:
    """启动实例：verify → 加锁 → 派生 → 早退检测 → 写 state → 健康检查。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    lc = _lifecycle(app_ctx)

    # 落盘前先跑权威校验（§9 第 5 步的理念：把 verify 请到最前面）
    text = app_ctx.config_path.read_text("utf-8")
    cfg.validate_text(
        text,
        binary=lc.binary(),
        version=lc.binary_version(),
        workdir=app_ctx.instance.dir,
        uses_unsafe=_config_uses_unsafe(text),
    )

    if foreground:
        ui.emit(f"前台运行 frps -c {app_ctx.config_path}（Ctrl-C 结束）")
        raise typer.Exit(
            subprocess.call([str(lc.binary()), "-c", str(app_ctx.config_path)], cwd=app_ctx.instance.dir)
        )

    report = lc.start(health_timeout=health_timeout)
    if app_ctx.json:
        ui.emit_json(_start_payload(report))
    else:
        ui.emit(f"已启动：pid {report.pid}，frps {report.version}")
        ui.emit(f"health   : {report.health.render()}")
    _emit_health_warnings(report.health, app_ctx)


@app.command()
def stop(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    force: bool = typer.Option(False, "--force", help="直接 SIGKILL（不做 SIGTERM 等待）"),
    timeout: float = typer.Option(10.0, "--timeout", help="SIGTERM 后等待秒数"),
) -> None:
    """停止实例。身份校验不通过时**拒绝**（退出码 11），绝不冒险 kill。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    report = _lifecycle(app_ctx).stop(timeout=timeout, force=force)
    if app_ctx.json:
        ui.emit_json({"stopped": report.stopped})
    else:
        ui.emit("已停止")


@app.command()
def restart(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    timeout: float = typer.Option(10.0, "--timeout", help="停止等待秒数"),
    health_timeout: float = typer.Option(10.0, "--health-timeout", help="健康检查等待秒数"),
) -> None:
    """重启实例（stop → start）。配置变更请用 `config set`，它会自动回滚。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    lc = _lifecycle(app_ctx)
    try:
        report = lc.restart(timeout=timeout, health_timeout=health_timeout)
    except AlreadyRunning:
        raise
    if app_ctx.json:
        ui.emit_json(_start_payload(report))
    else:
        ui.emit(f"已重启：pid {report.pid}，frps {report.version}")
        ui.emit(f"health   : {report.health.render()}")
    _emit_health_warnings(report.health, app_ctx)


def _start_payload(report) -> dict:
    return {
        "pid": report.pid,
        "version": str(report.version),
        "healthy": report.healthy,
        "health": {
            "l1_process": report.health.l1_process.value,
            "l2_control": report.health.l2_control.value,
            "l3_plugin": report.health.l3_plugin.value,
            "detail": report.health.detail,
        },
    }


def _emit_health_warnings(health, app_ctx: AppContext) -> None:
    """L3 失败不改变退出码，但必须显著提示（§3.7）。"""
    warning = health.plugin_warning
    if warning:
        ui.warn(f"⚠ {warning}")


@app.command()
def status(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    watch: bool = typer.Option(False, "--watch", help="持续刷新"),
    interval: float = typer.Option(2.0, "--interval", help="--watch 的刷新间隔秒数"),
) -> None:
    """状态聚合：owner / 状态 / pid / 版本 / 运行时长 / 客户端 / 代理 / 流量 / 健康。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    if watch:
        try:
            while True:
                if not app_ctx.json:
                    ui.emit("\033[2J\033[H")
                _print_status(app_ctx)
                time.sleep(interval)
        except KeyboardInterrupt:
            return
    _print_status(app_ctx)


def _print_status(app_ctx: AppContext) -> None:
    lc = _lifecycle(app_ctx)
    report = lc.status()

    info = None
    client_count = None
    proxy_counts: dict[str, int] = {}
    if report.state is State.RUNNING:
        try:
            with _admin(app_ctx) as client:
                if client is not None:
                    info = client.server_info()
                    client_count = info.client_counts
                    proxy_counts = info.proxy_type_counts
        except FrpsctlError:
            info = None

    if app_ctx.json:
        ui.emit_json(
            {
                "instance": report.instance,
                "owner": report.owner.value,
                "state": report.state.value,
                "pid": report.pid,
                "uptime_seconds": report.uptime_seconds,
                "binary": str(report.binary) if report.binary else None,
                "binary_version": report.binary_version,
                "disk_version": report.disk_version,
                "config": str(report.config) if report.config else None,
                "config_mode": report.config_mode,
                "health": None
                if report.health is None
                else {
                    "l1_process": report.health.l1_process.value,
                    "l2_control": report.health.l2_control.value,
                    "l3_plugin": report.health.l3_plugin.value,
                    "detail": report.health.detail,
                },
                "clients": client_count,
                "proxy_type_counts": proxy_counts,
                "proxy_total": sum_proxy_types(proxy_counts) if proxy_counts else None,
                "version_hint": report.version_hint,
            }
        )
        return

    ui.emit(f"instance : {report.instance:<18} owner : {report.owner.value}")
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
    lines: int = typer.Option(100, "--lines", "-n", help="显示行数"),
) -> None:
    """看日志。优先 `log.to` 指向的文件；缺失时回退到 startup 日志（ADR-5）。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    inst = app_ctx.instance
    target = _resolve_log_target(inst, app_ctx.config_path)

    if not target.exists():
        raise NotRunning(f"{inst.name}（日志文件不存在：{target}）")

    argv = ["tail", "-n", str(lines)]
    if follow:
        argv.append("-f")
    argv.append(str(target))
    raise typer.Exit(subprocess.call(argv))


def _resolve_log_target(inst: Instance, config_path: Path) -> Path:
    """从配置的 `log.to` 解析日志路径；为 `console` 或缺失时回退。"""
    import tomllib

    try:
        data = tomllib.loads(config_path.read_text("utf-8"))
    except Exception:
        data = {}
    to = (data.get("log") or {}).get("to")
    if isinstance(to, str) and to and to.lower() != "console":
        candidate = Path(to)
        return candidate if candidate.is_absolute() else (config_path.parent / candidate)
    latest = inst.latest_startup_log()
    return latest or inst.log_file


# ---------------------------------------------------------------------------
# config 子命令
# ---------------------------------------------------------------------------


@config_app.command("get")
def config_get(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="点分键，如 transport.tls.force"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    reveal: bool = typer.Option(False, "--reveal", help="显示敏感值（默认打码）"),
) -> None:
    """读单个键。敏感键默认打码（§10 硬约束 2）。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    doc = cfg.load_config(app_ctx.config_path)
    value = cfg.get_value(doc, key)
    secret = cfg.is_secret_key(key)

    if app_ctx.json:
        ui.emit_json({"key": key, "value": value if (reveal or not secret) else "***"})
    elif secret:
        ui.emit(ui.mask_secret(value, reveal=reveal))
    else:
        ui.emit(str(value))


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="点分键，如 bindPort"),
    value: str = typer.Argument(..., help="新值"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    no_restart: bool = typer.Option(False, "--no-restart", help="只写不重启（变更尚未生效）"),
    health_timeout: float = typer.Option(10.0, "--health-timeout", help="健康检查等待秒数"),
) -> None:
    """写单个键，走 §9 事务闭环（校验 → 备份 → 原子替换 → 重启 → 失败回滚）。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    lc = _lifecycle(app_ctx)

    # 2. 内存定点补丁（线上文件此刻未被触碰）
    plan = cfg.plan_set(app_ctx.config_path, key, value)
    if plan.is_noop:
        ui.emit(f"{key} 已经是 {plan.after}，无需变更")
        return

    outcome = apply_change(
        app_ctx.instance,
        dotted=key,
        new_text=plan.text,
        change_diff=plan.diff,
        before=plan.before,
        after=plan.after,
        lifecycle=lc,
        restart=not no_restart,
        health_timeout=health_timeout,
    )

    if app_ctx.json:
        ui.emit_json(
            {
                "key": outcome.dotted,
                "before": ui.mask_secret(outcome.before) if cfg.is_secret_key(key) else outcome.before,
                "after": ui.mask_secret(outcome.after) if cfg.is_secret_key(key) else outcome.after,
                "applied": outcome.applied,
                "restarted": outcome.restarted,
                "note": outcome.note,
            }
        )
        return

    ui.emit(outcome.diff.rstrip() or "(无文本差异)")
    ui.emit("")
    if outcome.note:
        ui.emit(f"✓ {outcome.note}")
    elif outcome.restarted:
        ui.emit("✓ 已写入并重启，健康检查通过")
    if outcome.plugin_warning:
        ui.warn(f"⚠ {outcome.plugin_warning}")


@config_app.command("edit")
def config_edit(
    ctx: typer.Context,
    health_timeout: float = typer.Option(10.0, "--health-timeout", help="健康检查等待秒数"),
) -> None:
    """用 $EDITOR 编辑，保存后走完全相同的闭环（先展示 diff 让人确认）。"""
    import tempfile

    app_ctx = _ctx(ctx).with_json(json_output)
    lc = _lifecycle(app_ctx)
    original = app_ctx.config_path.read_text("utf-8")

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8") as handle:
        handle.write(original)
        draft_path = Path(handle.name)
    try:
        code = subprocess.call([editor, str(draft_path)])
        if code != 0:
            raise ConfigError(f"编辑器退出码 {code}，放弃变更")
        draft = draft_path.read_text("utf-8")
    finally:
        draft_path.unlink(missing_ok=True)

    if draft == original:
        ui.emit("没有改动")
        return

    diff = cfg.diff_texts(original, draft, app_ctx.config_path.name)
    ui.emit(diff.rstrip())
    ui.emit("")
    if not app_ctx.yes and not typer.confirm("应用以上改动并重启？", default=True):
        ui.emit("已放弃")
        return

    outcome = apply_change(
        app_ctx.instance,
        dotted="(edit)",
        new_text=draft,
        change_diff=diff,
        before="(edited)",
        after="(edited)",
        lifecycle=lc,
        restart=True,
        health_timeout=health_timeout,
    )
    if outcome.note:
        ui.emit(f"✓ {outcome.note}")
    else:
        ui.emit("✓ 已写入并重启，健康检查通过")


@config_app.command("diff")
def config_diff(
    ctx: typer.Context,
    steps: int = typer.Option(1, "--steps", help="与第 N 新的快照比较"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """当前配置 vs 历史快照（unified diff）。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    entries = app_ctx.instance.history_entries()
    if not entries:
        raise ConfigError("没有配置快照", hint="快照在每次 config set / edit 时自动创建")
    index = max(0, steps - 1)
    if index >= len(entries):
        raise ConfigError(f"只找到 {len(entries)} 份快照")
    snapshot = entries[index] / "frps.toml"
    if not snapshot.exists():
        raise ConfigError(f"快照不完整：{snapshot}")
    diff = cfg.diff_texts(
        snapshot.read_text("utf-8"), app_ctx.config_path.read_text("utf-8"), "frps.toml"
    )
    if app_ctx.json:
        ui.emit_json({"snapshot": str(snapshot.parent), "diff": diff})
    else:
        ui.emit(diff.rstrip() or "(无差异)")
        ui.emit("")
        ui.emit(f"# 快照：{snapshot.parent.name}")


@config_app.command("rollback")
def config_rollback(
    ctx: typer.Context,
    steps: int = typer.Argument(1, help="回滚到 N 份之前的快照"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    health_timeout: float = typer.Option(10.0, "--health-timeout", help="健康检查等待秒数"),
) -> None:
    """回滚到 N 份之前。**复用同一闭环**，而不是简单 cp 覆盖。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    outcome = rollback_to(
        app_ctx.instance,
        steps=steps,
        lifecycle=_lifecycle(app_ctx),
        restart=True,
        health_timeout=health_timeout,
    )
    if app_ctx.json:
        ui.emit_json(
            {"target": outcome.after, "restarted": outcome.restarted, "diff": outcome.diff}
        )
        return
    ui.emit(outcome.diff.rstrip() or "(无文本差异)")
    ui.emit("")
    if outcome.restarted:
        ui.emit(f"✓ 已回滚到 {outcome.after} 并重启，健康检查通过")
    else:
        ui.emit(f"✓ 已回滚到 {outcome.after}（实例未运行，配置已就绪，start 后生效）")


# ---------------------------------------------------------------------------
# service / doctor / kick
# ---------------------------------------------------------------------------


@service_app.command("install")
def service_install(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的 unit 模板"),
    log_dir: Path = typer.Option(Path("/var/log/frps"), "--log-dir", help="unit 中 ReadWritePaths 的目录"),
) -> None:
    """安装 `frps@.service` 模板并 enable（需要 root）。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    lc = _lifecycle(app_ctx)
    systemd = Systemd(app_ctx.instance)
    path = systemd.install_template(binary=lc.binary(), log_dir=log_dir, force=force)
    if app_ctx.json:
        ui.emit_json({"unit": systemd.unit_name, "template": str(path), "owner": "systemd"})
    else:
        ui.emit(f"已安装 {path}")
        ui.emit(f"实例 unit：{systemd.unit_name}")
        ui.emit("")
        ui.emit("注意：unit 的 ExecStart 写的是**具体二进制路径**，")
        ui.emit("      因此 `frpsctl install` 换版本后需要 `systemctl restart` 才生效。")


@service_app.command("uninstall")
def service_uninstall(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """停用并删除 unit 模板（需要 root）。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    systemd = Systemd(app_ctx.instance)
    systemd.uninstall()
    if app_ctx.json:
        ui.emit_json({"unit": systemd.unit_name, "removed": True})
    else:
        ui.emit(f"已停用并移除 {systemd.unit_name}")


@service_app.command("status")
def service_status(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """显示 systemd 托管状态。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    systemd = Systemd(app_ctx.instance)
    active = systemd.is_active()
    pid = systemd.main_pid() if active else None
    if app_ctx.json:
        ui.emit_json({"unit": systemd.unit_name, "active": active, "main_pid": pid})
    else:
        ui.emit(f"unit     : {systemd.unit_name}")
        ui.emit(f"active   : {active}")
        if pid:
            ui.emit(f"main pid : {pid}")


@app.command()
def doctor(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """体检：二进制 / 配置 / 权限 / 暴露面 / 端口 / 所有权 / 插件。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    report = doc.run_doctor(app_ctx.instance, binary=app_ctx.binary)

    if app_ctx.json:
        ui.emit_json(
            {
                "instance": report.instance,
                "ok": report.ok,
                "findings": [
                    {
                        "check": f.check,
                        "severity": f.severity.value,
                        "message": f.message,
                        "hint": f.hint,
                    }
                    for f in report.sorted_findings()
                ],
            }
        )
    else:
        for finding in report.sorted_findings():
            ui.emit(f"[{finding.severity.value:5}] {finding.check}: {finding.message}")
            if finding.hint:
                ui.emit(f"        ↳ {finding.hint}")
        ui.emit("")
        ui.emit("体检通过" if report.ok else f"发现 {len(report.errors)} 个 ERROR")

    if not report.ok:
        raise typer.Exit(1)


@app.command()
def kick(
    ctx: typer.Context,
    proxy_name: str = typer.Argument(..., help="要下线的代理名"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """下线指定代理（`DELETE /api/proxies`）。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    with _admin(app_ctx) as client:
        if client is None:
            raise ConfigError(
                "dashboard 未启用（webServer.port = 0），无法下线代理",
                hint="kick 依赖 Admin API；请设置 webServer.port",
            )
        client.kick(proxy_name)
    if app_ctx.json:
        ui.emit_json({"kicked": proxy_name})
    else:
        ui.emit(f"已下线代理 {proxy_name}")


def main() -> None:
    """控制台入口（pyproject 的 `frpsctl` 指向这里）。"""
    run_cli(app)


if __name__ == "__main__":
    main()
