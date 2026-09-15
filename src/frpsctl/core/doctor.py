"""体检与安全 lint（设计文档 §8.7、§10）。

设计原则：**doctor 只报告，不修复**。每条发现都带上"哪个事实导致这条检查存在"，
因为一个说不出理由的检查项，用户只会选择忽略它。

严重度语义：

| 级别 | 含义 | 是否影响退出码 |
|------|------|--------------|
| ERROR | 服务不可能正常工作，或存在安全缺口 | 是（退出码 1） |
| WARN | 能工作，但偏离安全基线或有可用性风险 | 否 |
| INFO | 提示性信息 | 否 |
"""

from __future__ import annotations

import contextlib
import socket
import stat
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..errors import FrpsctlError
from .config import config_flags
from .health import HealthLayer, probe_plugins
from .healthcheck import parse_dashboard, parse_plugin_targets
from .instance import Instance
from .lifecycle import Lifecycle, Owner, State
from .lock import is_locked
from .schema import PORT_FIELDS
from .version import MINIMUM_VERSION, read_binary_version, upgrade_hint

__all__ = ["Severity", "Finding", "DoctorReport", "run_doctor"]


class Severity(Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"

    @property
    def rank(self) -> int:
        return {"INFO": 0, "WARN": 1, "ERROR": 2}[self.value]


@dataclass(frozen=True)
class Finding:
    check: str
    severity: Severity
    message: str
    hint: str = ""


@dataclass(frozen=True)
class DoctorReport:
    instance: str
    findings: list[Finding]

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def ok(self) -> bool:
        return not self.errors

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: -f.severity.rank)


def run_doctor(inst: Instance, *, binary: Path | None = None) -> DoctorReport:
    """跑完全部检查项，返回报告（不抛异常，除致命的环境问题外）。"""
    findings: list[Finding] = []
    lc = Lifecycle(inst, binary=binary)

    findings.extend(_check_binary(lc, inst))
    findings.extend(_check_config(lc, inst))
    findings.extend(_check_permissions(inst))
    findings.extend(_check_dashboard(inst))
    findings.extend(_check_hardening(inst))
    findings.extend(_check_ports(inst, lc))
    findings.extend(_check_ownership(inst, lc))
    findings.extend(_check_lock(inst))
    findings.extend(_check_plugins(inst))

    return DoctorReport(instance=inst.name, findings=findings)


# ---------------------------------------------------------------------------
# 各检查项
# ---------------------------------------------------------------------------


def _check_binary(lc: Lifecycle, inst: Instance) -> list[Finding]:
    """二进制存在性、可执行性、版本与"运行版本 vs 磁盘版本"。

    刻意**不接收** `binary` 参数：那会诱导实现去读"用户传了什么"，而这里要检查的
    是"实际会用哪个"——由 `lc.binary()` 统一解析（显式 --binary 优先，否则解软链）。
    """
    out: list[Finding] = []
    try:
        path = lc.binary()
    except FrpsctlError as exc:
        return [Finding("二进制", Severity.ERROR, exc.message, exc.hint or "")]

    if not path.exists():
        return [Finding("二进制", Severity.ERROR, f"不存在：{path}", "运行 `frpsctl install`")]
    if not path.stat().st_mode & stat.S_IXUSR:
        out.append(Finding("二进制", Severity.ERROR, f"不可执行：{path}", f"chmod 0755 {path}"))

    try:
        version = read_binary_version(path)
    except FrpsctlError as exc:
        return [Finding("二进制", Severity.ERROR, exc.message, exc.hint or "")]

    if version.tuple < MINIMUM_VERSION:
        out.append(
            Finding(
                "二进制版本",
                Severity.ERROR,
                f"{version} 低于最低支持版本 {'.'.join(map(str, MINIMUM_VERSION))}（无 v2 Admin API）",
                "运行 `frpsctl install` 安装达标版本",
            )
        )
    else:
        hint = upgrade_hint(version)
        if hint:
            out.append(Finding("二进制版本", Severity.WARN, hint, "0.71.0 修复了该远程 DoS"))
        else:
            out.append(Finding("二进制版本", Severity.INFO, f"frps {version} 受支持"))

    # 运行版本 vs 磁盘版本（§8.6.1）
    running = (inst.read_state() or {}).get("version")
    disk = lc.disk_version()
    if isinstance(running, str) and disk is not None and running != str(disk):
        out.append(
            Finding(
                "版本一致性",
                Severity.WARN,
                f"正在运行 {running}，磁盘上是 {disk}（重启后才会生效）",
                "换链不影响运行中的进程：`frpsctl restart` 后生效",
            )
        )
    return out


def _check_config(lc: Lifecycle, inst: Instance) -> list[Finding]:
    from .config import validate_text

    if not inst.config.exists():
        return [Finding("配置", Severity.ERROR, f"不存在：{inst.config}", "运行 `frpsctl init`")]
    try:
        text = inst.config.read_text("utf-8")
    except OSError as exc:
        return [Finding("配置", Severity.ERROR, f"无法读取：{exc}")]

    try:
        # binary_version() 在这里的作用不只是拿版本号：它会执行 §3.6 的版本门槛
        # 校验（不达标即抛），因此不能因为"返回值用不上"就删掉。
        lc.binary_version()
        binary = lc.binary()
    except FrpsctlError:
        return []  # 二进制问题已单独报过，此处无法做权威校验

    uses_unsafe = False
    with contextlib.suppress(Exception):
        data = tomllib.loads(text)
        source = (data.get("auth") or {}).get("tokenSource") or {}
        uses_unsafe = str(source.get("type", "")).lower() == "exec"

    try:
        validate_text(text, binary=binary, workdir=inst.dir, uses_unsafe=uses_unsafe)
    except FrpsctlError as exc:
        return [Finding("配置校验", Severity.ERROR, exc.message, exc.hint or "")]
    return [
        Finding(
            "配置校验",
            Severity.INFO,
            f"semantic + frps verify 均通过（标志："
            f"{' '.join(config_flags(uses_exec_token_source=uses_unsafe))}）",
        )
    ]


def _check_permissions(inst: Instance) -> list[Finding]:
    """含 token 的配置必须 0600（§8.7）。"""
    if not inst.config.exists():
        return []
    mode = inst.config.stat().st_mode & 0o777
    has_secret = False
    with contextlib.suppress(Exception):
        data = tomllib.loads(inst.config.read_text("utf-8"))
        auth = data.get("auth") or {}
        web = data.get("webServer") or {}
        has_secret = bool(auth.get("token") or web.get("password"))

    if mode & 0o077:
        shown = oct(mode)[2:].zfill(4)
        severity = Severity.ERROR if has_secret else Severity.WARN
        return [
            Finding(
                "配置文件权限",
                severity,
                f"{inst.config} 权限为 {shown}" + ("，且内含 token 或 dashboard 口令" if has_secret else ""),
                "chmod 600 该文件（frpsctl 写入时会自动设为 0600）",
            )
        ]
    return [Finding("配置文件权限", Severity.INFO, "0600 正常")]


def _check_dashboard(inst: Instance) -> list[Finding]:
    """dashboard 暴露面与弱口令（§8.7、§10 硬约束 1）。"""
    out: list[Finding] = []
    dash = parse_dashboard(inst.config)
    if not dash.enabled:
        out.append(
            Finding(
                "dashboard",
                Severity.INFO,
                "webServer.port = 0，未启用（Admin API 与 status 的统计将不可用）",
                "需要状态聚合时可设一个只监听回环的端口",
            )
        )
        return out

    loopback = dash.addr in ("127.0.0.1", "::1", "localhost")
    if not loopback and not dash.auth_enabled:
        out.append(
            Finding(
                "dashboard 暴露面",
                Severity.ERROR,
                f"绑定 {dash.addr}:{dash.port} 且 user/password 均为空 = **完全不鉴权**",
                "任何人都能读取全部状态并下线任意代理；请设置口令或改回 127.0.0.1",
            )
        )
    elif not dash.auth_enabled:
        out.append(
            Finding(
                "dashboard 弱口令",
                Severity.WARN,
                "user/password 均为空（当前仅监听回环，风险有限）",
                "frp 在两者同时为空时**完全不鉴权**，建议设置口令",
            )
        )
    elif dash.user == "admin" and dash.password == "admin":  # noqa: S105 - 这是弱口令检测，不是凭据
        out.append(Finding("dashboard 弱口令", Severity.WARN, "使用了 admin/admin"))

    if not loopback:
        out.append(Finding("dashboard", Severity.INFO, f"监听非回环地址 {dash.addr}:{dash.port}"))
    return out


def _check_hardening(inst: Instance) -> list[Finding]:
    """安全基线偏离项（§10）。"""
    out: list[Finding] = []
    try:
        data = tomllib.loads(inst.config.read_text("utf-8"))
    except Exception:
        return out

    tls = ((data.get("transport") or {}).get("tls")) or {}
    if not tls.get("force"):
        out.append(
            Finding(
                "transport.tls.force",
                Severity.WARN,
                "未开启：frpc 可用明文连接",
                "设为 true 可拒绝明文客户端（frp 默认就是 false）",
            )
        )

    if not data.get("allowPorts"):
        out.append(
            Finding(
                "allowPorts",
                Severity.WARN,
                "未设置：任何持有 token 的客户端都能申请任意端口",
                "按需限制端口段，例如 allowPorts = [{ start = 6000, end = 6100 }]",
            )
        )
    if not data.get("maxPortsPerClient"):
        out.append(
            Finding(
                "maxPortsPerClient",
                Severity.WARN,
                "未设置（frp 默认 0 = 不限）：单个客户端可耗尽端口",
                "建议设为 20 之类的小值",
            )
        )

    auth = data.get("auth") or {}
    if auth.get("method", "token") == "oidc":
        out.append(Finding("auth.method", Severity.INFO, "使用 oidc（frpsctl 不管理其凭据）"))
    return out


def _check_ports(inst: Instance, lc: Lifecycle) -> list[Finding]:
    """端口可绑定性探测（§8.7）。

    自己正在运行时占用端口是**正常**的，因此先判断实例状态，避免把
    "我们自己占着"报成冲突。
    """
    out: list[Finding] = []
    try:
        data = tomllib.loads(inst.config.read_text("utf-8"))
    except Exception:
        return out

    state, ref = lc.state()
    ours_running = state is State.RUNNING and ref is not None
    bind_addr = str(data.get("bindAddr") or "0.0.0.0")

    for dotted in PORT_FIELDS:
        value = _dig(data, dotted)
        if not isinstance(value, int) or value <= 0:
            continue
        host = (
            str(data.get("webServer", {}).get("addr") or "127.0.0.1")
            if dotted.startswith("webServer")
            else bind_addr
        )
        if _port_bindable(host, value):
            continue
        if ours_running:
            out.append(
                Finding(
                    "端口可绑定性",
                    Severity.INFO,
                    f"{dotted} = {value} 已被占用（本实例正在运行，属正常）",
                )
            )
        else:
            out.append(
                Finding(
                    "端口可绑定性",
                    Severity.ERROR,
                    f"{dotted} = {value} 无法绑定（地址 {host}）",
                    "端口已被其他进程占用，启动会失败；换个端口或停掉占用者",
                )
            )

    for dotted in PORT_FIELDS:
        value = _dig(data, dotted)
        if isinstance(value, int) and 0 < value < 1024:
            out.append(
                Finding(
                    "低端口",
                    Severity.INFO,
                    f"{dotted} = {value} 属于 <1024 端口",
                    "以非 root 运行时，systemd 需要 AmbientCapabilities=CAP_NET_BIND_SERVICE",
                )
            )
    return out


def _port_bindable(host: str, port: int) -> bool:
    """尝试绑定再立刻释放。SO_REUSEADDR 会掩盖 TIME_WAIT，故此处不设置它。"""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
    except OSError:
        return False
    return True


def _dig(data: dict, dotted: str):
    node: object = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _check_ownership(inst: Instance, lc: Lifecycle) -> list[Finding]:
    """systemd 与 direct 冲突检测（R3）。"""
    out: list[Finding] = []
    owner = lc.resolve_owner()
    state, ref = lc.state()

    if state is State.FOREIGN:
        out.append(
            Finding(
                "进程所有权",
                Severity.ERROR,
                f"state.json 记录的 pid {ref.pid if ref else '?'} 存活但身份校验不通过",
                "该 pid 可能已被复用；确认后删除 state.json",
            )
        )
    if state is State.STALE:
        out.append(
            Finding(
                "进程所有权",
                Severity.INFO,
                "存在陈旧 state.json（进程已退出）",
                "下次 start 会自动清理",
            )
        )

    from .systemd import Systemd

    systemd = Systemd(inst)
    if owner is Owner.SYSTEMD and inst.state.exists():
        out.append(
            Finding(
                "systemd 与 direct 冲突",
                Severity.ERROR,
                "同名 unit 处于 active，同时存在 state.json —— 两种所有权同时成立",
                "先确定由谁托管，再清理另一方（unit 用 service uninstall，direct 用 stop）",
            )
        )
    if owner is Owner.SYSTEMD:
        out.append(Finding("进程所有权", Severity.INFO, f"由 systemd 托管（{systemd.unit_name}）"))
    elif owner is Owner.DIRECT:
        out.append(Finding("进程所有权", Severity.INFO, "由 frpsctl 直接托管（direct）"))
    return out


def _check_lock(inst: Instance) -> list[Finding]:
    if is_locked(inst.lock):
        return [
            Finding(
                "实例锁",
                Severity.WARN,
                f"{inst.lock} 正被持有",
                "可能有另一个 frpsctl 正在操作该实例",
            )
        ]
    return []


def _check_plugins(inst: Instance) -> list[Finding]:
    """插件可达性与**暴露面**（§3.7 L3、§11.2）。

    **可达性仅告警，不影响退出码**——插件故障不是这份配置的错，而且 frp 侧对
    插件没有超时，插件挂掉意味着**所有客户端都无法登录**（fail-closed）。

    但"插件回调地址绑在非回环"是 **ERROR**：frp 的插件协议**没有任何认证**
    （`HTTPPluginOptions` 只有 name/addr/path/ops/tlsVerify），把回调指到非回环
    地址，等于让网络上任何人决定"谁能登录 frps"。
    """
    out: list[Finding] = []
    targets = parse_plugin_targets(inst.config)
    if not targets:
        return out

    for target in targets:
        if not _is_loopback_host(target.host):
            out.append(
                Finding(
                    "插件暴露面",
                    Severity.ERROR,
                    f"httpPlugins[{target.name}] 指向非回环地址 {target.host}:{target.port}",
                    "frp 插件协议没有任何认证，任何能访问该端口的人都能伪造 "
                    "Login/NewProxy 事件；请改为 127.0.0.1"
                    "（frpsctl 自带的插件服务也会拒绝绑非回环地址）",
                )
            )

    layer, detail = probe_plugins(targets)
    if layer is HealthLayer.FAIL:
        out.append(
            Finding(
                "插件可达性",
                Severity.WARN,
                f"插件不可达：{detail} —— 客户端将无法登录（fail-closed，§11.2）",
                "插件必须绑回环并由 systemd 守护（Restart=always）；"
                "也可用 `frpsctl plugin check` 离线核对策略",
            )
        )
    else:
        out.append(Finding("插件可达性", Severity.INFO, f"{len(targets)} 个目标可达"))
    return out


def _is_loopback_host(host: str) -> bool:
    import ipaddress

    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
