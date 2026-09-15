"""配额检查（`max_proxies`）。

**为什么配额只能在 `NewProxy` 上做**——这是实现期核对源码后的结论：

| op | 触发频率 | 能否用来做配额 |
|----|---------|--------------|
| `Login` | 每个客户端一次 | 太早：那时还不知道要建几个代理 |
| `NewProxy` | **低频**（每个代理一次） | ✅ 唯一合适的位置 |
| `NewUserConn` | **每一次用户 TCP 连接**（`server/proxy/proxy.go:273-283`） | ❌ 见下 |

`NewUserConn` 看似能做"并发连接上限"，但那会踩两条设计红线：① 它落在**每个
用户连接的关键路径**上，加一次 HTTP 往返直接抬高所有连接的延迟，违反 §11.2
的"handler 内绝不做慢速外部调用"；② 它的错误只以 **info 级**记录
（`manager.go:167-170`），被拒的连接不会有任何显式痕迹，运维根本发现不了；
③ `NewUserConnContent` 里没有连接 id，插件无法知道连接何时关闭，"当前并发数"
只能靠估算。

**所以本工具不实现"并发连接上限"**，只实现可被权威计数的 `max_proxies`。
需要真并发限制时应当在代理类型层面解决（frp 客户端侧的连接池与限流），而不是
在插件回调里做——这是能力边界，不是偷懒。

`max_proxies` 的计数用 dashboard 的 `data.total`（权威），只在 NewProxy 时查
一次，并且带 TTL 缓存以应对"批量建代理"的场景。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

__all__ = ["QuotaChecker", "QuotaResult"]


@dataclass(frozen=True)
class QuotaResult:
    allowed: bool
    current: int = 0
    limit: int = 0
    reason: str = ""
    #: 计数来源：`dashboard` 为权威；`local` 为退化（插件进程内观测）。
    source: str = ""


class QuotaChecker:
    """按用户查询当前代理数，并判断是否还能再建。

    **两种计数模式，且必须让人看出用的是哪种**：

    | 模式 | 触发条件 | 准确性 |
    |------|---------|-------|
    | `dashboard` | 策略里配了 `admin_url` | 权威（来自 frps 自己的统计） |
    | `local` | 未配 `admin_url` | **仅本进程观测**：重启归零、多实例各算各的 |

    退化模式仍要工作（不能因为查不到计数就让所有人建不了代理），但审计记录里会
    带上 `source=local`，`plugin check` 也会明确提示。
    """

    def __init__(
        self,
        *,
        admin_url: str = "",
        admin_user: str = "",
        admin_password: str = "",
        cache_ttl: float = 2.0,
        timeout: float = 2.0,
        clock=time.monotonic,
    ) -> None:
        self.admin_url = admin_url
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.cache_ttl = max(0.0, cache_ttl)
        self.timeout = timeout
        self._clock = clock
        self._local: dict[str, int] = {}
        self._cache: dict[str, tuple[float, int, str]] = {}
        self._lock = threading.Lock()

    # --- 查询 ----------------------------------------------------------

    def current(self, user: str) -> tuple[int, str]:
        """返回 `(当前代理数, 来源)`。"""
        if self.admin_url:
            cached = self._from_cache(user)
            if cached is not None:
                return cached
            try:
                count = self._query_dashboard(user)
            except Exception:  # noqa: BLE001 - 计数失败不能变成"建不了代理"
                # 退化为本地计数，并如实标注来源。**不抛异常**：
                # 配额是治理手段，dashboard 临时不可用不该让所有人无法建代理。
                return self._local_count(user), "local"
            with self._lock:
                self._cache[user] = (self._clock(), count, "dashboard")
            return count, "dashboard"
        return self._local_count(user), "local"

    def _query_dashboard(self, user: str) -> int:
        # 延迟导入：不配 admin_url 的用户不该为 httpx 付出任何代价
        from ..core.admin import AdminClient

        with AdminClient(
            self.admin_url, self.admin_user, self.admin_password, timeout=self.timeout
        ) as client:
            return client.proxy_count_for_user(user)

    def _from_cache(self, user: str) -> tuple[int, str] | None:
        with self._lock:
            entry = self._cache.get(user)
        if entry is None:
            return None
        at, count, source = entry
        if self._clock() - at <= self.cache_ttl:
            return count, source
        return None

    def _local_count(self, user: str) -> int:
        with self._lock:
            return self._local.get(user, 0)

    # --- 记录 ----------------------------------------------------------

    def note_created(self, user: str) -> None:
        """本地计数 +1（退化模式用；权威模式下也会记，作为兜底）。"""
        with self._lock:
            self._local[user] = self._local.get(user, 0) + 1
            # 本地计数变了，缓存立刻失效，避免"刚建完还读到旧数"
            self._cache.pop(user, None)

    def note_closed(self, user: str) -> None:
        with self._lock:
            if self._local.get(user, 0) > 0:
                self._local[user] -= 1
            self._cache.pop(user, None)

    # --- 判定 ----------------------------------------------------------

    def check(self, user: str, limit: int) -> QuotaResult:
        """还能再建吗？`limit <= 0` 表示不限。"""
        if limit <= 0:
            return QuotaResult(allowed=True, limit=0, source="unlimited")

        count, source = self.current(user)
        if count >= limit:
            return QuotaResult(
                allowed=False,
                current=count,
                limit=limit,
                source=source,
                reason=(
                    f"用户 {user!r} 的代理数已达上限 {limit}"
                    f"（当前 {count}，计数来源：{'dashboard' if source == 'dashboard' else '插件本地'}）"
                ),
            )
        return QuotaResult(allowed=True, current=count, limit=limit, source=source)
