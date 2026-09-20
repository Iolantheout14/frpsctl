"""frps 本体的 systemd 集成命令。"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
import typer
from ...core.systemd import (
    DEFAULT_LOG_DIR,
    Systemd,
    ensure_service_account,
    read_template_user,
    show_unit_accounts,
)
from ...errors import (
    FrpsctlError,
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
    log_dir: Path = typer.Option(
        DEFAULT_LOG_DIR, "--log-dir", help="unit 中 ReadWritePaths 的目录"
    ),
    user: str = typer.Option(
        None, "--user", help="运行 frps 的系统用户（默认：优先 frps，其次当前用户）"
    ),
    group: str = typer.Option(
        None, "--group", help="运行 frps 的系统组（默认：同名组，缺失则用户主组）"
    ),
    create_user: bool = typer.Option(
        False, "--create-user", help="服务用户不存在时自动创建系统账户（需 root；须配合 --user）"
    ),
) -> None:
    """安装 `frps@.service` 模板并 enable（需要 root）。

    安装前会做体检：服务账户存在、二进制对服务用户可执行、日志目录可写、
    路径不在家目录（`ProtectHome`）。任何一项不满足都会当场拒绝——unit
    装上去起不来，等于没装。

    账户解析（§12.2）：`--user` 缺省时优先系统已有的 frps 用户，否则当前
    用户；任何账户都可以作为服务用户，`--create-user` 可自动创建账户。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    lc = runtime._lifecycle(app_ctx)
    systemd = Systemd(app_ctx.instance)
    # 先验证二进制存在，再解析/创建账户：避免"账户已建、安装却因二进制
    # 缺失失败"的无谓副作用顺序（review 收口）。
    binary = lc.binary()
    identity = ensure_service_account(user, group, create_user=create_user)
    if force:
        # 模板为全部实例共享：覆盖前把"会改变所有实例的服务用户"说清楚
        existing = read_template_user(systemd.template_path)
        if existing is not None and existing != identity.user:
            ui.warn(
                f"⚠ unit 模板为全部实例共享：{systemd.template_path} 的 User= "
                f"将从 {existing} 改为 {identity.user}（影响所有使用该模板的实例）"
            )
    path = systemd.install_template(
        binary=binary,
        log_dir=log_dir,
        force=force,
        user=identity.user,
        group=identity.group,
    )
    # 账户告警（创建/组回退/root 安全）**无条件**进 stderr：JSON 模式也不豁免
    # ——脚本收集 stderr 时同样需要看到（同 `plugin service stop` 的先例）。
    for warning in runtime._identity_warnings(identity):
        ui.warn(warning)
    if app_ctx.json:
        ui.emit_json(
            {
                "unit": systemd.unit_name,
                "template": str(path),
                "owner": "systemd",
                "user": identity.user,
                "group": identity.group,
                "user_source": identity.source,
                "created_user": identity.created,
            }
        )
    else:
        ui.emit(f"已安装 {path}")
        ui.emit(runtime._identity_summary(identity))
        ui.emit(f"实例 unit：{systemd.unit_name}（User={identity.user}, Group={identity.group}）")
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
    """显示 systemd 托管状态（active / enabled / 运行账户）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    systemd = Systemd(app_ctx.instance)
    active = systemd.is_active()
    enabled = systemd.is_enabled()
    pid = systemd.main_pid() if active else None
    user: str | None = None
    group: str | None = None
    if active or enabled:
        with contextlib.suppress(FrpsctlError):
            # 附加展示字段：探测失败不拖垮 status（active/enabled 已先取到）
            user, group = show_unit_accounts(systemd.unit_name)
    if app_ctx.json:
        ui.emit_json(
            {
                "unit": systemd.unit_name,
                "active": active,
                "enabled": enabled,
                "main_pid": pid,
                "user": user,
                "group": group,
            }
        )
    else:
        ui.emit(f"unit     : {systemd.unit_name}")
        ui.emit(f"active   : {active}")
        ui.emit(f"enabled  : {enabled}（开机自启）")
        if pid:
            ui.emit(f"main pid : {pid}")
        if user:
            ui.emit(f"user     : {user}")
        if group:
            ui.emit(f"group    : {group}")


@service_app.command("logs")
def service_logs(
    ctx: typer.Context,
    lines: int = typer.Option(100, "--lines", "-n", min=1, max=100_000, help="显示行数（上限 100000）"),
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
