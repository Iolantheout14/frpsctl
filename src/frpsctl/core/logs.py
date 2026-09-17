"""日志读取（设计文档 §18）：CLI `log` 与 Web 管理台共用的路径解析与 tail。

**只读，不写**：主日志由 frp 自己写并按天轮转（ADR-5），frpsctl 绝不追加——
多一个写入者会和 frp 的轮转互相破坏。
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

from .instance import Instance

__all__ = ["resolve_log_target", "tail_lines"]


def resolve_log_target(inst: Instance) -> Path:
    """从配置的 `log.to` 解析日志路径；为 `console` 或配置不可读时回退。

    回退顺序（ADR-5）：`log.to` 指向的文件 → 最新的 startup 日志 → 实例目录
    下默认约定的 `frps.log`。`console` 表示 frp 把日志写进 stdout（那部分被
    startup 日志捕获）。
    """
    import tomllib

    try:
        data = tomllib.loads(inst.config.read_text("utf-8"))
    except Exception:  # noqa: BLE001 - 配置不可读时走回退路径（doctor/log 各自会报）
        data = {}
    to = (data.get("log") or {}).get("to")
    if isinstance(to, str) and to and to.lower() != "console":
        candidate = Path(to)
        return candidate if candidate.is_absolute() else (inst.config.parent / candidate)
    latest = inst.latest_startup_log()
    return latest or inst.log_file


def tail_lines(path: Path, lines: int) -> list[str]:
    """读文件尾部 `lines` 行（含换行符）；文件不存在/不可读返回空列表。

    用 ring buffer（`deque(maxlen)`）——百万行的大文件也只保留尾部 N 行，
    内存占用与文件大小无关。
    """
    if lines <= 0:
        return []
    ring: deque[str] = deque(maxlen=lines)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            ring.extend(handle)
    except OSError:
        return []
    return list(ring)
