"""进程级诊断开关（`--verbose` 的输出通道）。

**为什么在 core**：`--verbose` 要影响的调用点分布在 core/ 的多个模块（版本探测、
`frps verify`、下载、派生进程），而 §5 的分层约束是 core **不依赖** cli——若把
实现放在 `cli/ui.py`，core 就得反向 import cli（架构倒置，且给循环导入埋雷）。
因此实现放在这里，`cli/ui.py` 只做薄委托。

**必须走 stderr**：`--json` 的 stdout 是机器可读契约，一行诊断就能让 `jq`
解析失败。stderr 断开（`2>&1 | head` 之类）时静默放弃——诊断输出不值得让
主流程崩掉（与 `ui.note` / `ui.warn` 同一条纪律）。
"""

from __future__ import annotations

import contextlib
import sys

__all__ = ["set_verbose", "verbose_enabled", "trace"]

#: 进程级开关。`--verbose` 是全局选项，而它影响的调用点都没有 CLI 上下文，
#: 因此"全局调试开关"比给整条调用链加参数更诚实——它本来就是一个进程级开关。
_VERBOSE = False


def set_verbose(enabled: bool) -> None:
    global _VERBOSE
    _VERBOSE = bool(enabled)


def verbose_enabled() -> bool:
    return _VERBOSE


def trace(text: str) -> None:
    """详细模式下把诊断信息写到 **stderr**；非详细模式下完全静默。"""
    if _VERBOSE:
        with contextlib.suppress(OSError):
            sys.stderr.write(f"[trace] {text}\n")
