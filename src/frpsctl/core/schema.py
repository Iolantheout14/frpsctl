"""配置语义校验（设计文档 §8.4 第 3 步、附录 A）。

**职责边界（重要）**：这一层只做**范围与类型**检查，目的是给出比 frps 更快、
更好读的错误信息（"bindPort 必须是 1..65535 的整数"）。它**不下最终结论**：

| 检查 | 由谁负责 |
|------|---------|
| 类型、取值范围、互斥关系 | 本模块（pydantic） |
| 键名是否合法 | `frps verify --strict_config=true`（§3.1 推论 1） |
| 端口能否绑定 | `doctor` 的 bind 探测（§8.7） |

因此模型统一 `extra="allow"`：遇到不认识的键**不报错**，原样放行给官方 verify。
若在这里对未知键报错，就等于用一份会过期的模型去否决官方配置——那正是
ADR-2 要避免的"模型未覆盖的键被丢掉"的翻版。
"""

from __future__ import annotations

import tomllib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..errors import ConfigError
from .healthcheck import PORT_FIELDS, is_loopback

__all__ = [
    "ServerConfig",
    "validate_document",
    "validate_mapping",
    "PORT_FIELDS",
]

#: `PORT_FIELDS` 的唯一定义在 `core/healthcheck.py`（v0.3.1 迁出）：doctor 是
#: 它唯一的用户，而从本模块导入会拉起 pydantic。此处 re-export 保持既有
#: 导入点的兼容（`from frpsctl.core.schema import PORT_FIELDS` 仍可用）。

_LOG_LEVELS = ("trace", "debug", "info", "warn", "error")


class _Base(BaseModel):
    """统一配置：允许未知键（交给官方 verify 判定），且不做隐式类型转换以外的加工。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class PortRange(_Base):
    """`allowPorts` 的元素：`{single = 6000}` 或 `{start = 6000, end = 6010}`。"""

    single: int | None = None
    start: int | None = None
    end: int | None = None

    @model_validator(mode="after")
    def _check(self) -> PortRange:
        has_single = self.single is not None
        has_range = self.start is not None or self.end is not None
        if has_single and has_range:
            raise ValueError("single 不能与 start/end 同时出现")
        if not has_single and not has_range:
            raise ValueError("必须给出 single，或同时给出 start 与 end")
        if has_range:
            if self.start is None or self.end is None:
                raise ValueError("start 与 end 必须同时给出")
            if not (1 <= self.start <= 65535 and 1 <= self.end <= 65535):
                raise ValueError("start/end 必须在 1..65535 之间")
            if self.start > self.end:
                raise ValueError(f"start({self.start}) 不能大于 end({self.end})")
        if has_single and not (1 <= self.single <= 65535):  # type: ignore[operator]
            raise ValueError("single 必须在 1..65535 之间")
        return self


class TokenSource(_Base):
    """`auth.tokenSource`。type = "exec" 时 frps 要求 `--allow-unsafe`（§3.1）。"""

    type: Literal["file", "exec"] | None = None


class OidcConfig(_Base):
    """`auth.oidc`（服务端 OIDC 校验）。字段名以官方配置为准。

    frpsctl 只做**配置完整性**校验（见 `AuthConfig` 的 model_validator）与
    doctor 提示——OIDC 协议本身由 frps 实现，不在本工具范围内（硬边界：
    绝不重新实现 frp 已有能力）。
    """

    issuer: str | None = None
    audience: str | None = None
    skipExpiryCheck: bool | None = None
    skipIssuerCheck: bool | None = None


class AuthConfig(_Base):
    method: Literal["token", "oidc"] | None = None
    token: str | None = None
    additionalScopes: list[Literal["HeartBeats", "NewWorkConns"]] | None = None
    tokenSource: TokenSource | None = None
    oidc: OidcConfig | None = None

    @model_validator(mode="after")
    def _check(self) -> AuthConfig:
        if self.method == "oidc":
            if self.oidc is None or not self.oidc.issuer:
                raise ValueError("auth.method = oidc 时必须配置 auth.oidc.issuer")
            if not self.oidc.audience:
                raise ValueError("auth.method = oidc 时必须配置 auth.oidc.audience")
        return self


class WebServerTLS(_Base):
    certFile: str | None = None
    keyFile: str | None = None

    @model_validator(mode="after")
    def _check(self) -> WebServerTLS:
        if bool(self.certFile) != bool(self.keyFile):
            raise ValueError("certFile 与 keyFile 必须同时给出")
        return self


class WebServerConfig(_Base):
    addr: str | None = None
    port: int = Field(default=0, ge=0, le=65535)
    user: str | None = None
    password: str | None = None
    assetsDir: str | None = None
    pprofEnable: bool | None = None
    tls: WebServerTLS | None = None


class LogConfig(_Base):
    to: str | None = None
    level: Literal[_LOG_LEVELS] | None = None  # type: ignore[valid-type]
    maxDays: int | None = None
    disablePrintColor: bool | None = None


class TLSConfig(_Base):
    force: bool | None = None
    certFile: str | None = None
    keyFile: str | None = None
    trustedCaFile: str | None = None

    @model_validator(mode="after")
    def _check(self) -> TLSConfig:
        if bool(self.certFile) != bool(self.keyFile):
            raise ValueError("certFile 与 keyFile 必须同时给出")
        return self


class TransportConfig(_Base):
    tcpMux: bool | None = None
    tcpMuxKeepaliveInterval: int | None = None
    tcpKeepalive: int | None = None
    maxPoolCount: int | None = Field(default=None, ge=0)  # 负值非法（历史版本曾因此 panic）
    heartbeatTimeout: int | None = None
    tls: TLSConfig | None = None


class HTTPPlugin(_Base):
    name: str | None = None
    addr: str | None = None
    path: str | None = None
    ops: list[str] | None = None
    tlsVerify: bool | None = None


class ServerConfig(_Base):
    """frps 服务端配置模型（附录 A 的范围校验子集）。

    未列出的键（`custom404Page`、`natholeAnalysisDataReserveHours`…）由
    `extra="allow"` 原样放行——它们不需要范围校验，交给官方 verify。
    """

    bindAddr: str | None = None
    bindPort: int = Field(default=7000, ge=0, le=65535)
    kcpBindPort: int = Field(default=0, ge=0, le=65535)
    quicBindPort: int = Field(default=0, ge=0, le=65535)
    proxyBindAddr: str | None = None
    vhostHTTPPort: int = Field(default=0, ge=0, le=65535)
    vhostHTTPTimeout: int = 60
    vhostHTTPSPort: int = Field(default=0, ge=0, le=65535)
    tcpmuxHTTPConnectPort: int = Field(default=0, ge=0, le=65535)
    tcpmuxPassthrough: bool | None = None
    subDomainHost: str | None = None
    enablePrometheus: bool | None = None
    maxPortsPerClient: int = Field(default=0, ge=0)
    userConnTimeout: int = Field(default=10, ge=0)
    udpPacketSize: int = Field(default=1500, gt=0)
    detailedErrorsToClient: bool | None = None
    allowPorts: list[PortRange] | None = None

    auth: AuthConfig | None = None
    webServer: WebServerConfig | None = None
    log: LogConfig | None = None
    transport: TransportConfig | None = None
    httpPlugins: list[HTTPPlugin] | None = None


def _format_error(exc: ValidationError) -> str:
    """把 pydantic 的错误压成一行行"位置: 原因"，比原始 JSON 好读。"""
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(根)"
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


def check_dangerous_combination(data: dict[str, Any]) -> str | None:
    """返回"危险组合"的说明，或 None 表示安全。

    这条检查来自一个**已确认的事实**（§3.3）：`webServer.user` 与
    `webServer.password` **同时为空时 frp 完全不鉴权**——不是"要求登录"，而是
    任何人都能读取全部状态、并下线任意代理。

    ⚠️ 判据只覆盖"**完全不鉴权**"，因此刻意写成"两者都为空才拒绝"。frp 的
    鉴权开关是"**任一非空即启用 Basic Auth**"（实测：`user="admin"` + 空口令时
    无凭据请求得 401，而 `admin:`+空口令得 200）。也就是说"有 user 但口令为空"
    仍然是"免口令 dashboard"，但它**毕竟启用了鉴权**，属于强度不足而非缺口——
    由 `doctor` 以 WARN 提示，不在这里否决用户的显式选择。

    单看 `webServer.user = ""` 或 `webServer.addr = "0.0.0.0"` 都无害，**只有
    组合起来才是缺口**。因此校验必须针对**合并后的完整配置**，而不是被修改的
    那一个键——`config set webServer.user '""'` 自身永远看不出问题。

    设计文档 §10 硬约束 1 要求 `config set` 与 `doctor` 双重拦截，这个函数
    就是那个共享的判据（此前只有 doctor 一侧，`config set` 能一路写成
    `addr=0.0.0.0` + 双空口令并退出 0）。
    """
    web = data.get("webServer")
    if not isinstance(web, dict):
        return None
    port = web.get("port") or 0
    if not isinstance(port, int) or port <= 0:
        return None  # 未启用 dashboard，不存在暴露面
    addr = str(web.get("addr") or "127.0.0.1")
    if is_loopback(addr):
        return None
    user = web.get("user") or ""
    password = web.get("password") or ""
    if user or password:
        return None
    return (
        f"webServer 绑定非回环地址 {addr}:{port}，且 user/password 均为空 = "
        "**完全不鉴权**（任何人都能读取全部状态并下线任意代理）"
    )


def validate_mapping(data: dict[str, Any]) -> ServerConfig:
    """校验一个已解析的配置字典。"""
    try:
        return ServerConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            "配置语义校验未通过：\n" + _format_error(exc),
            hint="这层只检查类型与范围；键名合法性由 frps verify 判定",
        ) from None


def validate_document(text: str) -> ServerConfig:
    """从配置文本做语义校验（§9 第 3 步）。"""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置 TOML 语法错误：{exc}") from None
    return validate_mapping(data)
