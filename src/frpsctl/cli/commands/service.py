"""frps 本体的 systemd 集成命令。"""

from __future__ import annotations

import subprocess
from pathlib import Path
import typer
from ...core.systemd import DEFAULT_SERVICE_USER, Systemd
from ...errors import (
    UsageError,
)
from .. import ui

from ..app import service_app
from .. import runtime


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
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    lc = runtime._lifecycle(app_ctx)
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
    app_ctx = runtime._ctx(ctx).with_json(json_output)
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
    """显示 systemd 托管状态（active 与 enabled 分开报告）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    systemd = Systemd(app_ctx.instance)
    active = systemd.is_active()
    enabled = systemd.is_enabled()
    pid = systemd.main_pid() if active else None
    if app_ctx.json:
        ui.emit_json(
            {"unit": systemd.unit_name, "active": active, "enabled": enabled, "main_pid": pid}
        )
    else:
        ui.emit(f"unit     : {systemd.unit_name}")
        ui.emit(f"active   : {active}")
        ui.emit(f"enabled  : {enabled}（开机自启）")
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
    app_ctx = runtime._ctx(ctx)
    import shutil as _shutil

    if _shutil.which("journalctl") is None:
        raise UsageError(
            "找不到 journalctl（journald 不可用）",
            hint="用 `frpsctl log` 查看 frp 自己的日志文件，或安装 systemd-journald",
        )
    systemd = Systemd(app_ctx.instance)
    # 终端接管类操作（同 `start --foreground`）：argv 由 core 构造，CLI 负责执行
    raise typer.Exit(subprocess.call(systemd.journal_argv(lines=lines, follow=follow)))
