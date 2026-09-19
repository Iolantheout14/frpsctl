"""Web 管理台命令组（web serve/service/password）。"""

from __future__ import annotations

import os
import time
from pathlib import Path
import typer
from ...core import auditlog
from ...core import config as cfg
from ...core import web_audit
from ...core import healthcheck
from ...core.systemd import DEFAULT_SERVICE_USER, WebService
from ...web import WebServer, WebSettings, build_web_context, generate_password
from ...errors import (
    ConfigError,
    FrpsctlError,
    UsageError,
)
from .. import ui

from ..app import web_app, web_audit_app, web_service_app, web_password_app
from ..context import AppContext
from .. import runtime
from .config import _resolve_value_input


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
    trusted_proxy: bool = typer.Option(
        False,
        "--trusted-proxy",
        help="信任反向代理的 X-Forwarded-For（取最后一跳作为登录限速来源；默认关闭）",
    ),
    access_log: bool = typer.Option(
        False, "--access-log", help="把每个 HTTP 请求打进 stderr（排查用；默认关闭）"
    ),
    metrics: bool = typer.Option(
        False, "--metrics", help="暴露 /metrics（Prometheus 文本；Basic auth：用户名任意、口令=管理台口令）"
    ),
) -> None:
    """启动 Web 管理台（前台运行）。

    内置界面提供仪表盘、客户端/代理列表、日志、进程启停与配置编辑（带预览与
    回滚）——比 frp 自带 dashboard 多出**控制面**。

    默认只绑 127.0.0.1；绑非回环必须显式加 --allow-non-loopback：界面能改配置、
    停服务，而会话 Cookie 没有 TLS 保护时公网暴露等于把控制权交出去。

    在反向代理后运行时加 `--trusted-proxy`：登录限速按 X-Forwarded-For 的
    最后一跳区分来源（否则所有请求同源，攻击者的失败会连带锁住管理员）。
    前提是前面确实有一层会重写该头的可信代理。
    """
    app_ctx = runtime._ctx(ctx)
    if not healthcheck.is_loopback(bind) and not allow_non_loopback:
        raise UsageError(
            f"拒绝绑定非回环地址：{bind}",
            hint="如确需远程访问，请加 --allow-non-loopback（并建议反向代理 + TLS）",
        )
    resolved = (
        password
        or os.environ.get("FRPSCTL_WEB_PASSWORD")
        or runtime._web_password_from_file(password_file)
    )
    generated = False
    if not resolved:
        # 单点生成器（core.systemd.generate_web_password，经 web 包再导出）
        resolved = generate_password()
        generated = True
    web_ctx = build_web_context(app_ctx.instance, resolved)
    server = WebServer(
        web_ctx,
        WebSettings(
            bind=bind, trusted_proxy=trusted_proxy, access_log=access_log, metrics=metrics
        ),
    )
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
    trusted_proxy: bool = typer.Option(
        False,
        "--trusted-proxy",
        help="信任反向代理的 X-Forwarded-For（写入 unit 的 serve 参数）",
    ),
    access_log: bool = typer.Option(
        False,
        "--access-log",
        help="把逐请求日志写进 journald（写入 unit 的 serve 参数；unit 模板为全部实例共享）",
    ),
    metrics: bool = typer.Option(
        False,
        "--metrics",
        help="暴露 /metrics（写入 unit 的 serve 参数；unit 模板为全部实例共享）",
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

    注意：unit 模板（frpsctl-web@.service）为**全部实例共享**——bind、
    trusted-proxy 与 access-log 等都写在这一个模板里，多实例环境里重装会
    覆盖这些参数。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    if not healthcheck.is_loopback(bind) and not allow_non_loopback:
        raise UsageError(
            f"拒绝绑定非回环地址：{bind}",
            hint="如确需远程访问，请加 --allow-non-loopback（并建议反向代理 + TLS）",
        )
    service = WebService(app_ctx.instance)
    path, generated = service.install_template(
        exec_start=runtime._frpsctl_executable(),
        bind=bind,
        force=force,
        user=user,
        group=group,
        trusted_proxy=trusted_proxy,
        access_log=access_log,
        metrics=metrics,
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
    ui.emit("启动：frpsctl web service start（该命令在此，无需手工 systemctl）")
    ui.emit("已 enable（开机自启）；停止/重启/停用：web service stop|restart|uninstall")


@web_service_app.command("start")
def web_service_start(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """启动 Web 管理台（systemd，需要 root）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    service = WebService(app_ctx.instance)
    service.start()
    if app_ctx.json:
        ui.emit_json({"unit": service.unit_name, "started": True})
    else:
        ui.emit(f"已启动 {service.unit_name}")
        ui.emit("查看状态：frpsctl web service status（地址见 unit 的 --bind）")


@web_service_app.command("stop")
def web_service_stop(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """停止 Web 管理台（systemd，需要 root）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    service = WebService(app_ctx.instance)
    service.stop()
    if app_ctx.json:
        ui.emit_json({"unit": service.unit_name, "stopped": True})
    else:
        ui.emit(f"已停止 {service.unit_name}")


@web_service_app.command("restart")
def web_service_restart(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """重启 Web 管理台（systemd，需要 root）——口令轮换等改动重启后生效。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    service = WebService(app_ctx.instance)
    service.restart()
    if app_ctx.json:
        ui.emit_json({"unit": service.unit_name, "restarted": True})
    else:
        ui.emit(f"已重启 {service.unit_name}")


@web_service_app.command("uninstall")
def web_service_uninstall(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """停用并删除 Web 管理台 unit 模板（需要 root）。口令文件保留。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
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
    """显示 Web 管理台的 systemd 托管状态（active 与 enabled 分开报告）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    service = WebService(app_ctx.instance)
    active = service.is_active()
    enabled = service.is_enabled()
    pid = service.main_pid() if active else None
    if app_ctx.json:
        ui.emit_json(
            {"unit": service.unit_name, "active": active, "enabled": enabled, "main_pid": pid}
        )
        return
    ui.emit(f"unit     : {service.unit_name}")
    ui.emit(f"active   : {active}")
    ui.emit(f"enabled  : {enabled}（开机自启）")
    if pid:
        ui.emit(f"main pid : {pid}")


@web_password_app.command("set")
def web_password_set(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    read_stdin: bool = typer.Option(
        False, "--stdin", help="从标准输入读新口令（不进 argv 与 shell 历史）"
    ),
    prompt: bool = typer.Option(False, "--prompt", help="交互式隐藏输入新口令"),
) -> None:
    """设置（轮换）Web 管理台登录口令，写入 0600 口令文件。

    不给输入通道时自动生成一个随机口令并**只显示一次**；用 `--stdin` /
    `--prompt` 可提供自选口令（不回显）。口令由 systemd 托管的服务在**重启后**
    生效——正在运行的管理台仍接受旧口令。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    if read_stdin or prompt:
        value = _resolve_value_input(None, read_stdin=read_stdin, prompt=prompt)
        generated = False
    else:
        value = generate_password()
        generated = True

    service = WebService(app_ctx.instance)
    cfg.atomic_write(service.password_file, value + "\n", mode=0o600)
    # 口令**已经写盘**：下面的探测只决定"要不要提示重启"。systemctl 无响应时
    # 不能让"口令已设置"变成退出码 1（v0.3.1 自检补正）；`None` = 无法探测。
    try:
        active: bool | None = service.is_active()
    except FrpsctlError:
        active = None

    if app_ctx.json:
        payload: dict = {
            "password_file": str(service.password_file),
            "generated": generated,
            "service_active": active,
            # 无法探测时保守建议重启（宁可多重启一次，不要拿着旧口令排查）
            "restart_required": active is not False,
        }
        if generated:
            payload["password"] = value
        ui.emit_json(payload)
        return

    ui.emit(f"口令已写入 {service.password_file}（权限 0600）")
    if generated:
        ui.emit(f"新口令（仅显示这一次）：{value}")
    else:
        ui.emit("口令已按输入设置（不回显）")
    if active is None:
        ui.warn("⚠ 无法探测管理台是否在运行（systemctl 无响应）：若它正在运行，新口令需重启后生效")
    elif active:
        ui.emit("⚠ 管理台正在运行：新口令在重启后生效（`systemctl restart frpsctl-web@<实例>`）")
    else:
        ui.emit("管理台未在运行；下次启动时生效。")


@web_password_app.command("show")
def web_password_show(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """显示 `web service install` 生成的口令（文件缺失时报配置错误）。

    这是**显式索取明文**的命令（与 `config get --reveal` 同级）：输出可能进入
    终端回滚与重定向文件，请自行控制。文件权限过宽时会向 stderr 告警。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    path = WebService(app_ctx.instance).password_file
    try:
        value = path.read_text("utf-8").strip()
    except FileNotFoundError:
        raise ConfigError(
            f"口令文件不存在：{path}",
            hint="先运行 `frpsctl web service install` 生成，或直接 `web serve`（会自动生成口令）",
        ) from None
    except OSError as exc:
        raise ConfigError(f"无法读取口令文件 {path}：{exc}") from None
    if not value:
        raise ConfigError(
            f"口令文件为空：{path}",
            hint="删除该文件让服务重新生成，或手工写入一个口令",
        )
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        ui.warn(f"⚠ {path} 权限为 {oct(mode)[2:].zfill(4)}（应 0600）—— 它含管理台登录口令")
    if app_ctx.json:
        ui.emit_json({"password_file": str(path), "password": value})
    else:
        ui.emit(f"口令文件：{path}")
        ui.emit(value)


def _web_audit_line(record: dict) -> str:
    """一条 Web 操作审计的人读单行（文本模式与 `-f` 跟随共用）。"""
    at = str(record.get("at") or "-")
    action = str(record.get("action") or "-")
    result = str(record.get("result") or "-")
    mark = "成功" if result == "ok" else "失败"
    target = str(record.get("target") or "")
    source = str(record.get("source") or "-")
    params = record.get("params") or {}
    detail = " ".join(f"{k}={v}" for k, v in params.items()) if isinstance(params, dict) else ""
    line = f"{at} [{mark}] {action} {target} 来源={source} {detail}".rstrip()
    if result != "ok":
        line += f"（{result}）"
    return line


def _web_audit_target(app_ctx: AppContext, *, require_exists: bool = True) -> Path:
    """Web 审计文件路径。

    `require_exists=False`（`-f` 跟随）：文件可以尚不存在——`_follow_audit`
    会等待它出现（与 `plugin audit tail -f` 行为一致，v0.3.0 最终 review N7）。
    非跟随模式仍给出可行动错误。
    """
    path = web_audit.resolve_path(app_ctx.instance)
    if require_exists and not path.exists():
        raise ConfigError(
            f"Web 操作审计文件不存在：{path}",
            hint="Web 上的变更类操作（start/stop/restart/prune/rollback/config apply）与登录会写入这里",
        )
    return path


@web_audit_app.command("tail")
def web_audit_tail(
    ctx: typer.Context,
    lines: int = typer.Option(50, "--lines", "-n", min=1, max=10_000, help="显示条数"),
    follow: bool = typer.Option(False, "--follow", "-f", help="持续跟踪新记录（Ctrl-C 退出）"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出（不能与 -f 同用）"),
) -> None:
    """看 Web 操作审计的尾部（谁在什么时候改了配置 / 停了服务）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    if follow and app_ctx.json:
        raise UsageError(
            "--json 不能与 --follow 同时使用",
            hint="跟随请用文本模式；流式 JSON 请直接 tail 审计文件（每行一个 JSON 对象）",
        )
    path = _web_audit_target(app_ctx, require_exists=not follow)
    if follow and not path.exists():
        ui.emit(f"审计文件尚不存在，等待出现：{path}")

    tail = auditlog.read_tail(path, lines)
    if app_ctx.json:
        ui.emit_json({"path": str(path), "records": tail.records, "bad_lines": tail.bad_lines})
        return
    for record in tail.records:
        runtime._emit_stream_line(_web_audit_line(record))
    if tail.bad_lines:
        ui.warn(f"⚠ {tail.bad_lines} 行无法解析（进程被 kill 时最后一行可能是半截 JSON）")
    if not tail.records and not follow:
        ui.emit(f"审计文件为空：{path}")
        return
    if not follow:
        return
    runtime._follow_audit(path, _web_audit_line)


@web_audit_app.command("stats")
def web_audit_stats(
    ctx: typer.Context,
    since: str = typer.Option(
        None, "--since", help="窗口起点：24h / 7d / 30m、ISO 时间或 unix 时间戳（默认全量）"
    ),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """统计 Web 操作审计：总量 / 成功 / 失败 / 按动作与来源分布。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    path = _web_audit_target(app_ctx)
    window = auditlog.parse_since(since) if since else None
    summary = web_audit.summarize(path, since=window)

    payload = {
        "path": str(path),
        "since": window,
        "total": summary.total,
        "ok": summary.ok,
        "error": summary.error,
        "bad_lines": summary.bad_lines,
        "first_at": summary.first_at,
        "last_at": summary.last_at,
        "by_action": summary.by_action,
        "by_source": summary.by_source,
    }
    if app_ctx.json:
        ui.emit_json(payload)
        return

    ui.emit(f"审计文件：{path}")
    ui.emit(f"操作：{summary.total} 条（成功 {summary.ok} / 失败 {summary.error}）")
    if summary.bad_lines:
        ui.emit(f"坏行：{summary.bad_lines}")
    if summary.first_at is not None and summary.last_at is not None:
        first = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(summary.first_at))
        last = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(summary.last_at))
        ui.emit(f"时间范围：{first} → {last}")
    if summary.by_action:
        detail = "  ".join(f"{a}={c}" for a, c in sorted(summary.by_action.items()))
        ui.emit(f"按动作：{detail}")
    if summary.by_source:
        ui.emit("按来源：")
        for source, count in sorted(summary.by_source.items(), key=lambda item: -item[1]):
            ui.emit(f"  {source or '(未知)'}: {count}")
