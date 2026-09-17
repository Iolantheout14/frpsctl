"""输出渲染：人读与 `--json` 共享同一份 core 结果（设计文档 §5 分层约束）。

硬约束（§10）：**口令与 token 绝不进日志、绝不进 `--json`、绝不进异常消息**。
本模块的 `mask_secret()` 是这条约束的最后一道闸门。
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from typing import Any

__all__ = [
    "emit",
    "emit_json",
    "mask_secret",
    "human_bytes",
    "human_duration",
    "warn",
    "note",
    "progress",
    "end_progress",
    "set_verbose",
    "verbose_enabled",
    "trace",
]

_MASK = "***"

#: 进程级开关，由 `--verbose` 设置。
#:
#: 为什么是模块级而不是穿参：`--verbose` 是**全局**选项，而它要影响的调用点分布在
#: `core/release.py`（下载/复验子进程）与 `core/config.py`（`frps verify` 子进程）
#: 里，这些函数都没有 CLI 上下文。把它做成全局调试开关比给整条调用链加参数更诚实
#: ——它确实就是一个进程级的诊断开关。
#: 在 `--verbose` 出现之前这个选项被解析后**从未被读过**（文档却承诺"详细输出"），
#: 那比没有这个选项更糟。
_VERBOSE = False


def set_verbose(enabled: bool) -> None:
    global _VERBOSE
    _VERBOSE = bool(enabled)


def verbose_enabled() -> bool:
    return _VERBOSE


def trace(text: str) -> None:
    """详细模式下把诊断信息写到 **stderr**。

    必须走 stderr：`--json` 的 stdout 是机器可读契约，一行诊断就能让 `jq` 解析失败。
    非详细模式下完全静默。

    stderr 断开（`2>&1 | head` 之类）时静默放弃：诊断输出不值得让主流程崩掉。
    """
    if _VERBOSE:
        with contextlib.suppress(OSError):
            sys.stderr.write(f"[trace] {text}\n")


def mask_secret(value: object, *, reveal: bool = False) -> str:
    """打码。空值显示为 `(empty)`，便于区分"没设"和"设了但打码"。"""
    if reveal:
        return str(value)
    text = "" if value is None else str(value)
    if not text:
        return "(empty)"
    if len(text) <= 4:
        return _MASK
    return f"{text[:2]}{_MASK}{text[-2:]}"


def emit(text: str = "") -> None:
    """人读输出。统一走这里，便于将来换成 rich 而不改各命令。"""
    sys.stdout.write(text + "\n")


def warn(text: str) -> None:
    """告警输出（stderr）：不改变退出码，但必须让人看见。

    stderr 已断开时静默放弃：告警不值得让主流程崩掉，更不该在
    `map_exceptions` 的错误报告路径上抛二次异常（那会变成 traceback）。
    """
    with contextlib.suppress(OSError):
        sys.stderr.write(text + "\n")


def note(text: str) -> None:
    """进度/提示输出（stderr）。

    与 `warn` 的区别是语义（提示 vs 告警），渠道相同：stderr——`--json` 的
    stdout 是机器可读契约，任何进度信息都不能进去。
    """
    with contextlib.suppress(OSError):
        sys.stderr.write(text + "\n")


_progress_last = 0.0


def progress(text: str) -> None:
    """单行进度（stderr）：终端上原地刷新，非终端上按秒限流地按行输出。

    "等待健康检查"这类过程需要连续反馈（最长 10 秒），但两类输出环境的要求
    相反：终端想要**原地刷新**（\\r 不换行 + 清行尾防残影），重定向到文件/CI
    日志则既不能写控制字符、也不能每 0.3 秒刷一行。这里按 `isatty` 分流。

    本函数**绝不抛异常**：它挂在启动流程的健康等待里，一次写失败（管道断开）
    不该让刚派生的 frps 被当成"启动失败"收掉。
    """
    global _progress_last
    now = time.monotonic()
    with contextlib.suppress(OSError):
        if sys.stderr.isatty():
            # \033[K 清行尾：新文本短于旧文本（"10s" → "9s"）时不留残影
            sys.stderr.write(f"\r{text}\033[K")
            sys.stderr.flush()
            _progress_last = now
        elif now - _progress_last >= 1.0:
            sys.stderr.write(f"{text}\n")
            _progress_last = now


def end_progress() -> None:
    """收尾单行进度：终端上清掉可能残留的半行（非终端无操作）。"""
    with contextlib.suppress(OSError):
        if sys.stderr.isatty():
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


def emit_json(payload: Any, *, compact: bool = False) -> None:
    """机器可读输出。`default=str` 兜住 Path/Enum 之类，避免序列化失败。

    `compact=True` 输出**单行** JSON（NDJSON）：`status --watch --json` 会连续
    输出多个对象，多行缩进格式无法被 `jq -c` / 逐行消费工具处理。
    """
    if compact:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    else:
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")


def human_bytes(count: int) -> str:
    """流量展示：1.2 GiB 比 1288490188 好读（§7.4）。"""
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TiB"


def human_duration(seconds: float | None) -> str:
    """运行时长：2h13m / 45s / 3d4h。"""
    if seconds is None:
        return "-"
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"
