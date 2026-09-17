"""frpsctl 命令层（设计文档 §5、§7）。

**薄层约束**：`cli/` 不直接调用 `subprocess` 或 `httpx`，只调用 `core/`；
`core/` 不打印任何东西。这条约束让 `--json` 与人读输出共享同一份逻辑，
也让"命令的行为"可以脱离终端被测试。
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import typer

from .. import __version__
from ..core import config as cfg
from ..core import doctor as doc
from ..core import healthcheck, release
from ..core import platform as plat
from ..core.admin import AdminClient, sum_proxy_types
from ..core.instance import list_instances
from ..core.lifecycle import Lifecycle, StartReport, State
from ..core.lock import instance_lock
from ..core.systemd import DEFAULT_SERVICE_USER, PluginService, Systemd, WebService
from ..core.transaction import apply_edit, apply_set, rollback_to
from ..core.version import RECKONED_VERSION
from ..plugin.policy import PluginPolicy
from ..plugin.server import PluginServer, ServerSettings
from ..web import WebServer, WebSettings, build_web_context
from ..errors import (
    AdminUnreachable,
    ConfigError,
    ConfigKeyMissing,
    FrpsctlError,
    UnhealthyAfterStart,
    UsageError,
)
from . import ui
from .context import AppContext, build_context, run_cli

#: 只接受值的全局选项（`--name value` 或 `--name=value`）。
#: 刻意**不含 `-v`**：那个短选项与 `log`/`status` 的 `-n`/`--interval` 之类局部
#: 选项挤在一起，盲目前移会把子命令自己的参数搬到错误的位置。长名 `--verbose`
#: 不冲突，因此短写只支持写在子命令之前（与 frps 的 `-v` 是两回事）。
_GLOBAL_VALUE_FLAGS = frozenset({"--instance", "-i", "--root", "--config", "--binary", "--admin-password"})
#: 开关型全局选项（不带值）。
_GLOBAL_BOOL_FLAGS = frozenset({"--json", "--yes", "-y", "--verbose"})


class _AnywhereGroup(typer.core.TyperGroup):
    """让**全局选项出现在子命令之后**也能被识别。

    为什么必须做这件事：Typer/Click 原生只解析"子命令之前"的选项，而
    `frpsctl status --json` 这种写法既符合 README 的命令表，也符合所有人的直觉。
    早先的绕法是给**每一条**子命令手工加一个同名的 `--json` 选项再在 `with_json()`
    里做 OR —— 补了 19 处，于是 `--yes` / `--verbose` 写在后面仍然报
    `No such option: --yes`，而 README 却写着"全局选项写在子命令前后都可以"。
    逐命令打补丁是治标的：每加一条子命令、每加一个全局选项，都可能再漏一次。

    治本的做法是在**解析之前**把散落在子命令后面的全局选项整体搬到前面。这样只有
    这一处需要维护，且与子命令自身选项的优先级关系保持直观：

    - `frpsctl status --json` → `frpsctl --json status`
    - `frpsctl --instance web start` 原样不动（已经在前面）
    - 局部选项（`start --health-timeout 3`）不匹配全局名单，位置不变
    - `frpsctl install --version 0.71.0`：`--version` 不在名单里，因此仍归 `install`，
      不会被误当成根命令的 `--version`
    """

    def parse_args(self, ctx, args):  # noqa: ANN001, ANN201 - Click 的接口签名
        hoisted: list[str] = []
        rest: list[str] = []
        index = 0
        # `--` 之后的一切都是**位置参数**，绝不能再前移：用户显式声明了
        # "后面的 --json 是值而不是选项"（例如 `config set k -- --json`）。
        # 不遵守它会把值搬走，参数解析直接错位成 MissingParameter。
        terminated = False
        while index < len(args):
            item = args[index]
            if terminated:
                rest.append(item)
                index += 1
                continue
            if item == "--":
                rest.append(item)
                index += 1
                terminated = True
                continue
            if item in _GLOBAL_BOOL_FLAGS:
                hoisted.append(item)
                index += 1
                continue
            if item in _GLOBAL_VALUE_FLAGS:
                # 值紧随其后时一起搬；`--name=value` 形式已在下一个分支处理
                if index + 1 < len(args):
                    hoisted.extend([item, args[index + 1]])
                    index += 2
                else:
                    rest.append(item)
                    index += 1
                continue
            if any(item.startswith(f"{flag}=") for flag in _GLOBAL_VALUE_FLAGS):
                hoisted.append(item)
                index += 1
                continue
            rest.append(item)
            index += 1
        return super().parse_args(ctx, hoisted + rest)


app = typer.Typer(
    # 开启 shell 补全（`--install-completion` / `--show-completion`）。
    # 关闭它会让 `frpsctl conf<TAB>` 这类日常操作永远不可用——没有理由关。
    add_completion=True,
    # 关掉 Click 的自动帮助：缺子命令属于**用法错误**，应当走退出码 2（§7.3），
    # 而不是打印帮助后退出 0 —— 那会让脚本误判为成功。
    no_args_is_help=False,
    help="把 frp 服务端（frps）包装成命令行工具：配置翻译器 + 进程保镖 + 状态聚合器。",
    cls=_AnywhereGroup,
)
config_app = typer.Typer(no_args_is_help=True, help="配置读写与变更闭环。")
service_app = typer.Typer(no_args_is_help=True, help="systemd 集成。")
plugin_app = typer.Typer(no_args_is_help=True, help="服务端插件：多用户鉴权 + 端口白名单 + 审计。")
plugin_service_app = typer.Typer(no_args_is_help=True, help="插件服务的 systemd 集成。")
web_app = typer.Typer(no_args_is_help=True, help="Web 管理台（内置界面，含进程控制与配置编辑）。")
web_service_app = typer.Typer(no_args_is_help=True, help="Web 管理台的 systemd 集成。")
app.add_typer(config_app, name="config")
app.add_typer(service_app, name="service")
app.add_typer(plugin_app, name="plugin")
app.add_typer(web_app, name="web")
plugin_app.add_typer(plugin_service_app, name="service")
web_app.add_typer(web_service_app, name="service")


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
    admin_password: str = typer.Option(
        None, "--admin-password", help="dashboard 口令（优先于配置文件；也可用 FRPSCTL_ADMIN_PASSWORD）"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过交互确认"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="详细输出"),
    version: bool = typer.Option(  # noqa: ARG001 - 值由 eager callback 消费，函数体不需要它
        False,
        "--version",
        help="显示 frpsctl 版本",
        is_eager=True,
        callback=_show_version,
        # 值由 eager callback 处理；这里留着是为了让 Click 生成帮助条目
        expose_value=False,
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
        admin_password=admin_password,
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


def _require_admin(app_ctx: AppContext, *, feature: str) -> AdminClient:
    """需要 dashboard 的命令的统一入口；未启用时退出码 7（§7.3）。

    不用配置错误(3)：脚本据此区分"配置写错了"与"这个功能当前不可用"。
    """
    client = _admin(app_ctx)
    if client is None:
        raise AdminUnreachable(
            f"dashboard 未启用（webServer.port = 0），无法{feature}",
            hint="该命令依赖 Admin API；请在配置里设置 webServer.port",
        )
    return client


# ---------------------------------------------------------------------------
# install / init / verify
# ---------------------------------------------------------------------------


@app.command()
def install(
    ctx: typer.Context,
    version: str = typer.Option(
        ".".join(map(str, RECKONED_VERSION)),
        "--version",
        help="要安装的 frps 版本（默认当前推荐版本）",
    ),
    mirror: list[str] = typer.Option(
        None,
        "--mirror",
        help="下载源，可重复指定；默认内置源（也可用 FRPSCTL_MIRROR，逗号分隔）",
    ),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    force: bool = typer.Option(False, "--force", help="已存在同版本时重新下载"),
    only_download: bool = typer.Option(False, "--only-download", help="只落盘，不切换软链（§8.6.1）"),
    insecure: bool = typer.Option(False, "--insecure", help="拿不到官方校验和时仍继续（风险自负）"),
    with_frpc: bool = typer.Option(
        False, "--with-frpc", help="同时取出 frpc（供插件契约测试使用，不额外下载）"
    ),
) -> None:
    """下载官方 frps 二进制并强校验 sha256。

    低于 0.70.0 的版本会被直接拒绝——它们没有 v2 Admin API（§3.6）。
    镜像只影响可用性、不影响信任：信任锚始终是官方校验和文件。
    """
    app_ctx = _ctx(ctx).with_json(json_output)
    app_ctx.instance.ensure_dirs()
    result = release.install(
        bin_dir=app_ctx.instance.bin_dir,
        version=version,
        mirrors=release.resolve_mirrors(mirror or ()),
        insecure=insecure,
        force=force,
        switch=not only_download,
        with_frpc=with_frpc,
    )

    if app_ctx.json:
        ui.emit_json(
            {
                "version": result.version,
                "binary": str(result.binary),
                "downloaded": result.downloaded,
                "switched": result.switched,
                "switched_frpc": result.switched_frpc,
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
    elif result.downloaded:
        # 二进制刚落盘但软链没动：可能是 `--only-download`，也可能是**同版本已在盘上**
        # （此时切换是空操作）。两种原因的处置完全不同，不能笼统说"未切换"。
        ui.emit("软链未改动：该版本已就位，`frps` 仍指向它。")
    else:
        ui.emit("已按 --only-download 落盘，未切换软链。")

    if result.switched_frpc:
        ui.emit(f"frpc {result.version} → {app_ctx.instance.bin_dir / f'frpc-{result.version}'}")
        ui.emit("      （供插件契约测试使用；软链已指向它）")


@app.command()
def init(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的配置（会先备份）"),
    bind_port: int = typer.Option(7000, "--bind-port", min=1, max=65535, help="控制端口 bindPort"),
    dashboard_port: int = typer.Option(
        7500, "--dashboard-port", min=0, max=65535, help="dashboard 端口（0 = 不启用）"
    ),
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
    # 生成后先做一次语义自检（毫秒级）：init 是配置的源头，它的产物必须自身
    # 合法。此前 `--bind-port 99999` 会先生成、等到 verify/start 才报错——
    # 一个必然失败的配置本不该落盘。
    cfg.validate_semantics(text)
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
    ui.emit("下一步：frpsctl verify && frpsctl start")


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

    text = cfg.read_config_text(target)
    uses_unsafe = cfg.needs_unsafe_flag(text)
    cfg.validate_text(text, binary=binary, workdir=app_ctx.instance.dir, uses_unsafe=uses_unsafe)
    flags = " ".join(cfg.config_flags(uses_exec_token_source=uses_unsafe))
    if app_ctx.json:
        ui.emit_json({"file": str(target), "ok": True, "flags": flags})
    else:
        ui.emit(f"{target} 校验通过（frps {version}，标志：{flags}）")


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


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
    app_ctx = _ctx(ctx).with_json(json_output)
    lc = _lifecycle(app_ctx)

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
    timeout: float = typer.Option(10.0, "--timeout", min=0, help="停止等待秒数"),
    health_timeout: float = typer.Option(
        10.0, "--health-timeout", min=0, help="健康检查等待秒数"
    ),
) -> None:
    """重启实例（stop → start）。配置变更请用 `config set`，它会自动回滚。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    lc = _lifecycle(app_ctx)
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
    app_ctx = _ctx(ctx).with_json(json_output)
    if not watch:
        _print_status(app_ctx)
        return

    # `--watch` 是**持续刷新**，Ctrl-C 是它的正常退出方式，不是错误。
    # 这里刻意只在循环内渲染：以前写成"循环 + 循环后无条件再渲染一次"，于是
    # Ctrl-C 会多刷一屏（`except` 分支里的 `return` 让循环后的调用变成"退出前
    # 再打一次"），用户看到的是按了中断却又冒出一份状态。
    try:
        while True:
            # 清屏只对**终端**有意义：重定向到文件/管道时写 ANSI 转义码会污染
            # 输出（`frpsctl status --watch > state.txt` 拿到一堆 \x1b[2J）。
            if not app_ctx.json and sys.stdout.isatty():
                ui.emit("\033[2J\033[H")
            # --watch --json 用**单行** JSON（NDJSON）：多行缩进格式在连续
            # 输出时无法被逐行消费（脚本会拿到一串无法解析的片段）。
            _print_status(app_ctx, compact=app_ctx.json)
            time.sleep(interval)
    except KeyboardInterrupt:
        return


def _status_payload(
    report, *, client_count: int | None = None, proxy_counts: dict[str, int] | None = None
) -> dict:
    """`status` / `instances` 共用的 JSON 形状（含 `--watch` 的 NDJSON 行）。"""
    counts = proxy_counts or {}
    return {
        "instance": report.instance,
        "owner": report.owner.value,
        "state": report.state.value,
        "state_corrupted": report.state_corrupted,
        "pid": report.pid,
        "uptime_seconds": report.uptime_seconds,
        "binary": str(report.binary) if report.binary else None,
        "binary_version": report.binary_version,
        "disk_version": report.disk_version,
        "config": str(report.config) if report.config else None,
        "config_mode": report.config_mode,
        "listen": None
        if report.listen is None
        else {"addr": report.listen.addr, "port": report.listen.port},
        "systemd_unit": report.systemd_unit,
        "systemd_main_pid": report.systemd_main_pid,
        "health": None
        if report.health is None
        else {
            "l1_process": report.health.l1_process.value,
            "l2_control": report.health.l2_control.value,
            "l3_plugin": report.health.l3_plugin.value,
            "detail": report.health.detail,
        },
        "clients": client_count,
        "proxy_type_counts": counts,
        "proxy_total": sum_proxy_types(counts) if counts else None,
        "version_hint": report.version_hint,
    }


def _print_status(app_ctx: AppContext, *, compact: bool = False) -> None:
    lc = _lifecycle(app_ctx)
    report = lc.status()

    info = None
    client_count = None
    proxy_counts: dict[str, int] = {}
    if report.state is State.RUNNING:
        # 先取对象再判 None：`with None` 会直接 TypeError，而 dashboard 未启用
        # （webServer.port = 0）是**合法配置**，不是异常情况。status 必须永远能
        # 回答"现在什么情况"，不能因为没开 dashboard 就崩。
        client = _admin(app_ctx)
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
    app_ctx = _ctx(ctx)
    inst = app_ctx.instance
    from ..core.logs import resolve_log_target

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


#: `log --follow` 的轮询间隔（秒）。不用 inotify：那要引入额外依赖，收益仅是
#: 延迟，而 0.3s 对"看日志"这个场景完全够用。
FOLLOW_INTERVAL = 0.3


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
            time.sleep(FOLLOW_INTERVAL)
            handle = _reopen_if_rotated(path, handle)
    finally:
        with contextlib.suppress(OSError):
            handle.close()


def _reopen_if_rotated(path: Path, handle):
    """`path` 指向的 inode 与已打开的 handle 不一致时重开；否则原样返回。"""
    try:
        if path.stat().st_ino != os.fstat(handle.fileno()).st_ino:
            with contextlib.suppress(OSError):
                handle.close()
            return open(path, "r", encoding="utf-8", errors="replace")  # noqa: SIM115
    except FileNotFoundError:
        # 文件刚被移走、新的还没建：保持旧 handle，下一轮再试
        pass
    return handle


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
    raw_value = cfg.get_value(doc, key)
    # 递归打码：`config get auth` 取到的是整张表，只判 `is_secret_key("auth")`
    # 会漏掉表内的 token/password（实测会明文打印）。
    value = cfg.mask_tree(raw_value, prefix=key, reveal=reveal)

    if app_ctx.json:
        ui.emit_json({"key": key, "value": value})
    elif isinstance(value, (dict, list)):
        ui.emit(json.dumps(value, ensure_ascii=False, default=str))
    else:
        ui.emit(str(value))


@config_app.command("list")
def config_list(
    ctx: typer.Context,
    prefix: str = typer.Option("", "--prefix", help="只看某个前缀下的键（点分路径，如 webServer）"),
    tree: bool = typer.Option(False, "--tree", help="按表分组缩进展示（默认平铺点分键）"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """列出全部配置键（点分路径 + 打码后的值）。

    键名的**发现**入口：不必翻文档或逐个 `config get` 试。敏感值同样打码
    （`is_secret_key` 判定），要明文用 `config get <key> --reveal`。
    """
    app_ctx = _ctx(ctx).with_json(json_output)
    doc = cfg.load_config(app_ctx.config_path)
    all_entries = cfg.flatten_tree(doc)
    if prefix:
        entries = [
            (key, value)
            for key, value in all_entries
            if key == prefix or key.startswith(f"{prefix}.")
        ]
        if not entries:
            raise ConfigKeyMissing(prefix)
    else:
        entries = all_entries

    if app_ctx.json:
        ui.emit_json(
            {
                "keys": [
                    {
                        "key": key,
                        "value": ui.mask_secret(value) if cfg.is_secret_key(key) else _plain(value),
                    }
                    for key, value in entries
                ]
            }
        )
        return
    if tree:
        for line in _render_tree(entries):
            ui.emit(line)
        return
    for key, value in entries:
        ui.emit(f"{key} = {_render_leaf(key, value)}")


def _render_leaf(key: str, value: object) -> str:
    """单个键值的展示文本（敏感值打码）。"""
    if cfg.is_secret_key(key):
        return ui.mask_secret(value)
    return json.dumps(_plain(value), ensure_ascii=False, default=str)


def _render_tree(entries: list[tuple[str, object]]) -> list[str]:
    """把点分键列表渲染成按表分组的缩进视图（`config list --tree`）。

    表头用**完整点分路径**（`[transport.tls]`，与 TOML 的实际写法一致），
    表下叶子统一缩进 2 空格（层级已由表头表达，叶子不必再按深度缩进）；
    顶层键不缩进。
    """
    lines: list[str] = []
    seen: set[str] = set()
    for key, value in entries:
        parts = key.split(".")
        table = ".".join(parts[:-1])
        if table and table not in seen:
            seen.add(table)
            lines.append(f"[{table}]")
        indent = "  " if table else ""
        lines.append(f"{indent}{parts[-1]} = {_render_leaf(key, value)}")
    return lines


def _plain(value: object) -> object:
    """把 tomlkit 的包装类型转成可 JSON 化的原生值。"""
    return value.unwrap() if hasattr(value, "unwrap") else value


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="点分键，如 bindPort"),
    value: str = typer.Argument(..., help="新值"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    no_restart: bool = typer.Option(False, "--no-restart", help="只写不重启（变更尚未生效）"),
    health_timeout: float = typer.Option(
        10.0, "--health-timeout", min=0, help="健康检查等待秒数"
    ),
) -> None:
    """写单个键，走 §9 事务闭环（校验 → 备份 → 原子替换 → 重启 → 失败回滚）。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    lc = _lifecycle(app_ctx)

    # 候选生成（plan）与落盘在**同一把实例锁内**完成（apply_set）：锁外生成
    # 候选会被并发变更静默覆盖（第四轮 review 实测复现）。noop 同样锁内判定。
    outcome = apply_set(
        app_ctx.instance,
        dotted=key,
        raw=value,
        lifecycle=lc,
        restart=not no_restart,
        health_timeout=health_timeout,
    )

    if outcome.noop:
        if app_ctx.json:
            ui.emit_json(
                {
                    "key": key,
                    "before": ui.mask_secret(outcome.before) if cfg.is_secret_key(key) else outcome.before,
                    "after": ui.mask_secret(outcome.after) if cfg.is_secret_key(key) else outcome.after,
                    "applied": False,
                    "restarted": False,
                    "noop": True,
                }
            )
        else:
            ui.emit(f"{key} 已经是 {outcome.after}，无需变更")
        return

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

    ui.emit(cfg.mask_diff(outcome.diff).rstrip() or "(无文本差异)")
    ui.emit("")
    if outcome.note:
        ui.emit(f"✓ {outcome.note}")
    elif outcome.restarted:
        ui.emit("✓ 已写入并重启，健康检查通过")
    if outcome.plugin_warning:
        ui.warn(f"⚠ {outcome.plugin_warning}")


def _resolve_editor() -> list[str]:
    """把 `$EDITOR` 拆成 argv，支持 `EDITOR="vim -u NONE"` 这类带参数的写法。

    此前直接把整个字符串当可执行文件路径：带参数时 `subprocess.call` 抛
    `FileNotFoundError: 'vim -u NONE'` → "未分类错误(1)"，而这是完全正常的
    配置方式。引号不配对（`EDITOR="'vim"`）时给出用法错误(2) 与可行动提示，
    而不是让 shlex 的裸 `ValueError` 冒出去。
    """
    import shlex

    raw = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        raise UsageError(
            f"无法解析 EDITOR={raw!r}：{exc}",
            hint="检查引号是否配对，或改用不带引号的写法（如 EDITOR=vim）",
        ) from None
    if not argv:
        raise UsageError("EDITOR 为空", hint="设置 EDITOR=vim，或手工编辑配置文件")
    return argv


@config_app.command("edit")
def config_edit(
    ctx: typer.Context,
    health_timeout: float = typer.Option(
        10.0, "--health-timeout", min=0, help="健康检查等待秒数"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过「应用以上改动并重启？」的确认"),
) -> None:
    """用 $EDITOR 编辑，保存后走完全相同的闭环（先展示 diff 让人确认）。

    **不提供 `--json`**：它要展示 diff 并等待人确认，没有"机器可读"的语义。

    `--yes` 是**局部**选项（与全局同名）：`init` 早就有局部的 `--yes`，而这里此前
    只能靠全局那个——于是 `frpsctl config edit --yes` 会报 `No such option`。
    """

    import tempfile

    app_ctx = _ctx(ctx)
    lc = _lifecycle(app_ctx)
    original = cfg.read_config_text(app_ctx.config_path)  # 缺文件 → ConfigError(3)

    editor_argv = _resolve_editor()
    # 同 validate_text：先拿到路径再写，否则写失败会把含机密的草稿留在 /tmp
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        "w", suffix=".toml", delete=False, encoding="utf-8"
    )
    draft_path = Path(handle.name)
    try:
        with handle:
            handle.write(original)
        code = subprocess.call([*editor_argv, str(draft_path)])
        if code != 0:
            raise ConfigError(f"编辑器退出码 {code}，放弃变更")
        draft = draft_path.read_text("utf-8")
    finally:
        draft_path.unlink(missing_ok=True)

    if draft == original:
        ui.emit("没有改动")
        return

    diff = cfg.diff_texts(original, draft, app_ctx.config_path.name)
    ui.emit(cfg.mask_diff(diff).rstrip())
    ui.emit("")
    if not (yes or app_ctx.yes) and not typer.confirm("应用以上改动并重启？", default=True):
        ui.emit("已放弃")
        return

    # 编辑器交互在锁外（不能持锁等用户），写回由 apply_edit 在锁内做 CAS：
    # 编辑期间若有人改过配置，草稿会被拒绝而不是覆盖别人的改动。
    outcome = apply_edit(
        app_ctx.instance,
        draft=draft,
        expected_current=original,
        lifecycle=lc,
        restart=True,
        health_timeout=health_timeout,
    )
    if outcome.noop:
        ui.emit("没有改动")
    elif outcome.note:
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
    inst = app_ctx.instance
    # 锁内读两份文本：锁外读时"选中的快照"与"当前文本"可能来自不同时刻，
    # 展示的差异与真实状态不符（并发 config set 正在推进历史的窗口）。
    with instance_lock(inst.lock):
        entries = inst.history_entries()
        if not entries:
            raise ConfigError("没有配置快照", hint="快照在每次 config set / edit 时自动创建")
        index = max(0, steps - 1)
        if index >= len(entries):
            raise ConfigError(f"只找到 {len(entries)} 份快照")
        snapshot = entries[index] / "frps.toml"
        if not snapshot.exists():
            raise ConfigError(f"快照不完整：{snapshot}")
        snapshot_text = snapshot.read_text("utf-8")
        current_text = cfg.read_config_text(inst.config)
    diff = cfg.diff_texts(snapshot_text, current_text, "frps.toml")
    if app_ctx.json:
        ui.emit_json({"snapshot": str(snapshot.parent), "diff": cfg.mask_diff(diff)})
    else:
        ui.emit(cfg.mask_diff(diff).rstrip() or "(无差异)")
        ui.emit("")
        ui.emit(f"# 快照：{snapshot.parent.name}")


@config_app.command("rollback")
def config_rollback(
    ctx: typer.Context,
    steps: int = typer.Argument(1, help="回滚到 N 份之前的快照"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    health_timeout: float = typer.Option(
        10.0, "--health-timeout", min=0, help="健康检查等待秒数"
    ),
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
            {
                "target": outcome.after,
                "restarted": outcome.restarted,
                "diff": cfg.mask_diff(outcome.diff),
            }
        )
        return
    ui.emit(cfg.mask_diff(outcome.diff).rstrip() or "(无文本差异)")
    ui.emit("")
    if outcome.restarted:
        ui.emit(f"✓ 已回滚到 {outcome.after} 并重启，健康检查通过")
    else:
        ui.emit(f"✓ 已回滚到 {outcome.after}（实例未运行，配置已就绪，start 后生效）")


# ---------------------------------------------------------------------------
# service / doctor / prune
# ---------------------------------------------------------------------------


@service_app.command("install")
def service_install(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的 unit 模板"),
    log_dir: Path = typer.Option(Path("/var/log/frps"), "--log-dir", help="unit 中 ReadWritePaths 的目录"),
    user: str = typer.Option(
        DEFAULT_SERVICE_USER, "--user", help="运行 frps 的系统用户（需已存在）"
    ),
    group: str = typer.Option(None, "--group", help="运行 frps 的系统组（默认与 --user 相同）"),
) -> None:
    """安装 `frps@.service` 模板并 enable（需要 root）。

    安装前会做三项体检：服务用户存在、二进制对服务用户可执行、日志目录可写。
    任何一项不满足都会当场拒绝——unit 装上去起不来，等于没装。
    """
    app_ctx = _ctx(ctx).with_json(json_output)
    lc = _lifecycle(app_ctx)
    systemd = Systemd(app_ctx.instance)
    path = systemd.install_template(
        binary=lc.binary(),
        log_dir=log_dir,
        force=force,
        user=user,
        group=group,
    )
    if app_ctx.json:
        ui.emit_json(
            {
                "unit": systemd.unit_name,
                "template": str(path),
                "owner": "systemd",
                "user": user,
                "group": group or user,
            }
        )
    else:
        ui.emit(f"已安装 {path}")
        ui.emit(f"实例 unit：{systemd.unit_name}（User={user}, Group={group or user}）")
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


@service_app.command("logs")
def service_logs(
    ctx: typer.Context,
    lines: int = typer.Option(100, "--lines", "-n", min=1, help="显示行数"),
    follow: bool = typer.Option(False, "--follow", "-f", help="持续跟踪"),
) -> None:
    """查看 systemd 托管的实例日志（`journalctl -u frps@<name>`）。

    frp 自己的日志文件用 `frpsctl log`；unit 级日志（启动失败、OOM、权限拒绝）
    只在 journald 里，只有 journalctl 能看到。
    """
    app_ctx = _ctx(ctx)
    import shutil as _shutil

    if _shutil.which("journalctl") is None:
        raise UsageError(
            "找不到 journalctl（journald 不可用）",
            hint="用 `frpsctl log` 查看 frp 自己的日志文件，或安装 systemd-journald",
        )
    systemd = Systemd(app_ctx.instance)
    # 终端接管类操作（同 `start --foreground`）：argv 由 core 构造，CLI 负责执行
    raise typer.Exit(subprocess.call(systemd.journal_argv(lines=lines, follow=follow)))


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
    app_ctx = _ctx(ctx).with_json(json_output)
    admin = _require_admin(app_ctx, feature="清理离线代理记录")
    with admin:
        admin.clear_offline_proxies()
    if app_ctx.json:
        ui.emit_json({"cleared": True})
    else:
        ui.emit("已清理离线代理记录")


@app.command()
def clients(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """列出在线客户端（v2 Admin API，自动翻页取全量）。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    admin = _require_admin(app_ctx, feature="列出客户端")
    with admin:
        items = admin.list_clients()

    if app_ctx.json:
        ui.emit_json({"clients": items})
        return
    if not items:
        ui.emit("没有在线客户端")
        return
    ui.emit(f"{'name':<28} {'user':<10} {'hostname':<20} {'online':<7} {'ip':<16} version")
    for item in items:
        ui.emit(
            f"{_text(item.get('key')):<28} {_text(item.get('user')):<10} "
            f"{_text(item.get('hostname')):<20} {str(bool(item.get('online'))):<7} "
            f"{_text(item.get('clientIP')):<16} {_text(item.get('version'))}"
        )


@app.command()
def proxies(
    ctx: typer.Context,
    ptype: str = typer.Option("", "--type", help="只看某类型（tcp/udp/http/https/stcp/xtcp/tcpmux/sudp）"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """列出代理（v2 Admin API，自动翻页取全量）。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    admin = _require_admin(app_ctx, feature="列出代理")
    with admin:
        items = admin.list_proxies()
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
                        "cur_conns": item.cur_conns,
                        "today_traffic_in": item.today_traffic_in,
                        "today_traffic_out": item.today_traffic_out,
                    }
                    for item in items
                ]
            }
        )
        return
    if not items:
        ui.emit("没有代理")
        return
    ui.emit(f"{'name':<28} {'user':<10} {'type':<7} {'port':<6} {'phase':<8} {'conns':<6} traffic(in/out)")
    for item in items:
        port = str(item.remote_port) if item.remote_port else "-"
        traffic = f"{ui.human_bytes(item.today_traffic_in)} / {ui.human_bytes(item.today_traffic_out)}"
        ui.emit(
            f"{item.name:<28} {item.user:<10} {item.type:<7} {port:<6} "
            f"{item.phase:<8} {item.cur_conns:<6} {traffic}"
        )


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

    app_ctx = _ctx(ctx).with_json(json_output)
    root = app_ctx.instance.instances_root
    found = list_instances(root, data_home=app_ctx.instance.data_home)

    reports = []
    for inst in found:
        lc = Lifecycle(inst)
        report = lc.status()
        if health and report.state in (State.RUNNING, State.SYSTEMD_ACTIVE):
            with contextlib.suppress(FrpsctlError):
                report = replace(
                    report, health=lc.check_health(expect_pid=report.systemd_main_pid)
                )
        reports.append(report)

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


# ---------------------------------------------------------------------------
# plugin 子命令（设计文档 §11）
# ---------------------------------------------------------------------------


def _policy_path(app_ctx: AppContext, override: Path | None) -> Path:
    """策略文件位置：`--policy` > 环境变量 > 实例目录下的 plugin-policy.json。

    默认放进实例目录，是为了让"这个实例的策略"跟它的配置、历史待在一起——
    迁移实例时不会漏掉鉴权规则。
    """
    if override is not None:
        return override.expanduser()
    env = os.environ.get("FRPSCTL_PLUGIN_POLICY")
    if env:
        return Path(env).expanduser()
    return app_ctx.instance.dir / "plugin-policy.json"


def _load_policy(app_ctx: AppContext, override: Path | None) -> tuple[Path, PluginPolicy]:
    path = _policy_path(app_ctx, override)
    return path, PluginPolicy.load(path)


@plugin_app.command("init")
def plugin_init(
    ctx: typer.Context,
    policy: Path = typer.Option(None, "--policy", help="策略文件路径（默认 <实例>/plugin-policy.json）"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的策略文件"),
) -> None:
    """生成一份策略模板。

    默认是 **fail-closed**：模板里列出的用户才能登录，未列出的全部拒绝；
    且不允许随机端口——白名单的意义就是"只能拿到我批准的端口"。
    """
    app_ctx = _ctx(ctx).with_json(json_output)
    path = _policy_path(app_ctx, policy)
    if path.exists() and not force:
        raise ConfigError(
            f"策略文件已存在：{path}",
            hint="确认要覆盖请加 --force",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # 原子写：策略文件描述"谁能用哪些端口"，写一半崩溃会留下无法解析的半截
    # JSON；原先 write_text + chmod 还带一个"先落盘后收紧权限"的窗口。
    cfg.atomic_write(path, _render_policy_template(), mode=0o600)

    if app_ctx.json:
        ui.emit_json({"policy": str(path), "mode": "0600"})
        return
    ui.emit(f"已生成策略模板：{path}（权限 0600）")
    ui.emit("")
    ui.emit("编辑它来定义用户与端口白名单，然后：")
    ui.emit("  frpsctl plugin check      # 校验策略并试算几条典型裁决")
    ui.emit("  frpsctl plugin serve      # 启动插件服务")


def _render_policy_template() -> str:
    import json as _json

    template = {
        "_comment": "frpsctl 服务端插件策略。用户在 frpc 侧用 user + metadatas.client_id 声明身份。",
        "allow_unknown_user": False,
        "require_client_id": True,
        "audit": {
            "enabled": True,
            "path": "./plugin-audit.jsonl",
            "flush_every": 32,
            "flush_interval": 2.0,
        },
        "_admin_comment": (
            "若使用 max_proxies 配额，请填写 dashboard 地址，否则计数只在本进程内有效"
            "（重启归零、多实例各算各的）"
        ),
        "admin_url": "",
        "admin_user": "",
        "admin_password": "",
        "users": {
            "alice": {
                "allowed_ports": ["6000-6010"],
                "allow_random_port": False,
                "allowed_proxy_types": ["tcp", "udp"],
                "allowed_proxy_names": ["alice-*"],
                "max_proxies": 5,
                "note": "示例用户：换成本地实际用户，并收窄端口范围",
            }
        },
    }
    return _json.dumps(template, indent=2, ensure_ascii=False) + "\n"


@plugin_app.command("check")
def plugin_check(
    ctx: typer.Context,
    policy: Path = typer.Option(None, "--policy", help="策略文件路径"),
    bind: str = typer.Option("127.0.0.1:8080", "--bind", help="将要绑定的地址（用于校验）"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """离线校验策略：能载入吗？绑回环吗？典型裁决是否符合预期？"""
    app_ctx = _ctx(ctx).with_json(json_output)
    path, loaded = _load_policy(app_ctx, policy)
    # bind 校验是核心：非回环必须在这里就报错，而不是等部署完才发现
    loaded.validate(bind=bind)

    samples = _sample_decisions(loaded)
    if app_ctx.json:
        ui.emit_json(
            {
                "policy": str(path),
                "bind": bind,
                "users": sorted(loaded.users),
                "allow_unknown_user": loaded.allow_unknown_user,
                "require_client_id": loaded.require_client_id,
                "samples": samples,
            }
        )
    else:
        ui.emit(f"策略文件：{path}")
        for line in loaded.describe():
            ui.emit(f"  {line}")
        ui.emit("")
        ui.emit("典型裁决试算：")
        for item in samples:
            mark = "允许" if item["allowed"] else "拒绝"
            detail = item["reason"] or ""
            ui.emit(f"  [{mark}] {item['case']}" + (f" — {detail}" if detail else ""))
        ui.emit("")
        ui.emit("策略校验通过")

    if loaded.allow_unknown_user:
        ui.warn("⚠ allow_unknown_user 已开启：未列出的用户会被放行，鉴权形同虚设")


def _sample_decisions(policy: PluginPolicy) -> list[dict]:
    """用几个典型场景试算，让"策略到底会怎么判"在部署前就可见。

    ⚠️ 样本的**代理名必须满足该用户的 `allowed_proxy_names`**，否则试算会先被
    名称规则挡掉，"许可范围内的端口被允许"这一条就永远显示为拒绝——诊断输出
    反过来误导人（它看起来像"策略配错了"）。
    """
    from ..plugin.policy import decide_login, decide_new_proxy

    samples: list[dict] = []
    for name in sorted(policy.users):
        decision = decide_login(policy, user=name, client_id=name)
        samples.append({"case": f"Login {name}", "allowed": decision.allowed, "reason": decision.reason})

        user = policy.user(name)
        assert user is not None
        probe_name = _probe_proxy_name(user)
        probe_type = user.allowed_proxy_types[0] if user.allowed_proxy_types else "tcp"

        if user.allowed_ports:
            port = user.allowed_ports[0].start
            decision = decide_new_proxy(
                policy, user=name, proxy_name=probe_name, proxy_type=probe_type, remote_port=port
            )
            samples.append(
                {
                    "case": f"NewProxy {name} 申请 {port}（在许可范围内）",
                    "allowed": decision.allowed,
                    "reason": decision.reason,
                }
            )
        # 端口 1 对任何有白名单的用户都必然越界（白名单最低是 1，但极少配到它）
        decision = decide_new_proxy(
            policy, user=name, proxy_name=probe_name, proxy_type=probe_type, remote_port=1
        )
        samples.append(
            {
                "case": f"NewProxy {name} 申请 1（预期越界）",
                "allowed": decision.allowed,
                "reason": decision.reason,
            }
        )
    decision = decide_login(policy, user="__nobody__", client_id="__nobody__")
    samples.append(
        {
            "case": "Login __nobody__（未列出）",
            "allowed": decision.allowed,
            "reason": decision.reason,
        }
    )
    return samples


def _probe_proxy_name(user) -> str:
    """造一个必定通过该用户名称规则的探测名。

    没有名称规则时用固定名；有规则时取第一条并把 `*` 换成 `probe`，
    这样试算检验的是**我们想检验的那条规则**（端口），而不是被名称规则短路。
    """
    if not user.allowed_proxy_names:
        return "frpsctl-probe"
    pattern = user.allowed_proxy_names[0]
    return pattern.replace("*", "probe") if "*" in pattern else pattern


@plugin_app.command("serve")
def plugin_serve(
    ctx: typer.Context,
    policy: Path = typer.Option(None, "--policy", help="策略文件路径"),
    bind: str = typer.Option("127.0.0.1:8080", "--bind", help="绑定地址（必须回环）"),
    path: str = typer.Option("/handler", "--path", help="插件回调路径（需与 frps 的 httpPlugins.path 一致）"),
    access_log: bool = typer.Option(False, "--access-log", help="把每个请求打进 stderr"),
    json_output: bool = typer.Option(False, "--json", help="启动前以 JSON 输出一次状态（随后仍前台运行）"),
) -> None:
    """启动插件服务（前台）。

    ⚠️ 插件是**全部客户端登录的单点**且 fail-closed：它挂掉 = 所有人登录不了。
    生产环境请用 systemd 守护并设置 `Restart=always`。

    `--json` 输出的是**启动前的一次性状态**，之后仍然是前台阻塞运行——它不是
    "以 JSON 流式汇报"，脚本若需要探活请轮询 `GET /healthz`。
    收到 SIGTERM（systemd stop）会优雅退出并先把审计缓冲刷盘。
    """
    app_ctx = _ctx(ctx).with_json(json_output)
    policy_file, loaded = _load_policy(app_ctx, policy)

    settings = ServerSettings(bind=bind, path=path, access_log=access_log)
    server = PluginServer(loaded, settings)  # 非回环会在这里被拒绝

    if app_ctx.json:
        ui.emit_json(
            {
                "policy": str(policy_file),
                "bind": f"{settings.host}:{settings.port}",
                "path": settings.path,
                "users": sorted(loaded.users),
            }
        )
    else:
        ui.emit(f"插件策略：{policy_file}")
        for line in loaded.describe():
            ui.emit(f"  {line}")
        ui.emit("")
        ui.emit(f"监听：http://{settings.host}:{settings.port}{settings.path}")
        ui.emit("")
        ui.emit("frps 侧需要配置：")
        ui.emit("  [[httpPlugins]]")
        ui.emit('  name = "frpsctl"')
        ui.emit(f'  addr = "http://{settings.host}:{settings.port}"')
        ui.emit(f'  path = "{settings.path}"')
        ui.emit('  ops  = ["Login", "NewProxy"]')
        ui.emit("")
        ui.emit("⚠ fail-closed：本服务不可达时，所有客户端都无法登录。")
        ui.emit("   生产环境请用 systemd 守护并设置 Restart=always。")
        ui.emit("")
        if loaded.audit.enabled and loaded.audit.path is None:
            ui.warn("⚠ 审计已开启但没有配置 path：记录只留在内存里，进程退出即丢失")
            ui.warn("  在策略里设置 audit.path，或把 audit.enabled 设为 false 明确关闭")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # `serve_forever` 已经把 SIGTERM 转成 KeyboardInterrupt，因此这条分支同时
        # 覆盖 Ctrl-C 与 systemd stop；它自己负责 close()（含审计刷盘），此处不重复。
        ui.emit("")
        ui.emit(f"已停止。{server.audit.describe()}")


def _frpsctl_executable() -> Path:
    """当前 frpsctl 的可执行文件路径（写进插件 unit 的 ExecStart）。

    systemd 不读 PATH，必须是绝对路径。优先 `shutil.which`（全局安装时最可靠），
    其次 `sys.argv[0]`（开发态直接跑 venv 脚本）。`python -m frpsctl` 时
    argv[0] 是 `__main__.py`、不可直接执行——两种都拿不到就明确报错，
    而不是把一个坏路径写进 unit（错误会在 systemctl start 时才炸）。
    """
    import shutil as _shutil
    import sys as _sys

    found = _shutil.which("frpsctl")
    if found is not None:
        return Path(found).resolve()
    argv0 = Path(_sys.argv[0])
    if argv0.exists() and os.access(argv0, os.X_OK) and "frpsctl" in argv0.name:
        return argv0.resolve()
    raise UsageError(
        "无法确定 frpsctl 可执行文件路径（unit 的 ExecStart 需要绝对路径）",
        hint="请用 PATH 里的 `frpsctl` 命令运行本命令（而不是 python -m frpsctl）",
    )


@plugin_service_app.command("install")
def plugin_service_install(
    ctx: typer.Context,
    bind: str = typer.Option("127.0.0.1:8080", "--bind", help="监听地址（必须回环）"),
    handler_path: str = typer.Option(
        "/handler", "--path", help="回调路径（需与 frps 的 httpPlugins.path 一致）"
    ),
    policy: Path = typer.Option(None, "--policy", help="策略文件（默认 <实例>/plugin-policy.json）"),
    user: str = typer.Option(DEFAULT_SERVICE_USER, "--user", help="运行插件的系统用户（需已存在）"),
    group: str = typer.Option(None, "--group", help="运行插件的系统组（默认与 --user 相同）"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的 unit 模板"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """安装 frpsctl-plugin@.service 并 enable（需要 root）。

    渲染前体检：服务账户存在、frpsctl 对服务用户可达且不在家目录
    （`ProtectHome=true`）、策略文件存在且合法、绑定地址为回环。
    任何一项不满足都当场拒绝——装一个起不来的 unit 比不装更浪费时间。
    """
    app_ctx = _ctx(ctx).with_json(json_output)
    policy_path, _ = _load_policy(app_ctx, policy)  # 不存在/非法 JSON 在这里就会拒绝
    service = PluginService(app_ctx.instance)
    path = service.install_template(
        exec_start=_frpsctl_executable(),
        policy=policy_path,
        bind=bind,
        handler_path=handler_path,
        force=force,
        user=user,
        group=group,
    )
    if app_ctx.json:
        ui.emit_json(
            {
                "unit": service.unit_name,
                "template": str(path),
                "bind": bind,
                "policy": str(policy_path),
                "user": user,
                "group": group or user,
            }
        )
        return
    ui.emit(f"已安装 {path}")
    ui.emit(f"实例 unit：{service.unit_name}（User={user}, Group={group or user}）")
    ui.emit("")
    ui.emit(f"启动：sudo systemctl start {service.unit_name}")
    ui.emit("已 enable（开机自启）；停用：frpsctl plugin service uninstall")


@plugin_service_app.command("uninstall")
def plugin_service_uninstall(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """停用并删除插件 unit 模板（需要 root）。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    service = PluginService(app_ctx.instance)
    service.uninstall()
    if app_ctx.json:
        ui.emit_json({"unit": service.unit_name, "removed": True})
    else:
        ui.emit(f"已停用并移除 {service.unit_name}")


@plugin_service_app.command("status")
def plugin_service_status(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """显示插件服务的 systemd 托管状态。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    service = PluginService(app_ctx.instance)
    active = service.is_active()
    pid = service.main_pid() if active else None
    if app_ctx.json:
        ui.emit_json({"unit": service.unit_name, "active": active, "main_pid": pid})
        return
    ui.emit(f"unit     : {service.unit_name}")
    ui.emit(f"active   : {active}")
    if pid:
        ui.emit(f"main pid : {pid}")


def _web_password_from_file(path: Path | None) -> str | None:
    """从 `--password-file` 读口令；文件缺失/为空给出配置错误(3)。"""
    if path is None:
        return None
    try:
        value = path.read_text("utf-8").strip()
    except FileNotFoundError:
        raise ConfigError(
            f"口令文件不存在：{path}",
            hint="先运行 `frpsctl web service install` 生成，或去掉 --password-file 用自动生成的口令",
        ) from None
    except OSError as exc:
        raise ConfigError(f"无法读取口令文件 {path}：{exc}") from None
    if not value:
        raise ConfigError(
            f"口令文件为空：{path}",
            hint="删除该文件让服务重新生成，或手工写入一个口令",
        )
    return value


@web_app.command("serve")
def web_serve(
    ctx: typer.Context,
    bind: str = typer.Option("127.0.0.1:8787", "--bind", help="监听地址（默认只绑回环）"),
    password: str = typer.Option(
        None,
        "--password",
        help="登录口令（默认自动生成并打印一次；也可用 FRPSCTL_WEB_PASSWORD。"
        "⚠ 命令行参数对本机其他用户可见，生产建议用环境变量或 --password-file）",
    ),
    password_file: Path = typer.Option(
        None, "--password-file", help="从文件读取口令（systemd 部署用；文件需 0600）"
    ),
    allow_non_loopback: bool = typer.Option(
        False, "--allow-non-loopback", help="显式允许绑定非回环地址（建议配合反向代理 + TLS）"
    ),
) -> None:
    """启动 Web 管理台（前台运行）。

    内置界面提供仪表盘、客户端/代理列表、日志、进程启停与配置编辑（带预览与
    回滚）——比 frp 自带 dashboard 多出**控制面**。

    默认只绑 127.0.0.1；绑非回环必须显式加 --allow-non-loopback：界面能改配置、
    停服务，而会话 Cookie 没有 TLS 保护时公网暴露等于把控制权交出去。
    """
    app_ctx = _ctx(ctx)
    if not healthcheck.is_loopback(bind) and not allow_non_loopback:
        raise UsageError(
            f"拒绝绑定非回环地址：{bind}",
            hint="如确需远程访问，请加 --allow-non-loopback（并建议反向代理 + TLS）",
        )
    resolved = (
        password
        or os.environ.get("FRPSCTL_WEB_PASSWORD")
        or _web_password_from_file(password_file)
    )
    generated = False
    if not resolved:
        resolved = secrets.token_urlsafe(18)
        generated = True
    web_ctx = build_web_context(app_ctx.instance, resolved)
    server = WebServer(web_ctx, WebSettings(bind=bind, password=resolved))
    try:
        server.start()
    except OSError as exc:
        raise UsageError(f"无法绑定 {bind}：{exc}", hint="换一个端口，或检查是否有其他进程占用") from None
    ui.emit(f"Web 管理台：{server.url()}")
    if generated:
        ui.emit(f"登录口令（仅显示这一次）：{resolved}")
    else:
        ui.emit("登录口令：已从参数 / 环境变量 / 口令文件读取（不显示）")
    ui.emit("Ctrl-C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        ui.emit("")
        ui.emit("已停止")


@web_service_app.command("install")
def web_service_install(
    ctx: typer.Context,
    bind: str = typer.Option("127.0.0.1:8787", "--bind", help="监听地址（默认只绑回环）"),
    allow_non_loopback: bool = typer.Option(
        False, "--allow-non-loopback", help="显式允许绑定非回环地址（建议配合反向代理 + TLS）"
    ),
    user: str = typer.Option(DEFAULT_SERVICE_USER, "--user", help="运行管理台的系统用户（需已存在）"),
    group: str = typer.Option(None, "--group", help="运行管理台的系统组（默认与 --user 相同）"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的 unit 模板"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """安装 frpsctl-web@.service 并 enable（需要 root）。

    安装时生成 0600 的登录口令文件（unit 只引用路径，明文不进 unit），
    并执行与 frps/插件同样的四项体检（账户 / frpsctl 可达且不在家目录 /
    实例目录不在家目录）。
    """
    app_ctx = _ctx(ctx).with_json(json_output)
    if not healthcheck.is_loopback(bind) and not allow_non_loopback:
        raise UsageError(
            f"拒绝绑定非回环地址：{bind}",
            hint="如确需远程访问，请加 --allow-non-loopback（并建议反向代理 + TLS）",
        )
    service = WebService(app_ctx.instance)
    path, generated = service.install_template(
        exec_start=_frpsctl_executable(),
        bind=bind,
        force=force,
        user=user,
        group=group,
    )
    if app_ctx.json:
        ui.emit_json(
            {
                "unit": service.unit_name,
                "template": str(path),
                "bind": bind,
                "password_file": str(service.password_file),
                "password_generated": bool(generated),
                "user": user,
                "group": group or user,
            }
        )
        return
    ui.emit(f"已安装 {path}")
    ui.emit(f"实例 unit：{service.unit_name}（User={user}, Group={group or user}）")
    if generated:
        ui.emit("")
        ui.emit(f"登录口令（仅显示这一次，已写入 {service.password_file}）：{generated}")
    else:
        ui.emit(f"登录口令：沿用已有文件 {service.password_file}")
    ui.emit("")
    ui.emit(f"启动：sudo systemctl start {service.unit_name}")
    ui.emit("已 enable（开机自启）；停用：frpsctl web service uninstall")


@web_service_app.command("uninstall")
def web_service_uninstall(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """停用并删除 Web 管理台 unit 模板（需要 root）。口令文件保留。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    service = WebService(app_ctx.instance)
    service.uninstall()
    if app_ctx.json:
        ui.emit_json({"unit": service.unit_name, "removed": True})
    else:
        ui.emit(f"已停用并移除 {service.unit_name}")
        ui.emit(f"（登录口令文件保留在 {service.password_file}）")


@web_service_app.command("status")
def web_service_status(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """显示 Web 管理台的 systemd 托管状态。"""
    app_ctx = _ctx(ctx).with_json(json_output)
    service = WebService(app_ctx.instance)
    active = service.is_active()
    pid = service.main_pid() if active else None
    if app_ctx.json:
        ui.emit_json({"unit": service.unit_name, "active": active, "main_pid": pid})
        return
    ui.emit(f"unit     : {service.unit_name}")
    ui.emit(f"active   : {active}")
    if pid:
        ui.emit(f"main pid : {pid}")


def main() -> None:
    """控制台入口（pyproject 的 `frpsctl` 指向这里）。"""
    run_cli(app)


if __name__ == "__main__":
    main()
