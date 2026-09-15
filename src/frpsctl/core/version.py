"""版本解析与门槛判定（设计文档 §3.6）。

**门槛：只接受 >= 0.70.0**，其余一律拒绝（退出码 4）。理由见 §3.6——
低于 0.70.0 没有 v2 Admin API（ADR-3 因此无降级路径），且存在已知安全问题。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import UnsupportedVersion, VersionParseError

__all__ = [
    "MINIMUM_VERSION",
    "RECKONED_VERSION",
    "Version",
    "parse_version",
    "read_binary_version",
    "ensure_supported",
]

#: 最低支持版本。低于此值直接拒绝（§3.6）。
MINIMUM_VERSION: tuple[int, int, int] = (0, 70, 0)

#: 目标版本。0.70.x 会收到"建议升级"的提示（< 0.71.0 缺远程 DoS 修复）。
RECKONED_VERSION: tuple[int, int, int] = (0, 71, 0)

#: 用正则提取三元组，而不是对 split(".") 做 int()——
#: 后者对 `0.71.0-rc1`、`0.71.0+dev` 这类合法后缀不够稳健（§3.6）。
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True, order=True)
class Version:
    """有序的三段版本号。`frps -v` 输出**无 `v` 前缀**（§3.1）。"""

    major: int
    minor: int
    patch: int
    raw: str = ""

    @property
    def tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def parse_version(output: str) -> Version:
    """从 `frps -v` 的输出里提取版本号。

    解析失败即报错，**不猜**（ADR-7）：拿不到版本就无法判断是否达标，
    而放行一个未知版本意味着后面所有基于版本的行为都失去依据。
    """
    match = _VERSION_RE.search(output)
    if match is None:
        raise VersionParseError(output.strip()[:200])
    major, minor, patch = (int(g) for g in match.groups())
    return Version(major, minor, patch, raw=output.strip()[:200])


def read_binary_version(binary: Path, *, timeout: float = 5.0) -> Version:
    """运行 `<binary> -v` 并解析版本号。

    `-v` 是 Cobra 的持久标志，`verify` 子命令同样认识它（附录 B-2 复核纪律 1），
    但这里只用最朴素的 `-v` 形式。
    """
    try:
        proc = subprocess.run(
            [str(binary), "-v"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise VersionParseError(f"{binary} 不存在或不可执行") from None
    except subprocess.TimeoutExpired:
        raise VersionParseError(f"{binary} -v 超时未返回") from None
    return parse_version(proc.stdout or proc.stderr)


def ensure_supported(version: Version | tuple[int, int, int]) -> None:
    """门槛检查。不达标抛 `UnsupportedVersion`（退出码 4）。"""
    value = version.tuple if isinstance(version, Version) else version
    if value < MINIMUM_VERSION:
        raise UnsupportedVersion(value)


def upgrade_hint(version: Version | tuple[int, int, int]) -> str | None:
    """返回"建议升级"的提示文本，或 None 表示无需提示。

    `0.70.x` 可用但有已知远程 DoS（客户端传负数 pool_count 致 frps panic，
    v0.71.0 修复），因此只提示、不拒绝。
    """
    value = version.tuple if isinstance(version, Version) else version
    if MINIMUM_VERSION <= value < RECKONED_VERSION:
        return (
            f"frps {'.'.join(map(str, value))} 缺少远程 DoS 修复"
            f"（建议升级到 >= {'.'.join(map(str, RECKONED_VERSION))}）"
        )
    return None
