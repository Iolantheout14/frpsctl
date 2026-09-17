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
from frpsctl.core.transaction import apply_set, rollback_to
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
            outcome = apply_set(
                inst, dotted="bindPort", raw="18000", lifecycle=lc, health_timeout=5
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

            with pytest.raises(ChangeRolledBack) as excinfo:
                apply_set(
                    inst,
                    dotted="bindPort",
                    raw="18001",
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

        outcome = apply_set(
            inst, dotted="bindPort", raw="18002", lifecycle=lc, restart=False
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

    def test_process_dying_after_grace_is_startup_failure(self, inst, write_config):
        """进程在早退检测窗口**之后**才死：仍是启动失败（退出码 10）。

        这条路径此前会把一个已经死掉的进程当成"已启动"：state.json 已写、
        早退检测已过，之后没有任何地方重新确认 L1。现在健康等待阶段发现
        L1 失败会立即收尾并清理状态，避免留下假 RUNNING。
        """
        write_config(BASIC_CONFIG)
        fake = make_fake_frps(inst.bin_dir, mode="exit_after")
        lc = make_lifecycle(inst, fake)

        with pytest.raises(StartupFailed):
            lc.start(health_timeout=10)
        assert not inst.state.exists(), "进程已死却留下 state.json → 假 RUNNING"
        assert lc.state()[0] is State.STOPPED

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
        write_config('bindPort = 17000\n[auth]\ntoken = "t"\n[webServer]\nport = 0\n')
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
            BASIC_CONFIG + f'\n[[httpPlugins]]\nname = "auth"\naddr = "http://127.0.0.1:{dead}"\n'
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

        apply_set(
            inst, dotted="bindPort", raw="18003", lifecycle=lc, restart=False
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

    def test_rollback_creates_exactly_one_snapshot(self, inst, write_config):
        """一次回滚只消耗 1 份快照，且动作被正确记账。

        回归：`rollback_to` 与 `apply_change` 此前各自 `config_snapshot` 一次，
        两份内容完全相同——"保留 10 份"实际只够 5 次操作，`rollback N` 的
        计数里一半是重复项。修复后快照只在 apply_change 的锁内创建一次。
        """
        import json as _json

        from frpsctl.core.transaction import config_snapshot

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))

        config_snapshot(inst, action="baseline")
        before = len(inst.history_entries())

        rollback_to(inst, steps=1, lifecycle=lc, restart=False)

        entries = inst.history_entries()
        assert len(entries) == before + 1, f"一次回滚新增了 {len(entries) - before} 份快照"
        meta = _json.loads((entries[0] / "meta.json").read_text("utf-8"))
        assert meta["action"] == "rollback 1", meta

    def test_history_keeps_only_ten(self, inst, write_config):
        """§9 第 6 步：保留最近 10 份。"""
        write_config(BASIC_CONFIG)
        for i in range(14):
            from frpsctl.core.transaction import config_snapshot

            config_snapshot(inst, action=f"test-{i}")
        assert len(inst.history_entries()) == 10


class TestConcurrentChanges:
    """并发配置变更的锁边界（第四轮 review 的根因修复）。

    回归：`plan_set`（读-改-写里的"读"）曾在实例锁**外**执行，两个并发
    `config set` 都基于同一份旧文本生成完整新文本，后写入者覆盖前者，
    且两条命令都报成功——确定性复现过（bindPort 的改动被静默抹掉）。
    现在候选生成在锁内（`apply_set`），编辑器路径由 `apply_edit` 做 CAS。
    """

    def test_plan_happens_inside_the_instance_lock(self, inst, write_config, monkeypatch) -> None:
        """不变量（确定性）：`cfg.plan_set` 必须在本进程持锁时被调用。

        这条直接守根因，不依赖线程时序——锁内生成候选是并发正确性的充要条件。
        """
        from frpsctl.core import config as config_module
        from frpsctl.core import lock as lock_mod

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))

        observed: list[bool] = []
        real_plan = config_module.plan_set

        def spy(path, dotted, raw):
            observed.append(lock_mod.is_locked(inst.lock))
            return real_plan(path, dotted, raw)

        monkeypatch.setattr(config_module, "plan_set", spy)
        apply_set(inst, dotted="bindPort", raw="18000", lifecycle=lc, restart=False)

        assert observed == [True], "候选文本生成发生在实例锁之外（并发覆盖的根因）"

    def test_concurrent_sets_keep_both_changes(self, inst, write_config) -> None:
        """端到端：两个并发 `config set` 的变更都必须保留。"""
        import threading

        write_config(BASIC_CONFIG)
        fake = make_fake_frps(inst.bin_dir)
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def worker(key: str, value: str) -> None:
            lc = make_lifecycle(inst, fake)  # 每线程独立 Lifecycle（模拟两个进程）
            barrier.wait(timeout=10)
            try:
                apply_set(inst, dotted=key, raw=value, lifecycle=lc, restart=False)
            except BaseException as exc:  # noqa: BLE001 - 收集线程内异常供断言
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("bindPort", "18000")),
            threading.Thread(target=worker, args=("maxPortsPerClient", "30")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert not errors, errors
        data = tomllib.loads(inst.config.read_text("utf-8"))
        assert data["bindPort"] == 18000, "并发 set 的改动被另一路覆盖"
        assert data["maxPortsPerClient"] == 30

    def test_edit_rejects_draft_when_file_changed_concurrently(self, inst, write_config) -> None:
        """编辑器草稿在并发修改后必须被拒绝（CAS），而不是覆盖别人的改动。"""
        from frpsctl.core.transaction import apply_edit

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))
        original = cfg.read_config_text(inst.config)

        # 模拟"编辑期间"另一路 config set 改了配置
        apply_set(inst, dotted="maxPortsPerClient", raw="30", lifecycle=lc, restart=False)

        with pytest.raises(ConfigError, match="编辑期间"):
            apply_edit(
                inst,
                draft=original + "# 草稿\n",
                expected_current=original,
                lifecycle=lc,
                restart=False,
            )
        # 别人的改动必须完好，草稿不得落盘
        assert "maxPortsPerClient = 30" in inst.config.read_text("utf-8")
        assert "# 草稿" not in inst.config.read_text("utf-8")

    def test_edit_noop_does_not_touch_anything(self, inst, write_config) -> None:
        """草稿与基准一致（没有改动）→ 不写盘、不产生快照。"""
        from frpsctl.core.transaction import apply_edit

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))
        original = cfg.read_config_text(inst.config)

        outcome = apply_edit(
            inst, draft=original, expected_current=original, lifecycle=lc, restart=False
        )
        assert outcome.noop is True
        assert not inst.history_entries(), "无改动不应产生快照"

    def test_set_noop_is_decided_inside_the_lock(self, inst, write_config) -> None:
        """`config set` 写同值：返回 noop 且不产生快照/不改文件。"""
        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))

        outcome = apply_set(inst, dotted="bindPort", raw="17000", lifecycle=lc, restart=False)
        assert outcome.noop is True
        assert not inst.history_entries()


class TestUnsetAndDryRun:
    """`config unset` 与 `config set --dry-run` 的闭环行为。"""

    def test_unset_applies_and_snapshots(self, inst, write_config) -> None:
        from frpsctl.core.transaction import apply_unset

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))

        outcome = apply_unset(
            inst, dotted="webServer.password", lifecycle=lc, restart=False
        )

        assert outcome.applied is True
        assert outcome.before == "test-password"
        assert outcome.after is None
        assert "password" not in inst.config.read_text("utf-8")
        assert len(inst.history_entries()) == 1, "unset 也应留下变更前快照"

    def test_unset_missing_key_is_config_error(self, inst, write_config) -> None:
        from frpsctl.core.transaction import apply_unset

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))
        with pytest.raises(ConfigError):
            apply_unset(inst, dotted="webServer.nothere", lifecycle=lc, restart=False)

    def test_dry_run_writes_nothing(self, inst, write_config) -> None:
        """dry-run：真实验证（语义 + 权威 + 危险组合）都跑，但零落盘、零快照。"""
        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))
        before = inst.config.read_text("utf-8")

        outcome = apply_set(
            inst, dotted="bindPort", raw="18000", lifecycle=lc, dry_run=True
        )

        assert outcome.dry_run is True
        assert outcome.applied is False
        assert "dry-run" in outcome.note
        assert inst.config.read_text("utf-8") == before, "dry-run 改动了文件"
        assert not inst.history_entries(), "dry-run 不该产生快照"

    def test_dry_run_still_validates(self, inst, write_config) -> None:
        """dry-run 不是"跳过校验"：非法值照常配置错误(3)。"""
        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))
        before = inst.config.read_text("utf-8")
        with pytest.raises(ConfigError):
            apply_set(inst, dotted="bindPort", raw="99999", lifecycle=lc, dry_run=True)
        assert inst.config.read_text("utf-8") == before

    def test_dry_run_rejects_dangerous_combination(self, inst, write_config) -> None:
        """dry-run 必须跑危险组合拦截：否则用户会带着"校验通过"的错觉去掉 --dry-run。"""
        write_config('bindPort = 17000\n[webServer]\naddr = "0.0.0.0"\nport = 17500\n')
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))
        before = inst.config.read_text("utf-8")
        with pytest.raises(ConfigError, match="危险配置"):
            apply_set(inst, dotted="maxPortsPerClient", raw="30", lifecycle=lc, dry_run=True)
        assert inst.config.read_text("utf-8") == before

    def test_dry_run_unset_writes_nothing(self, inst, write_config) -> None:
        from frpsctl.core.transaction import apply_unset

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))
        before = inst.config.read_text("utf-8")
        outcome = apply_unset(
            inst, dotted="webServer.password", lifecycle=lc, dry_run=True
        )
        assert outcome.dry_run is True
        assert inst.config.read_text("utf-8") == before
        assert not inst.history_entries()

    def test_dry_run_multi_key_writes_nothing(self, inst, write_config) -> None:
        """`apply_sets` 的 dry-run 与单键同一语义（Web 表单底座的一致性）。"""
        from frpsctl.core.transaction import apply_sets

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))
        before = inst.config.read_text("utf-8")
        outcome = apply_sets(
            inst,
            changes=[("bindPort", "18010"), ("maxPortsPerClient", "30")],
            lifecycle=lc,
            dry_run=True,
        )
        assert outcome.dry_run is True
        assert outcome.applied is False
        assert inst.config.read_text("utf-8") == before
        assert not inst.history_entries()


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


class TestSystemdStartHealthTimeout:
    def test_health_timeout_is_forwarded_to_systemd_path(self, inst, write_config, monkeypatch) -> None:
        """systemd 路径必须使用调用方给的 health_timeout。

        回归：`_start_via_systemd` 硬编码 `_await_health(10.0)`，`--health-timeout`
        对 systemd 托管的实例**静默无效**——用户以为调了等待时长，实际没有。
        """
        from frpsctl.core import systemd as sd_mod
        from frpsctl.core.health import HealthLayer, HealthReport

        write_config(BASIC_CONFIG)
        fake = make_fake_frps(inst.bin_dir)
        lc = make_lifecycle(inst, fake)

        class _FakeSystemd:
            def __init__(self, _instance, **_kw) -> None:
                pass

            def is_active(self) -> bool:
                return True

            def start(self) -> None:
                pass

            def main_pid(self) -> int:
                return 4242

        monkeypatch.setattr(sd_mod, "Systemd", _FakeSystemd)
        received: list[float] = []

        def fake_await(timeout: float, **_kwargs: object) -> HealthReport:
            received.append(timeout)
            return HealthReport(HealthLayer.OK, HealthLayer.OK, HealthLayer.SKIPPED)

        monkeypatch.setattr(lc, "_await_health", fake_await)
        report = lc.start(health_timeout=3.5)

        assert received == [3.5], "health_timeout 没有传给 systemd 路径"
        assert report.pid == 4242


class TestApplySets:
    """`apply_sets`：多键一次事务（Web 配置表单的语义）。"""

    def test_multi_key_change_is_one_transaction(self, inst, write_config) -> None:
        """多键变更只产生一份快照、一次落盘。"""
        from frpsctl.core.transaction import apply_sets

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))

        outcome = apply_sets(
            inst,
            changes=[("bindPort", "18010"), ("maxPortsPerClient", "30")],
            lifecycle=lc,
            restart=False,
        )

        assert outcome.applied is True
        data = tomllib.loads(inst.config.read_text("utf-8"))
        assert data["bindPort"] == 18010
        assert data["maxPortsPerClient"] == 30
        assert len(inst.history_entries()) == 1, "多键变更只应产生一份快照"
        meta = json.loads((inst.history_entries()[0] / "meta.json").read_text("utf-8"))
        assert meta["action"].startswith("edit many:"), meta

    def test_cas_rejects_when_file_changed(self, inst, write_config) -> None:
        """`expected_current` 不匹配 → 拒绝（预览与落盘之间的并发保护）。"""
        from frpsctl.core.transaction import apply_sets

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))
        stale = cfg.read_config_text(inst.config)

        apply_set(inst, dotted="maxPortsPerClient", raw="30", lifecycle=lc, restart=False)

        with pytest.raises(ConfigError, match="预览期间"):
            apply_sets(
                inst,
                changes=[("bindPort", "18011")],
                expected_current=stale,
                lifecycle=lc,
                restart=False,
            )
        assert "bindPort = 17000" in inst.config.read_text("utf-8")

    def test_multi_key_noop_produces_no_snapshot(self, inst, write_config) -> None:
        from frpsctl.core.transaction import apply_sets

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))
        outcome = apply_sets(
            inst, changes=[("bindPort", "17000")], lifecycle=lc, restart=False
        )
        assert outcome.noop is True
        assert not inst.history_entries()

    def test_unsets_apply_in_same_transaction(self, inst, write_config) -> None:
        """删除键与赋值混在同一次事务：一份快照、一次落盘、键真的消失。

        Web 的"改两项 + 删一项"必须是**一次**操作——拆成两个事务会重启两次，
        中间那次还可能撞上危险组合检查。
        """
        from frpsctl.core.transaction import apply_sets

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))

        outcome = apply_sets(
            inst,
            changes=[("bindPort", "18012")],
            unsets=["webServer.user"],
            lifecycle=lc,
            restart=False,
        )

        assert outcome.applied is True
        data = tomllib.loads(inst.config.read_text("utf-8"))
        assert data["bindPort"] == 18012
        assert "user" not in data["webServer"]
        assert len(inst.history_entries()) == 1, "混合变更只应产生一份快照"

    def test_conflicting_set_and_unset_is_usage_error(self, inst, write_config) -> None:
        """同一键既赋值又删除 → 用法错误，且线上文件零影响。"""
        from frpsctl.core.transaction import apply_sets
        from frpsctl.errors import UsageError

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))
        with pytest.raises(UsageError, match="同时赋值与删除"):
            apply_sets(
                inst,
                changes=[("bindPort", "18013")],
                unsets=["bindPort"],
                lifecycle=lc,
                restart=False,
            )
        assert "bindPort = 17000" in inst.config.read_text("utf-8")

    def test_snapshot_diff_matches_cli_semantics(self, inst, write_config) -> None:
        """`snapshot_diff`：与"第 N 新快照"比较；无快照/越界是配置错误。

        它是 CLI `config diff` 与 Web `history/{steps}/diff` 的**唯一**实现，
        因此边界错误（无快照、越界）也在这里钉死。
        """
        from frpsctl.core.transaction import apply_set, snapshot_diff
        from frpsctl.errors import ConfigError

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))

        with pytest.raises(ConfigError, match="没有配置快照"):
            snapshot_diff(inst, steps=1)

        apply_set(inst, dotted="bindPort", raw="18014", lifecycle=lc, restart=False)
        result = snapshot_diff(inst, steps=1)
        assert result.snapshot.name.startswith("0001")
        assert "-bindPort = 17000" in result.diff
        assert "+bindPort = 18014" in result.diff

        with pytest.raises(ConfigError, match="只找到 1 份快照"):
            snapshot_diff(inst, steps=2)

    def test_steps_below_one_is_usage_error(self, inst, write_config) -> None:
        """`max(0, steps-1)` 会把 0/-1 静默归一成"回滚/比较一步"——必须拒绝。

        与 CLI 的 `min=1` 约束同一条纪律（v0.2.3）：参数笔误不能变成另一个动作。
        core 入口独立防御，未来的新调用方不会踩到静默归一。
        """
        from frpsctl.core.transaction import apply_set, rollback_to, snapshot_diff
        from frpsctl.errors import UsageError

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))
        apply_set(inst, dotted="bindPort", raw="18015", lifecycle=lc, restart=False)

        for bad in (0, -1):
            with pytest.raises(UsageError, match="必须 >= 1"):
                snapshot_diff(inst, steps=bad)
            with pytest.raises(UsageError, match="必须 >= 1"):
                rollback_to(inst, steps=bad, lifecycle=lc, restart=False)


class TestHealthTick:
    """健康等待期的进度回调（`on_tick`）——CLI 渲染逐轮进度的唯一通道。"""

    def test_tick_fires_each_poll_until_gate(self, inst, write_config, monkeypatch) -> None:
        from frpsctl.core.health import HealthLayer, HealthReport

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))

        sequence = iter(
            [
                HealthReport(HealthLayer.OK, HealthLayer.FAIL, HealthLayer.SKIPPED),
                HealthReport(HealthLayer.OK, HealthLayer.FAIL, HealthLayer.SKIPPED),
                HealthReport(HealthLayer.OK, HealthLayer.OK, HealthLayer.SKIPPED),
            ]
        )
        monkeypatch.setattr(lc, "check_health", lambda **_kw: next(sequence))
        monkeypatch.setattr("frpsctl.core.lifecycle.time.sleep", lambda _s: None)

        ticks: list[str] = []
        outcome = lc._await_health(
            5.0, on_tick=lambda _elapsed, report: ticks.append(report.l2_control.value)
        )
        assert outcome.gate is True
        assert ticks == ["fail", "fail"], ticks

    def test_tick_not_fired_when_healthy_immediately(self, inst, write_config, monkeypatch) -> None:
        from frpsctl.core.health import HealthLayer, HealthReport

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))
        monkeypatch.setattr(
            lc,
            "check_health",
            lambda **_kw: HealthReport(HealthLayer.OK, HealthLayer.OK, HealthLayer.SKIPPED),
        )

        ticks: list[float] = []
        lc._await_health(5.0, on_tick=lambda elapsed, _report: ticks.append(elapsed))
        assert ticks == [], "首轮即过 gate 不该触发进度回调"


    def test_tick_exception_does_not_break_the_wait(self, inst, write_config, monkeypatch) -> None:
        """进度回调抛异常必须被隔离——展示层不能拖垮启动流程。

        回归（第六轮 review 实测复现）：tick 抛 `BrokenPipeError`（stderr 管道
        断开）时会冒泡进 `start()` 的 `except BaseException`，触发
        `_reap_after_failure` 把刚派生的 frps 误杀。
        """
        from frpsctl.core.health import HealthLayer, HealthReport

        write_config(BASIC_CONFIG)
        lc = make_lifecycle(inst, make_fake_frps(inst.bin_dir))

        sequence = iter(
            [
                HealthReport(HealthLayer.OK, HealthLayer.FAIL, HealthLayer.SKIPPED),
                HealthReport(HealthLayer.OK, HealthLayer.OK, HealthLayer.SKIPPED),
            ]
        )
        monkeypatch.setattr(lc, "check_health", lambda **_kw: next(sequence))
        monkeypatch.setattr("frpsctl.core.lifecycle.time.sleep", lambda _s: None)

        def broken_tick(_elapsed: float, _report: object) -> None:
            raise BrokenPipeError("EPIPE")

        outcome = lc._await_health(5.0, on_tick=broken_tick)
        assert outcome.gate is True


class TestV2ApiAssertion:
    """ADR-3：启动后必须确认 v2 API 真的在。

    版本门槛（§3.6）已保证 >= 0.70.0，因此拿到 404 说明**二进制与预期不符**
    （例如 --binary 指向自编译版本）。此时应当报错退出 7，而不是降级——降级会
    让"状态显示"从此悄悄出错。
    """

    def test_missing_v2_api_is_reported(self, inst, write_config):
        from frpsctl.errors import ApiVersionMismatch

        write_config(BASIC_CONFIG)
        # 这个假二进制只有 /healthz，没有 v2 API
        fake = make_fake_frps(inst.bin_dir, no_v2=True)
        lc = make_lifecycle(inst, fake)

        with pytest.raises(ApiVersionMismatch) as excinfo:
            lc.start(health_timeout=5)
        assert int(excinfo.value.exit_code) == 7
        # 关键是：**不能**留下一个跑着的进程（校验失败要么重来要么收拾干净）
        assert not inst.state.exists()
        assert lc.state()[0] is State.STOPPED

    def test_dashboard_disabled_skips_the_check(self, inst, write_config):
        write_config('bindPort = 17000\n[auth]\ntoken = "t"\n[webServer]\nport = 0\n')
        fake = make_fake_frps(inst.bin_dir, no_v2=True)
        lc = make_lifecycle(inst, fake)
        report = lc.start(health_timeout=3)  # 未启用 dashboard → 不做 v2 校验
        try:
            assert report.healthy is True
        finally:
            kill_quietly(report.pid)
