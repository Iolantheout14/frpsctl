"""实例级互斥（设计文档 §8.2）。

串行化所有会改变实例状态的操作：start / stop / restart / 配置写入。
`flock` 随 fd 释放，进程崩溃时锁自动释放，不会留下需要人工清理的陈旧锁文件。

**为什么要做进程内可重入**：配置变更事务本身要持锁（防止并发改写），而它内部
会调用 `restart()`，后者同样要取这把锁。`flock` 是按 **fd** 计的——同一进程
里另开一个 fd 再锁同一文件会**阻塞自己**，表现为"变更后启动失败"这种完全不
指向真因的错误（实际是事务把自己锁死了）。因此这里按 (路径, 线程) 记录持锁
状态，重复获取时直接放行。
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..errors import LockBusy

__all__ = ["instance_lock", "is_locked"]


@dataclass
class _Held:
    """本进程内当前持有的锁。计数支持嵌套获取。"""

    fd: int
    depth: int = 1


_registry_lock = threading.Lock()
_held: dict[tuple[str, int], _Held] = {}


def _key(path: Path) -> tuple[str, int]:
    """按 (解析后的绝对路径, 线程) 记账。

    带上线程 id：`flock` 是进程级资源，但把不同线程的获取当成同一次会让
    "线程 A 持锁、线程 B 也以为拿到了"——那等于没有互斥。
    """
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path.absolute())
    return (resolved, threading.get_ident())


@contextlib.contextmanager
def instance_lock(path: Path, timeout: float = 5.0) -> Iterator[None]:
    """获取实例锁；超时抛 `LockBusy`（退出码 11）。

    为什么是 11 而不是 1：拿不到锁意味着"另一个 frpsctl 正在动这个实例"，
    属于所有权/并发冲突，与 ADR-1 的冲突语义同类，脚本应当区别对待。
    """
    key = _key(path)

    with _registry_lock:
        existing = _held.get(key)
        if existing is not None:
            existing.depth += 1  # 可重入：同一线程重复获取直接放行
            reentrant = True
        else:
            reentrant = False

    if reentrant:
        try:
            yield
        finally:
            with _registry_lock:
                entry = _held.get(key)
                if entry is not None:
                    entry.depth -= 1
                    if entry.depth <= 0:
                        _held.pop(key, None)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockBusy(
                        f"另一个 frpsctl 进程正持有实例锁：{path}",
                        hint="等待其完成，或确认无残留进程后重试",
                    ) from None
                time.sleep(0.1)
        with _registry_lock:
            _held[key] = _Held(fd=fd)
        yield
    finally:
        with _registry_lock:
            _held.pop(key, None)
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def is_locked(path: Path) -> bool:
    """只探测不获取：用于 `doctor` 判断"是否有并发操作正在进行"。

    本进程自己持有锁时返回 True——那确实意味着"有操作在进行"，而 doctor
    通常从另一个进程跑，所以这个语义在两种情形下都成立。

    ⚠️ 这是**瞬时采样**：探测与释放之间锁状态随时可能变化（TOCTOU）。
    调用方只能把它当"体检那一刻的线索"，绝不能用作任何决策依据——
    真正的互斥永远由 `instance_lock` 自己保证。
    """
    resolved = _key(path)[0]
    with _registry_lock:
        if any(key[0] == resolved for key in _held):
            return True

    if not path.exists():
        return False
    try:
        # 用 O_RDONLY：探测不该要求写权限（root 建的 0600 锁文件会让普通用户
        # 跑 doctor 时因 PermissionError 整个体检崩掉）。flock 在只读 fd 上
        # 同样可用。
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
