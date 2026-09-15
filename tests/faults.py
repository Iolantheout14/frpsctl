"""故障注入工具（设计文档 §13 第五层）。

**为什么需要这一层**：单元/集成/契约三层测的都是"正常路径 + 少数手写的异常
分支"。而 M5 之后的全量 review（§15.5）发现 4 处高危缺陷，**全部位于异常路径**
——正常路径完全正常，只有刻意让某个系统调用失败才会暴露。

这一层的目标不是"再测一遍功能"，而是验证一条**不变量**：

> 当任意一个系统边界失败时，frpsctl 必须
> ① 抛出**契约内的**异常（而不是裸 OSError / AttributeError / 卡死），
> ② 不留下**无人认领的进程**，
> ③ 不留下**半截或含机密的文件**，
> ④ 不留下**未释放的锁**，
> ⑤ 不把**机密**写进输出。

实现方式是 monkeypatch 真实调用点（而不是造一个假世界）：被测代码原样运行，
只有那一次系统调用被迫失败。这样测到的才是真实的错误处理路径。
"""

from __future__ import annotations

import contextlib
import errno
import os
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable

import pytest

__all__ = ["Fault", "FaultInjector", "injector", "ENOSPC", "EACCES", "ETIMEDOUT"]


ENOSPC = OSError(errno.ENOSPC, "No space left on device")
EACCES = OSError(errno.EACCES, "Permission denied")
ETIMEDOUT = OSError(errno.ETIMEDOUT, "Connection timed out")


#: 不该被注入故障的命令。两类：
#:  - **诊断工具**（pgrep/ps/tail）：断言辅助靠它们找残留进程；污染仪表会让
#:    故障假报成"测试基建崩了"，掩盖真实结论。
#:  - **框架内部探测**（systemctl）：`resolve_owner()` 每次都要问 systemd，
#:    把"派生 frps 失败"注入到它身上会让人误判失败点。
#:  - **下载器**（curl）：走 release._fetch，另有点位专门注入它。
_EXEMPT_COMMANDS = ("curl", "pgrep", "ps", "tail", "systemctl")


def _is_exempt(executable: str) -> bool:
    return any(executable.endswith(name) for name in _EXEMPT_COMMANDS)


@dataclass
class Fault:
    """一条待注入的故障：第 `at` 次匹配时抛出 `exc`。

    `exc` 可以是异常实例，也可以是"接收调用参数、返回异常"的可调用对象——
    有些异常必须知道调用上下文才能构造（例如 `subprocess.TimeoutExpired`
    需要 `cmd` 与 `timeout`）。
    """

    exc: BaseException | Callable[..., BaseException]
    #: 从第几次调用开始抛（1 = 第一次就抛）。
    at: int = 1
    #: 只对满足条件的调用生效（参数里含该子串）；None = 全部。
    matching: str | None = None
    hits: int = field(default=0, init=False)

    def build(self, args: tuple, kwargs: dict) -> BaseException:
        if callable(self.exc) and not isinstance(self.exc, BaseException):
            return self.exc(args, kwargs)
        return self.exc  # type: ignore[return-value]


class FaultInjector:
    """按"点"注入故障。每个点是一组 `Fault`，按序生效。"""

    def __init__(self) -> None:
        self._points: dict[str, list[Fault]] = {}
        self._lock = threading.Lock()

    # --- 注册 ----------------------------------------------------------

    def on(
        self,
        point: str,
        exc: BaseException | Callable[..., BaseException],
        *,
        at: int = 1,
        matching: str | None = None,
    ) -> None:
        """在 `point` 上注册一条故障。"""
        with self._lock:
            self._points.setdefault(point, []).append(Fault(exc=exc, at=at, matching=matching))

    def clear(self) -> None:
        with self._lock:
            self._points.clear()

    # --- 判定 ----------------------------------------------------------

    def should_fire(self, point: str, args: tuple = (), kwargs: dict | None = None) -> BaseException | None:
        """这次调用该不该失败？该则返回要抛的异常。"""
        with self._lock:
            faults = self._points.get(point)
            if not faults:
                return None
            rendered = " ".join(str(a) for a in args)
            for fault in faults:
                if fault.matching is not None and fault.matching not in rendered:
                    continue
                fault.hits += 1
                if fault.hits >= fault.at:
                    return fault.build(args, kwargs or {})
        return None

    def hits(self, point: str) -> int:
        with self._lock:
            return sum(f.hits for f in self._points.get(point, []))


@pytest.fixture
def injector(monkeypatch) -> FaultInjector:
    """把故障注入到真实调用点。

    每个包装器都保持原函数的签名语义（*args/**kwargs 透传），只在命中时抛异常。
    """
    faults = FaultInjector()

    # --- 文件系统 ------------------------------------------------------

    def wrap_os(name: str) -> None:
        real = getattr(os, name)

        def wrapper(*args, **kwargs):
            exc = faults.should_fire(f"os.{name}", args)
            if exc is not None:
                raise exc
            return real(*args, **kwargs)

        monkeypatch.setattr(os, name, wrapper)
        # 被测模块多是 `import os` 后 `os.replace(...)`，patch os 本身即可生效

    for name in ("replace", "fsync", "fchmod", "rename", "unlink", "mkdir"):
        if hasattr(os, name):
            wrap_os(name)

    import shutil

    real_rmtree = shutil.rmtree

    def rmtree_wrapper(*args, **kwargs):
        exc = faults.should_fire("shutil.rmtree", args)
        if exc is not None:
            raise exc
        return real_rmtree(*args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree_wrapper)

    # --- 子进程 --------------------------------------------------------

    real_run = subprocess.run

    def run_wrapper(*args, **kwargs):
        # 只把故障注入到**被测代码**发的子进程上。两类要排除：
        #  - curl：走的是 release._fetch（它自己再调 subprocess.run），注入到它
        #    身上会与 _fetch 的注入重复，还会让人以为"下载失败"是子进程层的问题；
        #  - pgrep：断言辅助（assert_no_orphan_frps）用它找残留进程。**诊断工具
        #    本身不能被污染**，否则故障会假报成"测试基建崩了"，掩盖真实结论。
        cmd = args[0] if args else kwargs.get("args", [])
        first = str(cmd[0]) if cmd else ""
        if not _is_exempt(first):
            exc = faults.should_fire("subprocess.run", args, kwargs)
            if exc is not None:
                raise exc
        # 超时注入：不真的等，直接抛 TimeoutExpired
        if kwargs.get("timeout") and faults.should_fire("subprocess.run.timeout", args):
            raise subprocess.TimeoutExpired(cmd=args[0] if args else "?", timeout=kwargs["timeout"])
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run_wrapper)

    real_popen = subprocess.Popen

    def popen_wrapper(*args, **kwargs):
        # Popen 同样要排除豁免命令：`subprocess.run` 内部就是走 Popen，
        # 不排除的话"注入 Popen 失败"会把 systemctl 探测一起打掉，
        # 让人误以为是派生 frps 失败。
        cmd = args[0] if args else kwargs.get("args", [])
        first = str(cmd[0]) if cmd else ""
        if not _is_exempt(first):
            exc = faults.should_fire("subprocess.Popen", args, kwargs)
            if exc is not None:
                raise exc
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen_wrapper)

    # --- 进程原语 ------------------------------------------------------

    from frpsctl.core import platform as plat

    real_kill = plat.terminate

    def terminate_wrapper(pid: int, *, force: bool = False):
        exc = faults.should_fire("platform.terminate", (pid, force))
        if exc is not None:
            raise exc
        return real_kill(pid, force=force)

    monkeypatch.setattr(plat, "terminate", terminate_wrapper)

    real_start_time = plat.proc_start_time

    def start_time_wrapper(pid: int):
        exc = faults.should_fire("platform.proc_start_time", (pid,))
        if exc is not None:
            # 真实失败模式是"读不到就返回 None"，而不是抛异常——生产代码内部
            # 已经吞掉 OSError。用 exc 作为信号，但仍然返回 None，才能测到
            # fail-closed 那条路径（抛异常会绕过它，测的是另一件事）。
            return None
        return real_start_time(pid)

    monkeypatch.setattr(plat, "proc_start_time", start_time_wrapper)

    # --- 网络 ----------------------------------------------------------

    import socket as _socket

    real_connect = _socket.create_connection

    def connect_wrapper(*args, **kwargs):
        exc = faults.should_fire("socket.create_connection", args)
        if exc is not None:
            raise exc
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(_socket, "create_connection", connect_wrapper)

    import httpx

    real_get = httpx.Client.get

    def get_wrapper(self, *args, **kwargs):
        exc = faults.should_fire("httpx.get", args)
        if exc is not None:
            raise exc
        return real_get(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "get", get_wrapper)

    from frpsctl.core import release as rel

    real_fetch = rel._fetch

    def fetch_wrapper(url: str, **kwargs):
        exc = faults.should_fire("release._fetch", (url,))
        if exc is not None:
            raise exc
        return real_fetch(url, **kwargs)

    monkeypatch.setattr(rel, "_fetch", fetch_wrapper)

    return faults


# ---------------------------------------------------------------------------
# 不变量断言
# ---------------------------------------------------------------------------


def assert_no_orphan_frps(inst) -> None:
    """实例目录下不应残留任何 frps 进程。

    这是故障注入最重要的不变量：进程一旦"无人认领"，工具就再也管不到它。
    判据用 cmdline 里的配置路径，而不是进程名——避免误伤别的实例。
    """
    out = subprocess.run(["pgrep", "-af", "frps"], capture_output=True, text=True).stdout
    leaked = [line for line in out.splitlines() if str(inst.dir) in line]
    assert not leaked, f"残留了无人认领的 frps 进程：{leaked}"


def assert_lock_released(inst) -> None:
    """实例锁必须已释放（否则后续所有操作都会被 LockBusy 挡住）。"""
    from frpsctl.core.lock import instance_lock

    with instance_lock(inst.lock, timeout=1.0):
        pass  # 能拿到就说明释放了


def assert_no_secret_in_output(text: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in text, f"机密泄漏到输出：{secret[:4]}…"


@contextlib.contextmanager
def tolerate(*exceptions: type[BaseException]):
    """允许这些异常通过（故障注入下它们是"预期的失败"）。

    用 `contextlib.suppress` 而不是 try/except/pass：语义完全一致，少一层缩进。
    """
    with contextlib.suppress(*exceptions):
        yield
