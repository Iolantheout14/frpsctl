"""策略模型与鉴权裁决（设计文档 §11.3 的扩展实现）。

**信任边界（重要，且设计文档未展开）**：frp 的插件协议**没有任何认证**——
`HTTPPluginOptions` 只有 `name/addr/path/ops/tlsVerify`（`pkg/config/v1/common.go:125-131`），
frps 发请求时只带 `X-Frp-Reqid` 与 `Content-Type`（`http.go:104-105`）。
**抓到这个端口的任何本地进程都能伪造 Login/NewProxy 事件。**

因此：

1. 插件**必须**绑回环（§11.2）。`PluginPolicy.validate()` 把这条做成硬约束，
   而不是写在文档里靠自觉。
2. 客户端自报的 `user` 在没有 `auth.tokenSource` exec 的前提下是**不可信的**。
   `require_client_id` 默认打开：客户端必须用 `metadatas.client_id` 声明身份，
   与 `user` 交叉比对，不一致即拒绝。这挡不住恶意本地进程，但能挡住"配置抄错
   导致互相冒用身份"这一类真实事故。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..core.auditlog import DEFAULT_AUDIT_FILE
from ..core.healthcheck import is_loopback
from ..errors import ConfigError, UsageError
from .quota import QuotaResult

__all__ = [
    "PortRange",
    "UserPolicy",
    "AuditSettings",
    "PluginPolicy",
    "Decision",
    "decide_login",
    "decide_new_proxy",
]

#: 需要限速/拒绝时的统一出口——所有拒绝理由都必须**可据以行动**。
_REASON_PREFIX = "frpsctl-plugin"


@dataclass(frozen=True)
class PortRange:
    """一个端口或一段端口。与 frps 的 `allowPorts` 语义一致。"""

    start: int
    end: int

    @property
    def is_single(self) -> bool:
        return self.start == self.end

    def contains(self, port: int) -> bool:
        return self.start <= port <= self.end

    def render(self) -> str:
        return str(self.start) if self.is_single else f"{self.start}-{self.end}"

    @classmethod
    def parse(cls, spec: str) -> PortRange:
        """解析 `"6000"` / `"6000-6100"`。"""
        text = spec.strip()
        if "-" in text:
            head, _, tail = text.partition("-")
            try:
                start, end = int(head), int(tail)
            except ValueError:
                raise ConfigError(f"无法解析端口段：{spec!r}") from None
        else:
            try:
                start = end = int(text)
            except ValueError:
                raise ConfigError(f"无法解析端口：{spec!r}") from None
        if not (1 <= start <= end <= 65535):
            raise ConfigError(f"端口段非法：{spec!r}（需 1 ≤ start ≤ end ≤ 65535）")
        return cls(start=start, end=end)


@dataclass(frozen=True)
class UserPolicy:
    """单个用户的权限。"""

    name: str
    #: 允许申请的端口 / 端口段。空集 = 不允许任何端口型代理。
    allowed_ports: tuple[PortRange, ...] = ()
    #: 是否允许随机端口（`remote_port = 0`，由 frps 分配）。
    #: **默认 False**：白名单的意义就是"只能拿到我批准的端口"，
    #: 放行随机端口等于白名单形同虚设。
    allow_random_port: bool = False
    #: 允许创建的代理名（支持 `*` 通配）。空集 = 不限制名称。
    #: 它不提升安全性，价值在于**防止用户互相抢占代理名**。
    allowed_proxy_names: tuple[str, ...] = ()
    #: 允许的代理类型。空集 = 不限类型。
    allowed_proxy_types: tuple[str, ...] = ()
    #: 该用户可同时存在的代理数上限。0 = 不限。
    #: 计数来自 dashboard 的 `data.total`（权威），见 `QuotaChecker`。
    max_proxies: int = 0
    #: 备注，仅用于审计可读性。
    note: str = ""

    def port_allowed(self, port: int) -> bool:
        return any(item.contains(port) for item in self.allowed_ports)

    def name_allowed(self, proxy_name: str) -> bool:
        if not self.allowed_proxy_names:
            return True
        return any(_glob_match(pattern, proxy_name) for pattern in self.allowed_proxy_names)

    def type_allowed(self, proxy_type: str) -> bool:
        if not self.allowed_proxy_types:
            return True
        return proxy_type in self.allowed_proxy_types

    def render_ports(self) -> str:
        if not self.allowed_ports:
            return "(无)"
        return ",".join(item.render() for item in self.allowed_ports)

    @classmethod
    def parse(cls, name: str, raw: dict[str, Any]) -> UserPolicy:
        if not isinstance(raw, dict):
            raise ConfigError(f"用户 {name!r} 的配置必须是对象")
        ports: list[PortRange] = []
        for item in _strict_list(raw, "allowed_ports", context=f"用户 {name!r}"):
            ports.append(PortRange.parse(str(item)))
        return cls(
            name=name,
            allowed_ports=tuple(ports),
            allow_random_port=_strict_bool(raw, "allow_random_port", False),
            allowed_proxy_names=tuple(
                str(x) for x in _strict_list(raw, "allowed_proxy_names", context=f"用户 {name!r}")
            ),
            allowed_proxy_types=tuple(
                str(x) for x in _strict_list(raw, "allowed_proxy_types", context=f"用户 {name!r}")
            ),
            max_proxies=_strict_int(raw, "max_proxies", 0, minimum=0),
            note=str(raw.get("note") or ""),
        )


@dataclass(frozen=True)
class AuditSettings:
    """审计设置。

    审计是**唯一能回答"谁在什么时候申请了什么端口"的地方**——frps 的日志没有
    这个视角。但它不能拖慢登录链路（§11.2：handler 内绝不做慢速外部调用），
    因此写入策略是"缓冲 + 定期刷盘"（见 audit.py）。

    `path` 的默认值是 `./plugin-audit.jsonl` 而不是 None：审计**默认开启**却有
    一半概率没地方落盘，会让"我开了审计啊"变成一句空话——记录只进了内存，
    进程一退就没了。默认给个相对路径，让默认行为与默认意图一致。

    相对路径的**解析基准是策略文件所在目录**（`core/auditlog.resolve_audit_path`）
    ——此前写入侧跟随进程 CWD，手工前台运行与 systemd 托管会写到两个地方。
    常量同样来自 `core/auditlog`：写入侧默认值与读取侧查找目标必须同一个源。
    """

    path: Path | None = Path(DEFAULT_AUDIT_FILE)
    enabled: bool = True
    #: 缓冲区达到多少条就刷盘。
    flush_every: int = 32
    #: 距上次刷盘多少秒后强制刷盘（即使没攒够）。
    flush_interval: float = 2.0
    #: 审计文件轮转阈值（MB）；0 = 不按大小轮转（不推荐：文件会无限增长）。
    max_mb: float = 10.0
    #: 审计文件轮转年龄（天）；0 = 不按天轮转。与 frp 日志的 maxDays 同语义。
    max_days: float = 7.0

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> AuditSettings:
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            # `"audit": "false"` 这类写法此前会走到 `"false".get(...)` → 裸
            # AttributeError → "未分类错误(1)"。
            raise ConfigError(f"audit 必须是对象，实际是 {type(raw).__name__}")
        # 显式 {"path": null} 才表示"仅内存"；键缺失则用默认路径
        path = raw.get("path", AuditSettings.path)
        return cls(
            path=Path(str(path)).expanduser() if path else None,
            enabled=_strict_bool(raw, "enabled", True),
            flush_every=_strict_int(raw, "flush_every", 32, minimum=1),
            flush_interval=_strict_float(raw, "flush_interval", 2.0, minimum=0.1),
            max_mb=_strict_float(raw, "max_mb", 10.0, minimum=0.0),
            max_days=_strict_float(raw, "max_days", 7.0, minimum=0.0),
        )


@dataclass(frozen=True)
class PluginPolicy:
    """插件策略全集。"""

    users: dict[str, UserPolicy] = field(default_factory=dict)
    #: 处理未在 `users` 中出现的客户端。
    #: **默认 false（fail-closed）**：允许"放行未知用户"会让整份策略失去意义，
    #: 因此它是显式开关，且开启时 doctor/启动日志会给出警告。
    allow_unknown_user: bool = False
    require_client_id: bool = True
    audit: AuditSettings = field(default_factory=AuditSettings)
    #: dashboard 的 v2 API 地址，用于读取**权威的**代理计数（`max_proxies`）。
    #: 为空则配额退化为"按本进程观测到的 NewProxy 事件计数"——那不准（重启即归零、
    #: 多插件实例各算各的），因此配了 `max_proxies` 就应当填它。
    admin_url: str = ""
    admin_user: str = ""
    admin_password: str = ""
    #: 限速：同一用户在 N 秒内最多触发几次拒绝后开始快速失败（防止日志被刷爆）。
    reject_log_burst: int = 20
    reject_log_window: float = 10.0

    # --- 校验 ----------------------------------------------------------

    def validate(self, *, bind: str) -> None:
        """启动前硬校验（§11.2、§3.7）。

        `bind` 必须回环：插件是全部客户端登录的单点，而它**没有任何认证**，
        暴露到非回环等于把"谁能登录 frps"的决定权交给网络上任何人。
        """
        host = _host_of(bind)
        if not is_loopback(host):
            raise UsageError(
                f"插件拒绝绑定非回环地址：{bind}",
                hint=(
                    "frp 的插件协议没有任何认证，任何能访问该端口的人都能伪造 "
                    "Login/NewProxy 事件。请绑 127.0.0.1（或在反向代理后自行加固）"
                ),
            )
        if self.allow_unknown_user and not self.users:
            raise ConfigError(
                "策略里没有任何用户，却开启了 allow_unknown_user",
                hint="那等于不做任何鉴权；要么配置用户，要么去掉这个开关",
            )
        for name, user in self.users.items():
            if not name:
                raise ConfigError("存在空用户名的策略条目")
            for item in user.allowed_ports:
                if item.start < 1 or item.end > 65535:
                    raise ConfigError(f"用户 {name!r} 的端口段越界：{item.render()}")

    # --- 查询 ----------------------------------------------------------

    def user(self, name: str) -> UserPolicy | None:
        return self.users.get(name)

    def describe(self) -> list[str]:
        """人读摘要（`plugin check` 与启动日志共用）。"""
        if not self.audit.enabled:
            audit_text = "关闭"
        elif self.audit.path is None:
            audit_text = "仅内存（未配置 path）"
        else:
            audit_text = f"写入 {self.audit.path}"

        lines = [
            f"用户数：{len(self.users)}"
            + ("（未列出的用户一律拒绝）" if not self.allow_unknown_user else "（未知用户放行 ⚠）"),
            f"客户端身份校验：{'开启' if self.require_client_id else '关闭 ⚠'}",
            f"审计：{audit_text}",
        ]
        if any(user.max_proxies for user in self.users.values()):
            source = self.admin_url or "（未配置 admin_url，配额计数不准确 ⚠）"
            lines.append(f"配额计数来源：{source}")
        for name, user in sorted(self.users.items()):
            suffix = f"  # {user.note}" if user.note else ""
            random_port = " +随机端口" if user.allow_random_port else ""
            quota = f"，最多 {user.max_proxies} 个代理" if user.max_proxies else ""
            lines.append(f"  - {name}: 端口 {user.render_ports()}{random_port}{quota}{suffix}")
        return lines

    # --- 载入 ----------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> PluginPolicy:
        """从 JSON 载入策略。

        选 JSON 而不是 TOML：这里没有注释保真的需求（策略由 frpsctl 自己管理），
        而 JSON 能被任何工具读写，运维脚本可以直接改。
        """
        try:
            raw = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            raise ConfigError(
                f"策略文件不存在：{path}",
                hint="先运行 `frpsctl plugin init --policy <path>` 生成一份模板",
            ) from None
        except json.JSONDecodeError as exc:
            raise ConfigError(f"策略文件不是合法 JSON：{path}（{exc}）") from None
        return cls.parse(raw)

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> PluginPolicy:
        if not isinstance(raw, dict):
            raise ConfigError("策略文件根节点必须是对象")
        users_raw = raw.get("users") or {}
        if not isinstance(users_raw, dict):
            raise ConfigError("users 必须是对象（用户名为键）")
        users = {str(name): UserPolicy.parse(str(name), value) for name, value in users_raw.items()}
        return cls(
            users=users,
            allow_unknown_user=_strict_bool(raw, "allow_unknown_user", False),
            require_client_id=_strict_bool(raw, "require_client_id", True),
            audit=AuditSettings.parse(raw.get("audit")),
            admin_url=str(raw.get("admin_url") or ""),
            admin_user=str(raw.get("admin_user") or ""),
            admin_password=str(raw.get("admin_password") or ""),
            reject_log_burst=_strict_int(raw, "reject_log_burst", 20, minimum=1),
            reject_log_window=_strict_float(raw, "reject_log_window", 10.0, minimum=0.1),
        )


# ---------------------------------------------------------------------------
# 裁决
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """一次裁决的结果。`reason` 会原样回到客户端，因此必须**可行动且不泄密**。"""

    allowed: bool
    reason: str = ""
    user: str = ""

    @classmethod
    def allow(cls, user: str) -> Decision:
        return cls(allowed=True, user=user)

    @classmethod
    def deny(cls, user: str, reason: str) -> Decision:
        return cls(allowed=False, user=user, reason=reason)


def decide_login(policy: PluginPolicy, *, user: str, client_id: str) -> Decision:
    """`Login` 裁决：这个用户能不能登录？

    只回答"是谁、允不允许"，不碰端口——端口在 `NewProxy` 时才知道。
    """
    if not user:
        if policy.allow_unknown_user:
            return Decision.allow("(anonymous)")
        return Decision.deny(
            "",
            f"{_REASON_PREFIX}: 客户端未声明用户名；请在 frpc 配置里设置 user",
        )

    known = policy.user(user)
    if known is None:
        if policy.allow_unknown_user:
            return Decision.allow(user)
        return Decision.deny(user, f"{_REASON_PREFIX}: 未知用户 {user!r}")

    if policy.require_client_id:
        if not client_id:
            return Decision.deny(
                user,
                f"{_REASON_PREFIX}: 用户 {user!r} 必须在 metadatas 中提供 client_id",
            )
        if client_id != user:
            return Decision.deny(
                user,
                f"{_REASON_PREFIX}: 声明的 user 与 client_id 不一致",
            )
    return Decision.allow(user)


def decide_new_proxy(
    policy: PluginPolicy,
    *,
    user: str,
    proxy_name: str,
    proxy_type: str,
    remote_port: int,
    current_ports: Iterable[int] = (),
    quota: QuotaResult | None = None,
) -> Decision:
    """`NewProxy` 裁决：这个用户能不能创建这个代理？

    四条检查，任何一条不过即拒绝：类型 → 名称 → 端口 → 随机端口。

    `remote_port` 的语义按代理类型区分：只有 tcp / udp 会带端口；http / https
    走域名，此时端口检查**不适用**（返回 0 是正常的，不该被当成"随机端口"拒绝）。
    """
    if not user:
        if not policy.allow_unknown_user:
            return Decision.deny("", f"{_REASON_PREFIX}: 未识别的客户端")
        return Decision.allow("(anonymous)")

    owner = policy.user(user)
    if owner is None:
        if policy.allow_unknown_user:
            return Decision.allow(user)
        return Decision.deny(user, f"{_REASON_PREFIX}: 未知用户 {user!r}")

    if not owner.type_allowed(proxy_type):
        allowed = "/".join(owner.allowed_proxy_types) or "(无)"
        return Decision.deny(
            user,
            f"{_REASON_PREFIX}: 用户 {user!r} 不允许创建 {proxy_type} 类型代理（允许：{allowed}）",
        )

    if not owner.name_allowed(proxy_name):
        return Decision.deny(
            user,
            f"{_REASON_PREFIX}: 用户 {user!r} 不允许使用代理名 {proxy_name!r}",
        )

    # 配额检查放在端口检查**之前**：端口白名单是"能不能"，配额是"还有没有名额"，
    # 两者都不过时先说名额，客户端更容易理解（"你建太多了"比"端口不对"更贴近实情）。
    if quota is not None and not quota.allowed:
        return Decision.deny(user, f"{_REASON_PREFIX}: {quota.reason}")

    port_based = proxy_type in ("tcp", "udp")
    if not port_based:
        # 域名型代理不涉及端口，端口白名单不适用
        return Decision.allow(user)

    if remote_port == 0:
        if owner.allow_random_port:
            return Decision.allow(user)
        return Decision.deny(
            user,
            f"{_REASON_PREFIX}: 用户 {user!r} 必须显式指定 remote_port（不允许随机端口）",
        )

    if remote_port not in range(1, 65536):
        return Decision.deny(user, f"{_REASON_PREFIX}: remote_port {remote_port} 越界")

    if owner.port_allowed(remote_port):
        return Decision.allow(user)

    in_use = set(current_ports)
    if remote_port in in_use:
        return Decision.deny(
            user,
            f"{_REASON_PREFIX}: 端口 {remote_port} 已被占用",
        )
    return Decision.deny(
        user,
        f"{_REASON_PREFIX}: 端口 {remote_port} 不在用户 {user!r} 的许可范围（允许：{owner.render_ports()}）",
    )


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _strict_bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    """严格布尔：只接受 JSON 的 `true` / `false`。

    **为什么不能 `bool(value)`**：JSON 里写 `"false"`（带引号的字符串）会被
    Python 的 `bool()` 判成 **True**——对 `allow_unknown_user` 而言，用户以为
    "关掉了放行未知用户"，实际把鉴权后门**完全打开**；`allow_random_port`
    同理（白名单形同虚设）。这是 fail-open 方向的静默错误，必须硬拒绝。
    """
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    raise ConfigError(
        f"策略字段 {key} 必须是布尔值（true / false），实际是 {type(value).__name__}：{value!r}"
    )


def _strict_int(raw: dict[str, Any], key: str, default: int, *, minimum: int | None = None) -> int:
    """严格整数。`bool` 是 `int` 的子类，必须显式排除（`true` 不是 1）。

    `minimum` 保留原有的钳制语义（负数刷盘间隔修正为最小值），类型错误则拒绝。
    """
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"策略字段 {key} 必须是整数，实际是 {type(value).__name__}：{value!r}"
        )
    if minimum is not None and value < minimum:
        return minimum
    return value


def _strict_float(
    raw: dict[str, Any], key: str, default: float, *, minimum: float | None = None
) -> float:
    """严格数值（int 或 float 均可，bool 除外）。"""
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"策略字段 {key} 必须是数字，实际是 {type(value).__name__}：{value!r}"
        )
    if minimum is not None and value < minimum:
        return float(minimum)
    return float(value)


def _strict_list(raw: dict[str, Any], key: str, *, context: str = "") -> list[Any]:
    """严格数组。字符串会被逐字符迭代——那种"看似能跑"的行为必须变成明确报错。"""
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        prefix = f"{context} 的 " if context else ""
        raise ConfigError(f"{prefix}{key} 必须是数组，实际是 {type(value).__name__}：{value!r}")
    return value


def _host_of(bind: str) -> str:
    """从 `host:port` / `[v6]:port` / 裸 host 里取主机部分。"""
    text = bind.strip()
    if text.startswith("["):
        return text[1 : text.index("]")] if "]" in text else text
    if text.count(":") == 1:
        return text.rsplit(":", 1)[0]
    return text


def _glob_match(pattern: str, value: str) -> bool:
    """只支持 `*` 的简单通配，避免引入正则带来的误用风险。"""
    if pattern == "*":
        return True
    if "*" not in pattern:
        return pattern == value
    head, _, tail = pattern.partition("*")
    return value.startswith(head) and value.endswith(tail) and len(value) >= len(head) + len(tail)
