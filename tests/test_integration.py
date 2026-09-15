"""集成层：生命周期与回滚（设计文档 §13 集成层）。

覆盖设计文档点名的完整链路：

    init → start → status → config set（成功）
         → config set 非法值（断言线上文件未变 + 退出码 3）
         → config set 导致启动失败（断言自动回滚 + 退出码 9）
         → stop

外加 §8.6.1 的**升级语义**（换软链不影响运行中进程、state 记录真实路径），
那是 R13 那个"自伤 bug"的直接守卫。
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from frpsctl.core import config as cfg
from frpsctl.core.lifecycle import Lifecycle, Owner, State
from frpsctl.core.transaction import apply_change, rollback_to
from frpsctl.errors import (
    ChangeRolledBack,
    ConfigError,
    NotRunning,
    OwnershipConflict,
    StartupFailed,
    UnsupportedVersion,
)

from .conftest import BASIC_CONFIG, free_port, kill_quietly, make_fake_frps, make_lifecycle

pytestmark = pytest.mark.integration


@pytest.fixture
def running(inst, write_config, tmp_path):
    """起一个正常运行的实例，测完必定清理。"""
    port = free_port()
    config = BASIC_CONFIG.replace("17500", str(port))
    write_config(config)
    fake = make_fake_frps(inst.bin_dir)
    lc = make_lifecycle(inst, fake)
    report = lc.start(health_timeout=5)
    yield lc, report, port
    kill_quietly(report.pid)


# ---------------------------------------------------------------------------
# 完整链路
# ---------------------------------------------------------------------------


class TestLifecycleChain:
    def test_full_chain(self, inst, write_config):
        """init → start → status → config set → 非法值 → 回滚 → stop。"""
        port = free_port()
        write_config(BASIC_CONFIG.replace("17500", str(port)))
        fake = make_fake_frps(inst.bin_dir)
        lc = make_lifecycle(inst, fake)

        # start
        report = lc.start(health_timeout=5)
        assert report.pid > 0
        assert report.healthy is True
        try:
            # status
            status = lc.status()
            assert status.owner is Owner.DIRECT
            assert status.state is State.RUNNING
            assert status.pid == report.pid
            assert status.health is not None and status.health.gate is True

            # config set（成功）
            plan = cfg.plan_set(inst.config, "bindPort", "18000")
            outcome = apply_change(
                inst,
                dotted="bindPort",
                new_text=plan.text,
                change_diff=plan.diff,
                before=plan.before,
                after=plan.after,
                lifecycle=lc,
                health_timeout=5,
            )
            assert outcome.applied is True
            assert outcome.restarted is True
            assert "18000" in inst.config.read_text("utf-8")

            # 非法值：线上文件必须零影响
            before_text = inst.config.read_text("utf-8")
            with pytest.raises(ConfigError):
                cfg.plan_set(inst.config, "bindPort", "99999")
            assert inst.config.read_text("utf-8") == before_text
        finally:
            lc.stop()

        assert lc.status().state is State.STOPPED

    def test_config_set_rolls_back_on_startup_failure(self, inst, write_config):
        """§9 第 8 步：verify 通过但真跑起来失败 → 自动回滚 + 退出码 9。"""
        port = free_port()
        write_config(BASIC_CONFIG.replace("17500", str(port)))
        ok = make_fake_frps(inst.bin_dir, mode="ok")
        lc = make_lifecycle(inst, ok)
        report = lc.start(health_timeout=5)
        original = inst.config.read_text("utf-8")

        try:
            # 让下一次启动必定失败（模拟端口被占 / 证书缺失）
            failing = make_fake_frps(inst.bin_dir, mode="exit")
            failing_lc = make_lifecycle(inst, failing)

            plan = cfg.plan_set(inst.config, "bindPort", "18001")
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
            assert int(excinfo.value.exit_code) == 9

            # 断言：配置已恢复成变更前的内容
            restored = inst.config.read_text("utf-8")
            assert restored == original, "配置未被回滚"
            data = tomllib.loads(restored)
            assert data["bindPort"] == 17000
        finally:
            kill_quietly(report.pid)

    def test_config_set_no_restart_marks_pending(self, inst, write_config):
        port = free_port()
        write_config(BASIC_CONFIG.replace("17500", str(port)))
        fake = make_fake_frps(inst.bin_dir)
        lc = make_lifecycle(inst, fake)

        plan = cfg.plan_set(inst.config, "bindPort", "18002")
        outcome = apply_change(
            inst,
            dotted="bindPort",
            new_text=plan.text,
            change_diff=plan.diff,
            before=plan.before,
            after=plan.after,
            lifecycle=lc,
            restart=False,
        )
        assert outcome.applied is True
        assert outcome.restarted is False
        assert "尚未生效" in outcome.note
        # 未运行时也照做校验并写入
        assert "18002" in inst.config.read_text("utf-8")


# ---------------------------------------------------------------------------
# 生命周期边界
# ---------------------------------------------------------------------------


class TestStartGuards:
    def test_startup_failure_reports_frps_output(self, inst, write_config):
        """G6：启动失败必须回显 frp 原始错误，而不是只给一个退出码。"""
        write_config(BASIC_CONFIG)
        fake = make_fake_frps(inst.bin_dir, mode="exit")
        lc = make_lifecycle(inst, fake)

        with pytest.raises(StartupFailed) as excinfo:
            lc.start(health_timeout=2)
        assert "address already in use" in (excinfo.value.hint or "")
        # 且不留下 state.json
        assert not inst.state.exists()

    def test_double_start_is_rejected(self, running):
        lc, report, _ = running
        from frpsctl.errors import AlreadyRunning

        with pytest.raises(AlreadyRunning):
            lc.start(health_timeout=2)

    def test_stop_when_not_running(self, inst, write_config):
        write_config(BASIC_CONFIG)
        fake = make_fake_frps(inst.bin_dir)
        lc = make_lifecycle(inst, fake)
        with pytest.raises(NotRunning):
            lc.stop()

    def test_foreign_process_is_never_killed(self, inst, write_config):
        """R2 / ADR-7 的核心：存活但身份不符 → 拒绝，绝不 kill。"""
        write_config(BASIC_CONFIG)
        fake = make_fake_frps(inst.bin_dir)
        lc = make_lifecycle(inst, fake)

        # 伪造 state.json 指向一个真实存在但无关的进程（本测试进程自己）
        inst.write_state(
            {
                "pid": os.getpid(),
                "start_time": None,
                "binary": "/nonexistent/frps",
                "config": str(inst.config),
                "version": "0.71.0",
                "owner": "direct",
            }
        )
        state, ref = lc.state()
        assert state is State.FOREIGN
        assert ref is not None and ref.pid == os.getpid()

        with pytest.raises(OwnershipConflict):
            lc.stop()
        assert _alive(os.getpid()), "绝不能杀掉校验不通过的进程"
        with pytest.raises(OwnershipConflict):
            lc.start(health_timeout=2)

    def test_stale_state_is_cleaned_and_startable(self, inst, write_config):
        """陈旧 state：进程已退出 → 提示可直接 start，并自动清理。"""
        write_config(BASIC_CONFIG)
        fake = make_fake_frps(inst.bin_dir)
        lc = make_lifecycle(inst, fake)

        inst.write_state(
            {
                "pid": 999_999,  # 不存在
                "start_time": 12345,
                "binary": str(fake.path),
                "config": str(inst.config),
                "version": "0.71.0",
                "owner": "direct",
            }
        )
        state, _ = lc.state()
        assert state is State.STALE

        report = lc.start(health_timeout=5)
        try:
            assert report.pid > 0
        finally:
            kill_quietly(report.pid)

    def test_unsupported_version_refused(self, inst, write_config):
        write_config(BASIC_CONFIG)
        make_fake_frps(inst.bin_dir, version="0.69.1")
        lc = Lifecycle(inst)
        with pytest.raises(UnsupportedVersion):
            lc.binary_version()


class TestStopSemantics:
    def test_sigterm_terminates_immediately(self, running):
        """§3.5：frps 没有信号处理器，SIGTERM 即终止，正常路径应在数百毫秒内。"""
        import time

        lc, report, _ = running
        started = time.monotonic()
        result = lc.stop(timeout=10)
        elapsed = time.monotonic() - started
        assert result.stopped is True
        assert elapsed < 2.0, f"SIGTERM 路径耗时 {elapsed:.2f}s，说明在等待超时"
        assert not inst_alive(report.pid)

    def test_hanging_process_escalates_to_sigkill(self, inst, write_config):
        write_config(BASIC_CONFIG)
        fake = make_fake_frps(inst.bin_dir, mode="hang")
        lc = make_lifecycle(inst, fake)
        report = lc.start(health_timeout=3)

        result = lc.stop(timeout=0.5)  # SIGTERM 无效 → 升级 SIGKILL
        assert result.stopped is True
        assert not inst_alive(report.pid)


# ---------------------------------------------------------------------------
# 三层健康
# ---------------------------------------------------------------------------


class TestHealthLayers:
    def test_l1_and_l2_ok(self, running):
        lc, _, _ = running
        health = lc.check_health()
        assert health.l1_process.value == "ok"
        assert health.l2_control.value == "ok"
        assert health.l3_plugin.value == "skipped"  # 未配置 httpPlugins
        assert health.gate is True

    def test_l2_skipped_when_dashboard_disabled(self, inst, write_config):
        write_config("bindPort = 17000\n[auth]\ntoken = \"t\"\n[webServer]\nport = 0\n")
        fake = make_fake_frps(inst.bin_dir)
        lc = make_lifecycle(inst, fake)
        report = lc.start(health_timeout=3)
        try:
            health = lc.check_health()
            assert health.l2_control.value == "skipped"
            assert health.gate is True, "未启用 dashboard 不该判为失败"
        finally:
            kill_quietly(report.pid)

    def test_plugin_failure_does_not_break_gate(self, inst, write_config):
        """§3.7 最关键的一条：L3 失败**不得**触发回滚判据失败。"""
        dead = free_port()
        write_config(
            BASIC_CONFIG
            + f'\n[[httpPlugins]]\nname = "auth"\naddr = "http://127.0.0.1:{dead}"\n'
            'path = "/handler"\nops = ["Login"]\n'
        )
        fake = make_fake_frps(inst.bin_dir)
        lc = make_lifecycle(inst, fake)
        report = lc.start(health_timeout=3)
        try:
            health = lc.check_health()
            assert health.l1_process.value == "ok"
            assert health.l2_control.value == "ok"
            assert health.l3_plugin.value == "fail"
            assert health.gate is True, "插件抖动绝不能否决一份正确的配置"
            assert health.plugin_warning is not None
            assert "无法登录" in health.plugin_warning
        finally:
            kill_quietly(report.pid)


# ---------------------------------------------------------------------------
# 升级语义（§8.6.1、R13）
# ---------------------------------------------------------------------------


class TestUpgradeSemantics:
    def test_switching_symlink_does_not_affect_running_process(self, inst, write_config):
        """换软链不影响运行中的进程——Linux 上映像已绑定 inode。"""
        write_config(BASIC_CONFIG)
        old = make_fake_frps(inst.bin_dir, version="0.70.1")
        lc = make_lifecycle(inst, old)
        report = lc.start(health_timeout=5)
        try:
            # 装一个新版本并换链
            make_fake_frps(inst.bin_dir, version="0.71.0")
            assert inst.active_binary().name == "frps-0.71.0"

            # 老进程仍然是我们的人：身份校验不能因为换链而失配
            state, ref = lc.state()
            assert state is State.RUNNING, "换软链后身份校验失配（R13 自伤 bug）"
            assert ref is not None and ref.is_ours() is True

            # status 应当同时显示两个版本
            status = lc.status()
            assert status.binary_version == "0.70.1"
            assert status.disk_version == "0.71.0"
            assert status.binary_matches_disk is False
        finally:
            kill_quietly(report.pid)

    def test_state_records_resolved_path_not_symlink(self, inst, write_config):
        write_config(BASIC_CONFIG)
        fake = make_fake_frps(inst.bin_dir)
        lc = make_lifecycle(inst, fake)
        report = lc.start(health_timeout=5)
        try:
            data = json.loads(inst.state.read_text("utf-8"))
            # state 记录的是实际运行的二进制（resolve 后），而不是软链
            assert Path(data["binary"]).name == "frps-0.71.0"
            assert data["binary"] == str(lc.actual_binary())
        finally:
            kill_quietly(report.pid)


# ---------------------------------------------------------------------------
# 回滚
# ---------------------------------------------------------------------------


class TestRollback:
    def test_rollback_restores_previous_snapshot(self, inst, write_config):
        write_config(BASIC_CONFIG)
        fake = make_fake_frps(inst.bin_dir)
        lc = make_lifecycle(inst, fake)

        plan = cfg.plan_set(inst.config, "bindPort", "18003")
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
        assert tomllib.loads(inst.config.read_text("utf-8"))["bindPort"] == 18003

        outcome = rollback_to(inst, steps=1, lifecycle=lc, restart=False)
        assert tomllib.loads(inst.config.read_text("utf-8"))["bindPort"] == 17000
        assert "17000" in outcome.diff

    def test_rollback_without_history_is_an_error(self, inst, write_config):
        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))
        with pytest.raises(ConfigError):
            rollback_to(inst, steps=1, lifecycle=lc, restart=False)

    def test_history_keeps_only_ten(self, inst, write_config):
        """§9 第 6 步：保留最近 10 份。"""
        write_config(BASIC_CONFIG)
        for i in range(14):
            from frpsctl.core.transaction import config_snapshot

            config_snapshot(inst, action=f"test-{i}")
        assert len(inst.history_entries()) == 10


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    from frpsctl.core.platform import pid_alive

    return pid_alive(pid)


def inst_alive(pid: int) -> bool:
    """进程是否仍**真正**存活（僵尸算已死）。

    用 `process_gone` 而不是 `pid_alive`：子进程退出后会短暂处于僵尸态，
    而 `kill(pid, 0)` 对僵尸返回成功——那是内核里残留的 task 结构，
    不代表服务还在。
    """
    import time

    from frpsctl.core.platform import process_gone

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if process_gone(pid):
            return False
        time.sleep(0.05)
    return True
