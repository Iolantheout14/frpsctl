"""日志读取（设计文档 §18）：CLI `log` 与 Web 管理台共用的路径解析与 tail。

**只读，不写**：主日志由 frp 自己写并按天轮转（ADR-5），frpsctl 绝不追加——
多一个写入者会和 frp 的轮转互相破坏。

**tail 从文件尾反向读**：早期实现用 `deque(maxlen)` 顺序扫全文件——语义正确，
但代价是 O(文件大小)：100MB 的日志每看一次就读 100MB、每刷一次 Web 页面再读
一次。现在按块从尾部回扫，找到"最后 N 行"就停——读取量与日志总量解耦。
"""

from __future__ import annotations

import os
from pathlib import Path

from .instance import Instance

__all__ = ["MAX_SINCE_BYTES", "TAIL_BLOCK_BYTES", "resolve_log_target", "tail_lines", "tail_since"]

#: 反向读取的块大小。frp 日志一行几十字节，64KiB 一块通常就覆盖几千行；
#: 它同时是"超长行"场景下的回扫步长。
TAIL_BLOCK_BYTES = 65536

#: 增量读取单次上限（字节）：一次拉爆量的防御（积压的日志分段取，
#: 前端按 offset 继续请求）。
MAX_SINCE_BYTES = 512 * 1024


def tail_since(
    path: Path, offset: int | None, *, max_lines: int = 2000
) -> tuple[list[str], int, bool]:
    """从 `offset` 字节处读增量（v0.3.4：Web 日志面板的增量模式）。

    返回 `(新增行, 新 offset, 是否重置)`。

    重置（`reset=True`，调用方应整段替换而不是追加）的三种情形：
    - `offset` 为空/负数（首次读取）；
    - `offset` 大于当前文件大小（日志被轮转或截断——frp 按天轮转）；
    - 文件不存在/不可读。

    只返回**完整行**：EOF 处的半行不返回，且新 offset 停在最后一个完整行
    的末尾——下一次请求再取它，避免把半行当完整行显示（与 `tail_lines`
    多要一个换行的纪律同源）。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return [], 0, True
    if offset is None or offset < 0 or offset > size:
        # 全量分支：与增量分支统一为"不含换行符的行"（调用方不必区分两种形态）
        return (
            [line.rstrip("\n").rstrip("\r") for line in tail_lines(path, max_lines)],
            size,
            True,
        )
    if offset == size:
        return [], offset, False
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(MAX_SINCE_BYTES)
    except OSError:
        return [], 0, True
    # 末尾半行：丢弃并回退 offset（多字节字符不会跨行边界，offset 总在
    # 换行之后，因此这里从 offset 解码是安全的）
    if chunk and not chunk.endswith(b"\n"):
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return [], offset, False   # 连一个完整行都没有：等下次
        keep = chunk[: cut + 1]
        new_offset = offset + len(keep)
    else:
        keep = chunk
        new_offset = offset + len(chunk)
    lines = keep.decode("utf-8", errors="replace").splitlines()
    if len(lines) > max_lines:
        # 积压超过请求行数：只给最后 max_lines 行（offset 仍推进到 EOF，
        # 中间被跳过的行如实丢弃——调用方按"显示行数"语义消费）
        lines = lines[-max_lines:]
    return lines, new_offset, False


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

    行为与"整个文件读进 ring buffer 再取尾"完全一致（含"末行无换行"、CRLF、
    空文件等边界），区别只在 I/O 量：从尾部按块回扫，数到第 `lines + 1` 个
    换行就停，因此 100MB 文件读最后 200 行通常只需一趟 64KiB。

    **为什么多要一个换行**：多出来的那个换行保证返回的字节串从一个完整行的
    **起点**开始——否则 `splitlines` 的首元素是"上一行的后半截"，行数一旦凑够
    就会被当成完整行输出。读到文件头仍不足 `lines`（小文件）时返回整个文件，
    此时天然从起点开始。
    """
    if lines <= 0:
        return []
    try:
        with open(path, "rb") as handle:
            data = _read_tail_bytes(handle, lines)
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").splitlines(keepends=True)[-lines:]


def _read_tail_bytes(handle, lines: int) -> bytes:
    """从尾部回扫，返回至少包含最后 `lines` 行的字节串（见 `tail_lines`）。"""
    handle.seek(0, os.SEEK_END)
    position = handle.tell()
    data = b""
    newlines = 0
    while position > 0 and newlines <= lines:
        read = min(TAIL_BLOCK_BYTES, position)
        position -= read
        handle.seek(position)
        chunk = handle.read(read)
        newlines += chunk.count(b"\n")
        data = chunk + data
    return data
