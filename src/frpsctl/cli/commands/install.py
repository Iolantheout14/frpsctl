"""二进制与实例的安装 / 初始化 / 校验 / 卸载命令。"""

from __future__ import annotations

import secrets
from pathlib import Path
import typer
from ...core import config as cfg
from ...core import release
from ...core.systemd import Systemd
from ...core.uninstall import execute_uninstall, plan_uninstall
from ...core.version import RECKONED_VERSION
from ...errors import (
    ConfigError,
    UsageError,
)
from .. import ui

from ..app import app
from .. import runtime


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
    app_ctx = runtime._ctx(ctx).with_json(json_output)
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
    app_ctx = runtime._ctx(ctx).with_json(json_output)
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
        from ...core.transaction import config_snapshot

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
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    target = file or app_ctx.config_path
    lc = runtime._lifecycle(app_ctx)
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


@app.command()
def uninstall(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    all_instances: bool = typer.Option(
        False, "--all", help="卸载实例根下的全部实例（默认只卸当前实例）"
    ),
    keep_data: bool = typer.Option(
        False, "--keep-data", help="保留实例数据（配置/快照/审计），只清 unit 与二进制"
    ),
    keep_bin: bool = typer.Option(False, "--keep-bin", help="保留共享二进制（其它实例仍要用）"),
    force: bool = typer.Option(False, "--force", help="运行中的实例先停止再卸载"),
) -> None:
    """完整卸载：实例数据 / unit / 共享二进制（默认先列出清单并要求确认）。

    ⚠️ 删除**不可恢复**：实例目录里有 auth.token、dashboard 口令、配置快照与
    插件审计。默认只卸当前实例；删除共享二进制要求目标覆盖全部实例（或显式
    --keep-bin）——多实例机器上删掉它会让其他实例起不来。运行中的实例默认
    **拒绝**卸载（先 `frpsctl stop`，或用 --force 让它先停止）。

    需要 root 的清理（unit）若权限不足，会汇总为"未清理项"并给出命令，
    而不是静默跳过；Python 包本身请用 pipx / pip / install.sh --uninstall 移除。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    plan = plan_uninstall(
        app_ctx.instance,
        all_instances=all_instances,
        keep_data=keep_data,
        keep_bin=keep_bin,
    )

    if app_ctx.json and not app_ctx.yes:
        # --json 的 stdout 是机器可读契约（确认提示会污染它），破坏性操作也
        # 不该因为输出模式而被隐式确认。
        raise UsageError("--json 下必须显式 --yes（破坏性操作不做隐式确认）")

    if plan.is_empty:
        if app_ctx.json:
            ui.emit_json({"instances": [], "removed": [], "kept": [], "stopped": [], "warnings": []})
        else:
            ui.emit("没有可卸载的内容（实例数据与二进制都不存在）")
        return

    if not app_ctx.json:
        _render_uninstall_plan(plan)
        if not app_ctx.yes and not typer.confirm("确认执行卸载？（此操作不可恢复）", default=False):
            ui.emit("已取消，未做任何改动")
            return

    report = execute_uninstall(plan, force=force)

    if app_ctx.json:
        ui.emit_json(
            {
                "instances": [item.name for item in plan.instances],
                "removed": report.removed,
                "kept": report.kept,
                "stopped": report.stopped,
                "warnings": report.warnings,
            }
        )
        return

    ui.emit("")
    if report.stopped:
        ui.emit("已停止：" + "、".join(report.stopped))
    for item in report.removed:
        ui.emit(f"已删除：{item}")
    for item in report.kept:
        ui.emit(f"已保留：{item}")
    for warning in report.warnings:
        ui.warn(f"⚠ {warning}")
    ui.emit("")
    ui.emit(
        "卸载完成。Python 包本身请用 `pipx uninstall frpsctl` / `pip uninstall frpsctl` / "
        "`./install.sh --uninstall` 移除。"
    )


def _render_uninstall_plan(plan) -> None:
    """人读的卸载清单（确认前的"会删什么"预览）。"""
    ui.emit("将执行以下卸载（删除不可恢复；实例数据含 auth.token / dashboard 口令 / 快照 / 审计）：")
    ui.emit("")
    if plan.instances:
        ui.emit("  实例：" + "、".join(item.name for item in plan.instances))
        for item in plan.instances:
            action = "删除数据" if plan.remove_data else "保留数据"
            ui.emit(f"    · {action}：{item.dir}")
        if plan.remove_unit_templates:
            ui.emit("  unit：停用并删除模板（frps@ / frpsctl-plugin@ / frpsctl-web@，需要 root）")
        else:
            ui.emit("  unit：仅停用本实例的 unit（共享模板保留，其它实例仍在用）")
    else:
        ui.emit("  实例：（无）")
    if plan.remove_bin:
        ui.emit(f"  共享二进制：删除 {plan.bin_dir}")
    else:
        ui.emit(f"  共享二进制：保留 {plan.bin_dir}")
    ui.emit("")
