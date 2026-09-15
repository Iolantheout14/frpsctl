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

import contextlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import (
    ChangeRolledBack,
    ConfigError,
    FrpsctlError,
    NotRunning,
)
from . import config as cfg
from .instance import Instance
from .lifecycle import Lifecycle, Owner, State
from .lock import instance_lock
from .version import Version

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
    slot = inst.next_history_slot()
    slot.mkdir(parents=True, exist_ok=True)
    if inst.config.exists():
        (slot / "frps.toml").write_text(inst.config.read_text("utf-8"), "utf-8")
    meta = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "action": action,
        "detail": detail,
        "config": str(inst.config),
    }
    (slot / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", "utf-8")
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
    version: Version = lifecycle.binary_version()
    binary = lifecycle.binary()

    with instance_lock(inst.lock):
        # 4–5. 权威校验：在动线上文件**之前**把 frps verify 请出来
        cfg.validate_text(
            new_text,
            binary=binary,
            version=version,
            workdir=inst.dir,
            uses_unsafe=_uses_unsafe(new_text),
        )

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
            _restore(inst, previous_text, snapshot, lifecycle=restore_with)
            raise ChangeRolledBack(f"重启失败：{exc.message}") from None

        if not report.healthy:
            detail = report.health.render()
            _restore(inst, previous_text, snapshot, lifecycle=restore_with)
            raise ChangeRolledBack(
                f"重启后健康检查未通过（{detail}）"
            ) from None

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
) -> None:
    """把配置恢复到变更前，并把服务重新拉起来。

    恢复用的文本取自**备份快照**（而不是内存里的 previous_text），因为快照是
    落过盘的、可复核的那一份。
    """
    source = snapshot / "frps.toml"
    text = source.read_text("utf-8") if source.exists() else previous_text
    if text is None:
        return
    cfg.atomic_write(inst.config, text, mode=0o600)

    # 实例本来就没运行时，恢复配置就够了 —— 不必、也不该顺手把它拉起来。
    restarter = lifecycle or Lifecycle(inst, binary=None)
    if restarter.read_ref() is None:
        return
    with contextlib.suppress(FrpsctlError):
        restarter.start()


def _uses_unsafe(text: str) -> bool:
    import tomllib

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    source = (data.get("auth") or {}).get("tokenSource") or {}
    return str(source.get("type", "")).lower() == "exec"
