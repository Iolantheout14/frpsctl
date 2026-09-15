"""frpsctl 服务端插件（设计文档 §11）。

frp 的服务端插件机制是官方预留的 HTTP 回调协议：frps 在 `Login` / `NewProxy`
等事件发生时 POST 一段 JSON 给指定 HTTP 服务，由它返回操作决策。本包用
**标准库**实现该回调，提供多用户鉴权、端口白名单与审计日志。

⚠️ **两条必须记住的部署约束**（§11.2）：

1. 插件是全部客户端登录的**单点**，且 **fail-closed**——插件报错则 Login /
   NewProxy 直接失败；
2. frp 侧对插件 HTTP 客户端**没有设置任何 timeout**——插件一 hang，登录链路
   跟着 hang。因此 handler 内绝不做事慢的 I/O，审计走异步队列。

又因为 **frp 的插件协议没有任何认证**（`HTTPPluginOptions` 只有
`name/addr/path/ops/tlsVerify`），插件必须绑回环——这条由
`PluginPolicy.validate()` 硬性拒绝非回环地址来保证。
"""

from __future__ import annotations

__all__ = [
    "APIVersion",
    "AuditLog",
    "DecisionEngine",
    "PluginPolicy",
    "PluginRequest",
    "PluginResponse",
    "PluginServer",
    "ServerSettings",
]

from .audit import AuditLog
from .engine import DecisionEngine
from .policy import PluginPolicy
from .server import PluginServer, ServerSettings
from .types import APIVersion, PluginRequest, PluginResponse
