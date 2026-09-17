"""systemd 集成（设计文档 §12.2、ADR-1）。

**所有权语义**：一旦某个实例被 systemd 托管，CLI 的 `start/stop/restart/status`
全部委托 `systemctl`，**pid 文件不参与任何判定**（ADR-1）。两套机制并存且互相
不知情，会产生"CLI 说已停止、systemd 立刻又拉起来"这类自相矛盾。

unit 以模板形式安装（`frps@.service`），多实例即多 unit，与 §6 的实例模型
天然对齐：实例名映射为 `%i`。

**部署体检（install_template 的前置检查）**：unit 装上去只是一半，能不能起来
取决于三个环境事实，缺任何一项都会在 `systemctl start` 时炸——而那时错误信息
与安装命令相隔很远。因此把它们搬进安装流程，当场拒绝：

1. `User`/`Group` 必须存在（否则 systemd 报 "Failed to determine user
   credentials"）；
2. `ExecStart` 的二进制对服务用户必须可达（`sudo frpsctl install` 会把二进制
   放进 `/root/...`，而 `/root` 是 0700——服务用户读不到，这是最隐蔽的一类
   部署失败）；
3. `ReadWritePaths` 的日志目录必须存在（`ProtectSystem=strict` 下 systemd
   拒绝挂载不存在的路径）。
"""

from __future__ import annotations

import contextlib
import grp
import os
import pwd
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import FrpsctlError, PermissionRequired, UsageError
from .healthcheck import is_loopback
from .instance import Instance

__all__ = [
    "Systemd",
    "PluginService",
    "WebService",
    "UNIT_TEMPLATE_PATH",
    "SYSTEMD_UNIT_DIR",
    "DEFAULT_SERVICE_USER",
    "render_unit",
    "render_plugin_unit",
    "render_web_unit",
    "generate_web_password",
]

SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
UNIT_TEMPLATE_PATH = SYSTEMD_UNIT_DIR / "frps@.service"
PLUGIN_UNIT_TEMPLATE_PATH = SYSTEMD_UNIT_DIR / "frpsctl-plugin@.service"
WEB_UNIT_TEMPLATE_PATH = SYSTEMD_UNIT_DIR / "frpsctl-web@.service"

#: 渲染用的 unit 模板（§12.2）。占位符：%i（systemd 实例说明符）与三个具名参数。
UNIT_TEMPLATE = """\
[Unit]
Description=frps service (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
ExecStart={exec_start} -c {config_dir}/%i/frps.toml
WorkingDirectory={config_dir}/%i
Restart=on-failure
RestartSec=2
LimitNOFILE=65535

# 若配置使用 <1024 端口（例如 vhostHTTPPort = 80）
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

# 加固
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
# 实例目录必须在列：ProtectSystem=strict 会把其余路径挂成只读，而 frp 默认
# 在实例目录下写日志（`log.to = "./frps.log"`）——不放行就等于让 frps 写不了盘。
ReadWritePaths={log_dir} {config_dir}/%i

[Install]
WantedBy=multi-user.target
"""

#: 默认服务用户：与 unit 模板、README 的部署示例保持一致。
DEFAULT_SERVICE_USER = "frps"

#: 插件服务的 unit 模板（§11.2：必须由 systemd 守护且 `Restart=always`——
#: 插件是全部客户端登录的单点且 fail-closed，它挂掉 = 所有人登录不了）。
PLUGIN_UNIT_TEMPLATE = """\
[Unit]
Description=frpsctl server plugin (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
ExecStart={exec_start} --instance %i plugin serve --policy {policy} --bind {bind} --path {handler_path}
WorkingDirectory={workdir}
# 插件是全部客户端登录的单点（fail-closed）：任何退出都必须被立刻拉起。
Restart=always
RestartSec=2

# 加固
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
# 实例目录同时是策略与审计文件（plugin-policy.json / plugin-audit.jsonl）的所在地
ReadWritePaths={workdir}

[Install]
WantedBy=multi-user.target
"""

#: Web 管理台的 unit 模板。`Restart=on-failure`（不是 always）——管理台崩了
#: 要拉起来，但它是交互工具而非登录单点，正常停止（systemctl stop）不该自启。
#: 口令从 `--password-file`（0600）读取，绝不写进 unit 命令行（unit 文件 0644）。
WEB_UNIT_TEMPLATE = """\
[Unit]
Description=frpsctl web console (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
ExecStart={exec_start} --instance %i web serve --bind {bind} --password-file {password_file}{extra}
WorkingDirectory={workdir}
Restart=on-failure
RestartSec=2

# 加固
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={workdir}

[Install]
WantedBy=multi-user.target
"""


def generate_web_password() -> str:
    """生成 Web 管理台口令（24 字符，与 `init` 的 dashboard 口令同量级）。

    单点实现：`web serve` 的启动口令与 `web service install` 的口令文件
    用的是同一个生成器。
    """
    import secrets

    return secrets.token_urlsafe(18)


def render_unit(
    *,
    binary: str,
    config_dir: Path,
    log_dir: Path,
    user: str = DEFAULT_SERVICE_USER,
    group: str | None = None,
) -> str:
    """渲染 unit 模板。

    `ExecStart` 写的是**具体二进制路径**而不是 `bin/frps` 软链：软链换版本后
    systemd 不会自动重启，这条差异必须在 `install` 输出里显式提醒（§8.6.1）。
    """
    return UNIT_TEMPLATE.format(
        exec_start=binary,
        config_dir=config_dir,
        log_dir=log_dir,
        user=user,
        group=group or user,
    )


def render_plugin_unit(
    *,
    exec_start: str,
    bind: str,
    handler_path: str,
    policy: Path,
    workdir: Path,
    user: str = DEFAULT_SERVICE_USER,
    group: str | None = None,
) -> str:
    """渲染插件 unit 模板。

    `ExecStart` 写**具体路径**（不是 `frpsctl` 命令名）：systemd 不读 PATH。
    pipx 默认装到 `~/.local/bin`，那条路径会被 `ProtectHome=true` 挡住——
    由 `PluginService.install_template` 的体检在安装前拒绝并给出替代方案。
    """
    return PLUGIN_UNIT_TEMPLATE.format(
        exec_start=exec_start,
        bind=bind,
        handler_path=handler_path,
        policy=policy,
        workdir=workdir,
        user=user,
        group=group or user,
    )


def render_web_unit(
    *,
    exec_start: str,
    bind: str,
    password_file: Path,
    workdir: Path,
    user: str = DEFAULT_SERVICE_USER,
    group: str | None = None,
    allow_non_loopback: bool = False,
) -> str:
    """渲染 Web 管理台 unit 模板。

    口令走 `--password-file`（0600），**绝不写进 unit 命令行**——unit 文件是
    0644，写明文等于向本机所有用户公开管理台。`--allow-non-loopback` 只在
    绑定地址非回环时渲染出来（CLI 层已要求显式开关）。
    """
    return WEB_UNIT_TEMPLATE.format(
        exec_start=exec_start,
        bind=bind,
        password_file=password_file,
        workdir=workdir,
        user=user,
        group=group or user,
        extra=" --allow-non-loopback" if allow_non_loopback else "",
    )


# ---------------------------------------------------------------------------
# systemctl 原语（Systemd 与 PluginService 共用一份实现）
# ---------------------------------------------------------------------------


def _systemctl(*args: str, timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=timeout)


def _systemctl_checked(*args: str) -> None:
    proc = _systemctl(*args)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        # 用 FrpsctlError(1) 而不是 UsageError(2)：systemctl 执行失败不是
        # "参数写错了"，脚本据此区分"重试可能有用"与"命令本身无效"。
        raise FrpsctlError(
            f"systemctl {' '.join(args)} 失败：{detail or proc.returncode}",
            hint="确认 unit 名与权限；systemd 操作通常需要 root",
        )


def _require_root(action: str) -> None:
    if os.geteuid() != 0:
        raise PermissionRequired(f"{action}需要 root 权限", hint="用 sudo 重新运行该命令")


@dataclass
class PluginService:
    """插件服务的 systemd 集成（`frpsctl plugin service install`）。

    独立于 `Systemd`（unit 模板与生命周期语义不同：插件是独立进程、独立风险
    面，README 要求 `Restart=always`），但复用同一套体检与 systemctl 委托。
    模板 `frpsctl-plugin@.service`，实例名映射为 `%i`，与 `frps@.service` 对齐。
    """

    inst: Instance
    unit_dir: Path = SYSTEMD_UNIT_DIR

    # --- 单元名 --------------------------------------------------------

    @property
    def unit_name(self) -> str:
        return f"frpsctl-plugin@{self.inst.name}.service"

    @property
    def template_path(self) -> Path:
        return self.unit_dir / "frpsctl-plugin@.service"

    @property
    def available(self) -> bool:
        return shutil.which("systemctl") is not None and self.unit_dir.exists()

    # --- 查询 ----------------------------------------------------------

    def is_active(self) -> bool:
        if not self.available:
            return False
        if self._run("cat", "--no-pager", self.unit_name).returncode != 0:
            return False
        return self._run("is-active", self.unit_name).stdout.strip() == "active"

    def main_pid(self) -> int | None:
        out = self._run("show", "-p", "MainPID", "--value", self.unit_name)
        text = out.stdout.strip()
        return int(text) if text.isdigit() and int(text) > 0 else None

    # --- 变更 ----------------------------------------------------------

    def start(self) -> None:
        self._require_root("启动插件服务")
        self._run_checked("start", self.unit_name)

    def stop(self) -> None:
        self._require_root("停止插件服务")
        self._run_checked("stop", self.unit_name)

    def restart(self) -> None:
        self._require_root("重启插件服务")
        self._run_checked("restart", self.unit_name)

    # --- 安装 ----------------------------------------------------------

    def install_template(
        self,
        *,
        exec_start: Path,
        policy: Path,
        bind: str = "127.0.0.1:8080",
        handler_path: str = "/handler",
        force: bool = False,
        user: str = DEFAULT_SERVICE_USER,
        group: str | None = None,
    ) -> Path:
        """安装 `frpsctl-plugin@.service` 并 `daemon-reload` + `enable`。**需要 root**。

        与 frps 的 `service install` 同样做前置体检（账户 / 可执行性 / 家目录 /
        策略文件），理由相同：装一个起不来的 unit 比不装更浪费时间，而错误
        现场与安装动作相隔很远。
        """
        self._require_root("安装插件 unit")
        if self.template_path.exists() and not force:
            raise UsageError(
                f"{self.template_path} 已存在",
                hint="确认要覆盖请加 --force（会覆盖同名的自定义 unit）",
            )

        # 插件协议没有任何认证（§11.2.1）：非回环地址直接拒绝，与运行期同一条判据。
        if not is_loopback(bind):
            raise UsageError(
                f"插件拒绝绑定非回环地址：{bind}",
                hint=(
                    "frp 的插件协议没有任何认证，任何能访问该端口的人都能伪造 "
                    "Login/NewProxy 事件。请绑 127.0.0.1"
                ),
            )

        accounts = _account_ids(user, group)
        if accounts is None:
            raise UsageError(
                f"系统用户或组不存在：{user}/{group or user}",
                hint=(
                    "先创建专用用户（推荐）："
                    f"sudo useradd --system --no-create-home --shell /usr/sbin/nologin {user}；"
                    "或用 --user / --group 指定已有账户"
                ),
            )
        uid, gid = accounts

        problem = _access_problem(exec_start, uid=uid, gid=gid)
        if problem is not None:
            raise UsageError(
                f"服务用户 {user!r} 无法执行 frpsctl：{problem}",
                hint=(
                    "pipx 默认装在 ~/.local/bin，会被 unit 的 ProtectHome=true 挡住；"
                    "请改装到系统路径（`sudo pipx install --global frpsctl` 或 "
                    "`sudo pip install frpsctl`），或调整权限"
                ),
            )
        for label, path in (("frpsctl", exec_start), ("实例目录", self.inst.dir)):
            home_prefix = _protect_home_conflict(path)
            if home_prefix is not None:
                raise UsageError(
                    f"{label}位于 {home_prefix} 下（{path}），会被 unit 的 ProtectHome=true 挡住",
                    hint=(
                        "把 frpsctl 与数据放到系统路径：`sudo pipx install --global frpsctl` "
                        "且用 `sudo FRPSCTL_DATA_HOME=/opt/frpsctl frpsctl ...`"
                    ),
                )
        if not policy.exists():
            raise UsageError(
                f"策略文件不存在：{policy}",
                hint="先运行 `frpsctl plugin init` 生成策略模板（或用 --policy 指定）",
            )

        _hand_over_instance(self.inst, uid=uid, gid=gid)

        # 同 Systemd.install_template：全部 resolve——systemd 不接受相对路径。
        content = render_plugin_unit(
            exec_start=str(exec_start.resolve()),
            bind=bind,
            handler_path=handler_path,
            policy=policy.resolve(),
            workdir=self.inst.dir.resolve(),
            user=user,
            group=group,
        )
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        self.template_path.write_text(content, "utf-8")
        self.template_path.chmod(0o644)
        self._run_checked("daemon-reload")
        self._run_checked("enable", self.unit_name)
        return self.template_path

    def uninstall(self) -> None:
        """停用并删除插件 unit 模板。**需要 root**。"""
        self._require_root("卸载插件 unit")
        self._run("disable", "--now", self.unit_name)
        if self.template_path.exists():
            self.template_path.unlink()
        self._run_checked("daemon-reload")

    # --- 内部 ----------------------------------------------------------

    def _run(self, *args: str, timeout: float = 10) -> subprocess.CompletedProcess:
        return _systemctl(*args, timeout=timeout)

    def _run_checked(self, *args: str) -> None:
        _systemctl_checked(*args)

    @staticmethod
    def _require_root(action: str) -> None:
        _require_root(action)


# ---------------------------------------------------------------------------
# 部署体检（纯函数，便于测试）
# ---------------------------------------------------------------------------


def _account_ids(user: str, group: str | None) -> tuple[int, int] | None:
    """查系统账户的 (uid, gid)；用户或组不存在返回 None。"""
    try:
        record = pwd.getpwnam(user)
    except KeyError:
        return None
    uid = record.pw_uid
    gid = record.pw_gid
    if group:
        try:
            gid = grp.getgrnam(group).gr_gid
        except KeyError:
            return None
    return uid, gid


def _mode_for(st: os.stat_result, *, uid: int, gid: int) -> int:
    """按目标用户的身份取权限三元组（owner → group → other，与内核一致）。"""
    if st.st_uid == uid:
        return (st.st_mode >> 6) & 0o7
    if st.st_gid == gid:
        return (st.st_mode >> 3) & 0o7
    return st.st_mode & 0o7


def _access_problem(path: Path, *, uid: int, gid: int) -> str | None:
    """目标用户能否执行该路径？返回问题描述，None 表示可达。

    逐级检查路径元素（含文件自身）：目录需要 `x` 才能穿过，文件需要 `x`
    才能执行。这能精确抓出"二进制落在 /root 或某个 0700 家目录下"这类
    部署失败——`stat` 直接给出权限位，不依赖当前进程的身份。
    """
    target = path.resolve()
    for element in (target, *target.parents):
        try:
            st = element.stat()
        except OSError:
            return f"{element} 不存在或无法 stat"
        if not _mode_for(st, uid=uid, gid=gid) & 0o1:
            kind = "文件" if element == target else "目录"
            return f"{kind} {element} 对服务用户缺少执行（x）权限"
    return None


def _protect_home_conflict(path: Path) -> str | None:
    """路径是否会被 unit 的 `ProtectHome=true` 挡住？返回冲突的家目录前缀。

    `ProtectHome=true` 会让 `/home`、`/root`、`/run/user` 对服务进程不可见
    ——权限位检查（`_access_problem`）看不出这一点，因为这是 systemd 的挂载
    隔离，不是文件模式。唯一可靠的做法是**识别路径位置**并提前拒绝。
    """
    text = str(path.resolve())
    for prefix in ("/home/", "/root/", "/run/user/"):
        if text.startswith(prefix) or text == prefix.rstrip("/"):
            return prefix
    return None


def _ensure_log_dir(log_dir: Path, *, uid: int, gid: int) -> None:
    """保证 `ReadWritePaths` 的目录存在且服务用户可写。

    systemd 在 `ProtectSystem=strict` 下会拒绝挂载一个不存在的 ReadWritePaths
    ——表现为 unit 启动失败。因此"顺手建目录"不是锦上添花，是 unit 能起来的
    一部分；目录已存在时尝试交给服务用户（chown 失败不阻断：可能已经是正确的
    属主，随后的可写性检查会给出准确结论）。
    """
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageError(
            f"无法创建日志目录 {log_dir}：{exc}",
            hint="用 --log-dir 指定一个可创建的目录，或先手工创建它",
        ) from None
    with contextlib.suppress(OSError):
        os.chown(log_dir, uid, gid)
    st = log_dir.stat()
    if _mode_for(st, uid=uid, gid=gid) & 0o3 != 0o3:
        raise UsageError(
            f"日志目录对服务用户不可写：{log_dir}",
            hint=(
                f"chown {uid}:{gid} {log_dir} 并确保属主有写权限，"
                "或用 --log-dir 指定其他目录"
            ),
        )


def _hand_over_instance(inst: Instance, *, uid: int, gid: int) -> None:
    """把实例目录（含配置与快照目录）移交给服务用户。

    为什么必须做：实例目录是 0700（§6，内含 token 与 dashboard 口令），而
    `init` 由 root 执行时属主是 root。不移交的话，以服务用户运行的 frps 在
    `ProtectSystem=strict` 下连 `frps.toml` 都读不到，unit 必然启动失败。

    安全性不降级：目录仍是 0700，只是属主从 root 换成专用的服务用户——
    同机其他用户依然读不到，而 root 不受权限位限制、后续运维照常。
    """
    targets = [inst.dir, inst.history_dir, inst.startup_dir]
    # `--config` 指向实例目录之外时**不碰那个文件**：unit 的 ExecStart 固定
    # 使用实例内的 frps.toml，那个外部文件与 systemd 托管无关，动它属于越界。
    if inst.config.exists() and inst.config.parent == inst.dir:
        targets.append(inst.config)
    for path in targets:
        if not path.exists():
            continue
        try:
            os.chown(path, uid, gid)
        except OSError:
            st = path.stat()
            if st.st_uid != uid or st.st_gid != gid:
                raise FrpsctlError(
                    f"无法把实例路径移交给服务用户：{path}",
                    hint="确认当前用户有权限（需要 root），或检查文件系统是否支持 chown",
                ) from None


@dataclass
class Systemd:
    """单个实例的 systemd 委托层。构造不做 I/O。"""

    inst: Instance
    unit_dir: Path = SYSTEMD_UNIT_DIR

    # --- 单元名 --------------------------------------------------------

    @property
    def unit_name(self) -> str:
        """`frps@<name>.service` —— 实例名就是 systemd 的 `%i`。"""
        return f"frps@{self.inst.name}.service"

    @property
    def template_path(self) -> Path:
        return self.unit_dir / "frps@.service"

    @property
    def available(self) -> bool:
        """systemctl 存在且能读到 unit 目录（容器里通常不满足）。"""
        return shutil.which("systemctl") is not None and self.unit_dir.exists()

    # --- 查询 ----------------------------------------------------------

    def is_active(self) -> bool:
        """unit 是否存在且处于 active。这是 `owner == SYSTEMD` 的唯一依据。"""
        if not self.available:
            return False
        # is-enabled 会因 unit 不存在而报错，先用 cat 判断存在性更稳
        if not self._unit_exists():
            return False
        return self._run("is-active", timeout=5).stdout.strip() == "active"

    def _unit_exists(self) -> bool:
        return self._run("cat", "--no-pager", self.unit_name, timeout=5).returncode == 0

    def main_pid(self) -> int | None:
        out = self._run("show", "-p", "MainPID", "--value", self.unit_name, timeout=5)
        text = out.stdout.strip()
        return int(text) if text.isdigit() and int(text) > 0 else None

    def same_config_active(self) -> bool:
        """是否有 unit 正用**同一份配置**跑着（防止 direct 与 systemd 双起）。

        比对 `ExecStart` 里的 `-c <config>` 路径，而不是 unit 名——用户完全可能
        自建一个名字不同的 unit 指向我们的配置。

        扫描范围是**全部 active service**，不按 unit 名过滤：此前 glob `frps*`
        只能扫到以 frps 开头的 unit，自建 unit（例如 `my-tunnel.service`）指向
        我们的配置时漏检，双起防护有洞。非 active 的 unit 不可能造成双起，
        因此先让 systemd 用 `--state=active` 过滤（通常只剩几十个），再逐个
        查 ExecStart。
        """
        if not self.available:
            return False
        out = self._run(
            "list-units",
            "--type=service",
            "--state=active",
            "--no-legend",
            "--plain",
            timeout=5,
        )
        for line in out.stdout.splitlines():
            parts = line.split()
            unit = parts[0] if parts else ""
            if not unit or not unit.endswith(".service"):
                continue
            show = self._run("show", "-p", "ExecStart", "--value", unit, timeout=5)
            if str(self.inst.config) in show.stdout:
                return True
        return False

    def journal_argv(self, *, lines: int = 100, follow: bool = False) -> list[str]:
        """构造查看该 unit 日志的 journalctl argv（CLI 负责执行）。

        分层约束：`core/` 不打印、不接管终端——这里只产出 argv。unit 级日志
        （启动失败、OOM、权限拒绝）只在 journald 里，`frpsctl log` 看不到。
        """
        argv = ["journalctl", "-u", self.unit_name, "-n", str(lines), "--no-pager"]
        if follow:
            argv.append("-f")
        return argv

    # --- 变更 ----------------------------------------------------------

    def start(self) -> None:
        self._require_root("启动 systemd 托管的实例")
        self._run_checked("start", self.unit_name)

    def stop(self) -> None:
        self._require_root("停止 systemd 托管的实例")
        self._run_checked("stop", self.unit_name)

    def restart(self) -> None:
        self._require_root("重启 systemd 托管的实例")
        self._run_checked("restart", self.unit_name)

    # --- 安装 ----------------------------------------------------------

    def install_template(
        self,
        *,
        binary: Path,
        log_dir: Path,
        force: bool = False,
        user: str = DEFAULT_SERVICE_USER,
        group: str | None = None,
    ) -> Path:
        """安装 `frps@.service` 模板并 `daemon-reload`。**需要 root**。

        渲染之前先做三项体检（见模块文档）：账户存在、二进制对服务用户可达、
        日志目录可写。任何一项不满足都当场拒绝——装一个起不来的 unit 比不装
        更浪费时间。
        """
        self._require_root("安装 systemd unit")
        if self.template_path.exists() and not force:
            raise UsageError(
                f"{self.template_path} 已存在",
                hint="确认要覆盖请加 --force（会覆盖同名的自定义 unit）",
            )

        accounts = _account_ids(user, group)
        if accounts is None:
            raise UsageError(
                f"系统用户或组不存在：{user}/{group or user}",
                hint=(
                    "先创建专用用户（推荐）："
                    f"sudo useradd --system --no-create-home --shell /usr/sbin/nologin {user}；"
                    "或用 --user / --group 指定已有账户"
                ),
            )
        uid, gid = accounts

        problem = _access_problem(binary, uid=uid, gid=gid)
        if problem is not None:
            raise UsageError(
                f"服务用户 {user!r} 无法执行 ExecStart 的二进制：{problem}",
                hint=(
                    "unit 以专用用户运行，二进制必须对它是可执行的。"
                    "常见原因是二进制落在 /root 或某个 0700 家目录下（sudo 场景）；"
                    "请用共享数据目录重装："
                    "`sudo FRPSCTL_DATA_HOME=/opt/frpsctl frpsctl install`，"
                    "或调整该路径权限"
                ),
            )

        # ProtectHome=true 是挂载隔离，权限位检查看不出来——只能识别路径位置。
        for label, path in (("二进制", binary), ("实例目录", self.inst.dir)):
            home_prefix = _protect_home_conflict(path)
            if home_prefix is not None:
                raise UsageError(
                    f"{label}位于 {home_prefix} 下（{path}），会被 unit 的 ProtectHome=true 挡住",
                    hint=(
                        "把数据放到系统路径（如 /opt/frpsctl、/etc/frps/instances）再安装托管："
                        "`sudo FRPSCTL_DATA_HOME=/opt/frpsctl frpsctl install`"
                    ),
                )

        unit_config = self.inst.dir / "frps.toml"
        if not unit_config.exists():
            raise UsageError(
                f"unit 需要的配置文件不存在：{unit_config}",
                hint=(
                    "ExecStart 固定使用实例目录内的 frps.toml。先 `frpsctl init`，"
                    "或把配置放到该路径；若你用 --config 指向了别处，systemd 托管不会使用它"
                ),
            )

        _ensure_log_dir(log_dir, uid=uid, gid=gid)
        _hand_over_instance(self.inst, uid=uid, gid=gid)

        # 全部 **resolve**：systemd 要求 ExecStart / WorkingDirectory / ReadWritePaths
        # 是绝对路径。用户在 `--root ./instances` / `--binary ./bin/frps` 这类相对
        # 输入下会渲染出 `ExecStart=bin/frps`——安装成功，`systemctl start` 才报
        # "Executable path is not absolute"，错误现场与安装动作相隔很远（第五轮
        # review 实测复现）。
        content = render_unit(
            binary=str(binary.resolve()),
            config_dir=self.inst.instances_root.resolve(),
            log_dir=log_dir.resolve(),
            user=user,
            group=group,
        )
        # 先建目录：`unit_dir` 可被注入（测试）或指向一个尚未存在的自定义位置；
        # 真实部署里 /etc/systemd/system 通常存在，但没有理由依赖这一点。
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        self.template_path.write_text(content, "utf-8")
        self.template_path.chmod(0o644)
        self._run_checked("daemon-reload")
        self._run_checked("enable", self.unit_name)
        return self.template_path

    def uninstall(self) -> None:
        """停用并删除模板。**需要 root**。"""
        self._require_root("卸载 systemd unit")
        self._run("disable", "--now", self.unit_name)
        if self.template_path.exists():
            self.template_path.unlink()
        self._run_checked("daemon-reload")

    # --- 内部 ----------------------------------------------------------

    def _run(self, *args: str, timeout: float = 10) -> subprocess.CompletedProcess:
        return _systemctl(*args, timeout=timeout)

    def _run_checked(self, *args: str) -> None:
        _systemctl_checked(*args)

    @staticmethod
    def _require_root(action: str) -> None:
        _require_root(action)


@dataclass
class WebService:
    """Web 管理台的 systemd 集成（`frpsctl web service install`）。

    与 `PluginService` 同一套体检与委托；差异有三：

    - `Restart=on-failure`：管理台是交互工具（不是登录单点），正常停止不自启；
    - 安装时生成 **0600 的口令文件**并移交给服务用户（unit 只引用路径）；
    - 绑定非回环时 unit 会带 `--allow-non-loopback`（CLI 层要求显式开关）。
    """

    inst: Instance
    unit_dir: Path = SYSTEMD_UNIT_DIR

    # --- 单元名 --------------------------------------------------------

    @property
    def unit_name(self) -> str:
        return f"frpsctl-web@{self.inst.name}.service"

    @property
    def template_path(self) -> Path:
        return self.unit_dir / "frpsctl-web@.service"

    @property
    def password_file(self) -> Path:
        """登录口令文件（实例目录内，0600）。卸载时**保留**（它是数据不是 unit）。"""
        return self.inst.dir / "web-password"

    @property
    def available(self) -> bool:
        return shutil.which("systemctl") is not None and self.unit_dir.exists()

    # --- 查询 ----------------------------------------------------------

    def is_active(self) -> bool:
        if not self.available:
            return False
        if self._run("cat", "--no-pager", self.unit_name).returncode != 0:
            return False
        return self._run("is-active", self.unit_name).stdout.strip() == "active"

    def main_pid(self) -> int | None:
        out = self._run("show", "-p", "MainPID", "--value", self.unit_name)
        text = out.stdout.strip()
        return int(text) if text.isdigit() and int(text) > 0 else None

    # --- 变更 ----------------------------------------------------------

    def start(self) -> None:
        self._require_root("启动 Web 管理台")
        self._run_checked("start", self.unit_name)

    def stop(self) -> None:
        self._require_root("停止 Web 管理台")
        self._run_checked("stop", self.unit_name)

    def restart(self) -> None:
        self._require_root("重启 Web 管理台")
        self._run_checked("restart", self.unit_name)

    # --- 安装 ----------------------------------------------------------

    def install_template(
        self,
        *,
        exec_start: Path,
        bind: str = "127.0.0.1:8787",
        force: bool = False,
        user: str = DEFAULT_SERVICE_USER,
        group: str | None = None,
    ) -> tuple[Path, str]:
        """安装 `frpsctl-web@.service` 并准备口令文件。**需要 root**。

        返回 `(unit 路径, 首次生成的口令)`——口令只在**本次生成**时非空，
        供 CLI 打印一次；已存在时返回空串（重装不重打）。
        """
        self._require_root("安装 Web 管理台 unit")
        if self.template_path.exists() and not force:
            raise UsageError(
                f"{self.template_path} 已存在",
                hint="确认要覆盖请加 --force（会覆盖同名的自定义 unit）",
            )

        accounts = _account_ids(user, group)
        if accounts is None:
            raise UsageError(
                f"系统用户或组不存在：{user}/{group or user}",
                hint=(
                    "先创建专用用户（推荐）："
                    f"sudo useradd --system --no-create-home --shell /usr/sbin/nologin {user}；"
                    "或用 --user / --group 指定已有账户"
                ),
            )
        uid, gid = accounts

        problem = _access_problem(exec_start, uid=uid, gid=gid)
        if problem is not None:
            raise UsageError(
                f"服务用户 {user!r} 无法执行 frpsctl：{problem}",
                hint=(
                    "pipx 默认装在 ~/.local/bin，会被 unit 的 ProtectHome=true 挡住；"
                    "请改装到系统路径（`sudo pipx install --global frpsctl` 或 "
                    "`sudo pip install frpsctl`），或调整权限"
                ),
            )
        for label, path in (("frpsctl", exec_start), ("实例目录", self.inst.dir)):
            home_prefix = _protect_home_conflict(path)
            if home_prefix is not None:
                raise UsageError(
                    f"{label}位于 {home_prefix} 下（{path}），会被 unit 的 ProtectHome=true 挡住",
                    hint=(
                        "把 frpsctl 与数据放到系统路径：`sudo pipx install --global frpsctl` "
                        "且用 `sudo FRPSCTL_DATA_HOME=/opt/frpsctl frpsctl ...`"
                    ),
                )

        # 口令文件：不存在则生成（0600）。unit 只引用路径，明文不进 unit。
        from .config import atomic_write

        password_plain = ""
        if not self.password_file.exists():
            password_plain = generate_web_password()
            atomic_write(self.password_file, password_plain + "\n", mode=0o600)

        _hand_over_instance(self.inst, uid=uid, gid=gid)
        # 口令文件要能被服务用户读（_hand_over_instance 只处理目录与配置）
        with contextlib.suppress(OSError):
            os.chown(self.password_file, uid, gid)

        content = render_web_unit(
            exec_start=str(exec_start.resolve()),
            bind=bind,
            password_file=self.password_file.resolve(),
            workdir=self.inst.dir.resolve(),
            user=user,
            group=group,
            allow_non_loopback=not is_loopback(bind),
        )
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        self.template_path.write_text(content, "utf-8")
        self.template_path.chmod(0o644)
        self._run_checked("daemon-reload")
        self._run_checked("enable", self.unit_name)
        return self.template_path, password_plain

    def uninstall(self) -> None:
        """停用并删除 unit 模板。**需要 root**。口令文件保留（数据不属于 unit）。"""
        self._require_root("卸载 Web 管理台 unit")
        self._run("disable", "--now", self.unit_name)
        if self.template_path.exists():
            self.template_path.unlink()
        self._run_checked("daemon-reload")

    # --- 内部 ----------------------------------------------------------

    def _run(self, *args: str, timeout: float = 10) -> subprocess.CompletedProcess:
        return _systemctl(*args, timeout=timeout)

    def _run_checked(self, *args: str) -> None:
        _systemctl_checked(*args)

    @staticmethod
    def _require_root(action: str) -> None:
        _require_root(action)
