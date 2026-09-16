"""systemd 集成（设计文档 §12.2、ADR-1）。

**所有权语义**：一旦某个实例被 systemd 托管，CLI 的 `start/stop/restart/status`
全部委托 `systemctl`，**pid 文件不参与任何判定**（ADR-1）。两套机制并存且互相
不知情，会产生"CLI 说已停止、systemd 立刻又拉起来"这类自相矛盾。

unit 以模板形式安装（`frps@.service`），多实例即多 unit，与 §6 的实例模型
天然对齐：实例名映射为 `%i`。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import FrpsctlError, PermissionRequired, UsageError
from .instance import Instance

__all__ = ["Systemd", "UNIT_TEMPLATE_PATH", "SYSTEMD_UNIT_DIR", "render_unit"]

SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
UNIT_TEMPLATE_PATH = SYSTEMD_UNIT_DIR / "frps@.service"

#: 渲染用的 unit 模板（§12.2）。占位符只有 %i —— systemd 自己的实例说明符。
UNIT_TEMPLATE = """\
[Unit]
Description=frps service (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=frps
Group=frps
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
ReadWritePaths={log_dir}

[Install]
WantedBy=multi-user.target
"""


def render_unit(*, binary: str, config_dir: Path, log_dir: Path) -> str:
    """渲染 unit 模板。

    `ExecStart` 写的是**具体二进制路径**而不是 `bin/frps` 软链：软链换版本后
    systemd 不会自动重启，这条差异必须在 `install` 输出里显式提醒（§8.6.1）。
    """
    return UNIT_TEMPLATE.format(
        exec_start=binary,
        config_dir=config_dir,
        log_dir=log_dir,
    )


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
        """
        if not self.available:
            return False
        out = self._run("list-units", "--type=service", "--all", "--no-legend", "--plain", "frps*", timeout=5)
        for line in out.stdout.splitlines():
            unit = line.split()[0] if line.split() else ""
            if not unit:
                continue
            if self._run("is-active", unit, timeout=5).stdout.strip() != "active":
                continue
            show = self._run("show", "-p", "ExecStart", "--value", unit, timeout=5)
            if str(self.inst.config) in show.stdout:
                return True
        return False

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

    def install_template(self, *, binary: Path, log_dir: Path, force: bool = False) -> Path:
        """安装 `frps@.service` 模板并 `daemon-reload`。**需要 root**。"""
        self._require_root("安装 systemd unit")
        if self.template_path.exists() and not force:
            raise UsageError(
                f"{self.template_path} 已存在",
                hint="确认要覆盖请加 --force（会覆盖同名的自定义 unit）",
            )
        content = render_unit(
            binary=str(binary),
            config_dir=self.inst.instances_root,
            log_dir=log_dir,
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
        return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=timeout)

    def _run_checked(self, *args: str) -> None:
        proc = self._run(*args)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            # 用 FrpsctlError(1) 而不是 UsageError(2)：systemctl 执行失败不是
            # "参数写错了"，脚本据此区分"重试可能有用"与"命令本身无效"。
            raise FrpsctlError(
                f"systemctl {' '.join(args)} 失败：{detail or proc.returncode}",
                hint="确认 unit 名与权限；systemd 操作通常需要 root",
            )

    @staticmethod
    def _require_root(action: str) -> None:
        import os

        if os.geteuid() != 0:
            raise PermissionRequired(
                f"{action}需要 root 权限",
                hint="用 sudo 重新运行该命令",
            )
