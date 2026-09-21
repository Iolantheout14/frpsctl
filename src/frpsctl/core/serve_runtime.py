"""后台服务运行时（v0.3.3）：Web 管理台 / 插件服务的非 systemd 后台启动。

与 frps 的 direct 模式（`core/lifecycle.py`）同一套纪律，但对象是**前端服务
进程**而不是 frps 本体：

- 子进程就是 `frpsctl web serve` / `frpsctl plugin serve`——与 systemd 的
  `ExecStart` 逐字同构，优雅退出语义不变（web 直接退出、plugin 先刷审计）；
- `<实例>/<key>-state.json`（0600）记录 pid / 启动时刻 / 命令 / 参数：
  `restart` 复用参数、`status` 可查询、`uninstall` 能发现孤儿进程；
- **三重校验**（pid 存活 + 启动时刻一致 + 命令行含 `serve` 标记）后才允许
  发信号——state 文件损坏 / 陈旧 / pid 复用一律拒绝而非猜测（ADR-7）；
- 启动后做**早退检测**（连续存活窗口）+ 端口探活，失败清理状态并回显日志
  尾部（与 frps `start` 同一条诊断路径）；
- 停止：SIGTERM → 等待 → 复核身份 → SIGKILL 兜底（与 frps `stop` 同型）；
- 日志：`<实例>/<key>.log`（0600；启动时超过 `LOG_MAX_BYTES` 轮转 `.1`）。

**容器场景不要用 direct 后台**：容器里前台 `web serve` 就是正确形态（进程即
容器主进程），后台化会与容器的进程管理打架（文档同步说明）。
"""

from __future__ import annotations

import contextlib
import enum
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import (
    ConfigError,
    OwnershipConflict,
    ServeAlreadyRunning,
    ServeNotRunning,
    StartupFailed,
    StopFailed,
)
from . import platform as plat
from .config import atomic_write
from .instance import Instance
from .lifecycle import _cmdline_matches, _uptime_from_ticks
from .logs import tail_lines

__all__ = [
    "ServeSpec",
    "ServeState",
    "ServeStatus",
    "ServeOwner",
    "WEB_SPEC",
    "PLUGIN_SPEC",
    "read_state",
    "write_state",
    "clear_state",
    "build_serve_argv",
    "start_background",
    "stop_background",
    "probe",
    "rotate_log",
    "log_tail",
    "LOG_MAX_BYTES",
    "STARTUP_GRACE",
    "PORT_WAIT",
    "STOP_TIMEOUT",
    "KILL_TIMEOUT",
]

#: 日志轮转阈值：启动时超过它就改名 `.1`（只保留一份历史）。
LOG_MAX_BYTES = 8 * 1024 * 1024

#: 早退检测窗口（秒）：进程必须在此窗口内**持续存活**才算启动成功。
STARTUP_GRACE = 0.7

#: 端口探活上限（秒）。serve 绑定是启动路径上的第一步，正常远快于此。
PORT_WAIT = 3.0

#: SIGTERM 之后的等待上限（秒）。
STOP_TIMEOUT = 10.0

#: SIGKILL 之后的等待上限（秒）。
KILL_TIMEOUT = 5.0


@dataclass(frozen=True)
class ServeSpec:
    """一个可后台化的服务（web / plugin）的静态描述。"""

    key: str
    label: str
    state_name: str
    log_name: str


WEB_SPEC = ServeSpec(
    key="web",
    label="Web 管理台",
    state_name="web-state.json",
    log_name="web.log",
)

PLUGIN_SPEC = ServeSpec(
    key="plugin",
    label="插件服务",
    state_name="plugin-state.json",
    log_name="plugin.log",
)


def frpsctl_executable() -> Path:
    """定位 `frpsctl` 可执行文件（后台子进程 argv[0]；v0.3.4 从 CLI 下沉）。

    systemd 不读 PATH，必须是绝对路径。优先 `shutil.which`（全局安装时最可靠），
    其次 `sys.argv[0]`（开发态直接跑 venv 脚本）。`python -m frpsctl` 时
    argv[0] 是 `__main__.py`、不可直接执行——两种都拿不到就明确报错，
    而不是把一个坏路径写进 unit（错误会在 systemctl start 时才炸）。
    """
    import shutil as _shutil
    import sys as _sys

    found = _shutil.which("frpsctl")
    if found is not None:
        return Path(found).resolve()
    argv0 = Path(_sys.argv[0])
    if argv0.exists() and os.access(argv0, os.X_OK) and "frpsctl" in argv0.name:
        return argv0.resolve()
    from ..errors import UsageError

    raise UsageError(
        "无法确定 frpsctl 可执行文件路径（unit 的 ExecStart 与后台子进程都需要绝对路径）",
        hint="请用 PATH 里的 `frpsctl` 命令运行本命令（而不是 python -m frpsctl）",
    )


def build_serve_argv(
    executable: str | Path,
    *,
    subcommand: str,
    bind: str,
    password_file: str | Path | None = None,
    policy: str | Path | None = None,
    handler_path: str | None = None,
    allow_non_loopback: bool = False,
    trusted_proxy: bool = False,
    access_log: bool = False,
    metrics: bool = False,
) -> list[str]:
    """拼装后台 serve 的命令行（CLI 与 Web 共用的单点，v0.3.4 下沉）。

    与 systemd unit 的 `ExecStart` 保持同构（同一子命令、同一选项集）；
    选项组合做**白名单校验**：web 走 `--password-file`，plugin 走
    `--policy/--path`——误传会在派生进程之前以用法错误暴露，而不是启动后
    才被 click 拒绝（那时错误现场已远离意图）。
    """
    from ..errors import UsageError

    allowed = {
        "web": {"password_file", "allow_non_loopback", "trusted_proxy", "access_log", "metrics"},
        "plugin": {"policy", "handler_path", "access_log"},
    }
    if subcommand not in allowed:
        raise UsageError(f"不支持的后台服务：{subcommand!r}", hint="可用：web / plugin")
    provided: dict[str, object] = {
        "password_file": password_file,
        "policy": policy,
        "handler_path": handler_path,
        "allow_non_loopback": allow_non_loopback,
        "trusted_proxy": trusted_proxy,
        "access_log": access_log,
        "metrics": metrics,
    }
    for name, value in provided.items():
        if value is None or value is False:
            continue
        if name not in allowed[subcommand]:
            raise UsageError(f"{subcommand} serve 不接受选项 {name}（实现错误，请上报）")
    argv: list[str] = [str(executable), subcommand, "serve", "--bind", bind]
    if password_file is not None:
        argv += ["--password-file", str(password_file)]
    if policy is not None:
        argv += ["--policy", str(policy)]
    if handler_path is not None:
        argv += ["--path", handler_path]
    if allow_non_loopback:
        argv.append("--allow-non-loopback")
    if trusted_proxy:
        argv.append("--trusted-proxy")
    if access_log:
        argv.append("--access-log")
    if metrics:
        argv.append("--metrics")
    return argv


@dataclass(frozen=True)
class ServeState:
    """`<实例>/<key>-state.json` 的内容（启动参数快照 + 进程引用）。"""

    pid: int
    start_time: int
    binary: str
    argv: tuple[str, ...]
    args: dict
    log: str
    started_at: str
    host: str | None = None
    port: int | None = None

    def to_payload(self) -> dict:
        return {
            "pid": self.pid,
            "start_time": self.start_time,
            "binary": self.binary,
            "argv": list(self.argv),
            "args": self.args,
            "log": self.log,
            "started_at": self.started_at,
            "host": self.host,
            "port": self.port,
        }

    @classmethod
    def from_payload(cls, data: dict) -> ServeState:
        """严格解析（字段缺失/类型不符 → `ConfigError`，绝不猜测）。

        这是**发信号前**的输入：宽松解析等于把一个可能错误的 pid 当目标，
        与 ADR-7 相反。
        """
        pid = data.get("pid")
        start_time = data.get("start_time")
        binary = data.get("binary")
        argv = data.get("argv")
        if not isinstance(pid, int) or pid <= 0:
            raise ConfigError("后台状态文件的 pid 非法", hint="删除该文件后重试（会重建）")
        if not isinstance(start_time, int) or start_time <= 0:
            raise ConfigError(
                "后台状态文件缺少合法的 start_time",
                hint="没有它无法识别 pid 复用，拒绝操作。删除该文件后重试",
            )
        if not isinstance(binary, str) or not binary:
            raise ConfigError("后台状态文件的 binary 非法", hint="删除该文件后重试")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise ConfigError("后台状态文件的 argv 非法", hint="删除该文件后重试")
        args = data.get("args")
        return cls(
            pid=pid,
            start_time=start_time,
            binary=binary,
            argv=tuple(argv),
            args=args if isinstance(args, dict) else {},
            log=str(data.get("log") or ""),
            started_at=str(data.get("started_at") or ""),
            host=str(data.get("host") or "") or None,
            port=data.get("port") if isinstance(data.get("port"), int) else None,
        )

    def is_ours(self) -> bool:
        """三重校验：pid 存活 + 启动时刻一致 + 命令行含 `serve` 标记。

        与 frps 的 `ProcessRef.is_ours` 同一纪律，任何一维不成立即 fail-closed
        （读不到就按"不是它"处理）。额外的 `serve` 标记用于把"某个以同一
        frpsctl 可执行文件启动的长命进程"排除在外（状态记录只由本工具写，
        多一维是没有代价的保险）。
        """
        if not plat.pid_alive(self.pid):
            return False
        if not plat.same_process(self.pid, self.start_time):
            return False
        argv = plat.proc_cmdline(self.pid)
        if "serve" not in argv:
            return False
        return _cmdline_matches(argv, self.binary)


class ServeOwner(enum.Enum):
    """后台服务的归属探测结果（与 frps `Owner` 同哲学）。"""

    NONE = "none"
    DIRECT = "direct"
    FOREIGN = "foreign"
    STALE = "stale"
    CORRUPTED = "corrupted"


@dataclass(frozen=True)
class ServeStatus:
    """`probe` 的聚合结果（供 CLI status / uninstall / doctor 复用）。"""

    spec: ServeSpec
    owner: ServeOwner
    state: ServeState | None = None
    error: str | None = None
    uptime_seconds: float | None = None

    @property
    def running(self) -> bool:
        return self.owner is ServeOwner.DIRECT


# ---------------------------------------------------------------------------
# 状态文件
# ---------------------------------------------------------------------------


def state_path(inst: Instance, spec: ServeSpec) -> Path:
    return inst.dir / spec.state_name


def log_path(inst: Instance, spec: ServeSpec) -> Path:
    return inst.dir / spec.log_name


def read_state(inst: Instance, spec: ServeSpec) -> tuple[ServeState | None, str | None]:
    """读后台状态：`(state, error)`。文件缺失 → `(None, None)`。

    损坏/字段非法 → `(None, 错误描述)`——调用方如实报告（status 显示
    CORRUPTED、变更操作拒绝），绝不当作"没在跑"。
    """
    path = state_path(inst, spec)
    try:
        raw = path.read_text("utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"无法读取 {path}：{exc}"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"后台状态文件已损坏：{path}（{exc}）"
    if not isinstance(data, dict):
        return None, f"后台状态文件根节点必须是对象：{path}"
    try:
        return ServeState.from_payload(data), None
    except ConfigError as exc:
        return None, f"{exc.message}（{path}）"


def write_state(inst: Instance, spec: ServeSpec, state: ServeState) -> None:
    atomic_write(
        state_path(inst, spec),
        json.dumps(state.to_payload(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        mode=0o600,
    )


def clear_state(inst: Instance, spec: ServeSpec) -> None:
    with contextlib.suppress(FileNotFoundError):
        state_path(inst, spec).unlink()


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def rotate_log(path: Path) -> None:
    """超过阈值就把当前日志改名 `.1`（覆盖旧的一份）；失败不阻断启动。"""
    with contextlib.suppress(OSError):
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            shutil.move(str(path), str(path) + ".1")


def log_tail(path: Path, *, lines: int = 15) -> str:
    """日志尾部（启动失败回显）。读不到返回空串。"""
    return "".join(tail_lines(path, lines)).strip()


def _open_log(path: Path):
    """打开日志文件（0600，O_APPEND）。失败抛 OSError（由调用方收口）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    os.close(fd)
    return open(path, "ab", buffering=0)  # noqa: SIM115 - 生命周期由调用方管理


# ---------------------------------------------------------------------------
# 进程等待（与 lifecycle 同语义的独立小实现：这里不涉及 cmdline 复核）
# ---------------------------------------------------------------------------


def _await_alive(pid: int, grace: float) -> bool:
    """进程在 `grace` 秒内**持续存活**？（frps `_await_alive` 的同型实现）"""
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if plat.process_gone(pid):
            return False
        time.sleep(0.05)
    return not plat.process_gone(pid)


def _still_ours(state: ServeState) -> bool:
    if not plat.pid_alive(state.pid):
        return False
    return plat.same_process(state.pid, state.start_time)


def _wait_gone(state: ServeState, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _still_ours(state) or plat.process_gone(state.pid):
            return True
        time.sleep(0.05)
    return not _still_ours(state) or plat.process_gone(state.pid)


def _port_ready(host: str, port: int, *, timeout: float = PORT_WAIT) -> bool:
    """轮询 TCP connect 直到就绪（0.0.0.0/:: 探回环）。"""
    probe_host = "127.0.0.1" if host in ("", "0.0.0.0", "::", "[::]") else host
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((probe_host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
# 启动 / 停止 / 探测
# ---------------------------------------------------------------------------


def start_background(
    inst: Instance,
    spec: ServeSpec,
    *,
    argv: list[str],
    args: dict,
    host: str | None = None,
    port: int | None = None,
    wait: float = STARTUP_GRACE,
) -> ServeState:
    """后台启动服务（`argv` 是完整命令行，argv[0] 为 frpsctl 可执行文件）。

    顺序：清理陈旧状态 → 轮转日志 → spawn → 早退检测 → 端口探活 → 写状态。
    任何一步失败都会收拾掉刚派生的进程并**不留状态**（失败进程绝不能被
    后续 stop 当成本服务的实例）。
    """
    existing, error = read_state(inst, spec)
    if error is not None:
        raise ConfigError(error, hint="删除该文件后重试（会重建）")
    if existing is not None:
        if existing.is_ours():
            raise ServeAlreadyRunning(spec.label, existing.pid)
        if plat.pid_alive(existing.pid):
            # 存活但身份不符：绝不覆盖（记录可能指向一个无关进程）
            raise OwnershipConflict(
                f"{spec.label}的后台状态指向 pid {existing.pid}，但它不属于本服务",
                hint="该 pid 可能已被复用为无关进程；确认后删除状态文件重试",
            )
        clear_state(inst, spec)  # 陈旧记录：安全清理（与 frps start 对 STALE 一致）

    inst.ensure_private()
    log = log_path(inst, spec)
    rotate_log(log)
    try:
        handle = _open_log(log)
    except OSError as exc:
        raise StartupFailed(f"无法打开日志文件 {log}：{exc}", subject=spec.label) from None
    try:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=inst.dir,  # 与 systemd unit 的 WorkingDirectory 对齐
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,  # 脱离 SSH 会话
            )
        except OSError as exc:
            raise StartupFailed(f"无法派生进程：{exc}", subject=spec.label) from None
    finally:
        handle.close()

    started = _await_alive(proc.pid, wait)
    port_ok = True
    if started and host is not None and port is not None:
        port_ok = _port_ready(host, port)
    if not started or not port_ok:
        reason = (
            "启动后立即退出"
            if not started
            else f"启动后 {PORT_WAIT:g} 秒内端口未就绪（{host}:{port}）"
        )
        diagnostic = f"{reason}；完整日志：{log}\n日志尾部：\n{log_tail(log) or '(空)'}"
        with contextlib.suppress(ProcessLookupError, PermissionError):
            plat.terminate(proc.pid, force=True)
        deadline = time.monotonic() + KILL_TIMEOUT
        while time.monotonic() < deadline and not plat.process_gone(proc.pid):
            time.sleep(0.05)
        clear_state(inst, spec)
        raise StartupFailed(diagnostic, subject=spec.label)

    start_time = plat.proc_start_time(proc.pid)
    if start_time is None:
        # 与 frps `_write_state` 同理由：没有启动时刻就无法安全停止
        with contextlib.suppress(ProcessLookupError, PermissionError):
            plat.terminate(proc.pid, force=True)
        clear_state(inst, spec)
        raise StartupFailed(
            f"无法读取 pid {proc.pid} 的启动时刻，进程将无法被安全管理，已放弃接管",
            subject=spec.label,
        )

    state = ServeState(
        pid=proc.pid,
        start_time=start_time,
        binary=argv[0],
        argv=tuple(argv),
        args=args,
        log=str(log),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        host=host,
        port=port,
    )
    write_state(inst, spec, state)
    return state


def stop_background(
    inst: Instance, spec: ServeSpec, *, timeout: float = STOP_TIMEOUT
) -> ServeState:
    """停止后台服务。返回被停止的 state（供调用方展示 pid 等）。

    - 没在跑（无状态/已陈旧）→ `ServeNotRunning`（陈旧记录顺带清理）；
    - 存活但身份不符 → `OwnershipConflict`（绝不杀）；
    - SIGTERM → 等待 → 复核身份后 SIGKILL 兜底。
    """
    state, error = read_state(inst, spec)
    if error is not None:
        raise ConfigError(error, hint="确认该服务没有在跑后，删除状态文件重试")
    if state is None:
        raise ServeNotRunning(spec.label)
    if not state.is_ours():
        if plat.pid_alive(state.pid):
            raise OwnershipConflict(
                f"{spec.label}的后台状态指向 pid {state.pid}，但它不属于本服务，拒绝停止",
                hint="该 pid 可能已被复用为无关进程；确认后删除状态文件重试",
            )
        clear_state(inst, spec)
        raise ServeNotRunning(spec.label)

    with contextlib.suppress(ProcessLookupError):
        plat.terminate(state.pid)  # SIGTERM：web 直接退 / plugin 刷审计
    gone = _wait_gone(state, timeout)
    if not gone and _still_ours(state):
        plat.terminate(state.pid, force=True)
        gone = _wait_gone(state, KILL_TIMEOUT)
    if not gone:
        raise StopFailed(
            state.pid,
            hint="服务可能处于不可中断状态；state 文件保留，确认后手工处理",
        )
    clear_state(inst, spec)
    return state


def probe(inst: Instance, spec: ServeSpec) -> ServeStatus:
    """探测后台服务归属（永不抛异常——status/uninstall/doctor 共用）。

    CORRUPTED：状态文件损坏（无法判断，如实报告）；
    DIRECT：存活且三重校验通过；
    FOREIGN：pid 存活但身份不符（绝不杀）；
    STALE：记录陈旧（进程已退出）；
    NONE：从未启动过。
    """
    state, error = read_state(inst, spec)
    if error is not None:
        return ServeStatus(spec=spec, owner=ServeOwner.CORRUPTED, error=error)
    if state is None:
        return ServeStatus(spec=spec, owner=ServeOwner.NONE)
    if state.is_ours():
        uptime = _uptime_from_ticks(state.start_time)
        return ServeStatus(spec=spec, owner=ServeOwner.DIRECT, state=state, uptime_seconds=uptime)
    if plat.pid_alive(state.pid):
        return ServeStatus(spec=spec, owner=ServeOwner.FOREIGN, state=state)
    return ServeStatus(spec=spec, owner=ServeOwner.STALE, state=state)
