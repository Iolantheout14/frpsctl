"""故障注入层测试（设计文档 §13、§15.5）。

前三层测的是正常路径与少数手写的异常分支。M5 后的全量 review 发现 4 处高危
缺陷**全部位于异常路径**——正常路径完全正常，只有刻意让某个系统调用失败才会
暴露。这一层就是为覆盖那个盲区。

每条场景验证的**不是功能**，而是一条不变量（见 `faults.py` 模块文档）：
契约内的异常、无遗留进程、无半截/含机密的文件、锁已释放、机密不外泄。

注入的是**真实调用点**（monkeypatch 系统调用本身），因此被测代码原样运行，
测到的就是它真实的错误处理路径。
"""

from __future__ import annotations

import errno
import importlib
import subprocess

import pytest

from frpsctl.core import config as cfg
from frpsctl.core import release as rel
from frpsctl.core.instance import Instance
from frpsctl.core.lifecycle import State
from frpsctl.core.transaction import apply_change
from frpsctl.errors import (
    BinaryError,
    ChecksumUnavailable,
    ConfigError,
    FrpsctlError,
    NotRunning,
    StartupFailed,
)

from .conftest import BASIC_CONFIG, kill_quietly, make_fake_frps, make_lifecycle
from .faults import (
    EACCES,
    ENOSPC,
    ETIMEDOUT,
    assert_lock_released,
    assert_no_orphan_frps,
    assert_no_secret_in_output,
)

#: pytest 的 fixture 必须存在于**本模块**的命名空间里才会被发现。
#: 用 importlib 显式装载（而不是 `from .faults import injector`）——后者会让
#: linter 把它当普通名字，与测试签名里的使用冲突（F811）。
injector = importlib.import_module("tests.faults").injector

pytestmark = pytest.mark.integration

#: 配置里出现的机密，用于断言不外泄
SECRET_TOKEN = "test-token"
SECRET_PASSWORD = "test-password"


def _assert_invariants(inst: Instance) -> None:
    """每处故障之后都必须成立的三件事。"""
    assert_no_orphan_frps(inst)
    assert_lock_released(inst)


def _all_text(exc: BaseException) -> str:
    """异常里可能外泄机密的全部文本。"""
    parts = [str(exc)]
    for attr in ("message", "hint"):
        value = getattr(exc, attr, None)
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)


@pytest.fixture
def ready(inst, write_config):
    """一个配置就绪、装有假 frps 的实例。"""
    write_config(BASIC_CONFIG)
    fake = make_fake_frps(inst.bin_dir)
    return inst, fake, make_lifecycle(inst, fake)


# ---------------------------------------------------------------------------
# 文件系统故障
# ---------------------------------------------------------------------------


class TestFilesystemFaults:
    def test_atomic_write_replace_failure_keeps_original(self, ready, injector) -> None:
        """`os.replace` 失败：原文件必须完好，且不留临时文件。"""
        inst, _, _ = ready
        original = inst.config.read_text("utf-8")
        injector.on("os.replace", ENOSPC)

        with pytest.raises(OSError):
            cfg.atomic_write(inst.config, "bindPort = 1\n")

        assert inst.config.read_text("utf-8") == original, "原配置被破坏"
        leftovers = [p for p in inst.dir.iterdir() if p.name.startswith(".frps.toml.")]
        assert not leftovers, f"残留临时文件：{leftovers}"

    def test_fsync_failure_does_not_leave_partial_file(self, ready, injector) -> None:
        inst, _, _ = ready
        original = inst.config.read_text("utf-8")
        injector.on("os.fsync", ENOSPC)
        with pytest.raises(OSError):
            cfg.atomic_write(inst.config, "bindPort = 1\n")
        assert inst.config.read_text("utf-8") == original

    def test_config_set_failure_leaves_config_and_service_untouched(self, ready, injector) -> None:
        """变更落盘失败：磁盘与运行中的服务都必须保持原样。"""
        inst, _, lc = ready
        report = lc.start(health_timeout=5)
        try:
            original = inst.config.read_text("utf-8")
            plan = cfg.plan_set(inst.config, "bindPort", "18000")
            injector.on("os.replace", ENOSPC, at=1)

            with pytest.raises((FrpsctlError, OSError)):
                apply_change(
                    inst,
                    dotted="bindPort",
                    new_text=plan.text,
                    change_diff=plan.diff,
                    before=plan.before,
                    after=plan.after,
                    lifecycle=lc,
                    health_timeout=3,
                )

            assert inst.config.read_text("utf-8") == original, "配置被改坏了"
            state, _ = lc.state()
            assert state is State.RUNNING, f"服务状态异常：{state}"
        finally:
            kill_quietly(report.pid)

    def test_state_write_failure_reaps_the_spawned_process(self, ready, injector) -> None:
        """**高危回归**：写 state.json 失败时，刚派生的 frps 必须被收拾掉。

        否则它会成为"无人认领"的进程：继续跑、继续占端口，而工具再也管不到它。
        """
        inst, _, lc = ready
        injector.on("os.replace", ENOSPC, matching="state.json")

        with pytest.raises((FrpsctlError, OSError)):
            lc.start(health_timeout=3)

        _assert_invariants(inst)
        assert not inst.state.exists(), "写失败时不该留下 state.json"

    def test_snapshot_dir_creation_failure_is_reported(self, ready, injector) -> None:
        """快照目录建不出来（磁盘满/只读）时必须报错，而不是静默跳过备份。"""
        inst, _, lc = ready
        injector.on("os.mkdir", EACCES, matching="0001-")
        plan = cfg.plan_set(inst.config, "bindPort", "18001")
        with pytest.raises((FrpsctlError, OSError)):
            apply_change(
                inst,
                dotted="bindPort",
                new_text=plan.text,
                change_diff=plan.diff,
                before=plan.before,
                after=plan.after,
                lifecycle=lc,
                restart=False,
            )
        _assert_invariants(inst)

    def test_history_prune_failure_is_visible(self, ready, injector, capsys) -> None:
        """`rmtree` 失败不能让"只留 10 份"静默失效（快照含机密）。"""
        inst, _, _ = ready
        from frpsctl.core.transaction import config_snapshot

        for i in range(12):
            config_snapshot(inst, action=f"f{i}")
        injector.on("shutil.rmtree", EACCES)
        inst.prune_history()
        captured = capsys.readouterr()
        assert "无法清理" in captured.err or len(inst.history_entries()) <= 10


# ---------------------------------------------------------------------------
# 子进程故障
# ---------------------------------------------------------------------------


class TestSubprocessFaults:
    def test_version_probe_timeout_is_a_binary_error(self, ready, injector) -> None:
        """`frps -v` 卡住 → 必须是可诊断的二进制错误，而不是卡死或裸异常。"""
        inst, _, lc = ready
        injector.on(
            "subprocess.run",
            lambda a, k: subprocess.TimeoutExpired(cmd=a[0] if a else "?", timeout=k.get("timeout", 5)),
            matching="-v",
        )

        with pytest.raises(FrpsctlError) as excinfo:
            lc.binary_version()
        assert int(excinfo.value.exit_code) == 4
        assert "超时" in excinfo.value.hint or "超时" in excinfo.value.message

    def test_verify_subprocess_timeout_does_not_hang(self, ready, injector) -> None:
        """配置校验的子进程超时：必须失败得清楚，且不留候选文件。"""
        inst, _, lc = ready
        injector.on(
            "subprocess.run",
            lambda a, k: subprocess.TimeoutExpired(cmd=a[0] if a else "?", timeout=k.get("timeout", 30)),
            matching="verify",
        )
        text = inst.config.read_text("utf-8")

        # 超时必须被接住并转成配置类错误(3)
        with pytest.raises(ConfigError) as excinfo:
            cfg.validate_text(text, binary=lc.binary(), workdir=inst.dir)
        assert int(excinfo.value.exit_code) == 3
        assert "超时" in excinfo.value.message
        # 候选文件必须被清掉（它可能含 token）
        leftovers = [p for p in inst.dir.iterdir() if p.suffix == ".toml" and p.name.startswith("tmp")]
        assert not leftovers, f"校验用的候选文件残留：{leftovers}"

    def test_spawn_failure_is_reported_and_cleans_up(self, ready, injector) -> None:
        """`Popen` 直接失败（例如二进制不可执行）：不留 state、不留进程。"""
        inst, _, lc = ready
        injector.on("subprocess.Popen", EACCES)

        with pytest.raises((FrpsctlError, OSError)):
            lc.start(health_timeout=3)

        _assert_invariants(inst)
        assert not inst.state.exists()

    def test_early_exit_reports_original_error(self, inst, write_config) -> None:
        """启动即退出：必须回显 frp 的原始报错（G6），而不是只给一个退出码。"""
        write_config(BASIC_CONFIG)
        fake = make_fake_frps(inst.bin_dir, mode="exit")
        lc = make_lifecycle(inst, fake)

        with pytest.raises(StartupFailed) as excinfo:
            lc.start(health_timeout=2)
        assert "address already in use" in (excinfo.value.hint or "")
        _assert_invariants(inst)


# ---------------------------------------------------------------------------
# 进程原语故障
# ---------------------------------------------------------------------------


class TestProcessFaults:
    def test_terminate_permission_error_is_reported(self, ready, injector) -> None:
        """发信号被拒（权限不足）：必须报错，且**保留 state.json**。

        保留它是刻意的：进程还在跑，state.json 是唯一的归属记录；清掉它
        等于把进程交给运气。
        """
        inst, _, lc = ready
        report = lc.start(health_timeout=5)
        try:
            injector.on("platform.terminate", PermissionError(errno.EPERM, "not permitted"))
            with pytest.raises(FrpsctlError) as excinfo:
                lc.stop(timeout=0.5)
            # 发不出信号 = 没停成 → 必须报冲突(11)并**保留 state.json**
            assert int(excinfo.value.exit_code) == 11
            assert inst.state.exists(), "信号未发出却清掉了 state.json → 进程将失去追踪"
            assert_lock_released(inst)
        finally:
            kill_quietly(report.pid)

    def test_stop_survives_start_time_read_failure(self, ready, injector) -> None:
        """`/proc` 读不到启动时刻：必须 fail-closed，绝不误杀。

        `is_ours()` 依赖启动时刻识别 pid 复用；读不到时按"不是我们的"处理，
        代价是拒绝停止，而不是冒险杀一个可能无关的进程。
        """
        inst, _, lc = ready
        report = lc.start(health_timeout=5)
        try:
            # 让"读启动时刻"全部返回 None（真实的 /proc 读取失败就是这样）
            injector.on("platform.proc_start_time", EACCES)
            with pytest.raises(FrpsctlError) as excinfo:
                lc.stop(timeout=0.5)
            # 身份不可知 → 拒绝停止(11) 或如实报未运行(5)，两者都是 fail-closed
            assert int(excinfo.value.exit_code) in (5, 11)

            # 这条用例的不变量是"**没发信号**"，不是"无残留进程"：进程本来就该
            # 继续跑着（我们刻意拒绝去杀它），由 finally 收尾。
            # fail-closed 的含义正是"宁可停不下来，也不能杀错"。
            assert not injector.hits("platform.terminate"), "身份不可知时绝不能发信号"
            assert inst.state.exists(), "拒绝停止时必须保留 state.json（进程仍在跑）"
        finally:
            kill_quietly(report.pid)

    def test_stop_when_already_gone_is_not_an_error(self, ready) -> None:
        """进程自然退出后再 stop：应当是"未运行"，而不是异常。"""
        inst, _, lc = ready
        report = lc.start(health_timeout=5)
        kill_quietly(report.pid)
        import time

        time.sleep(0.3)
        with pytest.raises(NotRunning):
            lc.stop(timeout=2)


# ---------------------------------------------------------------------------
# 网络故障
# ---------------------------------------------------------------------------


class TestNetworkFaults:
    def test_dashboard_timeout_degrades_health_but_not_gate(self, ready, injector) -> None:
        """dashboard 超时：L2 判失败，但**不影响** L1；插件探针同理不阻塞。"""
        inst, _, lc = ready
        report = lc.start(health_timeout=5)
        try:
            injector.on("httpx.get", ETIMEDOUT)
            health = lc.check_health()
            assert health.l1_process.value == "ok", "dashboard 超时不该影响进程层判定"
            assert health.l2_control.value == "fail"
        finally:
            kill_quietly(report.pid)

    def test_health_probe_connection_refused_is_survivable(self, ready, injector) -> None:
        inst, _, lc = ready
        report = lc.start(health_timeout=5)
        try:
            injector.on("socket.create_connection", ConnectionRefusedError("refused"))
            health = lc.check_health()
            assert health.l1_process.value == "ok"
        finally:
            kill_quietly(report.pid)

    def test_status_never_raises_on_network_failure(self, ready, injector) -> None:
        """`status` 必须永远能回答"现在什么情况"——网络全挂也要返回。"""
        inst, _, lc = ready
        report = lc.start(health_timeout=5)
        try:
            injector.on("httpx.get", ETIMEDOUT)
            injector.on("socket.create_connection", ConnectionRefusedError("refused"))
            status = lc.status()  # 不抛
            assert status.state is State.RUNNING
            assert status.pid == report.pid
        finally:
            kill_quietly(report.pid)

    def test_checksum_unavailable_refuses_install(self, tmp_path, injector) -> None:
        """拿不到校验和 → **拒绝安装**（ADR-7 的 fail-closed）。

        这一条是供应链安全的底线：宁可装不上，也不能执行来路不明的二进制。
        """
        injector.on("release._fetch", OSError("network down"))
        with pytest.raises(ChecksumUnavailable):
            rel.install(bin_dir=tmp_path / "bin", version="0.71.0")

    def test_checksum_mismatch_refuses_to_place_binary(self, tmp_path, injector, monkeypatch) -> None:
        """校验和不匹配 → 拒绝落盘（ADR-7）。"""
        import frpsctl.core.release as release

        asset = release.asset_name("0.71.0")
        # 校验和文件里给的哈希与下载内容不符
        monkeypatch.setattr(release, "download_checksums", lambda *_, **__: f"{'0' * 64}  {asset}\n")
        monkeypatch.setattr(release, "download", lambda *_, **__: b"not the real tarball")

        bin_dir = tmp_path / "bin"
        with pytest.raises(BinaryError):
            release.install(bin_dir=bin_dir, version="0.71.0")
        assert not (bin_dir / "frps-0.71.0").exists(), "校验失败却落盘了二进制"


# ---------------------------------------------------------------------------
# 机密不变量
# ---------------------------------------------------------------------------


class TestSecretInvariants:
    def test_fault_messages_never_contain_secrets(self, ready, injector) -> None:
        """任何故障的异常消息都不得包含 token/口令（§10 硬约束 2）。"""
        inst, _, lc = ready
        injector.on("os.replace", ENOSPC, matching="state.json")
        with pytest.raises((FrpsctlError, OSError)) as excinfo:
            lc.start(health_timeout=3)
        assert_no_secret_in_output(_all_text(excinfo.value), SECRET_TOKEN, SECRET_PASSWORD)

    def test_validate_failure_does_not_leak_config_body(self, ready, injector) -> None:
        """配置被拒时，异常里不能带配置原文（里面有 token）。"""
        inst, _, lc = ready
        injector.on(
            "subprocess.run",
            lambda a, k: subprocess.TimeoutExpired(cmd=a[0] if a else "?", timeout=k.get("timeout", 30)),
            matching="verify",
        )
        # 超时必须被接住并转成配置类错误(3)，而不是裸 TimeoutExpired
        with pytest.raises(ConfigError) as excinfo:
            cfg.validate_text(inst.config.read_text("utf-8"), binary=lc.binary(), workdir=inst.dir)
        assert int(excinfo.value.exit_code) == 3
        assert_no_secret_in_output(_all_text(excinfo.value), SECRET_TOKEN, SECRET_PASSWORD)

    def test_rollback_message_does_not_leak(self, ready, injector) -> None:
        """回滚路径的提示同样不能带机密。"""
        from frpsctl.errors import ChangeRolledBack

        inst, _, lc = ready
        lc.start(health_timeout=5)  # 报告对象用不上，进程由 finally 里的 pgrep 收尾
        try:
            failing = make_fake_frps(inst.bin_dir, mode="exit")
            failing_lc = make_lifecycle(inst, failing)
            plan = cfg.plan_set(inst.config, "bindPort", "18002")
            with pytest.raises(ChangeRolledBack) as excinfo:
                apply_change(
                    inst,
                    dotted="bindPort",
                    new_text=plan.text,
                    change_diff=plan.diff,
                    before=plan.before,
                    after=plan.after,
                    lifecycle=failing_lc,
                    health_timeout=3,
                    restore_lifecycle=failing_lc,
                )
            assert_no_secret_in_output(_all_text(excinfo.value), SECRET_TOKEN, SECRET_PASSWORD)
        finally:
            with_cleanup = subprocess.run(["pgrep", "-af", "frps"], capture_output=True, text=True).stdout
            for line in with_cleanup.splitlines():
                if str(inst.dir) in line:
                    kill_quietly(int(line.split()[0]))


class TestUnrecoverableProcess:
    """进程杀不掉时，**state.json 必须保留**——它是唯一的归属记录。

    这一段曾经因为重构中的缩进错位变成不可达代码（SIGKILL 升级逻辑整段失效），
    结果是卡死的 frps 再也停不掉。集成测试抓住了它；这里再加一条直接断言不变量。
    """

    def _hanging(self, inst):
        """起一个忽略 SIGTERM 的进程（只能用 SIGKILL 结束）。"""
        fake = make_fake_frps(inst.bin_dir, mode="hang")
        lc = make_lifecycle(inst, fake)
        return lc, lc.start(health_timeout=3)

    def test_escalation_to_sigkill_actually_happens(self, ready, monkeypatch) -> None:
        """SIGKILL 升级必须真的被执行（回归那段不可达代码）。

        用真实信号序列验证：SIGTERM 阶段（force=False）超时后，必须出现
        force=True 的第二次调用，且最终进程真的消失。
        """
        import frpsctl.core.platform as plat

        inst, _, _ = ready
        lc, report = self._hanging(inst)

        calls: list[bool] = []
        real = plat.terminate

        def spy(pid, *, force=False):
            calls.append(force)
            return real(pid, force=force)

        monkeypatch.setattr(plat, "terminate", spy)
        try:
            result = lc.stop(timeout=0.4)
            assert result.stopped is True
            assert calls == [False, True], f"信号序列异常（SIGKILL 升级缺失）：{calls}"
        finally:
            kill_quietly(report.pid)

    def test_state_preserved_when_process_truly_survives(self, ready, monkeypatch) -> None:
        """连 SIGKILL 都无效（模拟 D 状态）时：保留 state.json 并如实报错。

        `kill(pid, 0)` 仍成功、`/proc` 仍可读 → 在工具看来这个进程"还在且属于
        我们"。此时清掉 state.json 就等于放弃它，因此必须保留。
        """
        import frpsctl.core.platform as plat

        inst, _, _ = ready
        lc, report = self._hanging(inst)

        monkeypatch.setattr(plat, "terminate", lambda *_, **__: None)  # 信号全丢
        try:
            with pytest.raises(FrpsctlError) as excinfo:
                lc.stop(timeout=0.3)
            assert int(excinfo.value.exit_code) == 10  # "停不掉"，不是"没在跑"
            assert inst.state.exists(), "停不掉时清掉 state.json → 进程失去追踪"
            # 消息必须说"停不掉"，不能复用"frps 启动后立即退出"（与事实相反）
            assert "无法停止" in excinfo.value.message
            assert "启动" not in excinfo.value.message
            assert_lock_released(inst)
        finally:
            kill_quietly(report.pid)
