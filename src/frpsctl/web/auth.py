"""Web 管理台的认证与会话（设计文档 §18.3）。

**安全基线（比 frp 自带 dashboard 更严）**——frp 的教训（user/password 双空 =
完全不鉴权）是本项目全部安全决策的来源，而 Web 管理台能改配置、停服务，
是比 dashboard 高得多的价值目标：

| 风险 | 对策 |
|------|------|
| 暴露到非回环 | 默认只绑 `127.0.0.1`；绑非回环必须显式开关（CLI 层强制） |
| 无口令即默认放行 | **不允许空口令**：显式提供或自动生成并打印一次 |
| 会话劫持 | 随机 256 位 token；Cookie 带 `HttpOnly` + `SameSite=Strict`；TTL 过期 |
| CSRF | 一切变更请求必须带 `X-CSRF-Token`（登录时下发，仅存内存） |
| 口令爆破 | 来源级失败限速：窗口内 N 次失败后冷却，表现与"口令错误"完全一致 |
| 时序侧信道 | `hmac.compare_digest` 常量时间比较 |

会话只存在服务端内存里（重启即失效）——管理台是短生命周期工具，
持久化会话表只会多一个需要保护的机密文件。
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass

__all__ = [
    "AUDIT_FAIL_WINDOW",
    "AuthManager",
    "LoginAuditLimiter",
    "Session",
    "SessionInfo",
    "generate_password",
    "SESSION_COOKIE",
]

#: 会话 Cookie 名（解析与下发都用这一个常量）。
SESSION_COOKIE = "frpsctl_session"

#: 失败登录的**审计**限速窗口（秒）。
AUDIT_FAIL_WINDOW = 60.0

#: 审计限速的来源表上限（输入驱动的表必须有界，同 `MAX_TRACKED_SOURCES`）。
AUDIT_MAX_SOURCES = 1024


class LoginAuditLimiter:
    """失败登录的**审计**限速：每来源每 60 秒最多写一条失败记录。

    与认证的失败限速（`AuthManager`，5 次/60 秒）是两件事：那个决定"还能不能
    试"，这个决定"失败要不要逐条写审计"。爆破场景下逐条写会把审计文件刷爆，
    而第一条失败已经足以定位来源；限速窗口与认证侧同量级。

    v0.3.1：从 `web/server.py` 的**模块级单例**改为 `WebContext` 持有的实例
    ——模块级状态在多实例（测试里多个 WebServer）之间互相串味：前一个进程/
    用例的失败会压制后一个的审计记录，而它本来的语义就是"每个管理台进程一份"。
    """

    def __init__(
        self,
        window: float = AUDIT_FAIL_WINDOW,
        *,
        max_sources: int = AUDIT_MAX_SOURCES,
        clock=time.monotonic,
    ) -> None:
        self.window = window
        self.max_sources = max_sources
        self._clock = clock
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def allow(self, source: str) -> bool:
        """该来源此刻是否允许写审计（同来源在窗口内只放行一次）。"""
        now = self._clock()
        with self._lock:
            # 哨兵用 `None` 而不是 0.0：单调时钟的合法值可以是 0（可注入假时钟），
            # 用 0.0 当"从未见过"会让首次调用在 `now=0` 时被误判为冷却中
            # （v0.3.1，测试用假时钟从 0 起步时暴露）。
            last = self._last.get(source)
            if last is not None and now - last < self.window:
                return False
            self._last[source] = now
            if len(self._last) > self.max_sources:
                oldest = min(self._last, key=lambda key: self._last[key])
                self._last.pop(oldest, None)
            return True

#: 登录失败限速：窗口（秒）与窗口内允许的失败次数。
FAILURE_WINDOW = 60.0
MAX_FAILURES = 5

#: 失败来源表上限。窗口清理只在 login 时发生——持续、分散的失败请求会在
#: 窗口内堆积任意多的来源条目（慢速内存放大）。超过上限时驱逐最早失败的来源；
#: 攻击者要借此"挤掉"某个来源的限速记录，必须先把窗口内的失败来源填满
#: （上限 1024 个），成本远高于直接停手。
MAX_TRACKED_SOURCES = 1024

#: 默认会话有效期（8 小时）。
DEFAULT_SESSION_TTL = 8 * 3600.0


def _secure_equal(left: str, right: str) -> bool:
    """常量时间字符串比较。

    ⚠️ 必须先 `encode`：`hmac.compare_digest` 对含非 ASCII 字符的 **str** 会
    直接抛 `TypeError`（v0.3.0 review 实测：中文口令会让登录线程断开、管理台
    完全不可用）。用 `surrogatepass` 而不是默认策略——HTTP JSON 里可以构造
    lone surrogate（编码点 U+D800 单独出现），默认 encode 会抛
    `UnicodeEncodeError`，同样让线程断开（v0.3.0 最终 review N2）。畸形
    输入与任何真实口令都不等。
    """
    return hmac.compare_digest(
        left.encode("utf-8", "surrogatepass"),
        right.encode("utf-8", "surrogatepass"),
    )

#: 会话表上限。与失败来源表（`MAX_TRACKED_SOURCES`）同一条护栏纪律：持有口令
#: 的调用方可以不断登录开新会话，而惰性清理只在过期或再次登录时发生——没有
#: 上限意味着"一直登录"能把内存持续推高。超过上限时驱逐**最早到期**的会话
#: （等价于先开先出，因为 TTL 相同）；正在使用的会话在正常交互下远少于 32 个。
MAX_SESSIONS = 32


def generate_password() -> str:
    """生成管理台口令（24 字符）。

    实现在 `core/systemd.generate_web_password`：`web serve` 的启动口令、
    `web password set` 与 `web service install` 的口令文件必须由**同一个**
    生成器产出——此前三处各写一份 `token_urlsafe(18)`，是典型的"规律相同、
    实现三份"漂移风险。
    """
    from ..core.systemd import generate_web_password

    return generate_web_password()


@dataclass(frozen=True)
class Session:
    """一次登录的会话。`csrf` 通过登录响应下发给前端（只存内存变量）。

    v0.3.5 起记录 `source`（来源 IP / XFF 最后一跳）与 `created_at`——会话管理
    视图要回答"谁在哪儿登录了"，而这两个字段此前在 `login()` 里用完就丢。
    """

    token: str
    csrf: str
    expires_at: float
    source: str = ""
    created_at: float = 0.0

    def expired(self, now: float) -> bool:
        return now >= self.expires_at


@dataclass(frozen=True)
class SessionInfo:
    """一个活跃会话的**脱敏**视图（token 本身绝不出现在这里）。

    `fingerprint` 与 `core.web_audit.session_fingerprint` 是同一算法：
    审计记录里的 `session_id` 就是这个值，因此"哪个动作来自哪个会话"能对上。
    """

    fingerprint: str
    source: str
    created_at: float
    expires_in: float


def _fingerprint(token: str) -> str:
    """会话指纹（复用审计侧实现，保证两处口径一致）。"""
    from ..core.web_audit import session_fingerprint

    return session_fingerprint(token)


class AuthManager:
    """口令校验、会话与 CSRF 的唯一实现。线程安全（handler 每请求一线程）。"""

    def __init__(
        self,
        password: str,
        *,
        session_ttl: float = DEFAULT_SESSION_TTL,
        clock=time.monotonic,
        wall_clock=time.time,
    ) -> None:
        if not password:
            raise ValueError("Web 管理台不允许空口令")
        self._password = password
        self.session_ttl = session_ttl
        self._clock = clock
        #: 会话创建时间用**墙钟**（TTL 仍用单调时钟）：`created_at` 要能作为
        #: Unix 时间戳展示，而单调时钟的值跨进程/跨机器不可比（v0.3.5 review 修正）。
        self._wall_clock = wall_clock
        self._sessions: dict[str, Session] = {}
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # --- 登录 / 登出 ----------------------------------------------------

    def login(self, password: str, *, source: str) -> Session | None:
        """校验口令并开一个会话；失败（含冷却中）返回 None。

        冷却中的来源与"口令错误"**表现完全一致**——不给爆破者"这个来源被
        限速了"的信号（否则可以换来源继续猜）。
        """
        now = self._clock()
        with self._lock:
            self._prune(now)
            fails = self._failures.get(source, [])
            if len(fails) >= MAX_FAILURES:
                return None
            if not _secure_equal(password, self._password):
                fails.append(now)
                self._failures[source] = fails
                # 追加后立刻收敛到上限：任意时刻来源表都保持有界（若只在下次
                # login 的 prune 里收敛，表会长到 MAX+1 且"有界"语义变得模糊）。
                self._cap_sources()
                return None
            self._failures.pop(source, None)
            session = Session(
                token=secrets.token_urlsafe(32),
                csrf=secrets.token_urlsafe(32),
                expires_at=now + self.session_ttl,
                source=source,
                created_at=self._wall_clock(),
            )
            self._sessions[session.token] = session
            self._cap_sessions()
            return session

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def snapshot(self) -> list[SessionInfo]:
        """活跃会话的脱敏快照（按创建时间升序；token 不外泄）。"""
        with self._lock:
            now = self._clock()
            self._prune(now)
            items = [
                SessionInfo(
                    fingerprint=_fingerprint(token),
                    source=session.source,
                    created_at=session.created_at,
                    expires_in=max(0.0, session.expires_at - now),
                )
                for token, session in self._sessions.items()
            ]
        return sorted(items, key=lambda item: item.created_at)

    def revoke_all(self, *, keep_fingerprint: str | None = None) -> int:
        """登出除 `keep_fingerprint` 外的全部会话，返回被登出的数量。

        参数刻意是**指纹而不是 token**：调用方（Web 的动作 handler）只持有
        指纹（`RequestInfo.session_id`），token 永远只在认证层内部流动——
        少一条"token 出现在别处"的路径，就少一个泄露面。
        """
        with self._lock:
            targets = [
                token
                for token, session in self._sessions.items()
                if keep_fingerprint is None or _fingerprint(token) != keep_fingerprint
            ]
            for token in targets:
                self._sessions.pop(token, None)
            return len(targets)

    # --- 校验 -----------------------------------------------------------

    def check_password(self, password: str) -> bool:
        """常量时间口令比较（`/metrics` 的 Basic auth 用）。

        只比较、不建会话（不在会话表里堆积条目）。**失败计数由调用方负责**
        （`is_throttled` / `note_failure`）——`/metrics` 与登录共用同一张
        失败来源表，否则同一口令会有第二条无限次猜测通道（v0.3.0 review）。
        """
        return _secure_equal(password, self._password)

    def is_throttled(self, source: str) -> bool:
        """来源是否处于失败冷却中（与登录共用同一张失败表）。"""
        now = self._clock()
        with self._lock:
            self._prune(now)
            return len(self._failures.get(source, [])) >= MAX_FAILURES

    def note_failure(self, source: str) -> None:
        """记一次失败（`/metrics` 的 Basic auth 失败走这里，纳入同一限速）。

        先 `_prune` 再做窗口外的清理：否则单来源在窗口内被持续轰炸时，它的
        list 会无界增长（v0.3.0 最终 review N5）。
        """
        now = self._clock()
        with self._lock:
            self._prune(now)
            fails = self._failures.get(source, [])
            fails = [stamp for stamp in fails if now - stamp < FAILURE_WINDOW]
            fails.append(now)
            self._failures[source] = fails
            self._cap_sources()

    def check_session(self, token: str | None) -> Session | None:
        """会话是否有效；过期即删除（惰性清理）。"""
        if not token:
            return None
        now = self._clock()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if session.expired(now):
                self._sessions.pop(token, None)
                return None
            return session

    @staticmethod
    def check_csrf(session: Session, csrf: str | None) -> bool:
        """变更请求的 CSRF 校验（常量时间比较）。"""
        if not csrf:
            return False
        return hmac.compare_digest(csrf, session.csrf)

    # --- 内部 -----------------------------------------------------------

    def _prune(self, now: float) -> None:
        """清理过期会话与冷却窗口外的失败记录（持锁调用）。"""
        for token in [token for token, session in self._sessions.items() if session.expired(now)]:
            self._sessions.pop(token, None)
        for source in list(self._failures):
            kept = [stamp for stamp in self._failures[source] if now - stamp < FAILURE_WINDOW]
            if kept:
                self._failures[source] = kept
            else:
                self._failures.pop(source, None)
        # 来源表上限（见 MAX_TRACKED_SOURCES 的说明）：窗口内的来源数也必须
        # 有界，否则"每次失败一个新来源"就能持续放大内存。
        self._cap_sources()
        self._cap_sessions()

    def _cap_sources(self) -> None:
        """把来源表收敛到上限，驱逐最早失败的来源（持锁调用）。"""
        while len(self._failures) > MAX_TRACKED_SOURCES:
            oldest = min(self._failures, key=lambda source: self._failures[source][0])
            self._failures.pop(oldest, None)

    def _cap_sessions(self) -> None:
        """把会话表收敛到上限，驱逐最早到期的会话（持锁调用）。

        只可能发生在"持有正确口令的调用方反复登录"这一种情形（失败的登录不会
        创建会话），威胁模型低于来源表；但它与来源表是同一类"输入驱动的表必须
        有界"问题，护栏成本又只有几行，没有理由不做。
        """
        while len(self._sessions) > MAX_SESSIONS:
            oldest = min(self._sessions, key=lambda token: self._sessions[token].expires_at)
            self._sessions.pop(oldest, None)
