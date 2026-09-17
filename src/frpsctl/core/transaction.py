"""配置变更事务（设计文档 §9、ADR-6）。

frps **没有热重载**，所以"改配置"和"重启"是同一件事。frpsctl 把它实现为一次
带回滚的事务：

    取锁 → 内存补丁 → 语义校验 → 权威校验 → 备份 → 原子替换
         → 重启 → 健康检查 → 失败自动回滚

**回滚判据只有 L1 ∧ L2**（§3.7）。L3 插件不可达永不触发回滚——否则插件临时
抖动会让 frpsctl 把一份完全正确的配置回滚掉，而那份配置恰恰是用于修复问题的
那一份。

## 锁边界（v0.2.1 起由本模块保证）

"改配置"是典型的**读-改-写**，而锁必须覆盖整段——包括"读"。此前 `plan_set`
（读）在实例锁外、`apply_change`（写）在锁内，两个并发变更都基于同一份旧文本
生成完整新文本，后写入者会**静默覆盖**前者（实测复现：两条命令都报成功，其中
一个改动消失）。因此本模块只对外暴露三个入口，每个都在**同一把锁内**完成
"读取 → 生成候选 → 落盘"：

| 入口 | 场景 | 候选生成方式 |
|------|------|-------------|
| `apply_set` | `config set` | 锁内 `plan_set`（点分键 + 原值） |
| `apply_edit` | `config edit` | 锁内 **CAS**：先确认文件仍是编辑开始时的内容 |
| `rollback_to` | `config rollback` | 锁内选快照并读取 |

`apply_edit` 的 CAS 是刻意的：编辑器交互不能持锁（用户可能编十分钟），所以
草稿写回前必须确认"没有人在这段时间里改过文件"。不一致就拒绝并让人重新编辑
——把草稿硬写下去等于覆盖别人的改动。

`_apply_locked` 只接受**已生成的候选文本**，且内部仍会取锁（可重入）兜底：
它是本模块私有的，公共入口已保证候选在锁内产出。
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..errors import ChangeRolledBack, ConfigError, FrpsctlError
from . import config as cfg
from .instance import Instance
from .lifecycle import Lifecycle, State
from .lock import instance_lock

__all__ = [
    "ChangeOutcome",
    "apply_set",
    "apply_sets",
    "apply_unset",
    "apply_edit",
    "rollback_to",
    "config_snapshot",
]


@dataclass(frozen=True)
class ChangeOutcome:
    """一次变更的结果，供 CLI 渲染。

    `noop=True` 表示候选值与现值相同（`config set` 的幂等语义、`config edit`
    的无改动早退）：没有任何字节被写入，CLI 应据此渲染"无需变更"而不是 diff。

    `dry_run=True` 表示只做了校验与 diff 生成（`config set --dry-run`）：
    同样零落盘、零快照；`applied` 恒为 False。
    """

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
    noop: bool = False
    dry_run: bool = False


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


# ---------------------------------------------------------------------------
# 公共入口（锁内完成"读 → 候选 → 写"）
# ---------------------------------------------------------------------------


def apply_set(
    inst: Instance,
    *,
    dotted: str,
    raw: str,
    lifecycle: Lifecycle,
    restart: bool = True,
    health_timeout: float = 10.0,
    restore_lifecycle: Lifecycle | None = None,
    dry_run: bool = False,
) -> ChangeOutcome:
    """`config set` 的唯一入口：候选生成（plan）与落盘在**同一把实例锁**内。

    这是并发正确性的根因修复点：`plan_set` 是读-改-写里的"读"，放在锁外时
    两个并发变更会互相覆盖（后写入者的完整文本覆盖前者的改动），且两边都报
    成功。`noop`（现值等于目标值）同样在锁内判定，避免"判定时无变更、落盘时
    已有变更"的竞态。

    `dry_run=True` 时走完全相同的校验（语义 + `frps verify` + 危险组合），
    但不写文件、不产生快照、不重启——用于"改之前先看一眼"。
    """
    with instance_lock(inst.lock):
        plan = cfg.plan_set(inst.config, dotted, raw)
        if plan.is_noop:
            return ChangeOutcome(
                dotted=dotted,
                before=plan.before,
                after=plan.after,
                diff="",
                applied=False,
                restarted=False,
                rolled_back=False,
                noop=True,
            )
        if dry_run:
            return _dry_run_check(
                inst,
                lifecycle=lifecycle,
                dotted=dotted,
                new_text=plan.text,
                diff=plan.diff,
                before=plan.before,
                after=plan.after,
            )
        return _apply_locked(
            inst,
            dotted=dotted,
            new_text=plan.text,
            change_diff=plan.diff,
            before=plan.before,
            after=plan.after,
            lifecycle=lifecycle,
            restart=restart,
            health_timeout=health_timeout,
            restore_lifecycle=restore_lifecycle,
        )


def apply_unset(
    inst: Instance,
    *,
    dotted: str,
    lifecycle: Lifecycle,
    restart: bool = True,
    health_timeout: float = 10.0,
    restore_lifecycle: Lifecycle | None = None,
    dry_run: bool = False,
) -> ChangeOutcome:
    """`config unset` 的唯一入口：与 `apply_set` 同一套锁边界与落盘闭环。

    删除同样是"读-改-写"，因此候选生成（`plan_unset`）必须在锁内；落盘后走
    完全相同的校验 → 备份 → 替换 → 重启 → 失败回滚。危险组合检查自动覆盖
    （例如删掉 `webServer.password` 后"非回环 + 无凭据"会被拒绝）。
    """
    with instance_lock(inst.lock):
        plan = cfg.plan_unset(inst.config, dotted)
        if dry_run:
            return _dry_run_check(
                inst,
                lifecycle=lifecycle,
                dotted=dotted,
                new_text=plan.text,
                diff=plan.diff,
                before=plan.before,
                after=None,
            )
        return _apply_locked(
            inst,
            dotted=dotted,
            new_text=plan.text,
            change_diff=plan.diff,
            before=plan.before,
            after=None,
            lifecycle=lifecycle,
            restart=restart,
            health_timeout=health_timeout,
            restore_lifecycle=restore_lifecycle,
            snapshot_action=f"unset {dotted}",
            snapshot_detail=plan.diff[:2000],
        )


def apply_edit(
    inst: Instance,
    *,
    draft: str,
    expected_current: str,
    lifecycle: Lifecycle,
    restart: bool = True,
    health_timeout: float = 10.0,
    restore_lifecycle: Lifecycle | None = None,
) -> ChangeOutcome:
    """`config edit` 的唯一入口：锁内 CAS 检查 + 落盘。

    **为什么需要 CAS**：编辑器交互在锁外（不能持锁等用户编完），所以写回前
    必须验证"当前文件仍等于编辑开始时的内容"（`expected_current`）。编辑期间
    若有人 `config set`，草稿就基于过期文本——直接写入会覆盖别人的改动。
    此时拒绝并丢弃草稿，让人以最新内容为基准重新编辑。

    `draft` 与基准相同时返回 `noop=True`（"没有改动"），不产生快照。
    """
    with instance_lock(inst.lock):
        current = cfg.read_config_text(inst.config)
        if current != expected_current:
            raise ConfigError(
                "配置文件在编辑期间被其他操作修改，草稿已丢弃",
                hint="请重新运行 `frpsctl config edit`（以最新内容为基准）",
            )
        if draft == expected_current:
            return ChangeOutcome(
                dotted="(edit)",
                before="(edited)",
                after="(edited)",
                diff="",
                applied=False,
                restarted=False,
                rolled_back=False,
                noop=True,
            )
        diff = cfg.diff_texts(current, draft, inst.config.name)
        return _apply_locked(
            inst,
            dotted="(edit)",
            new_text=draft,
            change_diff=diff,
            before="(edited)",
            after="(edited)",
            lifecycle=lifecycle,
            restart=restart,
            health_timeout=health_timeout,
            restore_lifecycle=restore_lifecycle,
        )


def apply_sets(
    inst: Instance,
    *,
    changes: Sequence[tuple[str, str]],
    lifecycle: Lifecycle,
    restart: bool = True,
    health_timeout: float = 10.0,
    restore_lifecycle: Lifecycle | None = None,
    expected_current: str | None = None,
    dry_run: bool = False,
) -> ChangeOutcome:
    """**多键**变更（`frpsctl web` 的配置表单）：锁内合并补丁 + 一次闭环。

    与 `apply_set` 的区别只有"一次改几个键"：所有键作用在同一个文档上，
    只产生**一份快照、一次重启**。同一键重复出现时后者覆盖前者。

    `expected_current` 提供时做 CAS（与 `apply_edit` 同一语义）：
    预览与落盘之间文件被并发修改 → 拒绝而不是覆盖。Web 的"预览 diff →
    确认应用"两段式交互依赖它。
    """
    items = [(str(k), str(v)) for k, v in changes]
    dotted_label = ", ".join(key for key, _ in items) or "(空变更)"
    with instance_lock(inst.lock):
        current = cfg.read_config_text(inst.config)
        if expected_current is not None and current != expected_current:
            raise ConfigError(
                "配置文件在预览期间被其他操作修改，变更已丢弃",
                hint="请刷新页面后重新提交（以最新内容为基准）",
            )
        plan = cfg.plan_set_many(inst.config, items)
        if plan.is_noop:
            return ChangeOutcome(
                dotted=dotted_label,
                before=plan.before,
                after=plan.after,
                diff="",
                applied=False,
                restarted=False,
                rolled_back=False,
                noop=True,
            )
        if dry_run:
            return _dry_run_check(
                inst,
                lifecycle=lifecycle,
                dotted=dotted_label,
                new_text=plan.text,
                diff=plan.diff,
                before=plan.before,
                after=plan.after,
            )
        return _apply_locked(
            inst,
            dotted=dotted_label,
            new_text=plan.text,
            change_diff=plan.diff,
            before=plan.before,
            after=plan.after,
            lifecycle=lifecycle,
            restart=restart,
            health_timeout=health_timeout,
            restore_lifecycle=restore_lifecycle,
            snapshot_action=f"set many: {dotted_label}",
            snapshot_detail=plan.diff[:2000],
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

    快照选择与读取同样在锁内：锁外选快照时，并发的 `config set` 会在"选定"
    与"落盘"之间推进历史，回滚到的可能不是用户看到的那一份。
    """
    with instance_lock(inst.lock):
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

        # "回滚前先把当前存一份"由 _apply_locked 在**实例锁内**完成（它本来就
        # 要备份变更前的配置）。此前这里是单独一次 config_snapshot，导致一次
        # 回滚产生**两份内容完全相同**的快照：10 份历史实际只够 5 次操作，
        # `rollback N` 的计数里一半是重复项；而且那次快照发生在锁外，并发下
        # 顺序不可靠。
        return _apply_locked(
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
            snapshot_action=f"rollback {steps}",
            snapshot_detail=f"目标快照 {target.parent.name}",
        )


# ---------------------------------------------------------------------------
# 落盘闭环（只接受已生成的候选文本）
# ---------------------------------------------------------------------------


def _dry_run_check(
    inst: Instance,
    *,
    lifecycle: Lifecycle,
    dotted: str,
    new_text: str,
    diff: str,
    before: object,
    after: object,
) -> ChangeOutcome:
    """dry-run：跑完全部真实校验，但**零落盘、零快照、零重启**。

    校验集合与落盘路径完全一致（版本门槛 → 语义 → `frps verify` → 危险组合），
    否则用户会带着"dry-run 通过"的错觉去掉 `--dry-run`，然后在真实执行时撞上
    一个 dry-run 没做过的检查。

    与 `--no-restart` 的区别：那个是"写了但不生效"（文件已变），这个是什么都不写。
    """
    lifecycle.binary_version()  # 版本门槛（不达标即抛，退出码 4）
    binary = lifecycle.binary()
    cfg.validate_text(
        new_text,
        binary=binary,
        workdir=inst.dir,
        uses_unsafe=cfg.needs_unsafe_flag(new_text),
    )
    _reject_dangerous_combination(new_text)
    return ChangeOutcome(
        dotted=dotted,
        before=before,
        after=after,
        diff=diff,
        applied=False,
        restarted=False,
        rolled_back=False,
        dry_run=True,
        note="dry-run：校验通过，未写入（去掉 --dry-run 后执行才会生效）",
    )


def _apply_locked(
    inst: Instance,
    *,
    dotted: str,
    new_text: str,
    change_diff: str,
    before: object,
    after: object,
    lifecycle: Lifecycle,
    restart: bool,
    health_timeout: float,
    restore_lifecycle: Lifecycle | None,
    snapshot_action: str | None = None,
    snapshot_detail: str | None = None,
) -> ChangeOutcome:
    """把一份候选配置落盘，并按需重启 + 回滚。

    第 4–9 步：校验 → 备份 → 替换 → 重启 → 健康检查 → 回滚。候选文本由调用方
    （本模块的三个公共入口）在锁内生成；函数内部再取一次锁（可重入）作为兜底，
    防止未来有人从锁外直接调用。

    `restore_lifecycle` 仅在测试中注入：回滚后的重启应当用**同一套**生命周期
    配置（否则测试会用一个"必定成功"的替身掩盖回滚失败的真实行为）。

    `snapshot_action` / `snapshot_detail` 覆盖快照的记账文案：`rollback_to`
    复用同一个闭环，但它的"变更前备份"应当标注为 `rollback N` 而不是
    `set (...)`。**快照只在锁内创建一次**，它同时承担"回滚恢复来源"。
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
            uses_unsafe=cfg.needs_unsafe_flag(new_text),
        )

        # 5b. 危险组合拦截（§10 硬约束 1）。
        # 必须针对**合并后的完整配置**判断：单看被改的那一个键永远看不出问题
        # （把 user 改成空串本身无害，加上 addr=0.0.0.0 才是缺口）。
        _reject_dangerous_combination(new_text)

        # 6. 备份。这是本次变更**唯一**的快照：它既是变更前的留档，也是第 8 步
        #    失败回滚的恢复来源。动作名可由调用方覆盖（rollback 走同一闭环）。
        snapshot = config_snapshot(
            inst,
            action=snapshot_action or f"set {dotted}",
            detail=snapshot_detail if snapshot_detail is not None else change_diff[:2000],
        )
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
