"""插件协议层：与 frp 服务端交换的数据结构（设计文档 §11.1）。

这些字段名**全部**在 frp `v0.71.0` 源码上逐条核对过，不可凭直觉改：

| 事实 | 源码位置 |
|------|---------|
| 请求体 `{version, op, content}` | `pkg/plugin/server/types.go:21-25` |
| 响应 `{reject, reject_reason, unchange, content}` | `pkg/plugin/server/types.go:27-32` |
| `op` 在 **URL query**，不在请求头 | `pkg/plugin/server/http.go:93-97` |
| 请求头 `X-Frp-Reqid`、`Content-Type: application/json` | `http.go:104-105` |
| `Login` 的 `content.user` 是**字符串** | `types.go:34-38` 内嵌 `msg.Login.User string` |
| `NewProxy` 的 `content.user` 是**对象** `{user,metas,run_id}` | `types.go:41-50` |
| 协议版本固定 `"0.1.0"` | `pkg/plugin/server/plugin.go:22` |

⚠️ **全篇最危险的坑**：响应里的 `unchange` 若**不显式给出**，Go 反序列化后是零值
`false`，而 `manager.go:99` 在 `!Unchange` 时会 `content = retContent.(*T)` ——
把一个空指针断言成业务类型。也就是说，**只回 `{"reject": false}` 会把这次
Login/NewProxy 的内容清空**（`user` 变空串、`proxy_name` 变空）。

因此本模块的 `PluginResponse` **强制**要求显式声明 `unchange`：构造响应只有
`reject()` / `pass_through()` / `replace()` 三个入口，从类型上就不给你"忘记写"的机会。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "APIVersion",
    "Op",
    "PluginRequest",
    "PluginResponse",
    "LoginContent",
    "UserInfo",
    "NewProxyContent",
]

#: 协议版本，frp 源码里是常量 `APIVersion = "0.1.0"`。
APIVersion = "0.1.0"


class Op(str, Enum):
    """全部合法 op（`pkg/plugin/server/plugin.go` 的 Op* 常量）。"""

    LOGIN = "Login"
    NEW_PROXY = "NewProxy"
    CLOSE_PROXY = "CloseProxy"
    PING = "Ping"
    NEW_WORK_CONN = "NewWorkConn"
    NEW_USER_CONN = "NewUserConn"


@dataclass(frozen=True)
class PluginRequest:
    """frps 发来的请求。`raw_content` 保留原始字典，便于审计与按 key 取字段。"""

    op: Op
    version: str
    content: dict[str, Any]
    reqid: str = ""

    @classmethod
    def from_payload(cls, *, op: str, version: str, body: dict[str, Any], reqid: str = "") -> PluginRequest:
        try:
            parsed = Op(op)
        except ValueError:
            # 未知 op 不能当成正常流程放行——见 handler 的处理策略
            raise UnknownOp(op) from None
        content = body.get("content")
        return cls(
            op=parsed,
            version=version,
            content=content if isinstance(content, dict) else {},
            reqid=reqid,
        )


class UnknownOp(Exception):
    """收到了协议里没有的 op。"""

    def __init__(self, op: str) -> None:
        super().__init__(f"未知的插件 op：{op!r}")
        self.op = op


@dataclass(frozen=True)
class PluginResponse:
    """响应。**只能**通过三个工厂方法构造，保证 `unchange` 永远被显式给出。"""

    reject: bool
    unchange: bool
    reject_reason: str = ""
    content: dict[str, Any] | None = None

    @classmethod
    def reject_op(cls, reason: str) -> PluginResponse:
        """拒绝该操作。frps 会把它当作该操作的失败原因返回给客户端。"""
        return cls(reject=True, unchange=False, reject_reason=reason)

    @classmethod
    def pass_through(cls) -> PluginResponse:
        """放行，**保持原内容不变** —— 这是绝大多数情况下的正确返回值。

        显式带上 `unchange: true`：只回 `{"reject": false}` 会让 frps 用零值
        覆盖内容（见模块文档）。
        """
        return cls(reject=False, unchange=True)

    @classmethod
    def replace(cls, content: dict[str, Any]) -> PluginResponse:
        """放行，并**替换**内容。

        ⚠️ frp 侧会做 `retContent.(*T)` 类型断言，失败会 **panic**（源码注释：
        "Buggy Plugin implementations still panic here, by design"）。因此替换
        必须给出与原始 content **同构**的完整对象，而不是打补丁式的局部字段。
        本工具当前不使用这条路径（我们没有改写内容的需求），保留它是为了
        协议完整性，并在 handler 中显式标注风险。
        """
        return cls(reject=False, unchange=False, content=content)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "reject": self.reject,
            # 关键：无论哪条路径都显式输出 unchange
            "unchange": self.unchange,
        }
        if self.reject:
            payload["reject_reason"] = self.reject_reason
        elif not self.unchange:
            payload["content"] = self.content
        return payload


# ---------------------------------------------------------------------------
# 各 op 的 content 视图
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoginContent:
    """`Login` 的 content。

    注意 `user` 在这里是**字符串**（`msg.Login.User`），与 `NewProxyContent`
    的 `user` 对象**不同型**——写错会得到 `''`，而且不报错。
    """

    user: str
    version: str = ""
    hostname: str = ""
    run_id: str = ""
    client_address: str = ""
    metas: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, content: dict[str, Any]) -> LoginContent:
        raw_user = content.get("user")
        return cls(
            # 防御：万一上游改成对象，也降级成空串而不是崩掉
            user=raw_user if isinstance(raw_user, str) else "",
            version=_str(content.get("version")),
            hostname=_str(content.get("hostname")),
            run_id=_str(content.get("run_id")),
            client_address=_str(content.get("client_address")),
            metas=_str_map(content.get("metas")),
        )


@dataclass(frozen=True)
class UserInfo:
    """`NewProxy` 等 op 里的 `content.user` 对象（`types.go:41-45`）。"""

    user: str = ""
    run_id: str = ""
    metas: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> UserInfo:
        if not isinstance(raw, dict):
            return cls()
        return cls(
            user=_str(raw.get("user")),
            run_id=_str(raw.get("run_id")),
            metas=_str_map(raw.get("metas")),
        )


@dataclass(frozen=True)
class NewProxyContent:
    """`NewProxy` 的 content。

    `remote_port` 只在 tcp / udp 代理上有意义；http/https 走域名，因此
    "端口白名单"对它们不适用（由域名单独管，本工具当前不限制域名）。
    """

    user: UserInfo
    proxy_name: str = ""
    proxy_type: str = ""
    remote_port: int = 0
    subdomain: str = ""
    custom_domains: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, content: dict[str, Any]) -> NewProxyContent:
        return cls(
            user=UserInfo.parse(content.get("user")),
            proxy_name=_str(content.get("proxy_name")),
            proxy_type=_str(content.get("proxy_type")),
            remote_port=_int(content.get("remote_port")),
            subdomain=_str(content.get("subdomain")),
            custom_domains=[
                str(item) for item in (content.get("custom_domains") or []) if isinstance(item, str)
            ],
        )


# ---------------------------------------------------------------------------
# 宽松类型转换（插件不能因为上游多给/少给字段就崩）
# ---------------------------------------------------------------------------


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): _str(v) for k, v in value.items()}
