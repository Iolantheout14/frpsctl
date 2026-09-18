"""命令实现包：导入即注册。

每个模块在 import 时把命令挂到 `cli/app.py` 的对应 Typer 实例上。
0.3.0 从 3253 行的 `cli/__init__.py` 按域拆出（每模块 < 900 行）。
"""

from __future__ import annotations

from . import config, install, lifecycle, observe, plugin, service, web

__all__ = ["config", "install", "lifecycle", "observe", "plugin", "service", "web"]
