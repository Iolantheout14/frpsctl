"""op → 裁决的映射（设计文档 §11.3）。

**为什么要把这层从 HTTP 里拆出来**：协议解析一旦和 socket 混在一起，就只能靠
起服务来测。拆开之后，"只返回 `reject:false` 会清空 content""Login 的 user 是
字符串而 NewProxy 是对象"这些坑都能用纯函数断言，测试跑在毫秒级。

每个 op 的语义都来自 frp `v0.71.0` 源码，不是猜的：

| op | 我们做什么 | 依据 |
|----|-----------|------|
| `Login` | 鉴权：用户是否存在、`client_id` 是否与 `user` 一致 | `types.go:34-38`（user 是**字符串**） |
| `NewProxy` | 端口白名单 / 代理名 / 类型 / 随机端口 | `types.go:41-51`（user 是**对象**） |
| `CloseProxy` / `Ping` / `NewWorkConn` / `NewUserConn` | 放行（`unchange: true`） | 这些 op 不改变权限边界 |
| 未知 op | **拒绝** | 我们只注册了上面这些；收到别的说明有人在说话 |

> 未知 op 为什么拒绝而不是放行：frps 只会向插件发送 `ops` 里声明过的 op。
> 收到未声明的 op 意味着请求不是来自我们配置的那个 frps（或协议已变），
> 两种情况的正确反应都是**不猜**（ADR-7）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .audit import AuditLog, AuditRecord
from .policy import PluginPolicy, decide_login, decide_new_proxy
from .types import (
    LoginContent,
    NewProxyContent,
    Op,
    PluginRequest,
    PluginResponse,
)

__all__ = ["DecisionEngine", "EngineResult"]


@dataclass(frozen=True)
class EngineResult:
    response: PluginResponse
    record: AuditRecord


class DecisionEngine:
    """把 `PluginRequest` 变成 `PluginResponse`，并留下审计记录。"""

    def __init__(self, policy: PluginPolicy, audit: AuditLog | None = None) -> None:
        self.policy = policy
        self.audit = audit

    def handle(self, request: PluginRequest, *, source: str = "") -> PluginResponse:
        started = time.monotonic()
        response, record = self._dispatch(request, source=source)
        elapsed_ms = (time.monotonic() - started) * 1000
        self._write_audit(record, elapsed_ms)
        return response

    # --- 各 op ---------------------------------------------------------

    def _dispatch(self, request: PluginRequest, *, source: str) -> tuple[PluginResponse, AuditRecord]:
        common = {
            "op": request.op.value,
            "reqid": request.reqid,
            "source": source,
        }

        if request.op is Op.LOGIN:
            return self._login(request, common)
        if request.op is Op.NEW_PROXY:
            return self._new_proxy(request, common)

        # CloseProxy / Ping / NewWorkConn / NewUserConn：不涉及权限边界。
        # 必须显式 unchange=True，否则 frps 会用零值覆盖内容（§11.1）。
        return (
            PluginResponse.pass_through(),
            AuditRecord(user="", decision="allow", reason="pass-through", **common),
        )

    def _login(self, request: PluginRequest, common: dict) -> tuple[PluginResponse, AuditRecord]:
        # Login 时 content.user 是**字符串**（msg.Login.User）
        content = LoginContent.parse(request.content)
        client_id = content.metas.get("client_id", "")
        decision = decide_login(
            self.policy,
            user=content.user,
            client_id=client_id,
            reqid=request.reqid,
        )
        record = AuditRecord(
            user=decision.user or content.user,
            decision="allow" if decision.allowed else "deny",
            reason=decision.reason,
            client_id=client_id,
            **common,
        )
        if decision.allowed:
            return PluginResponse.pass_through(), record
        return PluginResponse.reject_op(decision.reason), record

    def _new_proxy(
        self, request: PluginRequest, common: dict
    ) -> tuple[PluginResponse, AuditRecord]:
        # NewProxy 时 content.user 是**对象**（UserInfo）——与 Login 不同型
        content = NewProxyContent.parse(request.content)
        decision = decide_new_proxy(
            self.policy,
            user=content.user.user,
            proxy_name=content.proxy_name,
            proxy_type=content.proxy_type,
            remote_port=content.remote_port,
        )
        record = AuditRecord(
            user=decision.user or content.user.user,
            decision="allow" if decision.allowed else "deny",
            reason=decision.reason,
            proxy_name=content.proxy_name,
            proxy_type=content.proxy_type,
            remote_port=content.remote_port,
            **common,
        )
        if decision.allowed:
            return PluginResponse.pass_through(), record
        return PluginResponse.reject_op(decision.reason), record

    # --- 审计 ----------------------------------------------------------

    def _write_audit(self, record: AuditRecord, elapsed_ms: float) -> None:
        if self.audit is None:
            return
        # AuditRecord 是 frozen 的，用 dataclasses.replace 补上耗时
        from dataclasses import replace

        self.audit.record(replace(record, elapsed_ms=elapsed_ms))
