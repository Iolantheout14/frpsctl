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

__all__ = ["configure_streams", "set_verbose", "verbose_enabled", "trace"]


def configure_streams() -> None:
    """把 stdout / stderr 固定为 **UTF-8 输出**（v0.3.1）。

    为什么需要：本工具的用户界面与错误消息大量使用中文，而 Python 的 stdio
    编码跟随 locale——在 `LC_ALL=C`（最小化容器、某些 systemd 环境）且
    PEP 538 coercion 失效（`PYTHONCOERCECLOCALE=0`，或系统没有 C.UTF-8）
    时，stdout 是 ASCII + strict：写中文直接抛 `UnicodeEncodeError`
    → 顶层兜底成"未分类错误(1)"（实测：`doctor`/`uninstall` 等输出中文
    报告的命令全部崩）。stderr 默认 `backslashreplace` 虽不崩，但输出
    `\\uXXXX` 乱码，同样不可用。

    修法：入口处 reconfigure 为 UTF-8 + `backslashreplace`——任何 locale 下
    输出都是一致的 UTF-8 字节；万一遇到无法编码的字符（理论上不会），转义
    形式仍然可诊断，绝不抛异常。`main()` / `web serve` / `plugin serve`
    都经由 CLI 入口，因此这里配置一次覆盖全部。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # 被测试替换成 StringIO 之类：跳过
            continue
        with contextlib.suppress(Exception):  # 已关闭/不支持时静默放弃
            reconfigure(encoding="utf-8", errors="backslashreplace")

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
