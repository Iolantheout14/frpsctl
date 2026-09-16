"""配置变更事务（设计文档 §9、ADR-6）。

frps **没有热重载**，所以"改配置"和"重启"是同一件事。frpsctl 把它实现为一次
带回滚的事务：

    取锁 → 内存补丁 → 语义校验 → 权威校验 → 备份 → 原子替换
         → 重启 → 健康检查 → 失败自动回滚

**回滚判据只有 L1 ∧ L2**（§3.7）。L3 插件不可达永不触发回滚——否则插件临时
抖动会让 frpsctl 把一份完全正确的配置回滚掉，而那份配置恰恰是用于修复问题的
那一份。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import ChangeRolledBack, ConfigError, FrpsctlError
from . import config as cfg
from .instance import Instance
from .lifecycle import Lifecycle, State
from .lock import instance_lock

__all__ = ["ChangeOutcome", "apply_change", "rollback_to", "config_snapshot"]


@dataclass(frozen=True)
class ChangeOutcome:
    """一次变更的结果，供 CLI 渲染。"""

    dotted: str
    before: object
    after: object
    diff: str
    applied: bool
    restarted: bool
    rolled_back: bool
    health_gate_ok: bool = True
    plugin_warning: str | None = None
    note: str = ""


def config_snapshot(inst: Instance, *, action: str, detail: str = "") -> Path:
    """把当前配置存进 `config-history/NNNN-<ts>/`（§9 第 6 步）。

    没有可回滚的东西，就没有回滚。快照同时写一份 `meta.json`（时间、操作、
    diff/结果），让"这份快照是怎么来的"可追溯。
    """
    slot = inst.next_history_slot()  # 已被原子占位，无需再 mkdir
    # 快照是**配置的完整副本**，里面同样有 auth.token 与 dashboard 口令。
    # 用原子写（它会把权限设为 0600 再落内容，无权限窗口）——此前直接
    # write_text 让快照以默认 0644 落盘，等于给配置文件开了个后门副本。
    if inst.config.exists():
        cfg.atomic_write(slot / "frps.toml", inst.config.read_text("utf-8"), mode=0o600)
    meta = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "action": action,
        "detail": detail,
        "config": str(inst.config),
    }
    cfg.atomic_write(
        slot / "meta.json",
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        mode=0o600,
    )
    inst.prune_history()
    return slot


def apply_change(
    inst: Instance,
    *,
    dotted: str,
    new_text: str,
    change_diff: str,
    before: object,
    after: object,
    lifecycle: Lifecycle,
    restart: bool = True,
    health_timeout: float = 10.0,
    restore_lifecycle: Lifecycle | None = None,
) -> ChangeOutcome:
    """把一份候选配置落盘，并按需重启 + 回滚。

    调用方（CLI）负责生成 `new_text`（走 `config.plan_set`），本函数负责
    第 4–9 步：校验 → 备份 → 替换 → 重启 → 健康检查 → 回滚。

    `restore_lifecycle` 仅在测试中注入：回滚后的重启应当用**同一套**生命周期
    配置（否则测试会用一个"必定成功"的替身掩盖回滚失败的真实行为）。
    """
    # binary_version() 会执行 §3.6 的版本门槛校验（不达标即抛），
    # 因此即使返回值用不上也不能省——但确实不需要保留这个变量。
    lifecycle.binary_version()
    binary = lifecycle.binary()

    with instance_lock(inst.lock):
        # 4–5. 权威校验：在动线上文件**之前**把 frps verify 请出来
        cfg.validate_text(
            new_text,
            binary=binary,
            workdir=inst.dir,
            uses_unsafe=_uses_unsafe(new_text),
        )

        # 5b. 危险组合拦截（§10 硬约束 1）。
        # 必须针对**合并后的完整配置**判断：单看被改的那一个键永远看不出问题
        # （把 user 改成空串本身无害，加上 addr=0.0.0.0 才是缺口）。
        _reject_dangerous_combination(new_text)

        # 6. 备份
        snapshot = config_snapshot(inst, action=f"set {dotted}", detail=change_diff[:2000])
        previous_text = inst.config.read_text("utf-8") if inst.config.exists() else None

        # 7. 原子替换
        cfg.atomic_write(inst.config, new_text, mode=0o600)

        if not restart:
            return ChangeOutcome(
                dotted=dotted,
                before=before,
                after=after,
                diff=change_diff,
                applied=True,
                restarted=False,
                rolled_back=False,
                note="已写入但尚未生效（--no-restart）。重启后生效。",
            )

        # 8. 若实例在运行：restart + 健康检查
        state, _ = lifecycle.state()
        if state is not State.RUNNING:
            return ChangeOutcome(
                dotted=dotted,
                before=before,
                after=after,
                diff=change_diff,
                applied=True,
                restarted=False,
                rolled_back=False,
                note=f"实例当前未运行（{state.value}），配置已写入且校验通过；start 后生效。",
            )

        restore_with = restore_lifecycle or lifecycle
        try:
            report = lifecycle.restart(health_timeout=health_timeout)
        except FrpsctlError as exc:
            problem = _restore(inst, previous_text, snapshot, lifecycle=restore_with, must_run=True)
            raise ChangeRolledBack(f"重启失败：{exc.message}", hint=problem) from None

        if not report.healthy:
            detail = report.health.render()
            problem = _restore(inst, previous_text, snapshot, lifecycle=restore_with, must_run=True)
            raise ChangeRolledBack(f"重启后健康检查未通过（{detail}）", hint=problem) from None

        return ChangeOutcome(
            dotted=dotted,
            before=before,
            after=after,
            diff=change_diff,
            applied=True,
            restarted=True,
            rolled_back=False,
            health_gate_ok=True,
            plugin_warning=report.health.plugin_warning,
        )


def rollback_to(
    inst: Instance,
    *,
    steps: int,
    lifecycle: Lifecycle,
    restart: bool = True,
    health_timeout: float = 10.0,
    restore_lifecycle: Lifecycle | None = None,
) -> ChangeOutcome:
    """回滚到 N 份之前（`config rollback [N]`，默认 N=1）。

    **复用同一闭环**，而不是简单 `cp` 覆盖：回滚目标同样要过 verify，
    重启失败同样要能再回滚——否则回滚本身会把服务搞坏。
    """
    entries = inst.history_entries()
    if not entries:
        raise ConfigError(
            "没有可回滚的配置快照",
            hint="快照在每次 `config set` / `config edit` 时自动创建于 config-history/",
        )
    index = max(0, steps - 1)
    if index >= len(entries):
        raise ConfigError(
            f"只找到 {len(entries)} 份快照，无法回滚 {steps} 步",
            hint="用 `frpsctl config diff` 查看当前与最近快照的差异",
        )
    target = entries[index] / "frps.toml"
    if not target.exists():
        raise ConfigError(f"快照不完整，缺少 {target.name}：{target.parent}")

    new_text = target.read_text("utf-8")
    current_text = inst.config.read_text("utf-8") if inst.config.exists() else ""
    diff = cfg.diff_texts(current_text, new_text, inst.config.name)

    # 回滚前先把"当前"也存一份——否则回滚本身不可撤销
    config_snapshot(inst, action=f"rollback {steps}", detail=f"目标快照 {target.parent.name}")
    return apply_change(
        inst,
        dotted=f"(rollback {steps} → {target.parent.name})",
        new_text=new_text,
        change_diff=diff,
        before="(current)",
        after=f"(snapshot {target.parent.name})",
        lifecycle=lifecycle,
        restart=restart,
        health_timeout=health_timeout,
        restore_lifecycle=restore_lifecycle,
    )


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _restore(
    inst: Instance,
    previous_text: str | None,
    snapshot: Path,
    *,
    lifecycle: Lifecycle | None = None,
    must_run: bool = False,
) -> str:
    """把配置恢复到变更前，并在需要时把服务拉起来。返回补充说明（给人看）。

    恢复用的文本取自**备份快照**（而不是内存里的 previous_text），因为快照是
    落过盘的、可复核的那一份。

    **这里曾经是"假回滚"**：只要 `read_ref()` 为 None 就直接 return，而"启动失败"
    这条路径下 state.json 恰好已被清掉——于是磁盘回滚了、实例却留在 DOWN，用户
    看到的是"已自动回滚到上一份配置"（退出码 9），以为服务没事。

    现在按"变更前它在跑"这个事实（`must_run`）来决定要不要真正重启，并且
    **不再吞掉**恢复过程中的异常：拿不回服务必须如实告诉用户，否则他会以为
    回滚把问题解决了。
    """
    source = snapshot / "frps.toml"
    text = source.read_text("utf-8") if source.exists() else previous_text
    if text is None:
        return "（没有可恢复的配置快照，未能回滚）"

    cfg.atomic_write(inst.config, text, mode=0o600)

    restarter = lifecycle or Lifecycle(inst, binary=None)

    if not must_run:
        # 变更前实例本来就没在跑：恢复配置就够了，不该顺手把它拉起来。
        return ""

    # 变更前它在跑，所以回滚的完成标准是"服务重新跑起来"，不是"文件改回去了"。
    # 用 restart 而不是 start：健康检查失败那条路径上进程**还活着**（已加载新
    # 配置，而 frps 没有热重载），直接 start 会抛 AlreadyRunning 被吞掉，
    # 结果磁盘是旧配置、跑着的是新配置——回滚等于没做。
    try:
        report = restarter.restart(health_timeout=10.0)
    except FrpsctlError as exc:
        return (
            f"⚠ 配置已恢复到上一版，但服务未能重新启动：{exc.message}。请手工执行 `frpsctl start` 并检查日志"
        )
    if not report.healthy:
        return (
            f"⚠ 配置已恢复到上一版，但重启后健康检查未通过（{report.health.render()}）。请检查 `frpsctl log`"
        )
    return "配置与服务均已恢复到变更前的状态"


def _reject_dangerous_combination(text: str) -> None:
    """拒绝会让 dashboard 变成"完全无鉴权"的配置（§10 硬约束 1）。"""
    import tomllib

    from .schema import check_dangerous_combination

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return  # 语法错误交给 frps verify 报，那里有更准确的定位
    problem = check_dangerous_combination(data)
    if problem:
        raise ConfigError(
            f"拒绝写入危险配置：{problem}",
            hint=("请先设置 webServer.user 与 webServer.password，或把 webServer.addr 改回 127.0.0.1"),
        )


def _uses_unsafe(text: str) -> bool:
    import tomllib

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    auth = data.get("auth")
    if not isinstance(auth, dict):
        return False  # `auth = "oops"` 这类类型错误交给 pydantic 报（退出码 3）
    source = auth.get("tokenSource")
    if not isinstance(source, dict):
        return False
    return str(source.get("type", "")).lower() == "exec"
