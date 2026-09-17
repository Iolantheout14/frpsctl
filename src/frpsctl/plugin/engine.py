"""op → 裁决的映射（设计文档 §11.3）。

**为什么要把这层从 HTTP 里拆出来**：协议解析一旦和 socket 混在一起，就只能靠
起服务来测。拆开之后，"只返回 `reject:false` 会清空 content""Login 的 user 是
字符串而 NewProxy 是对象"这些坑都能用纯函数断言，测试跑在毫秒级。

每个 op 的语义都来自 frp `v0.71.0` 源码，不是猜的：

| op | 我们做什么 | 依据 |
|----|-----------|------|
| `Login` | 鉴权：用户存在、`client_id` 与 `user` 一致 | `types.go:34-38`（user 是**字符串**） |
| `NewProxy` | 端口白名单 / 代理名 / 类型 / 随机端口 | `types.go:41-51`（user 是**对象**） |
| `CloseProxy` / `Ping` / `NewWorkConn` / `NewUserConn` | 放行（`unchange: true`） | 不涉及权限边界 |
| 未知 op | **拒绝** | 我们只注册了上面这些；收到别的说明有人在说话 |

> 未知 op 为什么拒绝而不是放行：frps 只会向插件发送 `ops` 里声明过的 op。
> 收到未声明的 op 意味着请求不是来自我们配置的那个 frps（或协议已变），
> 两种情况的正确反应都是**不猜**（ADR-7）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace

from .audit import AuditLog, AuditRecord
from .policy import PluginPolicy, decide_login, decide_new_proxy
from .quota import QuotaChecker
from .types import (
    LoginContent,
    NewProxyContent,
    Op,
    PluginRequest,
    PluginResponse,
)

__all__ = ["DecisionEngine", "EngineResult", "RejectLimiter"]


class RejectLimiter:
    """拒绝审计记录的限速器（`reject_log_burst` / `reject_log_window`）。

    语义：同一用户在 `window` 秒内最多产生 `burst` 条 deny 审计记录。超出部分
    **仍然拒绝请求**（安全语义不变），只是不再逐条刷日志——否则一个每秒重试
    的客户端能把审计文件刷爆，真正有用的记录被淹没（§11.3）。

    被抑制的条数会累计，在下一条被记录的拒绝上以 `suppressed` 字段汇报：
    审计可以降采样，但"降了多少"必须看得见（降级必须可见原则）。
    """

    def __init__(self, burst: int, window: float, clock=time.monotonic) -> None:
        self.burst = max(1, burst)
        self.window = max(0.1, window)
        self._clock = clock
        self._hits: dict[str, tuple[float, int]] = {}  # user -> (窗口起点, 已记条数)
        self._suppressed: dict[str, int] = {}
        self._lock = threading.Lock()

    def check(self, user: str) -> tuple[bool, int]:
        """返回 `(本条是否应记录, 需附加汇报的被抑制条数)`。"""
        now = self._clock()
        with self._lock:
            start, count = self._hits.get(user, (now, 0))
            if now - start >= self.window:
                start, count = now, 0  # 新窗口：记数从头来（抑制存量留待下条汇报）
            if count >= self.burst:
                self._suppressed[user] = self._suppressed.get(user, 0) + 1
                self._hits[user] = (start, count)
                return False, 0
            self._hits[user] = (start, count + 1)
            return True, self._suppressed.pop(user, 0)


@dataclass(frozen=True)
class EngineResult:
    response: PluginResponse
    record: AuditRecord


class DecisionEngine:
    """把 `PluginRequest` 变成 `PluginResponse`，并留下审计记录。"""

    def __init__(
        self,
        policy: PluginPolicy,
        audit: AuditLog | None = None,
        quota: QuotaChecker | None = None,
    ) -> None:
        self.policy = policy
        self.audit = audit
        # 只有在真的配了 max_proxies 时才需要外部计数，否则纯本地判断即可
        self.quota = quota or QuotaChecker(
            admin_url=policy.admin_url,
            admin_user=policy.admin_user,
            admin_password=policy.admin_password,
        )
        self._reject_limiter = RejectLimiter(policy.reject_log_burst, policy.reject_log_window)

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

        if request.op is Op.CLOSE_PROXY:
            # 关闭代理要递减本地计数，否则"建了又删"会白占配额
            closed = NewProxyContent.parse(request.content)
            self.quota.note_closed(closed.user.user)
            return (
                PluginResponse.pass_through(),
                AuditRecord(
                    user=closed.user.user,
                    decision="allow",
                    reason="pass-through",
                    proxy_name=closed.proxy_name,
                    **common,
                ),
            )

        # Ping / NewWorkConn / NewUserConn：不涉及权限边界。
        # 必须显式 unchange=True，否则 frps 会用零值覆盖内容（§11.1）。
        return (
            PluginResponse.pass_through(),
            AuditRecord(user="", decision="allow", reason="pass-through", **common),
        )

    def _login(self, request: PluginRequest, common: dict) -> tuple[PluginResponse, AuditRecord]:
        # Login 时 content.user 是**字符串**（msg.Login.User）
        content = LoginContent.parse(request.content)
        client_id = content.metas.get("client_id", "")
        decision = decide_login(self.policy, user=content.user, client_id=client_id)
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

    def _new_proxy(self, request: PluginRequest, common: dict) -> tuple[PluginResponse, AuditRecord]:
        # NewProxy 时 content.user 是**对象**（UserInfo）——与 Login 不同型
        content = NewProxyContent.parse(request.content)
        owner = self.policy.user(content.user.user)
        quota = None
        if owner is not None and owner.max_proxies:
            # reserve=True：检查与占名额在同一个临界区里完成，避免并发突破上限
            quota = self.quota.check(content.user.user, owner.max_proxies, reserve=True)
        decision = decide_new_proxy(
            self.policy,
            user=content.user.user,
            proxy_name=content.proxy_name,
            proxy_type=content.proxy_type,
            remote_port=content.remote_port,
            quota=quota,
        )
        if not decision.allowed and quota is not None and quota.allowed:
            # 预占了名额但最终因端口/名称被拒 → 把名额还回去，别让它漏掉
            self.quota.note_closed(content.user.user)
        record = AuditRecord(
            user=decision.user or content.user.user,
            decision="allow" if decision.allowed else "deny",
            reason=decision.reason,
            proxy_name=content.proxy_name,
            proxy_type=content.proxy_type,
            remote_port=content.remote_port,
            quota_source=quota.source if quota is not None else "",
            **common,
        )
        if decision.allowed:
            return PluginResponse.pass_through(), record
        return PluginResponse.reject_op(decision.reason), record

    # --- 审计 ----------------------------------------------------------

    def _write_audit(self, record: AuditRecord, elapsed_ms: float) -> None:
        if self.audit is None:
            return
        # 拒绝风暴限速：请求仍被拒绝，只是不再逐条刷审计；被抑制的条数累计到
        # 下一条记录上（`suppressed` 字段），降采样必须可见。
        if record.decision == "deny":
            keep, suppressed = self._reject_limiter.check(record.user)
            if not keep:
                return
            if suppressed:
                record = replace(record, suppressed=suppressed)
        # AuditRecord 是 frozen 的，用 dataclasses.replace 补上耗时
        self.audit.record(replace(record, elapsed_ms=elapsed_ms))
