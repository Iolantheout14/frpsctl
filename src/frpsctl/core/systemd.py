"""systemd 集成（设计文档 §12.2、ADR-1）。

**所有权语义**：一旦某个实例被 systemd 托管，CLI 的 `start/stop/restart/status`
全部委托 `systemctl`，**pid 文件不参与任何判定**（ADR-1）。两套机制并存且互相
不知情，会产生"CLI 说已停止、systemd 立刻又拉起来"这类自相矛盾。

unit 以模板形式安装（`frps@.service`），多实例即多 unit，与 §6 的实例模型
天然对齐：实例名映射为 `%i`。

**部署体检（install_template 的前置检查）**：unit 装上去只是一半，能不能起来
取决于四个环境事实，缺任何一项都会在 `systemctl start` 时炸——而那时错误信息
与安装命令相隔很远。因此把它们搬进安装流程，当场拒绝：

1. `User`/`Group` 必须存在（否则 systemd 报 "Failed to determine user
   credentials"）；
2. `ExecStart` 的二进制对服务用户必须可达（`sudo frpsctl install` 会把二进制
   放进 `/root/...`，而 `/root` 是 0700——服务用户读不到，这是最隐蔽的一类
   部署失败）；
3. `ReadWritePaths` 的日志目录必须存在（`ProtectSystem=strict` 下 systemd
   拒绝挂载不存在的路径）；
4. 二进制与实例目录不能落在 `/home`、`/root`、`/run/user` 下
   （`ProtectHome=true` 是挂载隔离，权限位检查看不出来）。

**服务账户解析（§12.2，v0.3.2）**：`frps` 不再是写死的唯一默认——任何系统账户
都可以作为服务用户：

- `--user` 显式给出 → 用它（支持用户名与数字 UID，数字会解析为账户名）；
- 缺省 → 系统已存在 `frps` 用户则用它（向后兼容），否则用**当前有效用户**
  （root 部署零配置即可安装）；
- `--group` 缺省 → 同名组；同名组不存在时回退用户的**主组**（可见提示）；
- `--create-user` → 目标用户缺失时以 `useradd --system` 自动创建（需 root；
  须配合显式 `--user`）。

安装参数（user/group/log_dir）会写入实例目录的 `service.json` 留档，供卸载
提示与 `doctor` 部署检查跟随实际配置——此前这些参数只存在于 unit 文件里，
工具自身在下游完全失明。
"""

from __future__ import annotations

import contextlib
import grp
import json
import os
import pwd
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path

from ..errors import FrpsctlError, PermissionRequired, UsageError
from .healthcheck import is_loopback
from .instance import SERVICE_MANIFEST_NAME, Instance

__all__ = [
    "Systemd",
    "PluginService",
    "WebService",
    "ServiceIdentity",
    "UNIT_TEMPLATE_PATH",
    "SYSTEMD_UNIT_DIR",
    "DEFAULT_SERVICE_USER",
    "DEFAULT_LOG_DIR",
    "SERVICE_MANIFEST_NAME",
    "resolve_service_identity",
    "ensure_service_account",
    "read_service_manifest",
    "remove_service_record",
    "read_template_user",
    "read_template_exec",
    "show_unit_accounts",
    "render_unit",
    "render_plugin_unit",
    "render_web_unit",
    "generate_web_password",
]

SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
UNIT_TEMPLATE_PATH = SYSTEMD_UNIT_DIR / "frps@.service"
PLUGIN_UNIT_TEMPLATE_PATH = SYSTEMD_UNIT_DIR / "frpsctl-plugin@.service"
WEB_UNIT_TEMPLATE_PATH = SYSTEMD_UNIT_DIR / "frpsctl-web@.service"

#: `service install` 的默认日志目录（ReadWritePaths）。集中在这里定义，
#: CLI 选项与卸载提示共用同一条来源。
DEFAULT_LOG_DIR = Path("/var/log/frps")

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

#: 默认服务用户**候选**：仅当系统已存在该账户时，`--user` 缺省才解析到它。
#: 它不再是一条硬编码假设——系统没有 frps 用户时默认解析为当前有效用户
#: （`resolve_service_identity`，§12.2 v0.3.2）。
DEFAULT_SERVICE_USER = "frps"

#: 自动创建系统账户时允许的名字（与 useradd/groupadd 的保守交集）。
_ACCOUNT_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

#: unit 文件里 `User=`/`Group=` 允许的 token。比账户名宽（系统里可能存在
#: `user@domain`、大写、点号等形态），但**绝不允许**换行/空格/等号——unit
#: 是逐行解析的文本，一个换行就能注入任意指令。
_IDENTITY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@+-]{0,63}$")

@dataclass(frozen=True)
class ServiceIdentity:
    """服务用户/组的解析结果（`resolve_service_identity` 的返回值）。

    `uid`/`gid` 为 None 表示"解析出了名字，但账户尚不存在"（`--create-user`
    的创建前状态）；`ensure_service_account` 把它推进到全部非空。
    """

    user: str
    group: str
    uid: int | None
    gid: int | None
    #: 来源：`explicit`（--user 给出）/ `default-frps` / `default-current`。
    source: str
    #: 本次调用是否真的创建了系统账户（--create-user 且账户原先不存在）。
    created: bool = False
    #: 需要展示给用户的说明（组回退等）。`core/` 不打印，由 CLI 决定展示。
    notes: tuple[str, ...] = ()

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
ExecStart={exec_start} --instance %i plugin serve --policy {policy} --bind {bind} --path {handler_path}{extra}
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
    access_log: bool = False,
) -> str:
    """渲染插件 unit 模板。

    `ExecStart` 写**具体路径**（不是 `frpsctl` 命令名）：systemd 不读 PATH。
    pipx 默认装到 `~/.local/bin`，那条路径会被 `ProtectHome=true` 挡住——
    由 `PluginService.install_template` 的体检在安装前拒绝并给出替代方案。

    `access_log` 对应 `plugin serve --access-log`（逐请求日志进 journald）；
    不开时模板与历史版本逐字节一致。
    """
    extra = " --access-log" if access_log else ""
    return PLUGIN_UNIT_TEMPLATE.format(
        exec_start=exec_start,
        bind=bind,
        handler_path=handler_path,
        policy=policy,
        workdir=workdir,
        user=user,
        group=group or user,
        extra=extra,
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
    trusted_proxy: bool = False,
    access_log: bool = False,
    metrics: bool = False,
) -> str:
    """渲染 Web 管理台 unit 模板。

    口令走 `--password-file`（0600），**绝不写进 unit 命令行**——unit 文件是
    0644，写明文等于向本机所有用户公开管理台。`--allow-non-loopback`、
    `--trusted-proxy` 与 `--access-log` 只在显式要求时渲染（CLI 层已做前置
    校验）。
    """
    flags: list[str] = []
    if allow_non_loopback:
        flags.append("--allow-non-loopback")
    if trusted_proxy:
        flags.append("--trusted-proxy")
    if access_log:
        flags.append("--access-log")
    if metrics:
        flags.append("--metrics")
    return WEB_UNIT_TEMPLATE.format(
        exec_start=exec_start,
        bind=bind,
        password_file=password_file,
        workdir=workdir,
        user=user,
        group=group or user,
        extra=f" {' '.join(flags)}" if flags else "",
    )


# ---------------------------------------------------------------------------
# systemctl 原语（Systemd 与 PluginService 共用一份实现）
# ---------------------------------------------------------------------------


def _systemctl(*args: str, timeout: float = 10) -> subprocess.CompletedProcess:
    """执行一次 systemctl。**超时必须收口**。

    `subprocess.TimeoutExpired` 既不是 `FrpsctlError` 也不是 `OSError`，放任它
    冒出去会在 CLI 顶层变成"未分类错误(1)"——把"systemd 无响应"误报成"工具
    内部出错"（与 `release._verify_binary` 在 v0.2.1 修过的是同一类缺口，
    systemd 一侧此前一直敞着）。收口为契约内异常后，调用方可以分别决定：
    查询路径降级并如实报告，变更路径 fail-closed 拒绝。
    """
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FrpsctlError(
            f"systemctl {' '.join(args)} 超时未返回（{timeout:g} 秒）",
            hint="systemd 可能无响应；用 `systemctl is-system-running` 确认后重试",
        ) from None
    except OSError as exc:
        # systemctl 在 `available` 检查与执行之间被删除/失去执行权限（罕见
        # 竞态）。必须同样收口：`resolve_owner` 只捕获 FrpsctlError，裸 OSError
        # 会让 status 崩成"未分类错误(1)"（v0.3.1 review 复查）。
        raise FrpsctlError(
            f"无法执行 systemctl：{exc}",
            hint="确认 systemd 已安装且在 PATH 中",
        ) from None


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

    def unit_exists(self) -> bool:
        """本实例的 unit 是否存在（`systemctl cat` 成功）。"""
        if not self.available:
            return False
        return self._run("cat", "--no-pager", self.unit_name).returncode == 0

    def is_active(self) -> bool:
        if not self.available:
            return False
        if not self.unit_exists():
            return False
        return self._run("is-active", self.unit_name).stdout.strip() == "active"

    def is_enabled(self) -> bool:
        """unit 是否处于 enabled（开机自启）——卸载时它同样需要被停掉。

        **与 `template_path.exists()` 的区别**：模板是全部实例共享的，模板存在
        不代表**本实例**的 unit 存在；只有 `is-enabled` 才对"本实例的 unit"
        有发言权——否则会对从未用过 systemd 的实例产生假警告（实测复现过）。
        """
        if not self.available:
            return False
        if not self.unit_exists():
            return False
        return self._run("is-enabled", self.unit_name).stdout.strip() in ("enabled", "enabled-runtime")

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
        user: str | None = None,
        group: str | None = None,
        access_log: bool = False,
    ) -> Path:
        """安装 `frpsctl-plugin@.service` 并 `daemon-reload` + `enable`。**需要 root**。

        与 frps 的 `service install` 同样做前置体检（账户 / 可执行性 / 家目录 /
        策略文件），理由相同：装一个起不来的 unit 比不装更浪费时间，而错误
        现场与安装动作相隔很远。`user`/`group` 缺省时经
        `resolve_service_identity` 解析（§12.2）。
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

        identity = resolve_service_identity(user, group)
        accounts = _account_ids(identity.user, identity.group)
        if accounts is None:
            raise UsageError(
                f"系统用户或组不存在：{identity.user}/{identity.group}",
                hint=(
                    "① 加 --create-user 自动创建系统账户（需 root，须配合 --user）；"
                    "② 用 --user/--group 指定已有账户；"
                    "③ 不带 --user 时默认优先 frps、其次当前用户"
                ),
            )
        uid, gid = accounts
        user, group = identity.user, identity.group

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
            access_log=access_log,
        )
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        self.template_path.write_text(content, "utf-8")
        self.template_path.chmod(0o644)
        self._run_checked("daemon-reload")
        self._run_checked("enable", self.unit_name)
        _record_service(
            self.inst,
            "plugin",
            user=identity.user,
            group=identity.group,
            bind=bind,
            policy=str(policy.resolve()),
            workdir=str(self.inst.dir.resolve()),
            unit=self.unit_name,
            template=str(self.template_path),
        )
        return self.template_path

    def disable(self) -> None:
        """停用**本实例**的 unit（`disable --now`），**不删共享模板**。需要 root。

        unit 模板（`frpsctl-plugin@.service`）是所有实例共享的：多实例环境里
        卸载单个实例时删模板会连累其他实例，因此拆出这个"只停用"的入口。
        """
        self._require_root("停用插件 unit")
        self._run_checked("disable", "--now", self.unit_name)

    def uninstall(self) -> None:
        """停用并删除插件 unit 模板。**需要 root**。"""
        self._require_root("卸载插件 unit")
        self._run("disable", "--now", self.unit_name)
        if self.template_path.exists():
            self.template_path.unlink()
        self._run_checked("daemon-reload")
        remove_service_record(self.inst, "plugin")

    # --- 内部 ----------------------------------------------------------

    def _run(self, *args: str, timeout: float = 10) -> subprocess.CompletedProcess:
        return _systemctl(*args, timeout=timeout)

    def _run_checked(self, *args: str) -> None:
        _systemctl_checked(*args)

    @staticmethod
    def _require_root(action: str) -> None:
        _require_root(action)


# ---------------------------------------------------------------------------
# 服务账户解析（§12.2 v0.3.2：任何用户都可以是服务用户）
# ---------------------------------------------------------------------------


def _user_record(name: str) -> pwd.struct_passwd | None:
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def _group_record(name: str) -> grp.struct_group | None:
    try:
        return grp.getgrnam(name)
    except KeyError:
        return None


def _validate_identity_token(value: str, *, what: str) -> str:
    if not _IDENTITY_TOKEN_RE.match(value):
        raise UsageError(
            f"非法的{what}：{value!r}",
            hint="只允许字母、数字与 _ . @ + -（unit 文件按行解析，不接受空格/换行/等号）",
        )
    return value


def resolve_service_identity(user: str | None, group: str | None = None) -> ServiceIdentity:
    """把 `--user`/`--group` 解析成具体的 (用户, 组)（§12.2）。

    解析规则（**不要求账户存在**——存在性由 `ensure_service_account` 推进）：

    1. `user` 为 None → 系统已存在 `frps` 用户则用它（向后兼容现有部署），
       否则用当前有效用户（root 部署零配置可安装）；
    2. `user` 为纯数字 → 按 UID 解析为账户名（UID 不存在则拒绝——无账户的
       裸 UID 无法被 `systemctl show` / `userdel` 等下游一致处理）；
    3. `group` 为 None → 用户的同名组；同名组不存在时回退**用户主组**
       （记入 `notes`，不静默）；
    4. `group` 为纯数字 → 按 GID 解析为组名。

    `frps` 只在这里作为"候选"出现一次：系统没有它时不影响任何功能。
    """
    notes: list[str] = []
    source = "explicit"
    if user is None:
        if _user_record(DEFAULT_SERVICE_USER) is not None:
            user = DEFAULT_SERVICE_USER
            source = "default-frps"
        else:
            try:
                user = pwd.getpwuid(os.geteuid()).pw_name
            except KeyError:  # 账户数据库异常：不猜测，明确拒绝
                raise FrpsctlError(
                    f"无法解析当前用户（uid={os.geteuid()}）",
                    hint="系统的 passwd 数据库不完整；用 --user 显式指定服务账户",
                ) from None
            source = "default-current"
            notes.append(
                f"系统没有 {DEFAULT_SERVICE_USER!r} 用户，默认使用当前用户 {user!r}"
                "（--user 可指定其它账户）"
            )
    elif user.isdigit():
        record = None
        with contextlib.suppress(KeyError):
            record = pwd.getpwuid(int(user))
        if record is None:
            raise UsageError(
                f"指定的用户 ID 不存在：{user}",
                hint="换一个已存在的 UID，或改用账户名 / --user <名字> --create-user 创建账户",
            )
        user = record.pw_name
    _validate_identity_token(user, what="服务用户")
    record = _user_record(user)

    if group is not None:
        if group.isdigit():
            entry = None
            with contextlib.suppress(KeyError):
                entry = grp.getgrgid(int(group))
            if entry is None:
                raise UsageError(f"指定的组 ID 不存在：{group}", hint="换一个已存在的 GID 或改用组名")
            group = entry.gr_name
        _validate_identity_token(group, what="服务组")
        entry = _group_record(group)
        return ServiceIdentity(
            user=user,
            group=group,
            uid=record.pw_uid if record is not None else None,
            gid=entry.gr_gid if entry is not None else None,
            source=source,
            notes=tuple(notes),
        )

    if record is not None:
        same = _group_record(user)
        if same is not None:
            return ServiceIdentity(
                user, user, record.pw_uid, same.gr_gid, source, notes=tuple(notes)
            )
        primary = None
        with contextlib.suppress(KeyError):
            primary = grp.getgrgid(record.pw_gid)
        if primary is None:
            raise UsageError(
                f"无法解析用户 {user!r} 的主组（gid={record.pw_gid}）",
                hint="系统的 group 数据库不完整；用 --group 显式指定一个已存在的组",
            )
        notes.append(f"没有与用户同名的组 {user!r}，使用其主组 {primary.gr_name!r}")
        return ServiceIdentity(
            user, primary.gr_name, record.pw_uid, primary.gr_gid, source, notes=tuple(notes)
        )

    # 用户尚不存在（--create-user 的创建前状态）：组名默认与用户同名，
    # `useradd --user-group` 会随用户一并创建同名组。
    return ServiceIdentity(user, user, None, None, source, notes=tuple(notes))


def ensure_service_account(
    user: str | None, group: str | None = None, *, create_user: bool = False
) -> ServiceIdentity:
    """解析并确保服务账户存在；`create_user=True` 时自动创建缺失的系统用户。

    - `create_user` 必须配合**显式** `--user`：缺省解析（frps/当前用户）
      下用户必然已存在，"创建谁"无从谈起；
    - 创建的是系统账户（`useradd --system --no-create-home --shell
      /usr/sbin/nologin --user-group`），幂等；
    - 显式 `--group` 缺失时**不自动建组**（唯一例外是同名用户组，由
      `--user-group` 随用户一起创建）。
    """
    if create_user and user is None:
        raise UsageError(
            "--create-user 需要配合 --user 指定账户名",
            hint="例如：frpsctl service install --user frps --create-user",
        )
    identity = resolve_service_identity(user, group)
    if identity.uid is not None and identity.gid is not None:
        return identity
    # 显式 --group 缺失时**先拒绝**：`--create-user` 只创建用户（同名组随建），
    # 不能为一个注定失败的安装先去创建账户（review 发现的副作用顺序缺陷）。
    if group is not None and identity.gid is None:
        raise UsageError(
            f"系统组不存在：{identity.group}",
            hint="用 --group 指定已有组，或先 groupadd 创建；--create-user 只创建用户（不建其它组）",
        )
    # 到这里 gid 缺失只可能来自"用户不存在"（隐式组 = 用户名，随 --user-group 创建）。
    if not create_user:
        raise UsageError(
            f"系统用户或组不存在：{identity.user}/{identity.group}",
            hint=(
                "① 加 --create-user 自动创建系统账户（需 root，须配合 --user）；"
                "② 用 --user/--group 指定已有账户；"
                "③ 不带 --user 时默认优先 frps、其次当前用户"
            ),
        )
    _create_system_user(identity.user)
    rebuilt = resolve_service_identity(identity.user, group)
    if rebuilt.uid is None or rebuilt.gid is None:
        raise FrpsctlError(
            f"系统账户创建后仍无法解析：{rebuilt.user}/{rebuilt.group}",
            hint="用 `id <user>` 与 `getent group <group>` 检查账户数据库后重试",
        )
    return replace(rebuilt, created=True)


def _create_system_user(user: str) -> None:
    """以 `useradd --system` 创建服务账户（幂等；需 root）。"""
    if not _ACCOUNT_NAME_RE.match(user):
        raise UsageError(
            f"不能自动创建系统用户：{user!r} 不是规范的系统账户名",
            hint=(
                "系统账户名只允许小写字母、数字与 _ -（以字母或 _ 开头，最长 32）；"
                "先手工创建，或改用规范名字"
            ),
        )
    _require_root("创建服务账户")
    useradd = shutil.which("useradd")
    if useradd is None:
        raise FrpsctlError(
            "找不到 useradd（shadow-utils 未安装）",
            hint=(
                "手工创建：sudo useradd --system --no-create-home "
                f"--shell /usr/sbin/nologin --user-group {user}"
            ),
        )
    argv = [
        useradd,
        "--system",
        "--no-create-home",
        "--shell",
        "/usr/sbin/nologin",
        "--user-group",
        user,
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise FrpsctlError(
            f"useradd 超时未返回（15 秒）：{user}",
            hint="检查系统负载或账户数据库锁（/etc/passwd.lock）后重试",
        ) from None
    except OSError as exc:
        raise FrpsctlError(
            f"无法执行 useradd：{exc}",
            hint="确认 shadow-utils 已安装且在 PATH 中",
        ) from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise FrpsctlError(
            f"创建系统用户失败（useradd 退出码 {proc.returncode}）：{detail or user}",
            hint=(
                "手工创建：sudo useradd --system --no-create-home "
                f"--shell /usr/sbin/nologin --user-group {user}"
            ),
        )


# ---------------------------------------------------------------------------
# 部署体检（纯函数，便于测试）
# ---------------------------------------------------------------------------


def _account_ids(user: str, group: str) -> tuple[int, int] | None:
    """查系统账户的 (uid, gid)；用户或组不存在返回 None。

    `group` 是**必填的具体组名**：unit 模板实际渲染的是 `group or user`，
    只查用户不查组会放行一个"用户存在但组不存在"的 unit——安装成功、
    `systemctl start` 才报 "Group not found"（v0.3.2 修复）。
    """
    record = _user_record(user)
    if record is None:
        return None
    entry = _group_record(group)
    if entry is None:
        return None
    return record.pw_uid, entry.gr_gid


# ---------------------------------------------------------------------------
# 服务安装留档（`<实例>/service.json`）：安装参数的快照
# ---------------------------------------------------------------------------
#
# 为什么需要它：unit 文件在 /etc/systemd/system 下、数据在实例目录，两边从
# 此脱节——卸载时不知道当初用了哪个服务账户与日志目录，doctor 也无法发现
# "账户被删/二进制被移走"。留档让工具在下游跟随**实际配置**而非默认假设。


def read_service_manifest(inst: Instance) -> tuple[dict, str | None]:
    """读安装留档。返回 `(数据, 错误描述)`；文件缺失 → `({}, None)`。

    损坏时不抛异常：卸载与 doctor 都要"如实报告而不是崩掉"——错误描述由
    调用方决定如何展示（doctor 报 WARN；卸载忽略并回退默认假设）。
    """
    path = inst.service_manifest
    try:
        raw = path.read_text("utf-8")
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, f"无法读取安装记录 {path}：{exc}"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return {}, f"安装记录不是合法 JSON：{path}（{exc}）"
    if not isinstance(data, dict):
        return {}, f"安装记录根节点必须是对象：{path}"
    return data, None


def _write_service_manifest(inst: Instance, data: dict) -> None:
    from .config import atomic_write

    try:
        atomic_write(
            inst.service_manifest,
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            mode=0o600,
        )
    except OSError as exc:
        raise FrpsctlError(
            f"安装记录写入失败：{inst.service_manifest}（{exc}）",
            hint="确认实例目录可写后重跑本命令（幂等）；unit 本身的状态不受影响",
        ) from None


def _record_service(inst: Instance, key: str, **fields: object) -> None:
    """把一次安装的参数写进留档（重复安装覆盖同一 key）。"""
    data, _ = read_service_manifest(inst)
    record = {k: v for k, v in fields.items() if v is not None}
    record["installed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    data[key] = record
    _write_service_manifest(inst, data)


def remove_service_record(inst: Instance, key: str) -> None:
    """删除某个服务的留档（对应 uninstall；文件空了就删掉）。"""
    data, _ = read_service_manifest(inst)
    if key not in data:
        return
    data.pop(key, None)
    if not data:
        with contextlib.suppress(FileNotFoundError):
            inst.service_manifest.unlink()
        return
    _write_service_manifest(inst, data)


# ---------------------------------------------------------------------------
# unit 文件读取（安装前警告 / doctor 部署检查）
# ---------------------------------------------------------------------------


def _template_value(path: Path, key: str) -> str | None:
    """读 unit 文件里 `key=` 行的值（第一个匹配）；读不到 → None。"""
    try:
        text = path.read_text("utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            value = line.split("=", 1)[1].strip()
            return value or None
    return None


def read_template_user(path: Path) -> str | None:
    """已安装 unit 里的 `User=`（覆盖前做"影响全部实例"警告用）。"""
    return _template_value(path, "User")


def read_template_exec(path: Path) -> str | None:
    """unit 的 `ExecStart=` 可执行路径（第一个 token；doctor 可达性检查用）。"""
    value = _template_value(path, "ExecStart")
    if not value:
        return None
    return value.split()[0]


def show_unit_accounts(unit_name: str) -> tuple[str | None, str | None]:
    """`systemctl show -p User -p Group` 解析出 unit 实际使用的账户。

    解析**键值行**而不是 `--value`：多个属性时 `--value` 只输出值且顺序按
    属性名字母序，不可依赖。systemd 对未显式设置的 unit 会报默认值
    （User=root），因此这里拿到的是"实际生效值"而非"文件里写了什么"。
    """
    out = _systemctl("show", "-p", "User", "-p", "Group", unit_name)
    if out.returncode != 0:
        raise FrpsctlError(
            f"无法读取 unit 账户信息：systemctl show {unit_name} 失败",
            hint=(out.stderr or "").strip() or "确认 unit 存在且 systemd 可用",
        )
    values: dict[str, str] = {}
    for line in out.stdout.splitlines():
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values.get("User") or None, values.get("Group") or None


def _dir_access_problem(path: Path, *, uid: int, gid: int) -> str | None:
    """服务用户能否进入并可写该目录？**只读判定**（doctor 用，绝不修改系统）。

    unit 的 `ReadWritePaths`（日志目录 / 实例目录）被删除、属主或权限漂移时，
    `systemctl start` 会因挂载或写盘失败而起不来——doctor 提前报告。
    """
    try:
        st = path.stat()
    except OSError:
        return f"目录不存在或无法访问：{path}"
    if not path.is_dir():
        return f"不是目录：{path}"
    mode = _mode_for(st, uid=uid, gid=gid)
    if not mode & 0o1:
        return f"服务用户缺少进入（x）权限：{path}"
    if not mode & 0o2:
        return f"服务用户缺少写（w）权限：{path}"
    return None


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
        if not self.unit_exists():
            return False
        return self._run("is-active", timeout=5).stdout.strip() == "active"

    def is_enabled(self) -> bool:
        """unit 是否处于 enabled（开机自启）——卸载时它同样需要被停掉。

        **与 `template_path.exists()` 的区别**：模板是全部实例共享的，模板存在
        不代表**本实例**的 unit 存在；只有 `is-enabled` 才对"本实例的 unit"
        有发言权——否则会对从未用过 systemd 的实例产生假警告（实测复现过）。
        """
        if not self.available:
            return False
        if not self.unit_exists():
            return False
        return self._run("is-enabled", timeout=5).stdout.strip() in ("enabled", "enabled-runtime")

    def unit_exists(self) -> bool:
        """本实例的 unit 是否存在（`systemctl cat` 成功）。

        doctor 的部署检查与 `service status` 的账户展示用它判定"这个服务
        装过没有"——与 `template_path.exists()` 的区别见 `disable` 的注释
        （模板是共享的，存在不代表本实例的 unit 存在）。
        """
        if not self.available:
            return False
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
        units: list[str] = []
        for line in out.stdout.splitlines():
            # 行首可能有 `●`（failed 标记）等状态符号：取第一个像 unit 名的 token。
            unit = next((part for part in line.split() if part.endswith(".service")), "")
            if unit:
                units.append(unit)
        if not units:
            return False
        # 批量一次查询（v0.3.1）：此前逐个 unit `systemctl show`——多实例机器上
        # 30 个 active unit 就是 30 次子进程（~300ms），而且发生在 start 的锁内。
        show = self._run("show", "-p", "ExecStart", "--value", *units, timeout=10)
        return str(self.inst.config) in show.stdout

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
        user: str | None = None,
        group: str | None = None,
    ) -> Path:
        """安装 `frps@.service` 模板并 `daemon-reload`。**需要 root**。

        渲染之前先做体检（见模块文档）：账户存在、二进制对服务用户可达、
        日志目录可写。任何一项不满足都当场拒绝——装一个起不来的 unit 比不装
        更浪费时间。

        `user`/`group` 缺省时经 `resolve_service_identity` 解析（§12.2，任何
        用户都可以是服务用户）；账户创建（`--create-user`）由 CLI 层先行完成
        （`ensure_service_account`），这里只做存在性体检。
        """
        self._require_root("安装 systemd unit")
        if self.template_path.exists() and not force:
            raise UsageError(
                f"{self.template_path} 已存在",
                hint="确认要覆盖请加 --force（会覆盖同名的自定义 unit）",
            )

        identity = resolve_service_identity(user, group)
        accounts = _account_ids(identity.user, identity.group)
        if accounts is None:
            raise UsageError(
                f"系统用户或组不存在：{identity.user}/{identity.group}",
                hint=(
                    "① 加 --create-user 自动创建系统账户（需 root，须配合 --user）；"
                    "② 用 --user/--group 指定已有账户；"
                    "③ 不带 --user 时默认优先 frps、其次当前用户"
                ),
            )
        uid, gid = accounts
        # 后续渲染与提示统一使用解析后的身份（含组回退）
        user, group = identity.user, identity.group

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
        _record_service(
            self.inst,
            "frps",
            user=identity.user,
            group=identity.group,
            log_dir=str(log_dir.resolve()),
            unit=self.unit_name,
            template=str(self.template_path),
        )
        return self.template_path

    def disable(self) -> None:
        """停用**本实例**的 unit（`disable --now`），**不删共享模板**。需要 root。

        unit 模板（`frps@.service`）是所有实例共享的：多实例环境里卸载单个实例
        时删模板会连累其他实例，因此拆出这个"只停用"的入口。
        """
        self._require_root("停用 systemd unit")
        self._run_checked("disable", "--now", self.unit_name)

    def uninstall(self) -> None:
        """停用并删除模板。**需要 root**。"""
        self._require_root("卸载 systemd unit")
        self._run("disable", "--now", self.unit_name)
        if self.template_path.exists():
            self.template_path.unlink()
        self._run_checked("daemon-reload")
        remove_service_record(self.inst, "frps")

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

    def unit_exists(self) -> bool:
        """本实例的 unit 是否存在（`systemctl cat` 成功）。"""
        if not self.available:
            return False
        return self._run("cat", "--no-pager", self.unit_name).returncode == 0

    def is_active(self) -> bool:
        if not self.available:
            return False
        if not self.unit_exists():
            return False
        return self._run("is-active", self.unit_name).stdout.strip() == "active"

    def is_enabled(self) -> bool:
        """unit 是否处于 enabled（开机自启）——卸载时它同样需要被停掉。

        **与 `template_path.exists()` 的区别**：模板是全部实例共享的，模板存在
        不代表**本实例**的 unit 存在；只有 `is-enabled` 才对"本实例的 unit"
        有发言权——否则会对从未用过 systemd 的实例产生假警告（实测复现过）。
        """
        if not self.available:
            return False
        if not self.unit_exists():
            return False
        return self._run("is-enabled", self.unit_name).stdout.strip() in ("enabled", "enabled-runtime")

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
        user: str | None = None,
        group: str | None = None,
        trusted_proxy: bool = False,
        access_log: bool = False,
        metrics: bool = False,
    ) -> tuple[Path, str]:
        """安装 `frpsctl-web@.service` 并准备口令文件。**需要 root**。

        返回 `(unit 路径, 首次生成的口令)`——口令只在**本次生成**时非空，
        供 CLI 打印一次；已存在时返回空串（重装不重打）。`user`/`group`
        缺省时经 `resolve_service_identity` 解析（§12.2）。
        """
        self._require_root("安装 Web 管理台 unit")
        if self.template_path.exists() and not force:
            raise UsageError(
                f"{self.template_path} 已存在",
                hint="确认要覆盖请加 --force（会覆盖同名的自定义 unit）",
            )

        identity = resolve_service_identity(user, group)
        accounts = _account_ids(identity.user, identity.group)
        if accounts is None:
            raise UsageError(
                f"系统用户或组不存在：{identity.user}/{identity.group}",
                hint=(
                    "① 加 --create-user 自动创建系统账户（需 root，须配合 --user）；"
                    "② 用 --user/--group 指定已有账户；"
                    "③ 不带 --user 时默认优先 frps、其次当前用户"
                ),
            )
        uid, gid = accounts
        user, group = identity.user, identity.group

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
            trusted_proxy=trusted_proxy,
            access_log=access_log,
            metrics=metrics,
        )
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        self.template_path.write_text(content, "utf-8")
        self.template_path.chmod(0o644)
        self._run_checked("daemon-reload")
        self._run_checked("enable", self.unit_name)
        _record_service(
            self.inst,
            "web",
            user=identity.user,
            group=identity.group,
            bind=bind,
            unit=self.unit_name,
            template=str(self.template_path),
        )
        return self.template_path, password_plain

    def disable(self) -> None:
        """停用**本实例**的 unit（`disable --now`），**不删共享模板**。需要 root。

        unit 模板（`frpsctl-web@.service`）是所有实例共享的：多实例环境里卸载
        单个实例时删模板会连累其他实例，因此拆出这个"只停用"的入口。
        """
        self._require_root("停用 Web 管理台 unit")
        self._run_checked("disable", "--now", self.unit_name)

    def uninstall(self) -> None:
        """停用并删除 unit 模板。**需要 root**。口令文件保留（数据不属于 unit）。"""
        self._require_root("卸载 Web 管理台 unit")
        self._run("disable", "--now", self.unit_name)
        if self.template_path.exists():
            self.template_path.unlink()
        self._run_checked("daemon-reload")
        remove_service_record(self.inst, "web")

    # --- 内部 ----------------------------------------------------------

    def _run(self, *args: str, timeout: float = 10) -> subprocess.CompletedProcess:
        return _systemctl(*args, timeout=timeout)

    def _run_checked(self, *args: str) -> None:
        _systemctl_checked(*args)

    @staticmethod
    def _require_root(action: str) -> None:
        _require_root(action)
