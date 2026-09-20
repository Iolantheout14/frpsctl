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
from .config import config_flags, needs_unsafe_flag
from .health import HealthLayer, probe_plugins
from .healthcheck import PORT_FIELDS, is_loopback, parse_dashboard, parse_plugin_targets
from .instance import Instance
from .lifecycle import Lifecycle, Owner, ProcessRef, State
from .lock import is_locked
from .version import MINIMUM_VERSION, Version, read_binary_version, upgrade_hint

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

    @property
    def counts(self) -> dict[str, int]:
        """按严重度计数（小写键，与 `--json` 的其余字段同风格）。

        CLI 末尾摘要、`doctor --json` 与 Web 体检卡片共用这一个口径——
        三处各数一遍迟早漂移，而"发现几个 ERROR"是退出码之外用户最先看的信息。
        """
        out = {"error": 0, "warn": 0, "info": 0}
        for finding in self.findings:
            out[finding.severity.value.lower()] += 1
        return out

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: -f.severity.rank)


def run_doctor(inst: Instance, *, binary: Path | None = None) -> DoctorReport:
    """跑完全部检查项，返回报告（不抛异常，除致命的环境问题外）。

    所有权与版本只探测一次并传给各检查项：它们本来就是"体检开始时的快照"，
    重复探测既慢（每次 `systemctl` / `frps -v` 都要 fork）又可能自相矛盾。
    """
    findings: list[Finding] = []
    lc = Lifecycle(inst, binary=binary)
    owner, state, ref = lc.state_with_owner()
    version = _read_version(lc)

    findings.extend(_check_binary(lc, inst, version=version))
    findings.extend(_check_config(lc, inst, version=version))
    findings.extend(_check_permissions(inst))
    findings.extend(_check_dashboard(inst))
    findings.extend(_check_hardening(inst))
    findings.extend(_check_ports(inst, state=state, ref=ref))
    findings.extend(
        _check_ownership(
            inst, owner=owner, state=state, ref=ref, probe_error=lc.owner_probe_error()
        )
    )
    findings.extend(_check_lock(inst))
    findings.extend(_check_plugins(inst))
    findings.extend(_check_web_password_file(inst))
    findings.extend(_check_systemd_deployment(inst))
    findings.extend(_check_local_services(inst))

    return DoctorReport(instance=inst.name, findings=findings)


def _read_version(lc: Lifecycle) -> Version | None:
    """读一次二进制版本（**裸读**，不做 §3.6 门槛判定）。

    门槛判定是 `_check_binary` 的展示逻辑：若在这里用 `lc.binary_version()`
    （自带门槛校验），`< 0.70.0` 会抛 `UnsupportedVersion` 被吞成 None，最终
    报成"无法读取版本 / 可能不是官方 frps"——而真因是"版本太低"。错误诊断
    把用户引向完全错误的方向（第五轮 review 实测复现）。
    """
    try:
        return read_binary_version(lc.binary())
    except FrpsctlError:
        return None


# ---------------------------------------------------------------------------
# 各检查项
# ---------------------------------------------------------------------------


def _check_binary(
    lc: Lifecycle, inst: Instance, *, version: Version | None
) -> list[Finding]:
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
    try:
        executable = bool(path.stat().st_mode & stat.S_IXUSR)
    except OSError as exc:
        # exists 与 stat 之间文件被删/不可读：doctor 只报告不崩（v0.3.1）。
        return [Finding("二进制", Severity.ERROR, f"无法读取权限：{path}（{exc}）")]
    if not executable:
        out.append(Finding("二进制", Severity.ERROR, f"不可执行：{path}", f"chmod 0755 {path}"))
    if version is None:
        out.append(
            Finding(
                "二进制版本",
                Severity.ERROR,
                f"无法读取版本：{path} -v 失败或超时",
                "该二进制可能不是官方 frps，或无法执行",
            )
        )
    elif version.tuple < MINIMUM_VERSION:
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


def _check_config(lc: Lifecycle, inst: Instance, *, version: Version | None) -> list[Finding]:
    from .config import validate_text

    if not inst.config.exists():
        return [Finding("配置", Severity.ERROR, f"不存在：{inst.config}", "运行 `frpsctl init`")]
    try:
        text = inst.config.read_text("utf-8")
    except OSError as exc:
        return [Finding("配置", Severity.ERROR, f"无法读取：{exc}")]

    if version is None or version.tuple < MINIMUM_VERSION:
        return []  # 二进制问题已单独报过（含"版本低于门槛"），此处无法做权威校验
    try:
        binary = lc.binary()
    except FrpsctlError:
        return []

    uses_unsafe = needs_unsafe_flag(text)

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
    try:
        mode = inst.config.stat().st_mode & 0o777
    except OSError as exc:
        # exists 与 stat 之间的竞态（并发删除/权限变化）：doctor 只报告不崩。
        return [Finding("配置文件权限", Severity.WARN, f"无法读取权限：{inst.config}（{exc}）")]
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

    loopback = is_loopback(dash.addr)
    if not loopback and not dash.auth_enabled:
        out.append(
            Finding(
                "dashboard 暴露面",
                Severity.ERROR,
                f"绑定 {dash.addr}:{dash.port} 且 user/password 均为空 = **完全不鉴权**",
                "任何人都能读取全部状态并下线任意代理；请设置口令或改回 127.0.0.1",
            )
        )
    elif dash.auth_enabled and not dash.password:
        # frp 的鉴权开关是"任一非空即启用"，而 Basic Auth 里的**空口令是合法口令**：
        # 实测 `user="admin"` + 空口令时，`admin:` 就能拿到 200。此时用户名是唯一
        # 防护，而它通常就是 "admin" —— 与"完全不鉴权"只差一步。
        out.append(
            Finding(
                "dashboard 弱口令",
                Severity.WARN,
                f"password 为空（user = {dash.user!r}）—— frp 把空口令当作合法口令，"
                "等于只用用户名保护 dashboard",
                "设置一个随机口令：`frpsctl config set webServer.password '\"...\"'`",
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

    auth = data.get("auth")
    if auth is not None and not isinstance(auth, dict):
        out.append(
            Finding(
                "auth",
                Severity.ERROR,
                f"auth 必须是对象，实际是 {type(auth).__name__}",
                "手工修正配置（[auth] 表写法）",
            )
        )
        return out
    auth = auth or {}
    if auth.get("method", "token") == "oidc":
        oidc = auth.get("oidc")
        if oidc is not None and not isinstance(oidc, dict):
            out.append(
                Finding(
                    "auth.oidc",
                    Severity.ERROR,
                    f"auth.oidc 必须是对象，实际是 {type(oidc).__name__}",
                    "修正配置（或用 `frpsctl plugin check` 之外的 `config edit` 手工改）",
                )
            )
            return out
        oidc = oidc or {}
        missing = [key for key in ("issuer", "audience") if not oidc.get(key)]
        if missing:
            out.append(
                Finding(
                    "auth.oidc",
                    Severity.ERROR,
                    f"method = oidc 但缺少 {', '.join('auth.oidc.' + key for key in missing)}",
                    "补全 OIDC 配置或改回 token 鉴权",
                )
            )
        else:
            out.append(
                Finding(
                    "auth.oidc",
                    Severity.INFO,
                    f"使用 oidc（issuer={oidc.get('issuer')}；frpsctl 不管理其凭据）",
                )
            )
        if auth.get("token"):
            out.append(
                Finding(
                    "auth.token",
                    Severity.WARN,
                    "method = oidc 时 auth.token 不会生效（容易误导）",
                    "删除 token 或改回 method = token",
                )
            )
    return out


def _check_ports(
    inst: Instance, *, state: State, ref: ProcessRef | None
) -> list[Finding]:
    """端口可绑定性探测（§8.7）。

    自己正在运行时占用端口是**正常**的，因此先判断实例状态，避免把
    "我们自己占着"报成冲突。状态由 `run_doctor` 一次性探测后传入。
    """
    out: list[Finding] = []
    try:
        data = tomllib.loads(inst.config.read_text("utf-8"))
    except Exception:
        return out

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


def _check_ownership(
    inst: Instance,
    *,
    owner: Owner,
    state: State,
    ref: ProcessRef | None,
    probe_error: str | None = None,
) -> list[Finding]:
    """systemd 与 direct 冲突检测（R3）。owner/state 由 run_doctor 单次探测传入。

    `probe_error` 非空表示这次探测是**降级**的（systemctl 超时等）——必须
    如实报告：不报的话，doctor 会拿一个"按 state.json 猜的所有权"当真结论。
    """
    out: list[Finding] = []
    if probe_error:
        out.append(
            Finding(
                "systemd 探测",
                Severity.WARN,
                f"systemctl 探测失败，所有权按 state.json 降级判定：{probe_error}",
                "恢复 systemd 后重跑；变更类操作（start/stop/config）在此期间会拒绝执行",
            )
        )

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


def _check_web_password_file(inst: Instance) -> list[Finding]:
    """Web 管理台口令文件的权限（`web service install` 生成，0600）。

    未部署管理台时文件不存在——那是常态，不产生任何输出。文件存在却权限过宽
    时必须报出来：它含登录口令，同一台机器上的其他用户能读到就等于拿到了
    管理台的完整控制权（改配置、停服务）。
    """
    from .systemd import WebService

    path = WebService(inst).password_file
    if not path.exists():
        return []
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return [Finding("Web 口令文件", Severity.WARN, f"无法读取权限：{path}")]
    if mode & 0o077:
        return [
            Finding(
                "Web 口令文件",
                Severity.WARN,
                f"{path} 权限为 {oct(mode)[2:].zfill(4)}（应 0600）",
                "它含管理台登录口令：chmod 600 该文件",
            )
        ]
    return [Finding("Web 口令文件", Severity.INFO, "0600 正常")]


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
        if not is_loopback(target.host):
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


def _port_open(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """TCP 可连接？（doctor 的只读探针；不发送任何业务请求）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _check_local_services(inst: Instance) -> list[Finding]:
    """web/plugin 的 direct 后台状态（v0.3.3）。

    systemd 托管由 `_check_systemd_deployment` 覆盖；这里只看 direct 后台
    （`web start` / `plugin start`）的状态文件与进程——未启动过零输出。
    """
    from . import serve_runtime

    out: list[Finding] = []
    for spec in (serve_runtime.WEB_SPEC, serve_runtime.PLUGIN_SPEC):
        status = serve_runtime.probe(inst, spec)
        if status.owner is serve_runtime.ServeOwner.CORRUPTED:
            out.append(
                Finding(
                    "后台服务",
                    Severity.WARN,
                    f"{spec.label}的状态文件已损坏：{status.error}",
                    "确认该服务没有在跑后删除状态文件（start 会重建）",
                )
            )
        elif status.owner is serve_runtime.ServeOwner.FOREIGN:
            out.append(
                Finding(
                    "后台服务",
                    Severity.ERROR,
                    f"{spec.label}的后台状态指向 pid {status.state.pid}，但它不属于本服务",
                    "该 pid 可能已被复用为无关进程；确认后删除状态文件",
                )
            )
        elif status.running:
            bind = status.state.args.get("bind") or "-"
            out.append(
                Finding(
                    "后台服务",
                    Severity.INFO,
                    f"{spec.label}在后台运行（pid {status.state.pid}，{bind}）",
                )
            )
            # 端口一致性（v0.3.3，P1）：状态说在跑、记录端口却连不上 → 服务
            # 可能已半死（进程在、监听没了），这正是 doctor 该发现的形态。
            host, port = status.state.host, status.state.port
            if host and port:
                probe_host = "127.0.0.1" if host in ("", "0.0.0.0", "::", "[::]") else host
                if not _port_open(probe_host, port):
                    out.append(
                        Finding(
                            "后台服务",
                            Severity.WARN,
                            f"{spec.label}标记为运行中，但 {host}:{port} 无法连接",
                            "服务可能已半死：查看其日志，用 stop 后重新 start",
                        )
                    )
        elif status.owner is serve_runtime.ServeOwner.STALE:
            out.append(
                Finding(
                    "后台服务",
                    Severity.INFO,
                    f"{spec.label}存在陈旧的后台状态（进程已退出）",
                    "下次 start 会自动清理",
                )
            )
    return out


def _check_systemd_deployment(inst: Instance) -> list[Finding]:
    """systemd 部署的下游一致性（v0.3.2）：账户还在吗？二进制还可达吗？

    此前 doctor 在"unit 已安装"之后完全失明：服务账户被删除、二进制被移走、
    路径变化都不会被发现，直到某次 `systemctl start` 才炸。检查以**实际生效
    值**为准（`systemctl show` 优先，安装留档 `service.json` 回退），未安装
    任何服务的实例零输出。降级（探测失败）进入 WARN 而不是拖垮 doctor。
    """
    from .systemd import (
        PluginService,
        Systemd,
        WebService,
        _access_problem,
        _account_ids,
        _dir_access_problem,
        _group_record,
        _protect_home_conflict,
        _user_record,
        read_service_manifest,
        read_template_exec,
        show_unit_accounts,
    )

    out: list[Finding] = []
    manifest, manifest_error = read_service_manifest(inst)
    if manifest_error:
        out.append(
            Finding(
                "安装记录",
                Severity.WARN,
                manifest_error,
                "重装对应服务会重写它；确认无用也可直接删除该文件",
            )
        )

    services = (
        ("frps", "frps", Systemd(inst), "frpsctl service install"),
        ("plugin", "插件", PluginService(inst), "frpsctl plugin service install"),
        ("web", "Web 管理台", WebService(inst), "frpsctl web service install"),
    )
    for key, label, service, reinstall in services:
        record = manifest.get(key)
        if not isinstance(record, dict):
            record = {}
        try:
            present = service.unit_exists()
        except FrpsctlError as exc:
            out.append(
                Finding(
                    "systemd 部署",
                    Severity.WARN,
                    f"无法探测 {service.unit_name}：{exc.message}",
                    "确认 systemd 可用后重跑 doctor",
                )
            )
            continue
        if not present and not record:
            continue  # 从未安装过这个服务：不是发现

        user: str | None = None
        group: str | None = None
        if present:
            try:
                user, group = show_unit_accounts(service.unit_name)
            except FrpsctlError as exc:
                out.append(
                    Finding(
                        "systemd 部署",
                        Severity.WARN,
                        f"无法读取 {service.unit_name} 的账户信息：{exc.message}",
                        "systemd 恢复后重跑 doctor",
                    )
                )
        if user is None:
            raw_user = record.get("user")
            user = raw_user if isinstance(raw_user, str) and raw_user else None
        if group is None:
            raw_group = record.get("group")
            group = raw_group if isinstance(raw_group, str) and raw_group else None
        if user is None:
            continue  # 既读不到实际值也没有留档：上一条 WARN 已说明

        if _user_record(user) is None:
            out.append(
                Finding(
                    "systemd 部署",
                    Severity.ERROR,
                    f"{label} unit 以用户 {user!r} 运行，但该用户不存在——启动必然失败",
                    f"重建账户（useradd），或重装：{reinstall} --user <账户>",
                )
            )
            continue
        if group is not None and _group_record(group) is None:
            out.append(
                Finding(
                    "systemd 部署",
                    Severity.ERROR,
                    f"{label} unit 使用组 {group!r}，但该组不存在——启动必然失败",
                    f"重建组（groupadd），或重装：{reinstall} --group <组>",
                )
            )
            continue
        accounts = _account_ids(user, group or user)
        if accounts is None:
            continue
        uid, gid = accounts

        exec_path = read_template_exec(service.template_path)
        if exec_path is not None:
            problem = _access_problem(Path(exec_path), uid=uid, gid=gid)
            if problem is not None:
                out.append(
                    Finding(
                        "systemd 部署",
                        Severity.ERROR,
                        f"{label}：{problem}——unit 启动必然失败",
                        f"调整该路径权限（或改用共享目录）后重装：{reinstall}",
                    )
                )
        for path_label, path in (("二进制", exec_path), ("实例目录", str(inst.dir))):
            if path is None:
                continue
            prefix = _protect_home_conflict(Path(path))
            if prefix is not None:
                out.append(
                    Finding(
                        "systemd 部署",
                        Severity.ERROR,
                        f"{label} 的{path_label}位于 {prefix} 下（{path}），"
                        "会被 unit 的 ProtectHome=true 挡住",
                        f"把 frpsctl 与数据放到系统路径后重装：{reinstall}",
                    )
                )
        if key == "frps":
            # ReadWritePaths 的可写性（v0.3.2）：日志目录与实例目录缺一不可，
            # 删除或属主/权限漂移时 unit 启动必然失败。
            log_value = record.get("log_dir")
            if isinstance(log_value, str) and log_value:
                problem = _dir_access_problem(Path(log_value), uid=uid, gid=gid)
                if problem is not None:
                    out.append(
                        Finding(
                            "systemd 部署",
                            Severity.ERROR,
                            f"日志目录不可用：{problem}"
                            "（unit 的 ReadWritePaths 要求它存在且可写）",
                            f"修复该目录后重装：{reinstall}",
                        )
                    )
            work_problem = _dir_access_problem(inst.dir, uid=uid, gid=gid)
            if work_problem is not None:
                out.append(
                    Finding(
                        "systemd 部署",
                        Severity.ERROR,
                        f"实例目录对服务用户不可用：{work_problem}",
                        f"把目录移交服务用户（chown）或重装：{reinstall}",
                    )
                )
        if present:
            out.append(
                Finding(
                    "systemd 部署",
                    Severity.INFO,
                    f"{label} unit 就绪（User={user}, Group={group or user}）",
                )
            )
    return out
