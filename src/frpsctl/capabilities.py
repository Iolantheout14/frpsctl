"""能力清单（`frpsctl capabilities` 与文档生成的数据源，0.3.0）。

从代码**派生**而非手写：命令树来自 Typer app、退出码来自 `ExitCode` 枚举、
环境变量来自 `env.ENV_VARS`——README 的命令表/退出码表/环境变量表由同一份
数据生成（`frpsctl.docs`），CI 对账不一致即失败。文档漂移从此变成构建错误。
"""

from __future__ import annotations

from typing import Any

__all__ = ["command_paths", "exit_codes", "payload"]


def command_paths() -> list[str]:
    """全部**叶子命令**的点分路径（`status` / `config set` / …）。"""
    from typer.main import get_command

    from .cli import app

    command = get_command(app)

    def walk(group: Any, prefix: str = "") -> list[str]:
        out: list[str] = []
        for name in sorted(group.commands):
            sub = group.commands[name]
            path = f"{prefix} {name}".strip()
            if hasattr(sub, "commands"):
                out.extend(walk(sub, path))
            else:
                out.append(path)
        return out

    return walk(command)


def exit_codes() -> dict[str, int]:
    """退出码表（名字 → 值）。"""
    from .errors import ExitCode

    return {member.name: int(member) for member in ExitCode}


def python_requires() -> str:
    """发行包声明的 Python 下限（安装态读元数据，源码态回退）。"""
    import contextlib

    value: str | None = None
    with contextlib.suppress(Exception):  # 未安装/元数据异常 → 回退字面量
        from importlib.metadata import metadata

        value = metadata("frpsctl").get("Requires-Python")
    return value or ">=3.11"


def payload() -> dict[str, Any]:
    """完整能力清单（`frpsctl capabilities --json` 的 payload）。

    全部字段**从代码派生**：版本、命令树、退出码、环境变量、frps 门槛
    （`core/version`）——手写字面量与"从代码派生"的承诺相矛盾（v0.3.0
    review 修正：门槛改动不会漂移）。
    """
    from . import __version__
    from .core.version import MINIMUM_VERSION, RECKONED_VERSION
    from .env import ENV_VARS

    def _render(version: tuple[int, int, int]) -> str:
        return ".".join(str(part) for part in version)

    return {
        "version": __version__,
        "python_requires": python_requires(),
        "platform": "linux",
        "frps_minimum": _render(MINIMUM_VERSION),
        "frps_reckoned": _render(RECKONED_VERSION),
        "commands": command_paths(),
        "exit_codes": exit_codes(),
        "env_vars": dict(ENV_VARS),
    }
