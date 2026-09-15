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
    StopFailed,
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
        """三重校验：pid 存活 + 启动时刻一致 + 命令行匹配（§8.3）。

        **三个维度都必须成立，缺一即不是我们的进程**（fail-closed）：

        - `start_time` 缺失（state.json 里没有，或 `/proc/<pid>/stat` 读不到）
          时**直接返回 False**。这里曾经写成"读不到就跳过这一维"，后果是三重
          校验退化成"pid 存活 + 命令行匹配"两维——而 pid 复用恰恰就是靠启动
          时刻识别的。实测：一个无关的 `/bin/sleep` 只要 argv 路径与记录的
          binary 相同，就会被判成"我们的"，随后被 stop() 杀掉。
        - 安全边界上的"读不到"必须按"不是它"处理（ADR-7：不猜测）。代价是
          极端情况下（`/proc` 异常）会拒绝停止一个其实属于我们的进程，用户
          会看到明确的错误而不是一个可能杀错的成功。
        """
        if not plat.pid_alive(self.pid):
            return False
        if self.start_time is None:
            return False
        if not plat.same_process(self.pid, self.start_time):
            return False  # 要么已退出，要么 pid 被复用 → 都不是我们的进程
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
    #: systemd 托管时填充（§8.3 要求状态表显示 unit 名与 ExecMainPID）
    systemd_unit: str | None = None
    systemd_main_pid: int | None = None
    #: state.json 损坏（无法判断进程归属）。status 必须如实报告而不是崩掉。
    state_corrupted: bool = False

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

            # 整个 L2 探针必须吞掉**所有**网络层异常并降级为 FAIL。
            # 只接 FrpsctlError 是不够的：httpx 的异常（InvalidURL、ConnectError…）
            # 继承自 Exception 而非我们的基类，socket 层的 OSError 同理。
            # 漏掉它们会让 `status` 在 dashboard 抖动时直接崩——而 status 的设计
            # 承诺是"永远能回答现在什么情况"。
            try:
                with AdminClient(dash.base_url, dash.user, dash.password, timeout=timeout) as client:
                    ok, ms = client.healthz()
            except Exception:  # noqa: BLE001 - 探针失败只能说明"不健康"，不是调用方的错
                ok, ms = False, 0.0
            l2 = HealthLayer.OK if ok else HealthLayer.FAIL
            detail = f"/healthz 200, {ms:.0f}ms" if ok else f"/healthz 无响应 ({dash.base_url})"

        # L3：插件面（无 httpPlugins → SKIPPED）。同样不能被异常打断：探针内部
        # 只捕 OSError，而 URL 解析等仍可能抛出别的东西。
        targets = parse_plugin_targets(self.inst.config)
        try:
            l3, l3_detail = probe_plugins(targets)
        except Exception:  # noqa: BLE001 - 探针失败只意味着"不可达"
            l3, l3_detail = HealthLayer.FAIL, "插件探针异常"
        if l3 is HealthLayer.FAIL:
            detail = l3_detail

        return HealthReport(l1_process=l1, l2_control=l2, l3_plugin=l3, detail=detail, ms=ms)

    # --- start ---------------------------------------------------------

    def start(self, *, health_timeout: float = 10.0) -> StartReport:
        """启动流程（§8.3）。返回报告；失败抛对应异常。"""
        self.inst.ensure_private()  # 状态与日志马上要落盘，先把目录收紧
        with instance_lock(self.inst.lock):
            if self.resolve_owner() is Owner.SYSTEMD:
                # ADR-1：所有权是 systemd 时**委托 systemctl**，不碰 pid 文件。
                # 直接拒绝（旧行为）会让"已被 systemd 纳管"的实例在 frpsctl 里
                # 完全不可操作，与文档承诺的"全部委托"相矛盾。
                return self._start_via_systemd()

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

            # ⚠️ 从这里开始，我们已经 fork 出了一个真实进程。**任何**后续异常都必须
            # 先把它收拾掉再往外抛，否则会留下一个"没人认领"的 frps：它继续跑、
            # 继续占端口，而 state.json 没写成 → stop() 认为是 NotRunning、
            # status 显示 STOPPED，工具再也管不到它，只能人工 kill。
            #
            # 触发面很实在：写 state.json 可能 ENOSPC/EACCES、`frps -v` 可能超时、
            # 配置里 webServer.addr 写成 "127.0.0.1:7500" 会让 AdminClient 抛
            # httpx.InvalidURL。
            proc = self._spawn(binary)
            try:
                if not self._await_alive(proc.pid, STARTUP_GRACE):
                    raise StartupFailed(self._startup_tail())
                self._write_state(proc, version=version)
                health = self._await_health(health_timeout)
                # ADR-3：确认 v2 API 真的在。版本门槛（§3.6）已保证 >= 0.70.0，
                # 因此这里拿到 404 说明**二进制与预期不符**（例如 --binary 指向
                # 自编译的怪版本）——报错退出 7，不猜、不降级。
                self._assert_v2_api()
            except BaseException:
                # pid 是自己刚 fork 的，此刻不可能被复用，直接 SIGKILL 是安全的
                self._reap_after_failure(proc.pid)
                raise
            return StartReport(pid=proc.pid, version=version, health=health)

    def _assert_v2_api(self, *, timeout: float = 3.0) -> None:
        """确认 dashboard 提供 v2 API（ADR-3）。

        要区分两种失败，它们的含义完全不同：

        - **404** → 端点不存在，说明这个二进制不是我们预期的版本（例如
          `--binary` 指向自编译的怪版本）。**立即报错**（退出码 7），不重试：
          重试一万次也还是 404。
        - **连接被拒/超时** → dashboard 可能只是还没绑定好端口。健康检查走的是
          `/healthz`，它与 dashboard 是同一个 server，但绑定完成的时刻未必
          早于我们这一问。这类**瞬时**失败按退避重试，超时后放弃（不再阻断启动，
          因为服务本身是好的，只是统计暂时取不到）。

        dashboard 未启用（`webServer.port = 0`）时直接跳过——那是合法配置。
        """
        dash = parse_dashboard(self.inst.config)
        if not dash.enabled:
            return
        from .admin import AdminClient
        from ..errors import ApiVersionMismatch, AdminUnreachable

        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while True:
            try:
                with AdminClient(dash.base_url, dash.user, dash.password, timeout=2.0) as client:
                    client.server_info()  # 404 → ApiVersionMismatch
                return
            except ApiVersionMismatch:
                raise  # 版本不符：重试没有意义
            except AdminUnreachable as exc:
                last = exc
                if time.monotonic() >= deadline:
                    # 服务本身已通过健康检查；统计暂时取不到不该拦下启动
                    return
                time.sleep(0.2)
            except FrpsctlError as exc:  # 其它业务异常同样不阻断启动
                last = exc
                return
        _ = last

    def _reap_after_failure(self, pid: int) -> None:
        """启动流程中途失败时收拾掉自己刚派生的进程。

        先 SIGKILL（不等 SIGTERM 的宽限期：这个进程还没被确认可用，留着只会占端口），
        再清掉可能已写一半的 state.json——**顺序不能反**：先清 state 会让这段时间里
        的 stop() 认为"没有进程"，而这个进程其实还活着。
        """
        with contextlib.suppress(ProcessLookupError, PermissionError):
            plat.terminate(pid, force=True)
        deadline = time.monotonic() + KILL_TIMEOUT
        while time.monotonic() < deadline:
            if plat.process_gone(pid):
                break
            time.sleep(0.05)
        self.inst.clear_state()

    def _spawn(self, binary: Path) -> subprocess.Popen:
        """派生进程。fd 生命周期必须正确：父进程写完就关，子进程持有独立 fd。"""
        log_path = self.inst.new_startup_log()
        # 这里刻意不用 with：fd 必须存活到 Popen 返回，由 finally 保证关闭。
        # 交给 with 会在 Popen 之前就关掉，子进程拿到的是已关闭的 fd。
        handle = open(log_path, "ab", buffering=0)  # noqa: SIM115
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

    def _write_state(self, proc: subprocess.Popen, *, version: Version) -> None:
        """落盘 state.json + pid 副本。

        两个字段不能想当然：

        - `binary` 记录**真实路径**（`actual_binary()` 做了 resolve），否则换软链后
          `is_ours()` 的 cmdline 比对会失配——升级功能最容易埋进去的自伤 bug（R13）。
        - `start_time` **必须**拿到。它是识别 pid 复用的唯一依据，缺失会让
          `is_ours()` 直接 fail-closed（返回 False），于是这个进程立刻变成"外人"，
          既 stop 不掉也 status 不出来。拿不到就当场失败、由调用方收拾进程，
          好过写一份注定无法管理的状态。

        `version` 由调用方传入：`start()` 已经算过一次，没必要再跑一次 `frps -v`
        （那是一次可抛、可超时的外部调用）。
        """
        start_time = plat.proc_start_time(proc.pid)
        if start_time is None:
            raise StartupFailed(
                f"无法读取 pid {proc.pid} 的启动时刻（/proc/{proc.pid}/stat）",
                hint="没有它就无法识别 pid 复用，该进程将无法被安全管理，已放弃接管",
            )
        binary = self.actual_binary()
        payload = {
            "pid": proc.pid,
            "start_time": start_time,
            "binary": str(binary),
            "config": str(self.inst.config),
            "version": str(version),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "owner": Owner.DIRECT.value,
        }
        self.inst.write_state(payload)
        with contextlib.suppress(OSError):
            self.inst.pidfile.write_text(f"{proc.pid}\n", "utf-8")

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
            if self.resolve_owner() is Owner.SYSTEMD:
                return self._stop_via_systemd()

            ref = self.read_ref()  # 损坏时抛 ConfigError(3)，绝不当作"未运行"
            if ref is None:
                self.inst.clear_state()
                raise NotRunning(self.inst.name)
            if not ref.is_ours():
                if plat.pid_alive(ref.pid):
                    raise OwnershipConflict(
                        f"pid {ref.pid} 存活但身份校验不通过，拒绝停止",
                        hint=(f"该 pid 可能已被复用为无关进程。确认后手工删除 {self.inst.state} 即可恢复"),
                    )
                self.inst.clear_state()
                raise NotRunning(self.inst.name)

            force_sent = force
            try:
                if force:
                    plat.terminate(ref.pid, force=True)
                    gone = self._wait_gone(ref, KILL_TIMEOUT)
                else:
                    with contextlib.suppress(ProcessLookupError):
                        plat.terminate(ref.pid)  # SIGTERM：frps 即终止（§3.5，非 graceful）
                    gone = self._wait_gone(ref, timeout)
            except PermissionError as exc:
                # 信号没发出去（进程不属于当前用户 / 权限被降级）。
                # **必须保留 state.json**：进程还在跑，它是唯一的归属记录；
                # 清掉就等于把一个活着的 frps 交给运气——工具再也找不到它。
                raise OwnershipConflict(
                    f"无权向 pid {ref.pid} 发送停止信号：{exc}",
                    hint=(
                        "该进程仍在运行。请用有权限的账号重试（例如 sudo），"
                        f"或确认它已停止后再删除 {self.inst.state}"
                    ),
                ) from None
            except OSError as exc:
                raise FrpsctlError(
                    f"停止 pid {ref.pid} 时出错：{exc}",
                    hint=f"state.json 已保留；确认进程状态后再处理 {self.inst.state}",
                ) from None

            # 兜底：SIGTERM 没奏效（卡死/无响应）→ 升级 SIGKILL。
            # ⚠️ 发 SIGKILL 之前**必须重验身份**：等待期间进程可能已退出，而 pid
            # 被无关进程复用——此时再发信号就是 R2 要防的误杀。
            # ⚠️ 这段曾在重构中被误缩进进 except 块而变成**不可达代码**，
            # 后果是卡死的 frps 再也停不掉（集成测试抓住了它）。
            if not gone and not force_sent and self._still_ours(ref):
                plat.terminate(ref.pid, force=True)
                gone = self._wait_gone(ref, KILL_TIMEOUT)
            elif not gone and not self._still_ours(ref):
                # 身份已失效（进程退出或 pid 复用）：视作已停止，不发第二个信号
                gone = True

            if gone:
                self.inst.clear_state()
            if not gone:
                # 进程仍在且**确属我们**才报"杀不掉"。此处保留 state.json：
                # 它是唯一的归属记录，清掉会让进程彻底失控（见 R2/M1）。
                raise StopFailed(ref.pid)
            return StopReport(stopped=True)

    def _still_ours(self, ref: ProcessRef) -> bool:
        """等待期间的轻量身份复核（只比启动时刻，不读 cmdline）。

        比 `is_ours()` 便宜且足够：这里要回答的只是"还能不能对这个 pid 发信号"，
        而启动时刻一致就排除了 pid 复用。
        """
        if ref.start_time is None:
            return False
        return plat.pid_alive(ref.pid) and plat.same_process(ref.pid, ref.start_time)

    def _wait_gone(self, ref: ProcessRef, timeout: float) -> bool:
        """等待进程**真的**结束。

        两个判据都不能省：

        - 用 `process_gone` 而不是 `not pid_alive`：后者对僵尸进程仍返回"存活"
          （见 `platform.is_zombie`），会让停止流程空等超时并误报失败。
        - 先看启动时刻是否还对得上：pid 一旦被复用，`process_gone` 会一直返回
          False（那个无关进程活着），于是流程会走到"超时"分支——**必须在发
          SIGKILL 之前用 `_still_ours` 拦住**（本函数只负责判定，不负责发信号）。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # 判据是"进程是否还在且仍属我们"，而不是"启动时刻是否还匹配"：
            # 进程退出后会短暂处于僵尸态，而僵尸的 starttime **仍然可读**，
            # 用 _still_ours 会把它当成"还没退出"→ 空等超时 → 误报"杀不掉"。
            if not self._still_ours(ref) or plat.process_gone(ref.pid):
                return True
            time.sleep(0.05)
        return not self._still_ours(ref) or plat.process_gone(ref.pid)

    # --- systemd 委托 ---------------------------------------------------

    def _start_via_systemd(self) -> StartReport:
        """委托 systemctl 启动，然后按三层健康检查确认结果。

        健康检查对 systemd 实例同样适用：`/healthz` 是 frps 自己提供的，与
        谁把它拉起来无关。版本从 unit 的 ExecStart 指向的二进制上读——pid 文件
        在 systemd 模式下不参与任何判定（ADR-1）。
        """
        from .systemd import Systemd

        systemd = Systemd(self.inst)
        systemd.start()
        pid = systemd.main_pid()
        version = self.disk_version() or read_binary_version(self.binary())
        health = self._await_health(10.0)
        if pid is None:
            raise StartupFailed(
                "systemd 报告启动成功，但拿不到 MainPID",
                hint=f"用 `systemctl status {systemd.unit_name}` 查看 unit 状态",
            )
        return StartReport(pid=pid, version=version, health=health)

    def _stop_via_systemd(self) -> StopReport:
        from .systemd import Systemd

        Systemd(self.inst).stop()
        return StopReport(stopped=True)

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
        corrupted = self.inst.state_corrupted()
        if corrupted:
            # status 必须**永远**能回答"现在什么情况"，不能因为状态文件损坏就
            # 以异常收场。这里如实报告"不可判定"，把处置交给用户（stop/start
            # 会拒绝，那是对的——它们要动进程）。
            mode: str | None = None
            if self.inst.config.exists():
                with contextlib.suppress(OSError):
                    mode = oct(self.inst.config.stat().st_mode & 0o777)[2:].zfill(4)
            return StatusReport(
                instance=self.inst.name,
                owner=Owner.NONE,
                state=State.STOPPED,
                config=self.inst.config if self.inst.config.exists() else None,
                config_mode=mode,
                state_corrupted=True,
            )
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
        unit_name: str | None = None
        unit_pid: int | None = None
        if state is State.SYSTEMD_ACTIVE:
            from .systemd import Systemd

            systemd = Systemd(self.inst)
            unit_name = systemd.unit_name
            with contextlib.suppress(Exception):
                unit_pid = systemd.main_pid()
            with contextlib.suppress(FrpsctlError):
                health = self.check_health(expect_pid=unit_pid)
        elif state is State.RUNNING:
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
            systemd_unit=unit_name,
            systemd_main_pid=unit_pid,
            state_corrupted=corrupted,
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
