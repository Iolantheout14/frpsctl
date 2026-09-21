"""systemd ↔ direct 后台的互斥守卫（CLI 与 Web 共用的单点）。

v0.3.4 从 `cli/runtime.py` 下沉到 core：Web 管理台的插件启停
（`/api/actions/plugin-*`）此前会绕过守卫直接调 `serve_runtime` ——
systemd 与 direct 同时拉起同一服务（双起、端口冲突、重启语义混乱）。
分层纪律要求"业务判定在 core"，CLI 与 Web 都只是调用方。

两个方向都不做"智能选择"：明确拒绝并给出切换命令（ADR-1 同一精神）。
"""

from __future__ import annotations

from ..errors import OwnershipConflict
from . import serve_runtime
from .instance import Instance
from .serve_runtime import ServeSpec

__all__ = ["guard_against_direct", "guard_against_systemd"]


def _service_for(inst: Instance, spec: ServeSpec):
    """按 spec 取对应的 systemd 服务类（延迟 import 避免模块级循环）。"""
    from .systemd import PluginService, WebService

    return WebService(inst) if spec.key == "web" else PluginService(inst)


def guard_against_systemd(inst: Instance, spec: ServeSpec) -> None:
    """direct 后台启动前：systemd 托管 active 时拒绝（退出码 11）。"""
    service = _service_for(inst, spec)
    if service.unit_exists() and service.is_active():
        raise OwnershipConflict(
            f"{spec.label}由 systemd 托管且处于 active，拒绝 direct 后台启动",
            hint=f"用 `{spec.key} service start|stop|status`；"
            f"要改用 direct 后台请先 `{spec.key} service uninstall`",
        )


def guard_against_direct(inst: Instance, spec: ServeSpec) -> None:
    """systemd service 安装/启动前：direct 后台存活时拒绝（退出码 11）。

    状态文件损坏/指向外人时同样拒绝（不猜测，ADR-7）。
    """
    status = serve_runtime.probe(inst, spec)
    if status.running:
        pid = status.state.pid if status.state is not None else "?"
        raise OwnershipConflict(
            f"{spec.label}已在后台运行（direct 模式，pid {pid}）",
            hint=f"先 `{spec.key} stop`，再安装/启动 systemd 托管",
        )
    if status.owner is serve_runtime.ServeOwner.CORRUPTED:
        raise OwnershipConflict(
            f"{spec.label}的后台状态文件已损坏，无法确认是否在运行",
            hint=f"确认后删除 {serve_runtime.state_path(inst, spec)} 再试",
        )
    if status.owner is serve_runtime.ServeOwner.FOREIGN:
        raise OwnershipConflict(
            f"{spec.label}的后台状态指向 pid {status.state.pid}，但它不属于本服务",
            hint="该 pid 可能已被复用；确认后删除状态文件重试",
        )
