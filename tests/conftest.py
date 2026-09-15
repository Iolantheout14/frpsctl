"""集成层共用的假 frps 二进制与实例夹具（设计文档 §13 集成层）。

**为什么需要假二进制**：集成层要验证的是 frpsctl 自己的编排——启动早退检测、
身份三重校验、状态判定、配置回滚——而不是 frp 的转发逻辑。用假二进制可以把
"frps 启动后立刻退出""frps 卡住不响应"这类**难以用真 frps 稳定复现**的场景
变成确定性用例。

契约层（`test_facts.py`）才用真二进制，两者职责不重叠。
"""

from __future__ import annotations

import functools
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from frpsctl.core.instance import Instance
from frpsctl.core.lifecycle import Lifecycle

FAKE_SOURCE = Path(__file__).parent / "fake_frps.py"

DEFAULT_VERSION = "0.71.0"


@dataclass
class FakeFrps:
    """一个可执行、可配置行为的假 frps。"""

    path: Path
    version: str
    mode: str
    #: True 表示这个假二进制**不提供** v2 Admin API（模拟 ADR-3 的 404 情形）
    no_v2: bool = False

    def env(self) -> dict[str, str]:
        """子进程环境。

        `FRPS_FAKE_VERSION` / `FRPS_FAKE_MODE` 每次显式设置，并先**剔除继承来的
        同名变量**：否则上一次用例残留的值会让"运行的是 frps-0.70.1"报出 0.71.0
        的版本号——这种假象会把测试引向一个根本不存在的问题。
        """
        base = {k: v for k, v in os.environ.items() if not k.startswith("FRPS_FAKE_")}
        base["FRPS_FAKE_VERSION"] = self.version
        base["FRPS_FAKE_MODE"] = self.mode
        if self.no_v2:
            base["FRPS_FAKE_NO_V2"] = "1"
        return base


def make_fake_frps(
    bin_dir: Path,
    *,
    version: str = DEFAULT_VERSION,
    mode: str = "ok",
    no_v2: bool = False,
) -> FakeFrps:
    """把假 frps 装成 `bin/frps-<version>` + `bin/frps` 软链。

    版本号写进文件名以贴近真实布局（§6：按版本并存 + 软链指向当前版本）。
    行为通过**运行时环境变量**切换，因此同一份文件可以扮演多种故障。
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / f"frps-{version}"
    target.write_text(FAKE_SOURCE.read_text("utf-8"), "utf-8")
    target.chmod(0o755)
    link = bin_dir / "frps"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target.name)
    return FakeFrps(path=target, version=version, mode=mode, no_v2=no_v2)


@pytest.fixture
def inst(tmp_path: Path) -> Instance:
    """一个隔离的实例目录（不碰用户的 ~/.local/share）。"""
    instance = Instance(
        name="test",
        instances_root=tmp_path / "instances",
        data_home=tmp_path / "data",
    )
    instance.ensure_dirs()
    return instance


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch):
    """测试期间清掉代理环境变量。

    httpx 在构造客户端时会读取 `HTTP_PROXY` / `NO_PROXY`；一旦这些变量写法不
    规范（例如 `NO_PROXY` 里写了带方括号的 IPv6 `[::1]`），httpx 会直接抛
    `InvalidURL`——于是每个健康检查都变成"dashboard 不可达"，而真因与 dashboard
    毫无关系。生产代码对此已有防御（回环地址 `trust_env=False`），这里清掉变量
    是为了让**非回环地址**的用例也不会被宿主环境污染。
    """
    for name in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def write_config(inst: Instance):
    """写一份最小可用配置。"""

    def _write(text: str) -> Path:
        inst.config.write_text(text, "utf-8")
        inst.config.chmod(0o600)
        return inst.config

    return _write


BASIC_CONFIG = """\
bindPort = 17000

[auth]
token = "test-token"

[webServer]
addr = "127.0.0.1"
port = 17500
user = "admin"
password = "test-password"

[transport.tls]
force = true
"""


def make_lifecycle(inst: Instance, fake: FakeFrps) -> Lifecycle:
    """夹具式 Lifecycle：二进制指向假 frps，并用 partial 固定它与实例的关键参数。

    注入而不是改环境变量：测试之间不共享状态，也就不存在顺序依赖。
    """
    lc = Lifecycle(inst, binary=fake.path)
    lc._spawn = functools.partial(_spawn_with_env, inst, fake)  # type: ignore[method-assign]
    return lc


def _spawn_with_env(inst: Instance, fake: FakeFrps, binary: Path) -> subprocess.Popen:
    """与 `Lifecycle._spawn` 同构，但额外注入假 frps 的行为开关。

    用 `[sys.executable, <script>]` 而不是直接执行脚本：假 frps 是 Python 脚本，
    直接 exec 会让 `Popen.pid` 指向解释器（经 shebang），从而**掩盖早退检测**——
    脚本崩了，`Popen` 与 `pid_alive` 都还认为进程活着。显式写解释器让被跟踪的
    pid 就是真正跑我们代码的那个进程。

    生产代码里 `argv[0]` 是 frps 自身；这里变成 `argv[1]`，因此它也顺带覆盖了
    `_cmdline_matches` 的解释器分支。
    """
    log_path = inst.new_startup_log()
    # 刻意不用 with：fd 必须存活到 Popen 返回，由 finally 关闭（同生产代码）
    handle = open(log_path, "ab", buffering=0)  # noqa: SIM115
    try:
        proc = subprocess.Popen(
            [sys.executable, str(binary), "-c", str(inst.config)],
            cwd=inst.dir,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
            env=fake.env(),
        )
    finally:
        handle.close()
    return proc


# ---------------------------------------------------------------------------
# 进程工具
# ---------------------------------------------------------------------------


def kill_quietly(pid: int) -> None:
    """测试收尾用：确保不留孤儿进程。"""
    import contextlib
    import signal

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def wait_port(host: str, port: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def free_ports(count: int) -> list[int]:
    """一次性拿到 `count` 个**互不相同**的空闲端口。

    ⚠️ 为什么不能用 `free_port()` 连续调用：它是"绑定→取号→关闭"，取号与真正
    绑定之间存在竞态窗口，而且内核可能把刚释放的号**再分配一次**，于是两次调用
    返回同一个端口。测试会表现为"frps 没起来"这种与真因无关的失败，
    或者更糟——flaky 通过。

    做法：**同时持有**所有 socket 直到全部取号完毕，再一起关闭。这样内核只能
    给出互不相同的号（它们在同一时刻都处于已绑定状态）。
    """
    if count < 1:
        raise ValueError("count 必须 >= 1")
    socks: list[socket.socket] = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            socks.append(sock)
        return [int(sock.getsockname()[1]) for sock in socks]
    finally:
        for sock in socks:
            sock.close()


def free_port() -> int:
    """单个空闲端口。**需要多个时请用 `free_ports(n)`**，见其说明。"""
    return free_ports(1)[0]
