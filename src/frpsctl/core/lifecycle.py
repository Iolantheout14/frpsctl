"""所有权探测与状态机（设计文档 §8.3、ADR-1）。

**一个实例的进程任何时刻只有一个所有者**，运行时探测，绝不混用：

    resolve_owner(instance):
        存在同名 unit 且处于 active           → SYSTEMD    （委托 systemctl，pid 文件不参与判定）
        否则 state.json 存在                  → DIRECT     （以 state.json 为权威）
        否则                                  → NONE

安全核心是**三重身份校验**（`ProcessRef.is_ours`）：pid 存活 + 启动时刻一致 +
命令行匹配。三者缺一不可——只看 pid 会在 pid 复用后杀掉无关进程（R2）。
"""

from __future__ import annotations

import contextlib
import enum
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import (
    AlreadyRunning,
    FrpsctlError,
    NotRunning,
    OwnershipConflict,
    StartupFailed,
)
from . import platform as plat
from .health import HealthLayer, HealthReport, probe_plugins
from .healthcheck import parse_dashboard, parse_plugin_targets
from .instance import Instance
from .lock import instance_lock
from .version import (
    Version,
    ensure_supported,
    parse_version,
    read_binary_version,
    upgrade_hint,
)

__all__ = [
    "Owner",
    "ProcessRef",
    "State",
    "StatusReport",
    "StartReport",
    "StopReport",
    "Lifecycle",
    "STARTUP_GRACE",
]

#: 早退检测窗口（秒）。端口被占、证书缺失、配置语义错误都会在此窗口内退出。
STARTUP_GRACE = 1.5

#: SIGTERM 之后的常规等待上限。frps 没有信号处理器，SIGTERM 即进程终止（§3.5），
#: 所以正常路径应在数百毫秒内完成。
STOP_TIMEOUT = 10.0

#: SIGKILL 之后的等待上限。
KILL_TIMEOUT = 5.0


class Owner(enum.Enum):
    NONE = "none"
    DIRECT = "direct"
    SYSTEMD = "systemd"


class State(enum.Enum):
    """`status` 的唯一真相来源（§8.3 状态判定表）。"""

    RUNNING = "RUNNING"
    FOREIGN = "FOREIGN"
    STALE = "STALE"
    STOPPED = "STOPPED"
    SYSTEMD_ACTIVE = "SYSTEMD_ACTIVE"


@dataclass(frozen=True)
class ProcessRef:
    """从 state.json 还原出来的进程引用。"""

    pid: int
    start_time: int | None
    binary: str
    config: str

    def is_ours(self) -> bool:
        """三重校验：pid 存活 + 启动时刻一致 + 命令行匹配（§8.3）。"""
        if not plat.pid_alive(self.pid):
            return False
        if self.start_time is not None:
            current = plat.proc_start_time(self.pid)
            if current is not None and current != self.start_time:
                return False  # pid 被复用 → 不是我们的进程
        return _cmdline_matches(plat.proc_cmdline(self.pid), self.binary)


def _cmdline_matches(argv: list[str], binary: str) -> bool:
    """argv 里的可执行路径与记录的二进制一致？

    比对 `argv[0]` **或** `argv[1]`，两者都要求是**完整路径相等**：

    - `argv[0]` 是真 frps 的情况（内核记录的是二进制自身路径）；
    - `argv[1]` 是解释器/包装脚本的情况——shebang 脚本（以及 `sh -c` 包装）
      的 `argv[0]` 会是解释器（如 `python3`），被执行的脚本落在 `argv[1]`。

    只做全路径相等而不是"某处包含"：`frps -c` 的参数里也可能出现 `frps` 字样，
    子串匹配会误判无关进程。两侧都 `resolve()`，规避软链形式差异。
    """
    if not argv or not binary:
        return False
    target = _canonical(binary)
    return any(_canonical(arg) == target for arg in argv[:2])


def _canonical(path: str) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).resolve())
    except (OSError, RuntimeError):
        return os.path.abspath(path)


@dataclass(frozen=True)
class StartReport:
    pid: int
    version: Version
    health: HealthReport

    @property
    def healthy(self) -> bool:
        return self.health.gate


@dataclass(frozen=True)
class StopReport:
    stopped: bool
    reason: str = ""


@dataclass(frozen=True)
class StatusReport:
    """状态聚合结果。`owner` 字段让"谁在管这个进程"永远无歧义（ADR-1）。"""

    instance: str
    owner: Owner
    state: State
    pid: int | None = None
    uptime_seconds: float | None = None
    binary: Path | None = None
    binary_version: str | None = None
    disk_version: str | None = None
    config: Path | None = None
    config_mode: str | None = None
    health: HealthReport | None = None
    version_hint: str | None = None

    @property
    def binary_matches_disk(self) -> bool:
        """运行中的版本是否等于软链指向的版本（§8.6.1）。"""
        if not self.binary_version or not self.disk_version:
            return True
        return self.binary_version == self.disk_version


class Lifecycle:
    """实例的生命周期操作。所有变更操作都在实例锁内进行。"""

    def __init__(self, inst: Instance, *, binary: Path | None = None) -> None:
        self.inst = inst
        self._explicit_binary = binary

    # --- 二进制 --------------------------------------------------------

    def binary(self) -> Path:
        """**配置层面**要用的二进制：显式 `--binary` 优先，否则解软链（§6）。

        注意与 `actual_binary()` 的区别：写入 `state.json` 的必须是后者。
        """
        if self._explicit_binary is not None:
            return self._explicit_binary
        return self.inst.active_binary()

    def actual_binary(self) -> Path:
        """**实际运行**的二进制路径——写进 state.json 的那一个。

        必须 `resolve()`：`/proc/<pid>/cmdline` 里记录的是内核解析后的真实路径，
        若我们把软链路径写进 state，换链之后三重校验的命令行比对就会失配，
        自家进程会被判成 FOREIGN（R13）。显式 `--binary` 同样要 resolve，
        否则与被跟踪进程的 argv[0] 不一致。
        """
        return self.binary().resolve()

    def binary_version(self) -> Version:
        version = read_binary_version(self.binary())
        ensure_supported(version)  # 门槛 >= 0.70.0（§3.6）
        return version

    def disk_version(self) -> Version | None:
        """软链指向的版本；拿不到返回 None（用于"运行版本 vs 磁盘版本"对比）。"""
        try:
            return read_binary_version(self.inst.active_binary())
        except FrpsctlError:
            return None

    # --- 状态判定 ------------------------------------------------------

    def read_ref(self) -> ProcessRef | None:
        """从 state.json 还原进程引用；缺失或损坏返回 None。"""
        data = self.inst.read_state()
        if not data:
            return None
        pid = data.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return None
        start_time = data.get("start_time")
        return ProcessRef(
            pid=pid,
            start_time=start_time if isinstance(start_time, int) else None,
            binary=str(data.get("binary") or ""),
            config=str(data.get("config") or ""),
        )

    def resolve_owner(self) -> Owner:
        """所有权探测（ADR-1）。systemd 优先——unit 存在即由它托管。"""
        from .systemd import Systemd

        systemd = Systemd(self.inst)
        if systemd.is_active():
            return Owner.SYSTEMD
        if self.inst.state.exists():
            return Owner.DIRECT
        return Owner.NONE

    def state(self) -> tuple[State, ProcessRef | None]:
        """状态判定表（§8.3）。"""
        owner = self.resolve_owner()
        if owner is Owner.SYSTEMD:
            return State.SYSTEMD_ACTIVE, None
        ref = self.read_ref()
        if ref is None:
            return State.STOPPED, None
        if ref.is_ours():
            return State.RUNNING, ref
        if plat.pid_alive(ref.pid):
            # 存活但身份不符：**绝不 kill**，也绝不覆盖（ADR-7）
            return State.FOREIGN, ref
        return State.STALE, ref

    # --- 健康 ----------------------------------------------------------

    def check_health(
        self,
        *,
        expect_pid: int | None = None,
        timeout: float = 3.0,
    ) -> HealthReport:
        """三层健康判定（§3.7）。L1 由进程身份决定，L2/L3 由配置决定是否 SKIPPED。"""
        ref = self.read_ref()
        pid = expect_pid or (ref.pid if ref else None)

        # L1：进程
        if pid is None:
            l1 = HealthLayer.UNKNOWN
        elif expect_pid is not None:
            l1 = HealthLayer.OK if plat.pid_alive(expect_pid) else HealthLayer.FAIL
        else:
            l1 = HealthLayer.OK if (ref and ref.is_ours()) else HealthLayer.FAIL

        # L2：控制面（webServer.port = 0 → SKIPPED）
        dash = parse_dashboard(self.inst.config)
        detail = ""
        ms = 0.0
        if not dash.enabled:
            l2 = HealthLayer.SKIPPED
        else:
            from .admin import AdminClient

            with AdminClient(dash.base_url, dash.user, dash.password, timeout=timeout) as client:
                ok, ms = client.healthz()
            l2 = HealthLayer.OK if ok else HealthLayer.FAIL
            if ok:
                detail = f"/healthz 200, {ms:.0f}ms"
            else:
                detail = f"/healthz 无响应 ({dash.base_url})"

        # L3：插件面（无 httpPlugins → SKIPPED）
        targets = parse_plugin_targets(self.inst.config)
        l3, l3_detail = probe_plugins(targets)
        if l3 is HealthLayer.FAIL:
            detail = l3_detail

        return HealthReport(l1_process=l1, l2_control=l2, l3_plugin=l3, detail=detail, ms=ms)

    # --- start ---------------------------------------------------------

    def start(self, *, health_timeout: float = 10.0) -> StartReport:
        """启动流程（§8.3）。返回报告；失败抛对应异常。"""
        with instance_lock(self.inst.lock):
            owner = self.resolve_owner()
            if owner is Owner.SYSTEMD:
                raise OwnershipConflict(
                    f"实例 {self.inst.name} 正由 systemd 托管",
                    hint="请使用 `frpsctl service status` 或 systemctl 操作该实例",
                )

            ref = self.read_ref()
            if ref is not None and ref.is_ours():
                raise AlreadyRunning(ref.pid)
            if ref is not None and plat.pid_alive(ref.pid):
                # 存活但不是我们的进程：绝不 kill，交给人处置（ADR-7）
                raise OwnershipConflict(
                    f"state.json 记录的 pid {ref.pid} 存活，但不属于本实例",
                    hint=(
                        f"该 pid 可能是被复用的无关进程。请确认后手工删除 "
                        f"{self.inst.state}，或修正 state.json 内容"
                    ),
                )
            if ref is not None:
                self.inst.clear_state()  # 陈旧状态，安全清理

            # 同一个配置正被 systemd 托管 → 拒绝（ADR-1）
            from .systemd import Systemd

            systemd = Systemd(self.inst)
            if systemd.same_config_active():
                raise OwnershipConflict(
                    "同一份配置正被 systemd 托管，拒绝直接启动",
                    hint="先 `frpsctl service uninstall`，或改用 `frpsctl service status`",
                )

            binary = self.binary()
            version = self.binary_version()

            proc = self._spawn(binary)
            if not self._await_alive(proc.pid, STARTUP_GRACE):
                raise StartupFailed(self._startup_tail())

            self._write_state(proc)
            health = self._await_health(health_timeout)
            return StartReport(pid=proc.pid, version=version, health=health)

    def _spawn(self, binary: Path) -> subprocess.Popen:
        """派生进程。fd 生命周期必须正确：父进程写完就关，子进程持有独立 fd。"""
        log_path = self.inst.new_startup_log()
        handle = open(log_path, "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                [str(binary), "-c", str(self.inst.config)],
                cwd=self.inst.dir,  # 让配置里的相对路径可预期
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,  # 脱离 SSH 会话，终端关闭不被 SIGHUP 带走
            )
        finally:
            handle.close()
        self._last_startup_log = log_path
        return proc

    def _await_alive(self, pid: int, grace: float) -> bool:
        """早退检测：进程在 grace 秒内**持续存活**才算启动成功。

        两个细节都不能省：

        1. 判据是 `process_gone` 而不是 `not pid_alive`——子进程退出后、父进程
           回收前是僵尸，而 `kill(pid, 0)` 对僵尸返回成功，于是这个"存活探测"
           会把已经崩掉的 frps 判为启动成功，用户看到的是"启动成功"但服务不在
           （见 `platform.is_zombie`）。
        2. 是"两次检查之间的持续存活"，而不是"检查时还没退出"——一次性
           `poll()` 在进程恰好于检查间隙死掉时会误判。
        """
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if plat.process_gone(pid):
                return False
            time.sleep(0.05)
        return not plat.process_gone(pid)

    def _startup_tail(self, *, lines: int = 20) -> str:
        """取**本次**启动日志的尾部——frp 的原始报错是诊断的关键（G6）。

        只认本次 spawn 对应的文件，而不是"目录里最新的那个"：并发或重试场景
        下，后者可能读到上一次的日志，把已经修好的错误报给用户。
        """
        log = getattr(self, "_last_startup_log", None) or self.inst.latest_startup_log()
        if log is None:
            return ""
        try:
            content = log.read_text("utf-8", errors="replace")
        except OSError:
            return ""
        return "".join(content.splitlines(keepends=True)[-lines:]).strip()

    def _write_state(self, proc: subprocess.Popen) -> None:
        """落盘 state.json + pid 副本。

        `binary` 记录**真实路径**（`actual_binary()` 做了 resolve），否则换软链后
        `is_ours()` 的 cmdline 比对会失配——这是升级功能最容易埋进去的自伤 bug（R13）。
        """
        binary = self.actual_binary()
        payload = {
            "pid": proc.pid,
            "start_time": plat.proc_start_time(proc.pid),
            "binary": str(binary),
            "config": str(self.inst.config),
            "version": str(self.binary_version()),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "owner": Owner.DIRECT.value,
        }
        self.inst.write_state(payload)
        try:
            self.inst.pidfile.write_text(f"{proc.pid}\n", "utf-8")
        except OSError:
            pass

    def _await_health(self, timeout: float) -> HealthReport:
        """等待 L1 ∧ L2 通过；超时返回最后一次报告（由调用方决定如何处置）。"""
        deadline = time.monotonic() + max(0.0, timeout)
        report = self.check_health()
        while not report.gate and time.monotonic() < deadline:
            time.sleep(0.3)
            report = self.check_health()
        return report

    # --- stop ----------------------------------------------------------

    def stop(self, *, timeout: float = STOP_TIMEOUT, force: bool = False) -> StopReport:
        """停止流程（§8.3）。

        SIGTERM → 轮询确认退出 → 超时 SIGKILL。身份不符则**拒绝**（退出码 11），
        因为那条路径上唯一能保证的就是"可能杀错进程"。
        """
        with instance_lock(self.inst.lock):
            owner = self.resolve_owner()
            if owner is Owner.SYSTEMD:
                raise OwnershipConflict(
                    f"实例 {self.inst.name} 正由 systemd 托管",
                    hint="请使用 systemctl 停止，或 `frpsctl service uninstall` 解除托管",
                )

            ref = self.read_ref()
            if ref is None:
                self.inst.clear_state()
                raise NotRunning(self.inst.name)
            if not ref.is_ours():
                if plat.pid_alive(ref.pid):
                    raise OwnershipConflict(
                        f"pid {ref.pid} 存活但身份校验不通过，拒绝停止",
                        hint=(
                            "该 pid 可能已被复用为无关进程。确认后手工删除 "
                            f"{self.inst.state} 即可恢复"
                        ),
                    )
                self.inst.clear_state()
                raise NotRunning(self.inst.name)

            if force:
                plat.terminate(ref.pid, force=True)
                gone = self._wait_gone(ref.pid, KILL_TIMEOUT)
            else:
                with contextlib.suppress(ProcessLookupError):
                    plat.terminate(ref.pid)  # SIGTERM：frps 立即终止（§3.5，非 graceful）
                gone = self._wait_gone(ref.pid, timeout)
                if not gone:
                    plat.terminate(ref.pid, force=True)  # 兜底：应对卡死/无响应
                    gone = self._wait_gone(ref.pid, KILL_TIMEOUT)

            self.inst.clear_state()
            if not gone:
                raise StartupFailed(
                    f"pid {ref.pid} 在 SIGKILL 后仍未退出",
                    hint="进程可能处于不可中断睡眠（D 状态），检查内核日志",
                )
            return StopReport(stopped=True)

    def _wait_gone(self, pid: int, timeout: float) -> bool:
        """等待进程**真的**结束。

        用 `process_gone` 而不是 `not pid_alive`：后者对僵尸进程仍然返回
        "存活"（见 platform.is_zombie 的说明），会让停止流程空等超时并误报失败。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if plat.process_gone(pid):
                return True
            time.sleep(0.05)
        return plat.process_gone(pid)

    # --- restart -------------------------------------------------------

    def restart(
        self,
        *,
        timeout: float = STOP_TIMEOUT,
        health_timeout: float = 10.0,
    ) -> StartReport:
        """stop → start。配置回滚由上层事务负责（§9 第 8 步）。"""
        with contextlib.suppress(NotRunning):
            self.stop(timeout=timeout)
        return self.start(health_timeout=health_timeout)

    # --- status --------------------------------------------------------

    def status(self) -> StatusReport:
        """聚合状态（§7.4）。不抛异常——`status` 必须永远能回答"现在什么情况"。"""
        state, ref = self.state()
        owner = self.resolve_owner()

        version_hint: str | None = None
        binary_version: str | None = None
        disk_version: str | None = None
        uptime: float | None = None

        data = self.inst.read_state() or {}
        recorded = data.get("version")
        if isinstance(recorded, str) and recorded:
            binary_version = recorded

        with contextlib.suppress(FrpsctlError):
            disk = self.disk_version()
            if disk is not None:
                disk_version = str(disk)

        if ref is not None and state is State.RUNNING:
            if ref.start_time is not None:
                uptime = _uptime_from_ticks(ref.start_time)
            if binary_version:
                with contextlib.suppress(FrpsctlError):
                    version_hint = upgrade_hint(parse_version(binary_version))

        health: HealthReport | None = None
        if state is State.RUNNING:
            with contextlib.suppress(FrpsctlError):
                health = self.check_health()

        mode: str | None = None
        if self.inst.config.exists():
            with contextlib.suppress(OSError):
                mode = oct(self.inst.config.stat().st_mode & 0o777)[2:].zfill(4)

        return StatusReport(
            instance=self.inst.name,
            owner=owner,
            state=state,
            pid=ref.pid if ref else None,
            uptime_seconds=uptime,
            binary=Path(ref.binary) if ref and ref.binary else None,
            binary_version=binary_version,
            disk_version=disk_version,
            config=self.inst.config if self.inst.config.exists() else None,
            config_mode=mode,
            health=health,
            version_hint=version_hint,
        )


def _uptime_from_ticks(start_ticks: int) -> float | None:
    """把 /proc 的 starttime（时钟滴答）换算成"已运行秒数"。

    两个输入都是单调量，差值可靠；不依赖 `/proc/uptime` 的解析。
    """
    self_ticks = plat.proc_start_time(os.getpid())
    if self_ticks is None:
        return None
    hz = os.sysconf("SC_CLK_TCK")
    if not isinstance(hz, int) or hz <= 0:
        return None
    return max(0.0, (self_ticks - start_ticks) / hz)
