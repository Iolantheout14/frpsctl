"""Linux 进程原语（设计文档 §8.1）。

本模块是**唯一**允许直接触碰 `os.kill` / `/proc` / `signal` 的地方，
其余模块一律通过它访问——这样"进程安全"只有一处需要审计。
"""

from __future__ import annotations

import errno
import os
import signal
from pathlib import Path

from ..errors import UnsupportedPlatform

__all__ = [
    "assert_supported",
    "pid_alive",
    "is_zombie",
    "process_gone",
    "proc_start_time",
    "proc_cmdline",
    "terminate",
    "IS_LINUX",
]

#: `/proc` 是身份校验的前提：没有它就无法识别 pid 复用，也就无法安全停止（§3.4）。
_PROC_SELF_STAT = Path("/proc/self/stat")

IS_LINUX = _PROC_SELF_STAT.exists()


def assert_supported() -> None:
    """在最前端做一次硬检查（§3.4）。

    **宁可拒绝启动，也不进入"身份校验失效"的降级路径**——后者的代价是杀掉
    无关进程（ADR-7）。调用点：CLI 入口，以及任何会发信号的代码路径之前。
    """
    if not IS_LINUX:
        raise UnsupportedPlatform(
            "frpsctl 仅支持 Linux：/proc 不可用，无法做进程身份校验",
            hint="非 Linux 内核（或未挂载 /proc 的容器）不在支持范围内",
        )


def pid_alive(pid: int) -> bool:
    """存活探测。绝不发送真实信号，绝不误杀。

    POSIX 下 `kill(pid, 0)` 对"存在但不属于当前用户"的进程抛 `PermissionError`
    而非 `ProcessLookupError`——**必须判定为存活**，判成"未运行"会导致重复启动
    与端口冲突（§3.4）。
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def proc_start_time(pid: int) -> int | None:
    """进程启动时刻（时钟滴答），用于识别 pid 复用——安全停止的前提。

    `/proc/<pid>/stat` 的 comm 字段可能含空格与右括号，因此必须**从最后一个
    `)` 处切分**。切分后第一个字段是 state，全局第 22 个字段 starttime 位于
    索引 19。
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    rparen = raw.rfind(b")")
    if rparen < 0:
        return None
    fields = raw[rparen + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def proc_cmdline(pid: int) -> list[str]:
    """读 `/proc/<pid>/cmdline`（NUL 分隔）。

    用 /proc 直读而非 `subprocess(ps)`：省一次 fork，也不受 `ps` 是否安装影响。
    进程恰在读取瞬间退出时返回空列表——调用方据此判定"不匹配"即可。
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def is_zombie(pid: int) -> bool:
    """进程是否已退出但尚未被其父进程回收（zombie）。

    **为什么必须单独判定**：`kill(pid, 0)` 对僵尸进程返回成功（内核里还留着
    task 结构），因此"存活探测"会把一个死掉的进程判为存活。停止流程若据此
    判断，就会在进程其实已经退出后仍去升级到 SIGKILL，并最终误报
    "SIGKILL 后仍未退出"——用户看到的是完全错误的诊断。

    `/proc/<pid>/stat` 切分后第一个字段就是状态字符，`Z` 表示僵尸。
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return False
    rparen = raw.rfind(b")")
    if rparen < 0:
        return False
    rest = raw[rparen + 2 :].split()
    return bool(rest) and rest[0] == b"Z"


def process_gone(pid: int) -> bool:
    """进程是否**真的**结束了（不存在，或存在但已是僵尸）。

    停止流程必须用这个而不是 `not pid_alive(pid)`，否则会对着僵尸空等
    一整个超时甚至误判为失败。
    """
    return not pid_alive(pid) or is_zombie(pid)


def terminate(pid: int, *, force: bool = False) -> None:
    """发送停止信号。

    注意 frps **没有**信号处理器（§3.5）：SIGTERM 即进程立即终止，不存在
    "存量连接收尾"。SIGKILL 兜底是为了应对卡死/无响应，不是为了"更彻底"。
    """
    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
