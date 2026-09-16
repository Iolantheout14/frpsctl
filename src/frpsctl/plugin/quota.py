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
        #: 全局锁只保护共享字典（`_local` / `_cache` / `_user_locks`），
        #: **绝不包住网络调用**。
        self._lock = threading.Lock()
        #: 按用户拆分的"读-判-占"锁。为什么不用一把全局锁：frp 侧对插件的
        #: HTTP 客户端**没有超时**（§11.2），dashboard 查询一旦变慢，全局锁会
        #: 把所有用户的登录链路一起挂住。拆到用户级后，只有同一用户的并发
        #: NewProxy 互相串行（这正是正确性需要的），不同用户完全并行。
        self._user_locks: dict[str, threading.Lock] = {}

    # --- 查询 ----------------------------------------------------------

    def current(self, user: str) -> tuple[int, str]:
        """返回 `(当前代理数, 来源)`。

        ⚠️ dashboard 模式下也要**叠加本地记账**：dashboard 的统计有秒级延迟，
        刚建完的代理还没反映过去；只读 dashboard 会让"连续建两个"都拿到旧计数。
        取两者较大值是保守做法——宁可早一点拦住，也不要让配额形同虚设。
        """
        local = self._local_count(user)
        if self.admin_url:
            cached = self._from_cache(user)
            if cached is not None:
                return max(cached[0], local), cached[1]
            try:
                count = self._query_dashboard(user)
            except Exception:  # noqa: BLE001 - 计数失败不能变成"建不了代理"
                # 退化为本地计数，并如实标注来源。**不抛异常**：
                # 配额是治理手段，dashboard 临时不可用不该让所有人无法建代理。
                return local, "local"
            with self._lock:
                self._cache[user] = (self._clock(), count, "dashboard")
            return max(count, local), "dashboard"
        return local, "local"

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

    def check(self, user: str, limit: int, *, reserve: bool = False) -> QuotaResult:
        """还能再建吗？`limit <= 0` 表示不限。

        `reserve=True` 时"检查 + 预占"在**同一用户的锁内**完成。这不是优化，
        是正确性：原先"读计数 → 决策 → 事后 +1"是典型的 check-then-act，
        两个并发的 NewProxy 会同时读到旧计数并双双通过（实测 limit=1 时
        两个都拿到 0）。frp 的 NewProxy 回调确实可能并发（多客户端同时上线）。

        **网络调用在用户锁内、全局锁外**：dashboard 查询只在缓存未命中时发生，
        只影响当前用户，与全局锁无关。
        """
        if limit <= 0:
            return QuotaResult(allowed=True, limit=0, source="unlimited")

        if not reserve:
            count, source = self.current(user)
            return self._verdict(user, count, source, limit)

        with self._lock_for(user):
            # 快路径：缓存命中（或纯本地模式）→ 不做任何网络调用。
            with self._lock:
                local = self._local.get(user, 0)
                cached = self._cache.get(user)
            if cached is not None and self._clock() - cached[0] <= self.cache_ttl:
                return self._settle(user, max(cached[1], local), cached[2], limit)
            if not self.admin_url:
                return self._settle(user, local, "local", limit)

            # 慢路径：缓存未命中 → 查一次 dashboard。可能耗时（网络），
            # 但只影响这个用户；查询期间的本地新增计数在 `_settle` 前重新读取。
            fresh: int | None
            try:
                fresh = self._query_dashboard(user)
            except Exception:  # noqa: BLE001 - 查不到不能变成"建不了代理"
                fresh = None
            with self._lock:
                current_local = self._local.get(user, 0)
                if fresh is not None:
                    self._cache[user] = (self._clock(), fresh, "dashboard")
                    count, source = max(fresh, current_local), "dashboard"
                else:
                    count, source = current_local, "local"
            # `_settle` 自己会取全局锁，**必须**在离开上面的 with 之后调用
            # （threading.Lock 不可重入，锁内取锁会自我死锁）。
            return self._settle(user, count, source, limit)

    def _lock_for(self, user: str) -> threading.Lock:
        with self._lock:
            lock = self._user_locks.get(user)
            if lock is None:
                lock = threading.Lock()
                self._user_locks[user] = lock
            return lock

    def _settle(self, user: str, count: int, source: str, limit: int) -> QuotaResult:
        """判定并按需预占一个名额（必须在用户锁内调用）。

        预占用**增量**写法：`note_created` / `note_closed` 可能在两次持锁之间
        修改过 `_local`（CloseProxy 回调不经过用户锁），直接赋值会覆盖它们。
        """
        verdict = self._verdict(user, count, source, limit)
        if verdict.allowed:
            with self._lock:
                self._local[user] = self._local.get(user, 0) + 1
                self._cache.pop(user, None)
        return verdict

    def _verdict(self, user: str, count: int, source: str, limit: int) -> QuotaResult:
        if count >= limit:
            origin = "dashboard" if source == "dashboard" else "插件本地"
            return QuotaResult(
                allowed=False,
                current=count,
                limit=limit,
                source=source,
                reason=(f"用户 {user!r} 的代理数已达上限 {limit}（当前 {count}，计数来源：{origin}）"),
            )
        return QuotaResult(allowed=True, current=count, limit=limit, source=source)
