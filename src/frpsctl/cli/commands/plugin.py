"""服务端插件命令组（plugin init/check/serve/user/audit/config/service）。"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
import typer
from ...core import config as cfg
from ...core import healthcheck
from ...core import serve_runtime
from ...core.auditlog import (
    DEFAULT_AUDIT_FILE,
    load_view,
    parse_since,
    read_tail,
    summarize,
)
from ...core.lock import instance_lock
from ...core.systemd import PluginService, ensure_service_account, read_template_user
from ...plugin.policy import PluginPolicy
from ...plugin.server import PluginServer, ServerSettings
from ...errors import (
    ConfigError,
    FrpsctlError,
    OwnershipConflict,
    UsageError,
)
from .. import ui

from ..app import plugin_app, plugin_user_app, plugin_audit_app, plugin_config_app, plugin_service_app
from .. import runtime
from ... import report as report_mod
from .config import _resolve_value_input


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
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    path = runtime._policy_path(app_ctx, policy)
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
            "max_mb": 10.0,
            "max_days": 7.0,
        },
        "_admin_comment": (
            "若使用 max_proxies 配额，请填写 dashboard 地址，否则计数只在本进程内有效"
            "（重启归零、多实例各算各的）"
        ),
        "admin_url": "",
        "_reject_log_comment": (
            "拒绝风暴限速：同一用户在 reject_log_window 秒内最多记录 reject_log_burst 条"
            "拒绝审计（超出部分仍被拒绝，只是不再逐条刷日志；被抑制的条数会累计汇报）"
        ),
        "reject_log_burst": 20,
        "reject_log_window": 10.0,
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
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    path, loaded = runtime._load_policy(app_ctx, policy)
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
    from ...plugin.policy import decide_login, decide_new_proxy

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
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    policy_file, loaded = runtime._load_policy(app_ctx, policy)

    # policy_path 进 settings：审计的 audit.path 相对**策略文件目录**解析
    # （读取侧同一规则），否则落点跟随 CWD，与 systemd 托管不一致。
    settings = ServerSettings(
        bind=bind, path=path, access_log=access_log, policy_path=policy_file
    )
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


@plugin_user_app.command("list")
def plugin_user_list(
    ctx: typer.Context,
    policy: Path = typer.Option(None, "--policy", help="策略文件路径"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """列出策略里的用户与权限摘要（不显示策略级凭据）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    path, loaded = runtime._load_policy(app_ctx, policy)
    if app_ctx.json:
        ui.emit_json(
            {
                "policy": str(path),
                "allow_unknown_user": loaded.allow_unknown_user,
                "require_client_id": loaded.require_client_id,
                "users": [
                    {
                        "name": user.name,
                        "allowed_ports": [item.render() for item in user.allowed_ports],
                        "allow_random_port": user.allow_random_port,
                        "allowed_proxy_types": list(user.allowed_proxy_types),
                        "allowed_proxy_names": list(user.allowed_proxy_names),
                        "max_proxies": user.max_proxies,
                        "note": user.note,
                    }
                    for user in (loaded.users[key] for key in sorted(loaded.users))
                ],
            }
        )
        return
    ui.emit(f"策略文件：{path}")
    for line in loaded.describe():
        ui.emit(f"  {line}")


@plugin_user_app.command("set")
def plugin_user_set(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="用户名"),
    ports: str = typer.Option(
        None, "--ports", help="允许的端口/端口段，逗号分隔（如 6000-6010,7000）；空串 = 清空白名单"
    ),
    types: str = typer.Option(None, "--types", help="允许的代理类型，逗号分隔（如 tcp,udp）；空串 = 不限"),
    names: str = typer.Option(
        None, "--names", help="允许的代理名通配，逗号分隔（如 'alice-*'）；空串 = 不限名称"
    ),
    max_proxies: int = typer.Option(None, "--max-proxies", min=0, help="代理数上限（0 = 不限）"),
    random_port: bool = typer.Option(False, "--random-port", help="允许 remote_port = 0（由 frps 分配）"),
    no_random_port: bool = typer.Option(False, "--no-random-port", help="不允许随机端口"),
    note: str = typer.Option(None, "--note", help="备注（空串 = 清除）"),
    policy: Path = typer.Option(None, "--policy", help="策略文件路径"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """新增或修改一个用户：只改**显式给出**的字段，其余保持原值。

    写入前用与 `plugin check` 相同的判据复验（严格类型、端口段格式、回环约束），
    并以 0600 原子写落盘；未覆盖的键（包括 `_comment` 等自定义字段）原样保留。
    插件服务在启动时载入策略——改完记得重启它才生效。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    if random_port and no_random_port:
        raise UsageError("--random-port 与 --no-random-port 不能同时给出")
    path = runtime._policy_path(app_ctx, policy)
    # 读-改-写必须串行化（与 config 写共用同一把实例锁）：两个并发
    # `plugin user set` 会各基于旧文本生成完整新文件，后写者静默覆盖前者
    # ——与第四轮修复的 `config set` 并发丢失是同一形态。
    with instance_lock(app_ctx.instance.lock):
        raw = runtime._load_policy_raw(path)
        users = raw.get("users")
        if users is None:
            users = {}
            raw["users"] = users
        if not isinstance(users, dict):
            raise ConfigError("策略文件的 users 必须是对象（用户名为键）")
        entry = users.get(name)
        if entry is None:
            entry = {}
        elif not isinstance(entry, dict):
            raise ConfigError(f"用户 {name!r} 的配置必须是对象")
        created = name not in users

        if ports is not None:
            if ports.strip():
                entry["allowed_ports"] = runtime._split_spec(ports)
            else:
                entry.pop("allowed_ports", None)
        if types is not None:
            if types.strip():
                entry["allowed_proxy_types"] = runtime._split_spec(types)
            else:
                entry.pop("allowed_proxy_types", None)
        if names is not None:
            if names.strip():
                entry["allowed_proxy_names"] = runtime._split_spec(names)
            else:
                entry.pop("allowed_proxy_names", None)
        if max_proxies is not None:
            entry["max_proxies"] = max_proxies
        if random_port:
            entry["allow_random_port"] = True
        if no_random_port:
            entry["allow_random_port"] = False
        if note is not None:
            if note:
                entry["note"] = note
            else:
                entry.pop("note", None)
        users[name] = entry

        loaded = runtime._save_policy_raw(path, raw)
    if app_ctx.json:
        ui.emit_json({"policy": str(path), "user": name, "created": created})
        return
    verb = "新增" if created else "更新"
    ui.emit(f"已{verb}用户 {name!r}（{path}）")
    user = loaded.user(name)
    assert user is not None
    quota = f"，最多 {user.max_proxies} 个代理" if user.max_proxies else ""
    ui.emit(f"  端口 {user.render_ports()}{quota}")
    ui.emit("")
    ui.emit("提示：`frpsctl plugin check` 可离线复核裁决；插件服务需重启后载入新策略。")


@plugin_user_app.command("remove")
def plugin_user_remove(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="用户名", autocompletion=runtime._complete_policy_user),
    policy: Path = typer.Option(None, "--policy", help="策略文件路径"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """删除一个用户（不存在时报配置错误，不静默成功）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    path = runtime._policy_path(app_ctx, policy)
    with instance_lock(app_ctx.instance.lock):
        raw = runtime._load_policy_raw(path)
        users = raw.get("users")
        if not isinstance(users, dict) or name not in users:
            raise ConfigError(
                f"策略里没有用户 {name!r}", hint="用 `frpsctl plugin user list` 查看现有用户"
            )
        del users[name]
        runtime._save_policy_raw(path, raw)
    if app_ctx.json:
        ui.emit_json({"policy": str(path), "user": name, "removed": True})
        return
    ui.emit(f"已删除用户 {name!r}（{path}）")
    ui.emit("提示：插件服务需重启后载入新策略。")


def _audit_line(record: dict) -> str:
    """一条审计记录的人读单行（文本模式与 `-f` 跟随共用）。"""
    at = str(record.get("at") or "-")
    decision = str(record.get("decision") or "?")
    mark = "允许" if decision == "allow" else "拒绝"
    user = str(record.get("user") or "-")
    op = str(record.get("op") or "-")
    port = record.get("remote_port")
    port_text = f" port={port}" if port else ""
    detail = str(record.get("reason") or "")
    line = f"{at} [{mark}] {op} {user}{port_text} {detail}".rstrip()
    suppressed = int(record.get("suppressed") or 0)
    if suppressed:
        # 降级必须可见：限速期间被抑制的同类拒绝条数如实附上
        line += f"（此前 {suppressed} 条同类拒绝被限速抑制）"
    return line


def _audit_target(view) -> Path:
    """把审计视图收口成"可读的文件路径"；不可用时给出可行动的错误(3)。"""
    if not view.available:
        raise ConfigError(
            view.reason or "策略文件不可用",
            hint="先运行 `frpsctl plugin init` 生成策略模板，或用 --policy 指定路径",
        )
    if not view.enabled:
        raise ConfigError(
            "审计已被策略关闭（audit.enabled = false）",
            hint="用 `frpsctl plugin config set audit.enabled true` 开启后重试",
        )
    if view.path is None:
        raise ConfigError(
            "审计未配置落盘路径（audit.path = null，仅内存）",
            hint="用 `frpsctl plugin config set audit.path plugin-audit.jsonl` 指定路径",
        )
    return view.path


@plugin_audit_app.command("tail")
def plugin_audit_tail(
    ctx: typer.Context,
    lines: int = typer.Option(50, "--lines", "-n", min=1, max=10_000, help="显示条数"),
    follow: bool = typer.Option(False, "--follow", "-f", help="持续跟踪新记录（Ctrl-C 退出）"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出（不能与 -f 同用）"),
    policy: Path = typer.Option(None, "--policy", help="策略文件路径"),
) -> None:
    """看审计日志的尾部（JSONL，与 `frpsctl log` 同规格的反向读取）。

    `-f` 跟随新记录（含日志轮转重开）：插件是登录单点，排查"某用户为什么
    登录不了"时这是第一手现场。`--json` 是**一次性**导出，因此与 `-f` 互斥
    ——流式 JSONL 请直接消费审计文件本身。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    view = load_view(app_ctx.instance, policy_override=policy)
    path = _audit_target(view)
    if follow and app_ctx.json:
        raise UsageError(
            "--json 不能与 --follow 同时使用",
            hint="跟随请用文本模式；流式 JSON 请直接 tail 审计文件（每行一个 JSON 对象）",
        )

    tail = read_tail(path, lines)
    if app_ctx.json:
        ui.emit_json(
            {
                "policy": str(view.policy_path),
                "path": str(path),
                "records": tail.records,
                "bad_lines": tail.bad_lines,
            }
        )
        return

    for record in tail.records:
        runtime._emit_stream_line(_audit_line(record))
    if tail.bad_lines:
        ui.warn(f"⚠ {tail.bad_lines} 行无法解析（进程被 kill 时最后一行可能是半截 JSON）")
    if not tail.records and not follow:
        ui.emit(f"审计文件为空或不存在：{path}")
        return

    if not follow:
        return
    runtime._follow_audit(path, _audit_line)


@plugin_audit_app.command("stats")
def plugin_audit_stats(
    ctx: typer.Context,
    since: str = typer.Option(
        None, "--since", help="窗口起点：24h / 7d / 30m、ISO 时间或 unix 时间戳（默认全量）"
    ),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    policy: Path = typer.Option(None, "--policy", help="策略文件路径"),
) -> None:
    """统计审计：总量 / 允许 / 拒绝 / 用户与操作分布 / 限速抑制累计。

    流式扫描整个审计文件（只计数、不驻留内存）；大文件配合 `--since` 限定
    窗口更快。坏行计入 `bad_lines` 并如实报告——它通常意味着进程被 kill 时
    最后一行是半截 JSON。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    view = load_view(app_ctx.instance, policy_override=policy)
    path = _audit_target(view)
    window = parse_since(since) if since else None

    summary = summarize(path, since=window)
    if app_ctx.json:
        ui.emit_json(
            {
                "policy": str(view.policy_path),
                "path": str(path),
                "since": window,
                # 统计体与 Web 审计视图共用（frpsctl.report.audit_summary_payload）
                **report_mod.audit_summary_payload(summary),
            }
        )
        return

    ui.emit(f"审计文件：{path}")
    ui.emit(f"记录：{summary.total} 条（允许 {summary.allow} / 拒绝 {summary.deny}）")
    if summary.elapsed_count:
        ui.emit(
            f"裁决耗时：平均 {summary.elapsed_avg_ms:.1f} ms / "
            f"最大 {summary.elapsed_max_ms:.1f} ms（{summary.elapsed_count} 条带耗时）"
        )
    if summary.suppressed_total:
        ui.emit(f"限速抑制：{summary.suppressed_total} 条同类拒绝未逐条记录")
    if summary.bad_lines:
        ui.emit(f"坏行：{summary.bad_lines}（进程被 kill 时最后一行可能是半截 JSON）")
    if summary.first_at is not None and summary.last_at is not None:
        first = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(summary.first_at))
        last = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(summary.last_at))
        ui.emit(f"时间范围：{first} → {last}")
    if summary.by_op:
        detail = "  ".join(f"{op}={count}" for op, count in sorted(summary.by_op.items()))
        ui.emit(f"按操作：{detail}")
    if summary.by_user:
        ui.emit("按用户：")
        for user, bucket in sorted(summary.by_user.items()):
            ui.emit(f"  {user or '(未声明)'}: 允许 {bucket['allow']} / 拒绝 {bucket['deny']}")


_POLICY_KEYS: dict[str, str] = {
    "allow_unknown_user": "布尔：未列出的用户是否放行（默认 false；true = 鉴权形同虚设）",
    "require_client_id": "布尔：是否要求 client_id 与 user 一致（默认 true）",
    "reject_log_burst": "整数：拒绝风暴限速——窗口内最多记录几条 deny（默认 20）",
    "reject_log_window": "数值：限速窗口秒数（默认 10）",
    "admin_url": "字符串：dashboard 地址（max_proxies 配额读取权威计数用）",
    "admin_user": "字符串：dashboard 用户",
    "admin_password": "字符串：dashboard 口令（敏感，建议 --stdin）",
    "audit.enabled": "布尔：是否开启审计（默认 true）",
    "audit.path": "字符串：审计文件路径（相对策略文件目录）；字面 null = 仅内存",
    "audit.max_mb": "数值：审计文件轮转阈值（MB）；0 = 不按大小轮转（文件会无限增长）",
    "audit.max_days": "数值：审计文件轮转年龄（天）；0 = 不按天轮转",
}


def _complete_policy_key(ctx, args, incomplete):  # noqa: ANN001, ARG001 - Typer 补全接口
    """`plugin config set <TAB>`：可编辑的策略级字段名。"""
    return [key for key in _POLICY_KEYS if key.startswith(incomplete or "")]


#: `audit.*` 字段缺失时的默认值（必须与 `plugin/policy.py` 的
#: `AuditSettings` 字段默认一致——v0.3.0 review 曾写死 `True`，导致新增的
#: `max_mb` / `max_days` 在老策略上显示为布尔值）。
_AUDIT_DEFAULTS: dict[str, object] = {
    "enabled": True,
    "path": DEFAULT_AUDIT_FILE,
    "flush_every": 32,
    "flush_interval": 2.0,
    "max_mb": 10.0,
    "max_days": 7.0,
}


def _policy_value(raw: dict, key: str):
    """从策略字典取 `key` 的当前值（`audit.x` 走子对象，缺失用字段默认值）。"""
    if key.startswith("audit."):
        field = key.split(".", 1)[1]
        fallback = _AUDIT_DEFAULTS.get(field, True)
        audit = raw.get("audit")
        if not isinstance(audit, dict):
            return fallback
        return audit.get(field, fallback)
    return raw.get(key)


@plugin_config_app.command("list")
def plugin_config_list(
    ctx: typer.Context,
    policy: Path = typer.Option(None, "--policy", help="策略文件路径"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """列出策略级设置（不显示用户表——那用 `plugin user list`）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    path = runtime._policy_path(app_ctx, policy)
    raw = runtime._load_policy_raw(path)
    if app_ctx.json:
        ui.emit_json(
            {
                "policy": str(path),
                "settings": {
                    key: (
                        "***"
                        if key == "admin_password" and _policy_value(raw, key)
                        else _policy_value(raw, key)
                    )
                    for key in _POLICY_KEYS
                },
            }
        )
        return
    ui.emit(f"策略文件：{path}")
    for key, description in _POLICY_KEYS.items():
        value = _policy_value(raw, key)
        if key == "admin_password" and value:
            value = "***"
        ui.emit(f"  {key} = {json.dumps(value, ensure_ascii=False)}")
        ui.emit(f"      {description}")
    ui.emit("")
    ui.emit("用户表：`frpsctl plugin user list`；修改后插件服务重启才生效。")


@plugin_config_app.command("set")
def plugin_config_set(
    ctx: typer.Context,
    key: str = typer.Argument(
        ...,
        help="字段名（见 `plugin config list`）",
        autocompletion=_complete_policy_key,
    ),
    value: str = typer.Argument(None, help="新值；或用 --stdin / --prompt（敏感值不进 argv）"),
    read_stdin: bool = typer.Option(False, "--stdin", help="从标准输入读值"),
    prompt: bool = typer.Option(False, "--prompt", help="交互式隐藏输入值"),
    policy: Path = typer.Option(None, "--policy", help="策略文件路径"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """设置一个策略级字段，写入前用与 `plugin check` 相同的判据复验。

    未知字段直接拒绝（ADR-7：不猜测）——拼错字段名的"成功写入"会让人以为
    某个安全开关生效了。`audit.path` 写字面 `null` 表示"仅内存"。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    if key not in _POLICY_KEYS:
        raise UsageError(
            f"未知的策略字段：{key!r}",
            hint="可用字段：" + "、".join(_POLICY_KEYS),
        )
    raw_value = _resolve_value_input(value, read_stdin=read_stdin, prompt=prompt)
    parsed = _parse_policy_value(key, raw_value)

    path = runtime._policy_path(app_ctx, policy)
    with instance_lock(app_ctx.instance.lock):
        raw = runtime._load_policy_raw(path)
        before = _policy_value(raw, key)
        if key.startswith("audit."):
            audit = raw.get("audit")
            if audit is None:
                audit = {}
                raw["audit"] = audit
            elif not isinstance(audit, dict):
                # 不静默覆盖非法值：把它改掉等于替用户"修正"了配置，而严格
                # 校验（plugin check / serve）会用另一套判断——两处不一致。
                raise ConfigError(
                    f"策略里的 audit 不是对象（实际是 {type(audit).__name__}）",
                    hint="先用 `frpsctl plugin check` 查看详情并修正策略文件",
                )
            audit[key.split(".", 1)[1]] = parsed
        else:
            raw[key] = parsed
        runtime._save_policy_raw(path, raw)  # 严格复验 + 0600 原子写

    shown_before = "***" if key == "admin_password" and before else before
    shown_after = "***" if key == "admin_password" and parsed else parsed
    if app_ctx.json:
        ui.emit_json(
            {
                "policy": str(path),
                "key": key,
                "before": shown_before,
                "after": shown_after,
            }
        )
        return
    before_text = json.dumps(shown_before, ensure_ascii=False)
    after_text = json.dumps(shown_after, ensure_ascii=False)
    ui.emit(f"{key}: {before_text} → {after_text}")
    if key == "allow_unknown_user" and parsed is True:
        ui.warn("⚠ allow_unknown_user 已开启：未列出的用户会被放行，鉴权形同虚设")
    ui.emit("提示：插件服务重启后载入新策略（`frpsctl plugin service status` 查看托管情况）。")


def _parse_policy_value(key: str, raw: str):
    """把命令行字符串转成策略字段的类型（严格：类型写错直接拒绝）。"""
    text = raw.strip()
    if key in ("allow_unknown_user", "require_client_id", "audit.enabled"):
        lowered = text.lower()
        if lowered not in ("true", "false"):
            raise UsageError(f"{key} 必须是 true / false，实际是 {raw!r}")
        return lowered == "true"
    if key == "reject_log_burst":
        try:
            parsed = int(text)
        except ValueError:
            raise UsageError(f"{key} 必须是整数，实际是 {raw!r}") from None
        if parsed < 1:
            raise UsageError(f"{key} 必须 >= 1，实际是 {parsed}")
        return parsed
    if key == "reject_log_window":
        try:
            parsed = float(text)
        except ValueError:
            raise UsageError(f"{key} 必须是数字，实际是 {raw!r}") from None
        if parsed < 0.1:
            raise UsageError(f"{key} 必须 >= 0.1，实际是 {parsed}")
        return parsed
    if key in ("audit.max_mb", "audit.max_days"):
        try:
            parsed_mb = float(text)
        except ValueError:
            raise UsageError(f"{key} 必须是数字，实际是 {raw!r}") from None
        if parsed_mb < 0:
            raise UsageError(f"{key} 不能为负，实际是 {parsed_mb}")
        return parsed_mb
    if key == "audit.path":
        # 字面 null = 仅内存（JSON 语义；空串会被值输入层按"漏填"拒绝）
        return None if text == "null" else raw
    return raw


@plugin_service_app.command("install")
def plugin_service_install(
    ctx: typer.Context,
    bind: str = typer.Option("127.0.0.1:8080", "--bind", help="监听地址（必须回环）"),
    handler_path: str = typer.Option(
        "/handler", "--path", help="回调路径（需与 frps 的 httpPlugins.path 一致）"
    ),
    policy: Path = typer.Option(None, "--policy", help="策略文件（默认 <实例>/plugin-policy.json）"),
    user: str = typer.Option(
        None, "--user", help="运行插件的系统用户（默认：优先 frps，其次当前用户）"
    ),
    group: str = typer.Option(
        None, "--group", help="运行插件的系统组（默认：同名组，缺失则用户主组）"
    ),
    create_user: bool = typer.Option(
        False, "--create-user", help="服务用户不存在时自动创建系统账户（需 root；须配合 --user）"
    ),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的 unit 模板"),
    access_log: bool = typer.Option(
        False, "--access-log", help="把逐请求日志写进 journald（排查用；unit 模板为全部实例共享）"
    ),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """安装 frpsctl-plugin@.service 并 enable（需要 root）。

    注意：unit 模板（frpsctl-plugin@.service）为**全部实例共享**——bind、策略
    路径、access-log 与服务用户等都写在这一个模板里，多实例环境里重装会覆盖
    这些参数。任何账户都可以作为服务用户（§12.2）。

    渲染前体检：服务账户存在、frpsctl 对服务用户可达且不在家目录
    （`ProtectHome=true`）、策略文件存在且合法、绑定地址为回环。
    任何一项不满足都当场拒绝——装一个起不来的 unit 比不装更浪费时间。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    policy_path, _ = runtime._load_policy(app_ctx, policy)  # 不存在/非法 JSON 在这里就会拒绝
    service = PluginService(app_ctx.instance)
    runtime._guard_against_direct(app_ctx.instance, serve_runtime.PLUGIN_SPEC)
    # 先定位 frpsctl 可执行文件（unit 的 ExecStart），再解析/创建账户——
    # 避免"账户已建、安装却因 ExecStart 路径不可用失败"（review 收口）。
    exec_start = runtime._frpsctl_executable()
    identity = ensure_service_account(user, group, create_user=create_user)
    if force:
        existing = read_template_user(service.template_path)
        if existing is not None and existing != identity.user:
            ui.warn(
                f"⚠ unit 模板为全部实例共享：{service.template_path} 的 User= "
                f"将从 {existing} 改为 {identity.user}（影响所有使用该模板的实例）"
            )
    path = service.install_template(
        exec_start=exec_start,
        policy=policy_path,
        bind=bind,
        handler_path=handler_path,
        force=force,
        user=identity.user,
        group=identity.group,
        access_log=access_log,
    )
    # 账户告警（创建/组回退/root 安全）**无条件**进 stderr：JSON 模式也不豁免。
    for warning in runtime._identity_warnings(identity):
        ui.warn(warning)
    if app_ctx.json:
        ui.emit_json(
            {
                "unit": service.unit_name,
                "template": str(path),
                "bind": bind,
                "policy": str(policy_path),
                "user": identity.user,
                "group": identity.group,
                "user_source": identity.source,
                "created_user": identity.created,
            }
        )
        return
    ui.emit(f"已安装 {path}")
    ui.emit(runtime._identity_summary(identity))
    ui.emit(f"实例 unit：{service.unit_name}（User={identity.user}, Group={identity.group}）")
    ui.emit("")
    ui.emit("启动：frpsctl plugin service start（该命令在此，无需手工 systemctl）")
    ui.emit("已 enable（开机自启）；停止/重启/停用：plugin service stop|restart|uninstall")


@plugin_service_app.command("start")
def plugin_service_start(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """启动插件服务（systemd，需要 root）。

    与 `frpsctl start` 的区别：那是 frps 进程的生命周期，这里动的是**插件
    服务**这个独立 unit（`frpsctl-plugin@<实例>`）。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    service = PluginService(app_ctx.instance)
    runtime._guard_against_direct(app_ctx.instance, serve_runtime.PLUGIN_SPEC)
    service.start()
    if app_ctx.json:
        ui.emit_json({"unit": service.unit_name, "started": True})
    else:
        ui.emit(f"已启动 {service.unit_name}")
        ui.emit("查看状态：frpsctl plugin service status")


@plugin_service_app.command("stop")
def plugin_service_stop(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """停止插件服务（systemd，需要 root）。⚠️ 停止期间所有客户端都无法登录（fail-closed）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    service = PluginService(app_ctx.instance)
    service.stop()
    # 告警**无条件**进 stderr：即使 --json（脚本收集 stderr 时也必须看到）
    # ——插件 fail-closed，停它等于停掉所有人的登录入口。
    ui.warn("⚠ 插件已停止：期间所有客户端都无法登录（fail-closed），请尽快恢复")
    if app_ctx.json:
        ui.emit_json({"unit": service.unit_name, "stopped": True})
    else:
        ui.emit(f"已停止 {service.unit_name}")


@plugin_service_app.command("restart")
def plugin_service_restart(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """重启插件服务（systemd，需要 root）——改完策略后让它载入新配置的常用动作。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    service = PluginService(app_ctx.instance)
    runtime._guard_against_direct(app_ctx.instance, serve_runtime.PLUGIN_SPEC)
    service.restart()
    if app_ctx.json:
        ui.emit_json({"unit": service.unit_name, "restarted": True})
    else:
        ui.emit(f"已重启 {service.unit_name}（策略已重新载入）")


@plugin_service_app.command("uninstall")
def plugin_service_uninstall(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """停用并删除插件 unit 模板（需要 root）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
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
    """显示插件服务的 systemd 托管状态（active 与 enabled 分开报告）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    service = PluginService(app_ctx.instance)
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


# ---------------------------------------------------------------------------
# 后台运行（direct 模式，v0.3.3；非 systemd 环境）
# ---------------------------------------------------------------------------


def _start_direct_plugin(
    app_ctx,
    *,
    policy: Path | None,
    bind: str | None,
    handler_path: str | None,
    access_log: bool | None,
    fallback_args: dict | None = None,
) -> tuple[serve_runtime.ServeState, Path, PluginPolicy]:
    """direct 后台启动插件的共同实现（start 与 restart 共用）。

    `fallback_args`：restart 场景**先读后停**保存下来的旧参数（同 web 的
    review 修复）。
    """
    inst = app_ctx.instance
    runtime._guard_against_systemd(inst, serve_runtime.PLUGIN_SPEC)

    last, error = serve_runtime.read_state(inst, serve_runtime.PLUGIN_SPEC)
    if error is not None:
        raise ConfigError(error, hint="删除状态文件后重试（会重建）")
    old = dict(last.args) if last is not None else dict(fallback_args or {})

    resolved_policy = policy
    if resolved_policy is None and old.get("policy"):
        resolved_policy = Path(str(old["policy"]))
    policy_file, loaded = runtime._load_policy(app_ctx, resolved_policy)

    resolved_bind = str(bind if bind is not None else old.get("bind") or "127.0.0.1:8080")
    resolved_path = str(
        handler_path if handler_path is not None else old.get("path") or "/handler"
    )
    resolved_access = bool(access_log) if access_log is not None else bool(old.get("access_log", False))
    host, port = healthcheck.parse_bind(resolved_bind, default_port=8080)
    if not healthcheck.is_loopback(host):
        # 插件协议没有任何认证：非回环直接拒绝（与 serve/安装体检同一判据）
        raise UsageError(
            f"插件拒绝绑定非回环地址：{resolved_bind}",
            hint="任何能访问该端口的人都能伪造 Login/NewProxy 事件；请绑 127.0.0.1",
        )

    argv = [
        str(runtime._frpsctl_executable()),
        "plugin",
        "serve",
        "--policy",
        str(policy_file),
        "--bind",
        resolved_bind,
        "--path",
        resolved_path,
    ]
    if resolved_access:
        argv.append("--access-log")
    args = {
        "policy": str(policy_file),
        "bind": resolved_bind,
        "path": resolved_path,
        "access_log": resolved_access,
    }
    state = serve_runtime.start_background(
        inst, serve_runtime.PLUGIN_SPEC, argv=argv, args=args, host=host, port=port
    )
    return state, policy_file, loaded


@plugin_app.command("start")
def plugin_start(
    ctx: typer.Context,
    policy: Path = typer.Option(None, "--policy", help="策略文件（默认复用上次参数，否则实例默认）"),
    bind: str = typer.Option(None, "--bind", help="绑定地址（默认复用上次参数，否则 127.0.0.1:8080）"),
    path: str = typer.Option(None, "--path", help="回调路径（默认复用上次参数，否则 /handler）"),
    access_log: bool = typer.Option(None, "--access-log", help="把每个请求打进日志文件"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """后台启动插件服务（direct 模式；非 systemd 环境使用）。

    子进程就是 `plugin serve`（收到 SIGTERM 会先刷审计再退出），进程与启动
    参数记录在 `<实例>/plugin-state.json`；停止用 `plugin stop`。

    ⚠️ 与 systemd 托管**互斥**：由 systemd 托管时请用 `plugin service start`。
    插件是全部客户端登录的单点（fail-closed），变更策略后需 `plugin restart`。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    state, policy_file, loaded = _start_direct_plugin(
        app_ctx, policy=policy, bind=bind, handler_path=path, access_log=access_log
    )
    if app_ctx.json:
        ui.emit_json(
            {
                "owner": "direct",
                "pid": state.pid,
                "bind": state.args.get("bind"),
                "path": state.args.get("path"),
                "policy": str(policy_file),
                "users": sorted(loaded.users),
                "log": state.log,
            }
        )
        return
    ui.emit(f"插件服务已在后台启动（pid {state.pid}）")
    ui.emit(f"监听：http://{state.args.get('bind')}{state.args.get('path')}")
    ui.emit(f"策略：{policy_file}")
    ui.emit(f"日志：tail -f {state.log}")
    ui.emit("⚠ fail-closed：本服务不可达时所有客户端都无法登录，请确保它会随机器恢复")


@plugin_app.command("stop")
def plugin_stop(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """停止后台运行的插件服务（SIGTERM 优雅退出并先刷审计）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    inst = app_ctx.instance
    service = PluginService(inst)
    if service.unit_exists() and service.is_active():
        raise OwnershipConflict(
            "插件服务由 systemd 托管且处于 active",
            hint="用 `plugin service stop`；direct 后台模式没有在运行的实例",
        )
    state = serve_runtime.stop_background(inst, serve_runtime.PLUGIN_SPEC)
    # ⚠ 无条件进 stderr（同 `plugin service stop` 先例）：停止期间不可登录
    ui.warn("⚠ 插件已停止：期间所有客户端都无法登录（fail-closed），请尽快恢复")
    if app_ctx.json:
        ui.emit_json({"owner": "direct", "stopped": True, "pid": state.pid})
    else:
        ui.emit(f"已停止插件服务（pid {state.pid}），审计缓冲已随优雅退出刷盘")


@plugin_app.command("restart")
def plugin_restart(
    ctx: typer.Context,
    policy: Path = typer.Option(None, "--policy", help="策略文件（默认复用上次参数）"),
    bind: str = typer.Option(None, "--bind", help="绑定地址（默认复用上次参数）"),
    path: str = typer.Option(None, "--path", help="回调路径（默认复用上次参数）"),
    access_log: bool = typer.Option(None, "--access-log", help="把每个请求打进日志文件"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """重启后台运行的插件服务（未在运行时直接启动）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    inst = app_ctx.instance
    # 参数复用必须**先读后停**：stop 会删除状态文件（v0.3.3 review 修复）
    last, error = serve_runtime.read_state(inst, serve_runtime.PLUGIN_SPEC)
    if error is not None:
        raise ConfigError(error, hint="删除状态文件后重试（会重建）")
    old_args = dict(last.args) if last is not None else {}
    with contextlib.suppress(FrpsctlError):
        serve_runtime.stop_background(inst, serve_runtime.PLUGIN_SPEC)
    state, policy_file, loaded = _start_direct_plugin(
        app_ctx,
        policy=policy,
        bind=bind,
        handler_path=path,
        access_log=access_log,
        fallback_args=old_args,
    )
    if app_ctx.json:
        ui.emit_json(
            {
                "owner": "direct",
                "restarted": True,
                "pid": state.pid,
                "policy": str(policy_file),
                "users": sorted(loaded.users),
            }
        )
        return
    ui.emit(f"插件服务已重启（pid {state.pid}）：http://{state.args.get('bind')}{state.args.get('path')}")
    ui.emit(f"日志：tail -f {state.log}")


@plugin_app.command("status")
def plugin_status(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """插件服务的托管状态（systemd / direct / 未运行 的统一视图）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    inst = app_ctx.instance
    service = PluginService(inst)

    probe_error: str | None = None
    systemd_active = False
    if service.available:
        try:
            systemd_active = service.is_active()
        except FrpsctlError as exc:
            probe_error = exc.message
    direct = serve_runtime.probe(inst, serve_runtime.PLUGIN_SPEC)

    owner = "none"
    pid: int | None = None
    main_pid: int | None = None
    if systemd_active:
        owner = "systemd"
        with contextlib.suppress(FrpsctlError):
            main_pid = service.main_pid()
        pid = main_pid
    elif direct.running:
        owner = "direct"
        pid = direct.state.pid
    elif direct.owner is serve_runtime.ServeOwner.CORRUPTED:
        owner = "corrupted"
    elif direct.owner is serve_runtime.ServeOwner.FOREIGN:
        owner = "foreign"

    if app_ctx.json:
        ui.emit_json(
            {
                "owner": owner,
                "active": systemd_active or direct.running,
                "pid": pid,
                "uptime_seconds": direct.uptime_seconds if direct.running else None,
                "unit": service.unit_name,
                "main_pid": main_pid,
                "bind": (direct.state.args.get("bind") if direct.state else None),
                "path": (direct.state.args.get("path") if direct.state else None),
                "policy": (direct.state.args.get("policy") if direct.state else None),
                "log": (direct.state.log if direct.state else None),
                "error": direct.error or probe_error,
            }
        )
        return
    if owner == "systemd":
        ui.emit("owner    : systemd")
        ui.emit(f"unit     : {service.unit_name}")
        ui.emit(f"active   : True（MainPID {main_pid if main_pid is not None else '-'}）")
    elif owner == "direct":
        ui.emit("owner    : direct（非 systemd 后台）")
        ui.emit("active   : True")
        ui.emit(f"pid      : {pid}")
        ui.emit(f"bind     : {direct.state.args.get('bind')}{direct.state.args.get('path')}")
        ui.emit(f"policy   : {direct.state.args.get('policy')}")
        ui.emit(f"log      : {direct.state.log}")
    elif owner == "corrupted":
        ui.emit("owner    : direct（状态文件损坏，无法判定是否在运行）")
        ui.emit(f"error    : {direct.error}")
        ui.emit("处置     : 确认插件服务没有在跑后，删除状态文件重试")
    elif owner == "foreign":
        ui.emit("owner    : direct（状态指向的 pid 存活但不属于本服务）")
        ui.emit(f"pid      : {direct.state.pid}")
    else:
        ui.emit("owner    : none（未运行）")
    if probe_error:
        ui.warn(f"⚠ systemd 探测失败（状态按 direct 判定）：{probe_error}")
