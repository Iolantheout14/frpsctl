"""frpsctl Web 管理台（设计文档 §18）。

目标：比 frp 自带 dashboard 更好用的界面，**并且**提供官方 dashboard 完全没有的
控制面——进程启停、配置编辑（带回滚）、代理下线。

架构约束（与 `cli/` 同级的"又一个前端"）：

    web/ 只调 `core/`，不复制任何业务逻辑；`core/` 不打印、不感知 HTTP。

安全基线（比 frp 自带 dashboard 更严——那正是本项目安全决策的事实来源）见
`web/auth.py` 的模块文档。
"""

from __future__ import annotations

from ..core.instance import Instance
from .api import WebContext
from .auth import DEFAULT_SESSION_TTL, AuthManager, generate_password
from .server import WebServer, WebSettings

__all__ = [
    "AuthManager",
    "WebContext",
    "WebServer",
    "WebSettings",
    "build_web_context",
    "generate_password",
]


def build_web_context(
    inst: Instance, password: str, *, session_ttl: float = DEFAULT_SESSION_TTL
) -> WebContext:
    """组装 `WebContext`（CLI 与测试共用的入口）。"""
    return WebContext(inst=inst, auth=AuthManager(password, session_ttl=session_ttl))
