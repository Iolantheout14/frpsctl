"""frpsctl —— 把 frp 服务端（frps）包装成命令行工具。

设计文档：frpsctl-设计方案.md
硬边界：绝不重新实现 frp 已有的能力（配置合法性判定走官方 verify，
状态采集走官方 v2 Admin API，转发逻辑一行不碰）。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
