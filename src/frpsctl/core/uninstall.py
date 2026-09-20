"""完整卸载（设计文档 §21）：把 frpsctl 产生的东西清理干净。

**作用域模型**：默认卸载**当前实例**；`--all` 覆盖实例根下的全部实例。
两个"保留"维度各自独立：`keep_data` 保留实例数据（配置/快照/审计），
`keep_bin` 保留共享二进制（多实例环境下还有别的实例要用）。

三条安全原则（都是项目既有纪律在这里的应用）：

1. **不确定就拒绝**（ADR-7）：实例在运行、身份不明（FOREIGN）、状态文件损坏、
   多实例共用二进制却要求删它——一律拒绝并说明处置，而不是"顺手删掉"。
2. **顺序安全**：先停服务 → 再清 unit → 再删数据 → 最后删共享二进制。
   任何一步失败都不会留下"服务还在跑但数据已被删"的失控状态。
3. **降级必须可见**：需要 root 才能做的清理（unit）与非本工具创建的东西
   （`/var/log/frps`、服务账户）都进入 `warnings` 如实汇报，绝不静默跳过。
"""

from __future__ import annotations

import os
import pwd
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import (
    ConfigError,
    FrpsctlError,
    OwnershipConflict,
    PermissionRequired,
    UsageError,
)
from .instance import Instance, list_instances
from .lifecycle import Lifecycle, Owner, State
from .lock import instance_lock
from .systemd import (
    DEFAULT_LOG_DIR,
    DEFAULT_SERVICE_USER,
    PluginService,
    Systemd,
    WebService,
    read_service_manifest,
)

__all__ = [
    "UninstallPlan",
    "UninstallReport",
    "DEFAULT_LOG_DIR",
    "plan_uninstall",
    "execute_uninstall",
]


@dataclass(frozen=True)
class UninstallPlan:
    """一次卸载的"计划"：只做规则判定，不触碰任何文件。"""

    instances: tuple[Instance, ...]
    remove_data: bool
    remove_bin: bool
    #: 是否允许删除**共享** unit 模板（`frps@.service` 等）——只有目标覆盖
    #: 实例根下全部实例时才允许，否则会连累其他实例。
    remove_unit_templates: bool
    bin_dir: Path

    @property
    def is_empty(self) -> bool:
        """没有任何可清理的内容（仅用于 `--all` 且实例与二进制都不存在的场景）。

        ⚠️ **只要目标实例存在就不是空计划**：`--keep-data --keep-bin` 的组合下
        数据与二进制都保留，但"停用 unit / 停掉服务"仍可能有事可做——把它判成
        "没有可卸载的内容"会让 unit 静默留在系统里（降级不可见）。
        """
        if self.instances:
            return False
        has_data = self.remove_data and any(inst.dir.exists() for inst in self.instances)
        return not has_data and not (self.remove_bin and self.bin_dir.exists())


@dataclass
class UninstallReport:
    """执行结果（供 CLI 渲染；`core/` 不打印）。"""

    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def plan_uninstall(
    inst: Instance,
    *,
    all_instances: bool = False,
    keep_data: bool = False,
    keep_bin: bool = False,
) -> UninstallPlan:
    """解析作用域与保留项，产出计划；非法组合在这里就拒绝。

    二进制目录（`bin/`）是**全局共享**的：只有当目标实例覆盖了实例根下的
    全部实例时才能删它——否则删掉会让其他实例起不来，而"默认只卸当前实例"
    正是最常见的用法（多实例机器上卸一个）。不猜意图，要求显式 `--all`。
    """
    existing = list_instances(inst.instances_root, data_home=inst.data_home)
    if all_instances:
        targets = tuple(existing)
    else:
        if not inst.dir.exists():
            raise ConfigError(
                f"实例不存在：{inst.dir}",
                hint="用 `frpsctl instances` 查看全部实例；或加 --all 卸载实例根下的所有实例",
            )
        targets = (inst,)

    target_names = {item.name for item in targets}
    others = [item for item in existing if item.name not in target_names]
    covers_all = not others

    if not keep_bin and not covers_all:
        raise UsageError(
            f"实例根下还有 {len(others)} 个其它实例共用二进制目录："
            f"{', '.join(item.name for item in others)}",
            hint="删除共享二进制会破坏它们。用 --all 卸载全部实例，或加 --keep-bin 保留二进制",
        )

    return UninstallPlan(
        instances=targets,
        remove_data=not keep_data,
        remove_bin=not keep_bin,
        remove_unit_templates=covers_all,
        bin_dir=inst.bin_dir,
    )


def execute_uninstall(
    plan: UninstallPlan,
    *,
    force: bool = False,
    unit_dir: Path | None = None,
    log_dir: Path | None = None,
) -> UninstallReport:
    """按计划执行卸载。`force=True` 时运行中的实例会先被停止。

    分四个阶段顺序执行（见模块文档的"顺序安全"）。**预检**在任何破坏性动作
    之前跑完：所有实例都必须处于"可卸载"状态，否则一个都不动。
    """
    report = UninstallReport()

    # ⚠️ 留档（service.json）必须**在删除数据之前**读取：`_remove_data` 会把
    # 实例目录整个删掉，之后再读只会得到"文件不存在"——自定义服务账户与
    # 实际日志路径的提示会静默退化成默认假设（v0.3.2 review 实测复现）。
    manifest_users, manifest_log_dirs = _collect_manifest_hints(plan.instances)

    # 预检（纯检查）：状态不允许就在动手之前拒绝。
    for inst in plan.instances:
        _precheck(inst, force=force)

    # 1) 停止（保留数据的卸载同样要停——否则服务还在跑而 unit 已被清）。
    for inst in plan.instances:
        _ensure_stopped(inst, force=force, unit_dir=unit_dir, report=report)

    # 2) unit：模板只在"覆盖全部实例"时删；否则只停用本实例的 unit。
    for inst in plan.instances:
        _clean_units(inst, remove_templates=plan.remove_unit_templates, unit_dir=unit_dir, report=report)

    # 3) 数据。
    if plan.remove_data:
        for inst in plan.instances:
            _remove_data(inst, report=report)
        # `--config` 指向实例目录之外的配置：删实例目录不会碰到它，如实提示
        # （否则用户以为"卸载干净了"，而含 token 的配置还在别处）。
        for inst in plan.instances:
            if (
                inst.config_override is not None
                and inst.config.parent != inst.dir
                and inst.config.exists()
            ):
                report.warnings.append(
                    f"配置文件在实例目录之外（--config），未删除：{inst.config}"
                )
    else:
        report.kept.extend(str(inst.dir) for inst in plan.instances)

    # 4) 共享二进制。
    if plan.remove_bin and plan.bin_dir.exists():
        try:
            shutil.rmtree(plan.bin_dir)
        except OSError as exc:
            raise FrpsctlError(
                f"无法删除二进制目录 {plan.bin_dir}：{exc}",
                hint="确认权限后手工删除，或重跑本命令",
            ) from None
        report.removed.append(str(plan.bin_dir))

    _courtesy_warnings(
        report, users=manifest_users, log_dirs=manifest_log_dirs, log_dir=log_dir
    )
    return report


# ---------------------------------------------------------------------------
# 各阶段
# ---------------------------------------------------------------------------


def _precheck(inst: Instance, *, force: bool) -> None:
    """实例必须处于可卸载状态；任何不确定都拒绝（ADR-7）。

    用 `Lifecycle.status()` 而不是自己读 state：status 的承诺是"永不异常、如实
    报告"，且内部已收口了"损坏检查与读取之间"的 TOCTOU。
    """
    status = Lifecycle(inst).status()
    if status.state_corrupted:
        raise ConfigError(
            f"实例 {inst.name} 的状态文件已损坏：{inst.state}",
            hint="无法判断进程归属，拒绝卸载。请确认没有 frps 在跑后手工删除该文件，再重试",
        )
    if status.systemd_probe_error:
        # 探测失败时 owner 是降级值：systemd 托管的实例会被误判为可直接卸载。
        # 删除数据而 unit 仍在跑 = "服务在跑、数据已删"——顺序安全原则的反例。
        raise OwnershipConflict(
            f"实例 {inst.name} 的 systemd 状态探测失败，无法安全判定所有权，拒绝卸载",
            hint=f"{status.systemd_probe_error}；恢复 systemd 后重试",
        )
    if status.owner is Owner.SYSTEMD:
        if not force:
            raise OwnershipConflict(
                f"实例 {inst.name} 由 systemd 托管且处于 active，拒绝卸载",
                hint="先 `sudo systemctl stop` 对应 unit，或加 --force 由本命令先停止",
            )
        if os.geteuid() != 0:
            raise PermissionRequired(
                "停止 systemd 托管的实例需要 root",
                hint="用 sudo 重新运行（--force 会先停止再卸载）",
            )
        return
    if status.state is State.RUNNING and not force:
        raise OwnershipConflict(
            f"实例 {inst.name} 正在运行（pid {status.pid}），拒绝卸载",
            hint="先 `frpsctl stop`，或加 --force 由本命令先停止再卸载",
        )
    if status.state is State.FOREIGN:
        raise OwnershipConflict(
            f"实例 {inst.name} 记录的 pid {status.pid} 存活但身份校验不通过，拒绝卸载",
            hint="该 pid 可能是无关进程。确认后手工删除 state.json，再重试",
        )
    # STALE / STOPPED：直接进入下一阶段（陈旧状态会随目录一起删掉）。


def _ensure_stopped(
    inst: Instance, *, force: bool, unit_dir: Path | None, report: UninstallReport
) -> None:
    """把实例停下来。**只有 `--force` 才允许停运行中的实例。**

    预检（`_precheck`）与这里之间存在时间窗口——实例可能在窗口里被并发启动。
    因此这里**复核一次**并重新应用同一条授权规则：发现运行态而 `--force` 未给
    就中止（退出码 11），绝不因为"预检时它是停的"就越权停掉一个刚起来的服务。
    """
    status = Lifecycle(inst).status()
    if status.systemd_probe_error:
        # 预检与这里之间 systemd 可能刚好无响应——同 `_precheck`：拒绝而非降级。
        raise OwnershipConflict(
            f"实例 {inst.name} 的 systemd 状态探测失败，中止卸载",
            hint=f"{status.systemd_probe_error}；恢复 systemd 后重试",
        )
    if status.owner is Owner.SYSTEMD:
        if not force:
            raise OwnershipConflict(
                f"实例 {inst.name} 在卸载期间变为 systemd 托管且 active，中止卸载",
                hint="先 `sudo systemctl stop` 对应 unit，或加 --force 由本命令先停止",
            )
        _systemd_for(inst, unit_dir).stop()
        report.stopped.append(f"{inst.name}（systemd）")
        return
    if status.state is State.RUNNING:
        if not force:
            raise OwnershipConflict(
                f"实例 {inst.name} 在卸载期间被启动（pid {status.pid}），中止卸载",
                hint="先 `frpsctl stop`，或加 --force 由本命令先停止再卸载",
            )
        Lifecycle(inst).stop()
        report.stopped.append(f"{inst.name}（pid {status.pid}）")


def _clean_units(
    inst: Instance,
    *,
    remove_templates: bool,
    unit_dir: Path | None,
    report: UninstallReport,
) -> None:
    """清理本实例的三个 unit（frps / 插件 / Web 管理台）。

    `available=False`（容器等无 systemd）或 unit 从未安装时静默跳过；需要 root
    而当前不是 root 时收进 warnings 并给出可复制的命令——降级必须可见。
    """
    services = (
        ("frps", _systemd_for(inst, unit_dir), "frpsctl service uninstall"),
        ("插件", PluginService(inst, unit_dir=unit_dir) if unit_dir else PluginService(inst),
         "frpsctl plugin service uninstall"),
        ("Web 管理台", WebService(inst, unit_dir=unit_dir) if unit_dir else WebService(inst),
         "frpsctl web service uninstall"),
    )
    for label, service, hint_cmd in services:
        if not service.available:
            continue
        # 判定用 is_active / is_enabled，**不是** template_path.exists()：
        # 模板是全部实例共享的，它存在不代表本实例的 unit 存在——用错会产生
        # "需要 root 才能停用 frps@x" 这类假警告（对从未用过 systemd 的实例）。
        try:
            stop_needed = service.is_active() or service.is_enabled()
        except FrpsctlError as exc:
            # 预检与清理之间 systemd 可能刚好无响应（v0.3.1 自检补正）：
            # 收进 warnings 并跳过**这一项**，而不是让整个卸载流程崩在探测上。
            # 降级必须可见——用户需要手工确认这个 unit 的最终状态。
            report.warnings.append(
                f"无法探测{label} unit 状态（{service.unit_name}）：{exc.message}；"
                "请手工确认它已停止（systemctl status）"
            )
            continue
        remove_template = remove_templates and service.template_path.exists()
        if not stop_needed and not remove_template:
            continue
        try:
            if remove_template:
                service.uninstall()
                report.removed.append(f"{service.unit_name}（unit 已停用并删除模板）")
            else:
                service.disable()
                report.removed.append(
                    f"{service.unit_name}（已停用；共享模板保留，其它实例仍在用）"
                )
        except PermissionRequired:
            action = "停用并删除 unit 模板" if remove_template else "停用 unit"
            report.warnings.append(
                f"需要 root 才能{action}（{service.unit_name}）：sudo {hint_cmd}"
            )
        except FrpsctlError as exc:
            report.warnings.append(f"停用{label} unit 失败（{service.unit_name}）：{exc.message}")


def _systemd_for(inst: Instance, unit_dir: Path | None) -> Systemd:
    return Systemd(inst, unit_dir=unit_dir) if unit_dir else Systemd(inst)


def _remove_data(inst: Instance, *, report: UninstallReport) -> None:
    """删除实例目录（配置 / state / 快照 / 插件策略与审计 / 口令文件 / 日志）。

    取实例锁：并发 `config set` 不能在我们删除的同时写文件。删除持有中的
    `.lock` 文件本身是安全的——锁按 fd 持有，unlink 不影响已有 flock。
    删除失败**不吞异常**：数据里含机密，半删状态的处置必须立刻可见。
    """
    if not inst.dir.exists():
        return
    try:
        with instance_lock(inst.lock):
            shutil.rmtree(inst.dir)
    except OSError as exc:
        raise FrpsctlError(
            f"删除实例目录失败 {inst.dir}：{exc}",
            hint="确认权限（目录属主可能是服务用户）后手工删除，或重跑本命令",
        ) from None
    report.removed.append(str(inst.dir))


def _collect_manifest_hints(instances: tuple[Instance, ...]) -> tuple[set[str], set[Path]]:
    """从安装留档收集"服务账户"与"日志目录"（**必须在删数据之前调用**）。

    留档缺失（旧部署）时返回空集合——调用方回退默认假设。
    """
    users: set[str] = set()
    log_dirs: set[Path] = set()
    for inst in instances:
        manifest, _ = read_service_manifest(inst)
        for key in ("frps", "plugin", "web"):
            record = manifest.get(key)
            if not isinstance(record, dict):
                continue
            name = record.get("user")
            if isinstance(name, str) and name:
                users.add(name)
            if key == "frps":
                value = record.get("log_dir")
                if isinstance(value, str) and value:
                    log_dirs.add(Path(value))
    return users, log_dirs


def _courtesy_warnings(
    report: UninstallReport,
    *,
    users: set[str],
    log_dirs: set[Path],
    log_dir: Path | None = None,
) -> None:
    """提示**不属于本工具**、因而不代删的东西（v0.3.2：跟随安装留档）。

    - 服务账户：逐个提示**实际使用过**的账户——此前写死只查 `frps`：用
      `--user alice` 部署时 alice 的残留不可见，而系统里恰好有 frps 时又
      会误报（两条都在 v0.3.2 修复）。
    - 日志目录：优先留档里的实际 `--log-dir`，回退默认路径。
    - 传空集合（旧部署，无留档）时回退到默认假设，行为与历史版本一致。
    """
    if not users:
        users = {DEFAULT_SERVICE_USER}  # 旧部署（无留档）回退
    if log_dir is not None:
        log_dirs = {log_dir}  # 调用方显式注入（测试/特殊场景）
    elif not log_dirs:
        log_dirs = {DEFAULT_LOG_DIR}

    for path in sorted(log_dirs):
        if path.exists():
            report.warnings.append(
                f"未清理 {path}（可能仍被其它实例使用；确认无用后：sudo rm -rf {path}）"
            )
    for name in sorted(users):
        try:
            pwd.getpwnam(name)
        except KeyError:
            continue
        report.warnings.append(
            f"未删除服务账户 {name!r}（可能另有用途；确认无用后：sudo userdel {name}）"
        )
