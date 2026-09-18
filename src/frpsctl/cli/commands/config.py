"""配置读写与变更闭环命令（config 子命令组）。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
import typer
from ...core import config as cfg
from ...core.transaction import apply_sets
from ...core.transaction import (
    apply_edit,
    apply_set,
    apply_unset,
    rollback_to,
    snapshot_diff,
)
from ...errors import (
    ConfigError,
    ConfigKeyMissing,
    UsageError,
)
from .. import ui
from ..context import AppContext

from ..app import config_app
from .. import runtime


@config_app.command("get")
def config_get(
    ctx: typer.Context,
    key: str = typer.Argument(
        ..., help="点分键，如 transport.tls.force", autocompletion=runtime._complete_config_key
    ),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    reveal: bool = typer.Option(False, "--reveal", help="显示敏感值（默认打码）"),
) -> None:
    """读单个键。敏感键默认打码（§10 硬约束 2）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
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
    app_ctx = runtime._ctx(ctx).with_json(json_output)
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
    key: str = typer.Argument(
        ..., help="点分键，如 bindPort", autocompletion=runtime._complete_config_key
    ),
    value: str = typer.Argument(None, help="新值；或用 --stdin / --prompt 提供（敏感值不进 argv）"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    no_restart: bool = typer.Option(False, "--no-restart", help="只写不重启（变更尚未生效）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只校验并展示 diff，不写入、不重启"),
    read_stdin: bool = typer.Option(
        False, "--stdin", help="从标准输入读值（敏感值不进 argv 与 shell 历史）"
    ),
    prompt: bool = typer.Option(
        False, "--prompt", help="交互式隐藏输入值（敏感值不进 argv 与 shell 历史）"
    ),
    health_timeout: float = typer.Option(
        10.0, "--health-timeout", min=0, help="健康检查等待秒数"
    ),
) -> None:
    """写单个键，走 §9 事务闭环（校验 → 备份 → 原子替换 → 重启 → 失败回滚）。

    值的三种来源互斥：位置参数 / `--stdin` / `--prompt`。令牌与口令建议用后两者：
    位置参数会进入 shell 历史，也会出现在 `/proc/<pid>/cmdline` 里。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    raw = _resolve_value_input(value, read_stdin=read_stdin, prompt=prompt)
    lc = runtime._lifecycle(app_ctx)

    # 候选生成（plan）与落盘在**同一把实例锁内**完成（apply_set）：锁外生成
    # 候选会被并发变更静默覆盖（第四轮 review 实测复现）。noop 同样锁内判定。
    outcome = apply_set(
        app_ctx.instance,
        dotted=key,
        raw=raw,
        lifecycle=lc,
        restart=not no_restart,
        health_timeout=health_timeout,
        dry_run=dry_run,
    )
    _render_change_outcome(app_ctx, key, outcome)


@config_app.command("unset")
def config_unset(
    ctx: typer.Context,
    key: str = typer.Argument(
        ..., help="要删除的点分键（回落 frp 默认值）", autocompletion=runtime._complete_config_key
    ),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    no_restart: bool = typer.Option(False, "--no-restart", help="只写不重启（变更尚未生效）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只校验并展示 diff，不写入、不重启"),
    health_timeout: float = typer.Option(
        10.0, "--health-timeout", min=0, help="健康检查等待秒数"
    ),
) -> None:
    """删除一个键，让它回落到 frp 的默认值。

    与 `config set` 走**同一事务闭环**（校验 → 快照 → 原子替换 → 重启 → 失败
    自动回滚）；危险组合检查照常生效（删除口令后"非回环 + 无凭据"会被拒绝）。
    键不存在时是配置错误(3)：拼错键名的"成功删除"会让人以为清掉了某个设置。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    lc = runtime._lifecycle(app_ctx)
    outcome = apply_unset(
        app_ctx.instance,
        dotted=key,
        lifecycle=lc,
        restart=not no_restart,
        health_timeout=health_timeout,
        dry_run=dry_run,
    )
    _render_change_outcome(app_ctx, key, outcome)


def _resolve_value_input(value: str | None, *, read_stdin: bool, prompt: bool) -> str:
    """解析 `config set` 的值来源：位置参数 / `--stdin` / `--prompt`（三者互斥）。

    敏感值走 argv 会进 shell 历史与 `/proc/<pid>/cmdline`——`--stdin`（脚本化）
    与 `--prompt`（交互）是替代通道。空值一律拒绝：清空字符串值请显式写 `'""'`，
    删除键请用 `config unset`（空输入几乎总是误操作，而不是"我想写空"）。
    """
    sources = [
        name
        for name, present in (("值参数", value is not None), ("--stdin", read_stdin), ("--prompt", prompt))
        if present
    ]
    if len(sources) > 1:
        raise UsageError(f"值的来源只能有一个，同时给了：{' 与 '.join(sources)}")
    if read_stdin:
        import sys

        line = sys.stdin.readline()
        if line == "":
            raise UsageError("--stdin 没有读到任何内容")
        text = line.rstrip("\n").rstrip("\r")
    elif prompt:
        import getpass

        try:
            text = getpass.getpass("新值（输入不回显）：")
        except EOFError:
            raise UsageError(
                "未能读取输入：--prompt 需要交互式终端",
                hint="在脚本 / 管道里请改用 --stdin（如：echo -n \"$PW\" | frpsctl ... --stdin）",
            ) from None
    elif value is None:
        raise UsageError(
            "缺少值：给出位置参数，或用 --stdin / --prompt",
            hint="示例：frpsctl config set webServer.password --prompt",
        )
    else:
        text = value
    if not text.strip():
        # 纯空白与空输入同视：它们几乎总是误操作（漏填变量、多敲了空格），
        # 而不是"我想写入空白"。显式空串请写 '""'，删键请用 `config unset`。
        raise UsageError(
            "值不能为空",
            hint='如需把某个字符串值清空，写入 \'""\'；如需删除键，请用 `frpsctl config unset`',
        )
    return text


def _json_change_value(key: str, value: object) -> object:
    """变更输出里的值：敏感键打码；`None` 原样（表示"已删除/不存在"）。"""
    if value is None:
        return None
    return ui.mask_secret(value) if cfg.is_secret_key(key) else value


def _render_change_outcome(app_ctx: AppContext, key: str, outcome) -> None:
    """`config set` / `config unset` 共用的人读与 `--json` 渲染。"""
    if outcome.noop:
        if app_ctx.json:
            ui.emit_json(
                {
                    "key": key,
                    "before": _json_change_value(key, outcome.before),
                    "after": _json_change_value(key, outcome.after),
                    "applied": False,
                    "restarted": False,
                    "noop": True,
                }
            )
        else:
            # 敏感键同样打码（§10 硬约束 2）：noop 时 before==after，明文会把
            # 当前口令/token 打到终端与重定向文件里（v0.3.0 review）。
            ui.emit(f"{key} 已经是 {_json_change_value(key, outcome.after)}，无需变更")
        return

    if app_ctx.json:
        ui.emit_json(
            {
                "key": outcome.dotted,
                "before": _json_change_value(key, outcome.before),
                "after": _json_change_value(key, outcome.after),
                "applied": outcome.applied,
                "restarted": outcome.restarted,
                "note": outcome.note,
                "dry_run": outcome.dry_run,
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

    app_ctx = runtime._ctx(ctx)
    lc = runtime._lifecycle(app_ctx)
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
    steps: int = typer.Option(1, "--steps", min=1, help="与第 N 新的快照比较"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """当前配置 vs 历史快照（unified diff）。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    # 快照选择与读取在 core 的 snapshot_diff 里（锁内完成）——与 Web 的
    # "查看差异"共用同一份实现与边界错误。
    result = snapshot_diff(app_ctx.instance, steps=steps)
    if app_ctx.json:
        ui.emit_json({"snapshot": str(result.snapshot), "diff": cfg.mask_diff(result.diff)})
    else:
        ui.emit(cfg.mask_diff(result.diff).rstrip() or "(无差异)")
        ui.emit("")
        ui.emit(f"# 快照：{result.snapshot.name}")


@config_app.command("rollback")
def config_rollback(
    ctx: typer.Context,
    steps: int = typer.Argument(1, min=1, help="回滚到 N 份之前的快照"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    health_timeout: float = typer.Option(
        10.0, "--health-timeout", min=0, help="健康检查等待秒数"
    ),
) -> None:
    """回滚到 N 份之前。**复用同一闭环**，而不是简单 cp 覆盖。"""
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    outcome = rollback_to(
        app_ctx.instance,
        steps=steps,
        lifecycle=runtime._lifecycle(app_ctx),
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


@config_app.command("apply")
def config_apply(
    ctx: typer.Context,
    sets: list[str] = typer.Option(
        (), "--set", help="键=值（可重复）；与 --unset 至少给一个"
    ),
    unsets: list[str] = typer.Option((), "--unset", help="删除键（可重复，回落 frp 默认值）"),
    json_output: bool = typer.Option(False, "--json", help="机器可读输出"),
    no_restart: bool = typer.Option(False, "--no-restart", help="只写不重启（变更尚未生效）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只校验并展示 diff，不写入、不重启"),
    health_timeout: float = typer.Option(
        10.0, "--health-timeout", min=0, help="健康检查等待秒数"
    ),
) -> None:
    """**多键**变更：一次提交 → 一份快照 → 一次重启（与 Web 配置表单同语义）。

    每个 `--set` 的写法是 `键=值`（值里的 `=` 保留，只有第一个分隔）；
    `--unset` 让键回落 frp 默认值。单个键的快速写入仍用 `config set`。
    混用 set 与 unset 是**一个事务**——拆成多条命令会重启多次，中间那次还
    可能撞上危险组合检查。
    """
    app_ctx = runtime._ctx(ctx).with_json(json_output)
    if not sets and not unsets:
        raise UsageError(
            "至少要有一个 --set 或 --unset",
            hint="示例：frpsctl config apply --set bindPort=7001 --unset log.maxDays",
        )

    changes: list[tuple[str, str]] = []
    for item in sets:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise UsageError(f"--set 需要 `键=值` 形式，实际是 {item!r}")
        changes.append((key.strip(), value))

    lc = runtime._lifecycle(app_ctx)
    outcome = apply_sets(
        app_ctx.instance,
        changes=changes,
        unsets=unsets,
        lifecycle=lc,
        restart=not no_restart,
        health_timeout=health_timeout,
        dry_run=dry_run,
    )

    if app_ctx.json:
        ui.emit_json(
            {
                "keys": [key for key, _ in changes] + [f"-{key}" for key in unsets],
                "applied": outcome.applied,
                "restarted": outcome.restarted,
                "noop": outcome.noop,
                "dry_run": outcome.dry_run,
                "note": outcome.note,
                "diff": cfg.mask_diff(outcome.diff),
                "plugin_warning": outcome.plugin_warning,
            }
        )
        return

    if outcome.noop:
        ui.emit("这些值与当前配置相同，无需变更")
        return
    ui.emit(cfg.mask_diff(outcome.diff).rstrip() or "(无文本差异)")
    ui.emit("")
    if outcome.note:
        ui.emit(f"✓ {outcome.note}")
    elif outcome.restarted:
        ui.emit("✓ 已写入并重启，健康检查通过")
    if outcome.plugin_warning:
        ui.warn(f"⚠ {outcome.plugin_warning}")
