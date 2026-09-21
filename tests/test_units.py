"""单元层：进程原语、锁、原子写、语义校验（设计文档 §13 单元层）。

这一层守的是**边界**：进程身份、并发、配置往返。这些地方的错误不会让程序
崩溃，只会让它悄悄做错事——所以每条断言都对应一个具体的失效场景。
"""

from __future__ import annotations

import errno
import os
import subprocess
from pathlib import Path

import pytest

from frpsctl.core import config as cfg
from frpsctl.core import platform as plat
from frpsctl.core.lock import instance_lock, is_locked
from frpsctl.errors import ConfigError, LockBusy, TemplateSyntaxRejected

from .conftest import BASIC_CONFIG, make_fake_frps


# ---------------------------------------------------------------------------
# core/platform.py —— 进程原语
# ---------------------------------------------------------------------------


class TestPidAlive:
    def test_current_process_is_alive(self) -> None:
        assert plat.pid_alive(os.getpid()) is True

    def test_non_positive_pid_is_not_alive(self) -> None:
        # pid 0 表示"当前进程组"，1 表示 init——都不是我们要探测的对象
        assert plat.pid_alive(0) is False
        assert plat.pid_alive(-1) is False

    def test_unused_pid_is_not_alive(self) -> None:
        assert plat.pid_alive(999_999) is False

    def test_permission_error_counts_as_alive(self, monkeypatch) -> None:
        """§3.4：PermissionError 表示"存在但不属于当前用户" → 必须判为存活。

        判成"未运行"会导致重复启动与端口冲突——这是最容易犯的反向错误。
        """

        def fake_kill(pid: int, sig: int) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "kill", fake_kill)
        assert plat.pid_alive(4 * 1024) is True


class TestProcStartTime:
    def test_matches_self(self) -> None:
        assert plat.proc_start_time(os.getpid()) is not None

    def test_missing_pid_returns_none(self) -> None:
        assert plat.proc_start_time(999_999) is None

    def test_parses_comm_containing_spaces_and_parens(self, tmp_path, monkeypatch) -> None:
        """§8.1：comm 字段可能含空格与右括号，必须从**最后一个** `)` 处切分。

        造一个假的 /proc 文件：comm 是 `weird) name (x`，后面 20 个字段，
        第 20 个（索引 19）是 starttime = 424242。
        """
        fake_stat = b"1234 (weird) name (x) S " + b" ".join([b"0"] * 18 + [b"424242"]) + b"\n"
        fake_proc = tmp_path / "proc"
        pid_dir = fake_proc / "1234"
        pid_dir.mkdir(parents=True)
        (pid_dir / "stat").write_bytes(fake_stat)

        real_path = Path

        class FakePath(real_path):  # type: ignore[misc]
            def __new__(cls, *args, **kwargs):  # noqa: D102
                if args and isinstance(args[0], str) and args[0].startswith("/proc/"):
                    return real_path(str(fake_proc / args[0].split("/proc/", 1)[1]))
                return real_path(*args, **kwargs)

        monkeypatch.setattr("frpsctl.core.platform.Path", FakePath)
        assert plat.proc_start_time(1234) == 424242

    def test_too_few_fields_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "frpsctl.core.platform.Path",
            lambda _p: type("P", (), {"read_bytes": staticmethod(lambda: b"1 (x) S 0 0")})(),
        )
        assert plat.proc_start_time(1234) is None


class TestProcCmdline:
    def test_reads_self(self) -> None:
        argv = plat.proc_cmdline(os.getpid())
        assert argv, "自己的 cmdline 不该为空"
        assert "python" in argv[0].lower() or argv[0].endswith("pytest")

    def test_missing_pid_returns_empty(self) -> None:
        assert plat.proc_cmdline(999_999) == []


class TestAssertSupported:
    def test_ok_on_linux(self) -> None:
        plat.assert_supported()  # 本测试只在 Linux 上跑，不该抛

    def test_rejects_when_no_proc(self, monkeypatch) -> None:
        """§3.4：非 Linux 直接拒绝，而降级会导致身份校验失效。"""
        from frpsctl.errors import UnsupportedPlatform

        monkeypatch.setattr(plat, "IS_LINUX", False)
        with pytest.raises(UnsupportedPlatform):
            plat.assert_supported()


# ---------------------------------------------------------------------------
# core/lock.py —— 实例级互斥
# ---------------------------------------------------------------------------


class TestInstanceLock:
    def test_excludes_other_processes(self, tmp_path) -> None:
        """跨进程互斥：这才是锁存在的意义（frpsctl 之间的并发 start/stop）。

        用子进程验证而不是同进程另开 fd：`flock` 是 **fd 级** 的，同进程内
        另开 fd 再锁同一文件会阻塞自己——而配置变更事务恰好需要这种可重入
        （它持锁后内部会调 restart）。所以本测试用一个真正独立的进程来验证
        互斥，另有用例验证可重入。
        """
        import subprocess
        import sys

        path = tmp_path / ".lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

        with instance_lock(path, timeout=1.0):
            # 子进程尝试非阻塞获取同一把锁，必须失败
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import fcntl,os,sys\n"
                    "fd=os.open(sys.argv[1], os.O_RDWR)\n"
                    "try:\n"
                    "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                    "except BlockingIOError:\n"
                    "    sys.exit(7)\n"
                    "sys.exit(0)\n",
                    str(path),
                ],
                capture_output=True,
            )
            assert proc.returncode == 7, "另一个进程竟然拿到了锁"

        # 释放后子进程应当能拿到
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl,os,sys\n"
                "fd=os.open(sys.argv[1], os.O_RDWR)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "sys.exit(0)\n",
                str(path),
            ],
            capture_output=True,
        )
        assert proc.returncode == 0, "锁未随 fd 释放"

    def test_released_after_exit(self, tmp_path) -> None:
        path = tmp_path / ".lock"
        with instance_lock(path, timeout=0.5):
            pass
        with instance_lock(path, timeout=0.5):
            pass  # 不该抛

    def test_released_on_exception(self, tmp_path) -> None:
        path = tmp_path / ".lock"
        with pytest.raises(RuntimeError), instance_lock(path, timeout=0.5):
            raise RuntimeError("boom")
        with instance_lock(path, timeout=0.5):
            pass

    def test_is_locked_probe(self, tmp_path) -> None:
        path = tmp_path / ".lock"
        assert is_locked(path) is False  # 文件都不存在
        with instance_lock(path, timeout=0.5):
            assert is_locked(path) is True

    def test_creates_lock_with_0600(self, tmp_path) -> None:
        path = tmp_path / "sub" / ".lock"
        with instance_lock(path, timeout=0.5):
            mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_is_reentrant_within_same_thread(self, tmp_path) -> None:
        """回归：配置变更事务持锁后会在内部调 restart()，而 restart 也要取锁。

        `flock` 按 **fd** 计，同一进程另开 fd 再锁同一文件会阻塞自己——那样
        每一次 `config set` 都会以"变更后启动失败"告终（而真因是事务自锁）。
        因此锁必须对同一线程可重入。
        """
        path = tmp_path / ".lock"
        with instance_lock(path, timeout=0.5):
            with instance_lock(path, timeout=0.5):  # 嵌套获取不得阻塞
                pass
            assert is_locked(path) is True  # 外层仍持锁

    def test_nested_release_is_balanced(self, tmp_path) -> None:
        path = tmp_path / ".lock"
        with instance_lock(path, timeout=0.5), instance_lock(path, timeout=0.5):
            pass
        assert is_locked(path) is False  # 计数归零后才真正释放
        with instance_lock(path, timeout=0.5):
            pass

    def test_exclusive_across_threads(self, tmp_path) -> None:
        """不同线程之间必须真正互斥（否则并发 start 会双起）。"""
        import threading

        path = tmp_path / ".lock"
        acquired: list[bool] = []

        def worker() -> None:
            try:
                with instance_lock(path, timeout=0.4):
                    acquired.append(True)
            except LockBusy:
                acquired.append(False)

        with instance_lock(path, timeout=0.5):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()
        assert acquired == [False], "另一个线程不该拿到本线程持有的锁"


# ---------------------------------------------------------------------------
# core/config.py —— 无损补丁与原子写
# ---------------------------------------------------------------------------


SAMPLE = """\
# frps 示例配置（这段注释必须原样存活）
bindPort = 7000        # 行内注释也要活着

[webServer]
addr = "127.0.0.1"
port = 7500

# 下面是安全相关设置
[transport.tls]
force = true
"""


class TestPlanSet:
    def test_preserves_comments_and_layout(self, tmp_path) -> None:
        """ADR-2 的核心不变量：除目标键外每个字节都不变。"""
        path = tmp_path / "frps.toml"
        path.write_text(SAMPLE, "utf-8")

        plan = cfg.plan_set(path, "bindPort", "8000")

        assert "# frps 示例配置（这段注释必须原样存活）" in plan.text
        assert "# 行内注释也要活着" in plan.text
        assert "# 下面是安全相关设置" in plan.text
        assert "[webServer]" in plan.text
        assert "bindPort = 8000" in plan.text
        # 改动只有一行
        changed = [
            ln
            for ln in plan.diff.splitlines()
            if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
        ]
        assert len(changed) == 2, plan.diff

    def test_nested_key_creates_missing_table(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        plan = cfg.plan_set(path, "transport.tls.force", "true")
        assert "[transport" in plan.text or "transport.tls" in plan.text
        assert "force = true" in plan.text

    def test_does_not_drop_unknown_keys(self, tmp_path) -> None:
        """模型未覆盖的键（allowPorts / httpPlugins）绝不能被 config set 丢掉。"""
        path = tmp_path / "frps.toml"
        path.write_text(
            "bindPort = 7000\nmaxPortsPerClient = 20\n"
            '[[httpPlugins]]\nname = "auth"\naddr = "http://127.0.0.1:8080"\n',
            "utf-8",
        )
        plan = cfg.plan_set(path, "bindPort", "8000")
        assert "maxPortsPerClient = 20" in plan.text
        assert 'name = "auth"' in plan.text
        assert 'addr = "http://127.0.0.1:8080"' in plan.text

    def test_type_coercion(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        assert cfg.plan_set(path, "bindPort", "8000").after == 8000
        assert cfg.plan_set(path, "transport.tls.force", "false").after is False

    @pytest.mark.parametrize("raw", ['"{{ .Envs.HOME }}"', '["{{ x }}"]'])
    def test_rejects_template_syntax(self, tmp_path, raw) -> None:
        """§3.1 推论 2：frp 会做 text/template 渲染，因此含 `{{` 的值必须拒绝。"""
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        with pytest.raises(TemplateSyntaxRejected):
            cfg.plan_set(path, "subDomainHost", raw)

    def test_noop_detection(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        assert cfg.plan_set(path, "bindPort", "7000").is_noop is True
        assert cfg.plan_set(path, "bindPort", "7001").is_noop is False

    def test_does_not_touch_original_file(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text(SAMPLE, "utf-8")
        cfg.plan_set(path, "bindPort", "8000")
        assert path.read_text("utf-8") == SAMPLE  # plan 只做内存补丁


class TestAtomicWrite:
    def test_result_is_complete(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        cfg.atomic_write(path, "bindPort = 7000\n")
        assert path.read_text("utf-8") == "bindPort = 7000\n"

    def test_mode_is_0600_from_the_start(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        cfg.atomic_write(path, "auth = 1\n")
        assert path.stat().st_mode & 0o777 == 0o600

    def test_original_survives_mid_write_failure(self, tmp_path, monkeypatch) -> None:
        """§13 单元层：写入中途抛异常时，原文件必须完好。

        这是原子写存在的理由——否则断电/被杀会留下半截配置。
        """
        path = tmp_path / "frps.toml"
        path.write_text("original = true\n", "utf-8")

        real_replace = os.replace

        def boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("模拟写入中途失败")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            cfg.atomic_write(path, "new = true\n")
        monkeypatch.setattr(os, "replace", real_replace)

        assert path.read_text("utf-8") == "original = true\n"
        # 且不留下临时文件
        leftovers = [p for p in tmp_path.iterdir() if p.name != "frps.toml"]
        assert leftovers == [], leftovers

    def test_overwrites_atomically(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        cfg.atomic_write(path, "a = 1\n")
        cfg.atomic_write(path, "b = 2\n")
        assert path.read_text("utf-8") == "b = 2\n"

    def test_preserves_existing_owner(self, tmp_path, monkeypatch) -> None:
        """重建文件必须保留原属主。

        回归：systemd 部署会把配置/目录**移交**给服务用户（§12.2），而
        `config set` 走 atomic_write 重建文件——若属主被换回安装者（root），
        frps 用户下次启动就读不到配置，unit 直接失败，且报错现场离本次
        操作很远。
        """
        path = tmp_path / "frps.toml"
        path.write_text("a = 1\n", "utf-8")
        expected = os.stat(path)

        recorded: list[tuple[int, int]] = []
        real_fchown = os.fchown

        def spy(fd, uid, gid):
            recorded.append((uid, gid))
            return real_fchown(fd, uid, gid)

        monkeypatch.setattr(os, "fchown", spy)
        cfg.atomic_write(path, "b = 2\n")

        assert recorded == [(expected.st_uid, expected.st_gid)]
        assert path.read_text("utf-8") == "b = 2\n"

    def test_fchown_failure_does_not_break_write(self, tmp_path, monkeypatch) -> None:
        """非 root / 不支持 chown 的文件系统上，写盘不能被属主保留拖垮。"""
        path = tmp_path / "frps.toml"
        path.write_text("a = 1\n", "utf-8")

        def boom(*args, **kwargs):
            raise PermissionError("not permitted")

        monkeypatch.setattr(os, "fchown", boom)
        cfg.atomic_write(path, "b = 2\n")
        assert path.read_text("utf-8") == "b = 2\n"

    def test_new_file_has_no_owner_to_preserve(self, tmp_path) -> None:
        """目标不存在时正常落盘（fchown 那条路径不得反过来破坏写入）。"""
        path = tmp_path / "fresh.toml"
        cfg.atomic_write(path, "a = 1\n")
        assert path.read_text("utf-8") == "a = 1\n"


class TestSecrets:
    def test_secret_detection(self) -> None:
        assert cfg.is_secret_key("webServer.password") is True
        assert cfg.is_secret_key("auth.token") is True
        assert cfg.is_secret_key("bindPort") is False
        assert cfg.is_secret_key("auth.oidc.clientSecret") is True


class TestMaskDiff:
    """`mask_diff` 是 diff 输出（config set / edit / diff / rollback）的最后一道闸门。

    回归：它此前只识别逐行赋值（`token = "..."`），而 **TOML 内联表**会把机密
    藏进值里（`auth = { token = "..." }`）——实测确认原文会被打印出来，
    等于给 §10 硬约束 2 开了一个出口。
    """

    SECRET = "SUPERSECRET123456"

    def test_masks_plain_assignment(self) -> None:
        diff = '+[auth]\n+token = "SUPERSECRET123456"\n'
        out = cfg.mask_diff(diff)
        assert self.SECRET not in out
        assert "***" in out

    def test_masks_inline_table(self) -> None:
        diff = f'+auth = {{ token = "{self.SECRET}", method = "token" }}\n'
        out = cfg.mask_diff(diff)
        assert self.SECRET not in out, f"内联表机密泄露：{out}"
        # 非敏感键必须保留（打码不能把有用信息一起抹掉）
        assert "method" in out

    def test_masks_nested_inline_table(self) -> None:
        diff = f'+webServer = {{ addr = "127.0.0.1", creds = {{ password = "{self.SECRET}" }} }}\n'
        assert self.SECRET not in cfg.mask_diff(diff)

    def test_masks_inline_table_under_a_table_header(self) -> None:
        diff = f'+[auth]\n+oidc = {{ clientSecret = "{self.SECRET}" }}\n'
        assert self.SECRET not in cfg.mask_diff(diff)

    def test_keeps_non_secret_inline_values_untouched(self) -> None:
        diff = '+allowPorts = [{ start = 6000, end = 6100 }]\n'
        assert cfg.mask_diff(diff) == diff

    def test_masks_array_of_inline_tables_with_secret(self) -> None:
        diff = f'+[[httpPlugins]]\n+name = "x"\n+metadata = [{{ token = "{self.SECRET}" }}]\n'
        assert self.SECRET not in cfg.mask_diff(diff)

    def test_array_table_header_does_not_corrupt_table_tracking(self) -> None:
        """`[[httpPlugins]]` 的表名解析必须正确（此前正则会把 `[` 带进表名）。"""
        diff = '+[[httpPlugins]]\n+token = "SUPERSECRET123456"\n'
        out = cfg.mask_diff(diff)
        assert self.SECRET not in out, f"数组表下的敏感键未被识别：{out}"

    def test_unparsable_value_with_secret_like_content_is_masked_conservatively(self) -> None:
        """值解析失败但形似含机密时，**保守打码**而不是原样输出。"""
        diff = '+auth = { token = "SUPERSECRET123456"\n'  # 缺右括号，解析必失败
        out = cfg.mask_diff(diff)
        assert self.SECRET not in out, f"不可解析行未保守打码：{out}"

    def test_masks_multiline_double_quoted_string(self) -> None:
        """三引号多行字符串：值本体与内容行都必须打码。

        回归：状态机之前只处理单行，`token = \"\"\"` 之后的内容行原样打印。
        """
        diff = f'+token = """\n+{self.SECRET}\n+"""\n'
        out = cfg.mask_diff(diff)
        assert self.SECRET not in out, f"多行字符串泄露：{out}"

    def test_masks_multiline_literal_string(self) -> None:
        diff = f"+token = '''\n+{self.SECRET}\n+'''\n"
        assert self.SECRET not in cfg.mask_diff(diff)

    def test_masks_inline_table_split_across_lines(self) -> None:
        """内联表被拆成多行书写：起始行打码，续行定点打码。"""
        diff = f'+auth = {{\n+  token = "{self.SECRET}",\n+}}\n'
        out = cfg.mask_diff(diff)
        assert self.SECRET not in out, f"多行内联表泄露：{out}"

    def test_masks_secret_nested_in_multiline_array(self) -> None:
        """非敏感键的多行数组里内嵌敏感内联表片段。"""
        diff = f'+foo = [\n+  {{ token = "{self.SECRET}" }},\n+]\n'
        out = cfg.mask_diff(diff)
        assert self.SECRET not in out, f"多行数组内嵌机密泄露：{out}"

    def test_masks_secret_in_unclosed_start_line(self) -> None:
        """非敏感键的未闭合起始行里已经写着敏感片段。"""
        diff = f'+foo = {{ token = "{self.SECRET}"\n'
        assert self.SECRET not in cfg.mask_diff(diff)

    def test_recovers_after_multiline_structure(self) -> None:
        """跨行结构闭合后，后续行必须恢复常规处理（状态机不能卡死）。"""
        diff = (
            f'+auth = {{\n+  token = "{self.SECRET}",\n+}}\n'
            "+bindPort = 7000\n"
            f'+token = "{self.SECRET}_B"\n'
        )
        out = cfg.mask_diff(diff)
        assert self.SECRET not in out
        assert "bindPort = 7000" in out

    def test_non_secret_multiline_array_is_untouched(self) -> None:
        """无敏感内容的多行数组必须原样保留（打码不能牺牲可读性）。"""
        diff = "+allowPorts = [\n+  { start = 6000, end = 6100 },\n+]\n"
        assert cfg.mask_diff(diff) == diff

    def test_bare_key_is_treated_as_secret(self) -> None:
        """裸 `token = "..."`（无表头）也必须打码（防御性判定）。"""
        diff = f'+token = "{self.SECRET}"\n'
        assert self.SECRET not in cfg.mask_diff(diff)


class TestPlanSetMany:
    """`plan_set_many`：多键一次补丁（Web 配置表单的底座）。"""

    def test_edits_multiple_keys_at_once(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text(SAMPLE, "utf-8")

        plan = cfg.plan_set_many(path, [("bindPort", "8000"), ("webServer.port", "8500")])

        assert "bindPort = 8000" in plan.text
        assert "port = 8500" in plan.text
        assert plan.before == {"bindPort": 7000, "webServer.port": 7500}
        assert plan.after == {"bindPort": 8000, "webServer.port": 8500}
        assert plan.is_noop is False
        # 注释与排版照常保留（与单键补丁同一套定点赋值）
        assert "# frps 示例配置（这段注释必须原样存活）" in plan.text
        assert "# 行内注释也要活着" in plan.text

    def test_noop_when_all_unchanged(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        assert cfg.plan_set_many(path, [("bindPort", "7000")]).is_noop is True

    def test_last_write_wins_for_same_key(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        plan = cfg.plan_set_many(path, [("bindPort", "8000"), ("bindPort", "9000")])
        assert plan.after == {"bindPort": 9000}
        assert "bindPort = 9000" in plan.text

    def test_any_invalid_key_rejects_the_whole_batch(self, tmp_path) -> None:
        """任一键非法 → 整批拒绝，线上文件零影响（原子性在内存层就成立）。"""
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        before = path.read_text("utf-8")
        with pytest.raises(ConfigError):
            cfg.plan_set_many(path, [("bindPort", "8000"), ("bindPort", "99999")])
        assert path.read_text("utf-8") == before

    def test_empty_changes_is_noop(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        plan = cfg.plan_set_many(path, [])
        assert plan.is_noop is True
        assert plan.text == "bindPort = 7000\n"


class TestHealthDetailAggregation:
    """健康 detail 必须把**所有失败层**的详情都带上。

    回归（P0 级展示缺陷）：L3 失败时 `detail = l3_detail` **直接覆盖** L2 的
    失败详情——控制面与插件同时挂掉时，`status` 里看不到"dashboard 为什么没
    起来"，而它恰恰是先要修的那一层（控制面恢复前连统计都拿不到）。
    """

    def test_render_shows_l2_detail_on_l2_failure(self) -> None:
        from frpsctl.core.health import HealthLayer, HealthReport

        report = HealthReport(
            HealthLayer.OK,
            HealthLayer.FAIL,
            HealthLayer.SKIPPED,
            detail="/healthz 无响应 (http://127.0.0.1:1)",
        )
        assert "/healthz 无响应" in report.render()

    def test_l2_and_l3_failures_are_both_visible(self, inst, write_config) -> None:
        from frpsctl.core.health import HealthLayer
        from frpsctl.core.lifecycle import Lifecycle

        from .conftest import free_port

        dead = free_port()
        write_config(
            BASIC_CONFIG.replace("17500", str(dead))
            + f'[[httpPlugins]]\nname = "auth"\naddr = "http://127.0.0.1:{dead}"\n'
            'path = "/handler"\nops = ["Login"]\n'
        )
        report = Lifecycle(inst).check_health()

        assert report.l2_control is HealthLayer.FAIL
        assert report.l3_plugin is HealthLayer.FAIL
        assert "/healthz 无响应" in report.detail, "L2 的失败详情被 L3 覆盖了"
        assert "auth 127.0.0.1" in report.detail, "L3 的失败详情缺失"


class TestPlanUnset:
    """`plan_unset`：删除单个键（回落 frp 默认），内存补丁不落盘。"""

    def test_removes_target_key_and_keeps_everything_else(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text(SAMPLE, "utf-8")

        plan = cfg.plan_unset(path, "webServer.port")

        assert plan.before == 7500
        assert plan.after is None
        assert "port = 7500" not in plan.text
        # 同表的其他键、注释、排版全部保留
        assert 'addr = "127.0.0.1"' in plan.text
        assert "# frps 示例配置（这段注释必须原样存活）" in plan.text
        assert "# 下面是安全相关设置" in plan.text
        assert "-port = 7500" in plan.diff
        # 只做内存补丁
        assert path.read_text("utf-8") == SAMPLE

    def test_removing_last_key_in_table_leaves_valid_toml(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text('bindPort = 7000\n\n[webServer]\nport = 7500\n', "utf-8")

        plan = cfg.plan_unset(path, "webServer.port")

        # 删空一张表后，剩下的文本必须仍是合法 TOML（空表头合法）
        import tomllib

        parsed = tomllib.loads(plan.text)
        assert parsed["bindPort"] == 7000
        assert parsed.get("webServer", {}) == {}

    def test_missing_key_is_a_config_error(self, tmp_path) -> None:
        """键不存在 → 配置错误(3)，而不是静默 noop。

        拼错键名得到"成功删除"会让用户以为清掉了某个设置，而它从未存在——
        与 `config get` 对不存在键报错同理（ADR-7：不猜测）。
        """
        from frpsctl.errors import ConfigKeyMissing

        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        with pytest.raises(ConfigKeyMissing):
            cfg.plan_unset(path, "webServer.port")

    def test_path_through_scalar_is_a_config_error(self, tmp_path) -> None:
        from frpsctl.errors import ConfigKeyMissing

        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        with pytest.raises(ConfigKeyMissing):
            cfg.plan_unset(path, "bindPort.sub")

    def test_illegal_dotted_name_is_usage_error(self, tmp_path) -> None:
        from frpsctl.errors import UsageError

        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        with pytest.raises(UsageError):
            cfg.plan_unset(path, ".bindPort")


class TestPlanChangeMany:
    """`plan_change_many`：set + unset 的混合内存补丁（Web 配置表单的候选生成）。

    它是"改两项 + 删一项 = 一次事务"的关键：所有操作作用在同一个文档上，
    一次 dumps 就是合并结果——拆成两次调用会重启两次。
    """

    def test_mixed_set_and_unset_in_one_document(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\nmaxPortsPerClient = 20\n", "utf-8")

        plan = cfg.plan_change_many(
            path, changes=[("bindPort", "8000")], unsets=["maxPortsPerClient"]
        )

        assert plan.before == {"bindPort": 7000, "maxPortsPerClient": 20}
        assert plan.after == {"bindPort": 8000, "maxPortsPerClient": None}
        assert plan.deletes == ("maxPortsPerClient",)
        assert plan.is_noop is False
        assert "bindPort = 8000" in plan.text
        assert "maxPortsPerClient" not in plan.text
        assert "-maxPortsPerClient = 20" in plan.diff
        # 只做内存补丁：线上文件一字未动
        assert path.read_text("utf-8") == "bindPort = 7000\nmaxPortsPerClient = 20\n"

    def test_conflict_between_set_and_unset_is_usage_error(self, tmp_path) -> None:
        """同一键既赋值又删除：两边的意图互相矛盾，必须拒绝而不是静默取其一。"""
        from frpsctl.errors import UsageError

        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        with pytest.raises(UsageError, match="同时赋值与删除"):
            cfg.plan_change_many(path, changes=[("bindPort", "8000")], unsets=["bindPort"])

    def test_missing_unset_key_is_config_error(self, tmp_path) -> None:
        """删除不存在的键 = 配置错误(3)（与 `plan_unset` 同一条语义）。"""
        from frpsctl.errors import ConfigKeyMissing

        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        with pytest.raises(ConfigKeyMissing):
            cfg.plan_change_many(path, changes=[], unsets=["webServer.port"])

    def test_unset_only_is_a_real_change(self, tmp_path) -> None:
        """只删除也是有效变更（`is_noop` 为假，after 为 None）。"""
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\nmaxPortsPerClient = 20\n", "utf-8")

        plan = cfg.plan_change_many(path, changes=[], unsets=["maxPortsPerClient"])

        assert plan.is_noop is False
        assert plan.after["maxPortsPerClient"] is None

    def test_noop_when_nothing_changes(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")

        plan = cfg.plan_change_many(path, changes=[("bindPort", "7000")], unsets=[])

        assert plan.is_noop is True
        assert plan.diff == ""

    def test_empty_change_is_a_noop_plan(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")

        plan = cfg.plan_change_many(path, changes=[], unsets=[])

        assert plan.text == "bindPort = 7000\n"
        assert plan.before == {} and plan.after == {}

    def test_plan_set_many_delegates(self, tmp_path) -> None:
        """`plan_set_many` 是纯 set 形态的兼容入口——行为必须与混合版一致。"""
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")

        plan = cfg.plan_set_many(path, [("bindPort", "8000")])

        assert plan.after["bindPort"] == 8000
        assert plan.deletes == ()

    def test_string_unsets_is_rejected(self, tmp_path) -> None:
        """字符串 unsets 会被**逐字符迭代**（"ab" → 删掉 a 与 b）——必须报错。

        对抗性实测复现：`plan_change_many(path, changes=[], unsets="ab")` 曾
        静默删掉两个单字符键。与 `policy._strict_list` 的教训同型："看似能跑"
        的输入必须在边界变成用法错误。
        """
        from frpsctl.errors import UsageError

        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\na = 1\nb = 2\n", "utf-8")
        with pytest.raises(UsageError, match="unsets 必须是键名数组"):
            cfg.plan_change_many(path, changes=[], unsets="ab")
        assert path.read_text("utf-8") == "bindPort = 7000\na = 1\nb = 2\n"

    def test_string_changes_is_rejected(self, tmp_path) -> None:
        """`changes` 传字符串同样拒绝（旧实现会解包失败成裸 ValueError）。"""
        from frpsctl.errors import UsageError

        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        with pytest.raises(UsageError, match="changes 必须"):
            cfg.plan_change_many(path, changes="bindPort")
        with pytest.raises(UsageError, match="changes 必须"):
            cfg.plan_change_many(path, changes={"bindPort": "8000"})

    def test_tuple_inputs_are_accepted(self, tmp_path) -> None:
        """list / tuple 两种容器都接受（核心内部一律用 tuple 传参）。"""
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\nmaxPortsPerClient = 20\n", "utf-8")

        plan = cfg.plan_change_many(
            path, changes=[["bindPort", "8000"]], unsets=("maxPortsPerClient",)
        )

        assert plan.after == {"bindPort": 8000, "maxPortsPerClient": None}
        assert plan.deletes == ("maxPortsPerClient",)


class TestGetValue:
    def test_missing_key_raises(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        doc = cfg.load_config(path)
        with pytest.raises(ConfigError):
            cfg.get_value(doc, "transport.tls.force")

    def test_nested_get(self, tmp_path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text(SAMPLE, "utf-8")
        doc = cfg.load_config(path)
        assert cfg.get_value(doc, "transport.tls.force") is True
        assert cfg.get_value(doc, "webServer.port") == 7500


# ---------------------------------------------------------------------------
# core/release.py —— 解包、复验、原子就位（供应链关键路径）
# ---------------------------------------------------------------------------


def make_fake_asset(
    tmp_path: Path,
    *,
    version: str = "0.71.0",
    members: dict[str, bytes] | None = None,
) -> Path:
    """造一个与官方资产**同结构**的 tar.gz（含 frps / frpc / LICENSE）。

    这一步不能省：`extract_frps` 的路径穿越防护、`_place_binary` 的
    "临时文件 → 复验 → `os.replace`"顺序，都只有在真实 tar 字节流上才测得出来。
    """
    import io
    import tarfile

    payload = members or {
        "frps": b"#!/bin/sh\necho " + version.encode() + b"\n",
        "frpc": b"#!/bin/sh\necho " + version.encode() + b"\n",
    }
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tar:
        for name, content in payload.items():
            info = tarfile.TarInfo(name=f"frp_{version}_linux_amd64/{name}")
            info.size = len(content)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(content))
    path = tmp_path / f"frp_{version}_linux_amd64.tar.gz"
    path.write_bytes(blob.getvalue())
    return path


class TestExtractFrps:
    def test_extracts_by_basename(self, tmp_path) -> None:
        from frpsctl.core import release as rel

        asset = make_fake_asset(tmp_path)
        assert rel.extract_frps(asset.read_bytes()) == b"#!/bin/sh\necho 0.71.0\n"

    def test_extracts_frpc_too(self, tmp_path) -> None:
        from frpsctl.core import release as rel

        asset = make_fake_asset(tmp_path)
        assert rel.extract_frps(asset.read_bytes(), member_name="frpc").startswith(b"#!/bin/sh")

    def test_missing_member_is_a_binary_error(self, tmp_path) -> None:
        from frpsctl.core import release as rel
        from frpsctl.errors import BinaryError

        asset = make_fake_asset(tmp_path, members={"frps": b"x"})
        with pytest.raises(BinaryError, match="找不到 frpc"):
            rel.extract_frps(asset.read_bytes(), member_name="frpc")

    def test_path_traversal_member_is_not_extracted(self, tmp_path) -> None:
        """`../../bin/sh` 这类名字的 basename 不是 frps，必须被跳过（不是落盘）。"""
        import io
        import tarfile

        from frpsctl.core import release as rel
        from frpsctl.errors import BinaryError

        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode="w:gz") as tar:
            content = b"evil"
            info = tarfile.TarInfo(name="../../bin/sh")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        with pytest.raises(BinaryError):
            rel.extract_frps(blob.getvalue())

    def test_directory_entry_is_skipped(self, tmp_path) -> None:
        """只有普通文件才算数：同名目录项不得被当成二进制读出。"""
        import io
        import tarfile

        from frpsctl.core import release as rel
        from frpsctl.errors import BinaryError

        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="pkg/frps")
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        with pytest.raises(BinaryError):
            rel.extract_frps(blob.getvalue())


class TestPlaceBinary:
    """`_place_binary` 是"下载→校验→就位"的最后一步，此前零覆盖。"""

    def test_places_executable_atomically(self, tmp_path) -> None:
        from frpsctl.core import release as rel

        asset = make_fake_asset(tmp_path)
        dest = tmp_path / "bin" / "frps-0.71.0"
        dest.parent.mkdir()
        rel._place_binary(asset.read_bytes(), member="frps", dest=dest)

        assert dest.read_bytes().startswith(b"#!/bin/sh")
        assert dest.stat().st_mode & 0o111, "落盘的二进制必须可执行"
        # 不留下临时文件
        assert not [p for p in dest.parent.iterdir() if p.name != dest.name]

    def test_reverify_failure_leaves_nothing_behind(self, tmp_path) -> None:
        """`-v` 复验失败（版本不达标）→ 目标文件不得出现，临时文件不得残留。

        这是"**校验不通过绝不落盘**"在最后一步的守卫：资产里的 frps 版本低于
        门槛（§3.6）时，必须在就位之前停下来。
        """
        from frpsctl.core import release as rel
        from frpsctl.errors import UnsupportedVersion

        asset = make_fake_asset(tmp_path, version="0.69.1")
        dest = tmp_path / "bin" / "frps-0.69.1"
        dest.parent.mkdir()

        with pytest.raises(UnsupportedVersion):
            rel._place_binary(asset.read_bytes(), member="frps", dest=dest)

        assert not dest.exists(), "复验失败却把二进制就位了"
        assert not list(dest.parent.iterdir()), "留下了临时文件"

    def test_reverify_timeout_is_a_binary_error(self, tmp_path, monkeypatch) -> None:
        """复验超时必须收口成 BinaryError(4)，不能漏出裸 TimeoutExpired。

        漏出去的话，CLI 会把它当"未分类错误(1)"——把"二进制有问题"误报成
        "工具内部出错"，用户据此会去查 frpsctl 的 bug 而不是那个二进制。
        """
        from frpsctl.core import release as rel
        from frpsctl.errors import BinaryError

        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="frps -v", timeout=10)

        monkeypatch.setattr(rel.subprocess, "run", boom)
        with pytest.raises(BinaryError) as excinfo:
            rel._verify_binary(tmp_path / "frps-0.71.0")
        assert int(excinfo.value.exit_code) == 4
        assert "超时" in excinfo.value.message

    def test_reverify_exec_failure_is_a_binary_error(self, tmp_path, monkeypatch) -> None:
        """EACCES / ENOEXEC 同样必须是 BinaryError(4)。"""
        from frpsctl.core import release as rel
        from frpsctl.errors import BinaryError

        def boom(*args, **kwargs):
            raise OSError(errno.ENOEXEC, "Exec format error")

        monkeypatch.setattr(rel.subprocess, "run", boom)
        with pytest.raises(BinaryError) as excinfo:
            rel._verify_binary(tmp_path / "frps-0.71.0")
        assert int(excinfo.value.exit_code) == 4


class TestInstallChecksumOrder:
    def test_checksum_is_checked_before_download(self, tmp_path, monkeypatch) -> None:
        """拿不到校验和时必须**在下载之前**拒绝。

        顺序反了也能得到正确结论，但会白下载一次，而且让 `--insecure` 的判断
        看起来发生在下载之后。
        """
        from frpsctl.core import release as rel
        from frpsctl.errors import ChecksumUnavailable

        downloads: list[str] = []

        def no_checksums(version, mirrors=None):
            raise rel.BinaryError("校验和文件取不到")

        def spy_download(asset, version, mirrors=None):
            downloads.append(asset)
            return b"should not be reached"

        monkeypatch.setattr(rel, "download_checksums", no_checksums)
        monkeypatch.setattr(rel, "download", spy_download)

        with pytest.raises(ChecksumUnavailable):
            rel.install(bin_dir=tmp_path / "bin", version="0.71.0")
        assert downloads == [], "校验和还没拿到就开始下载了"

    def test_insecure_still_downloads(self, tmp_path, monkeypatch) -> None:
        """`--insecure` 是显式跳过校验：此时必须继续走到下载。"""
        import io
        import tarfile

        from frpsctl.core import release as rel

        payload = b"#!/bin/sh\necho 0.71.0\n"
        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="pkg/frps")
            info.size = len(payload)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(payload))

        monkeypatch.setattr(
            rel, "download_checksums", lambda *_a, **_k: (_ for _ in ()).throw(rel.BinaryError("nope"))
        )
        monkeypatch.setattr(rel, "download", lambda *_a, **_k: blob.getvalue())

        result = rel.install(bin_dir=tmp_path / "bin", version="0.71.0", insecure=True)
        assert result.downloaded is True
        assert result.binary.exists()


class _FakeResponse:
    """最小 http 响应替身（只支持 with + read）。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class TestFetchFallback:
    """`_fetch` 的双通道：curl 失败必须回退 urllib（此前兜底代码不可达）。

    回归：`if shutil.which("curl"): ... raise OSError(...)` 让 curl 存在但失败
    （代理配置损坏、TLS 太老）时所有下载直接结束，而 urllib 那条路径永远不会
    被执行——镜像回退再多也没用。
    """

    def test_falls_back_to_urllib_when_curl_fails(self, monkeypatch) -> None:
        from frpsctl.core import release as rel

        monkeypatch.setattr(rel.shutil, "which", lambda _name: "/usr/bin/curl")
        monkeypatch.setattr(
            rel.subprocess,
            "run",
            lambda *a, **_k: subprocess.CompletedProcess(
                a[0] if a else "curl", 22, stdout="", stderr="boom"
            ),
        )
        monkeypatch.setattr(
            rel.urllib.request,
            "urlopen",
            lambda *_args, **_kwargs: _FakeResponse(b"from-urllib"),
        )
        assert rel._fetch("https://example.invalid/x") == b"from-urllib"

    def test_reports_both_channels_when_both_fail(self, monkeypatch) -> None:
        from frpsctl.core import release as rel

        monkeypatch.setattr(rel.shutil, "which", lambda _name: "/usr/bin/curl")
        monkeypatch.setattr(
            rel.subprocess,
            "run",
            lambda *a, **_k: subprocess.CompletedProcess(
                a[0] if a else "curl", 22, stdout="", stderr="boom"
            ),
        )

        def boom(url, timeout=None):
            raise OSError("dns failure")

        monkeypatch.setattr(rel.urllib.request, "urlopen", boom)
        with pytest.raises(OSError) as excinfo:
            rel._fetch("https://example.invalid/x")
        assert "curl" in str(excinfo.value)
        assert "urllib" in str(excinfo.value)

    def test_curl_progress_output_is_not_captured(self, monkeypatch) -> None:
        """curl 的 stderr 不得捕获：进度条与错误信息是唯一的可见通道。"""
        from frpsctl.core import release as rel

        calls: list[dict] = []

        def fake_run(argv, **kwargs):
            calls.append(kwargs)
            out = Path(argv[argv.index("-o") + 1])
            out.write_bytes(b"payload")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(rel.shutil, "which", lambda _name: "/usr/bin/curl")
        monkeypatch.setattr(rel.subprocess, "run", fake_run)
        assert rel._fetch("https://example.invalid/x") == b"payload"
        assert calls and "capture_output" not in calls[0], calls


class TestResolveMirrors:
    """镜像解析优先级：CLI 参数 > FRPSCTL_MIRROR > 内置。

    此前 DEFAULT_MIRRORS 硬编码在代码里，文档却写着"下载地址可被镜像替换"——
    用户拿到一个无法兑现的承诺。
    """

    def test_cli_wins_over_env(self, monkeypatch) -> None:
        from frpsctl.core import release as rel

        monkeypatch.setenv("FRPSCTL_MIRROR", "https://env.example")
        assert rel.resolve_mirrors(["https://cli.example/"]) == ("https://cli.example",)

    def test_env_used_when_no_cli(self, monkeypatch) -> None:
        from frpsctl.core import release as rel

        monkeypatch.setenv("FRPSCTL_MIRROR", "https://a.example, https://b.example/")
        assert rel.resolve_mirrors() == ("https://a.example", "https://b.example")

    def test_defaults_when_nothing_set(self, monkeypatch) -> None:
        from frpsctl.core import release as rel

        monkeypatch.delenv("FRPSCTL_MIRROR", raising=False)
        assert rel.resolve_mirrors() == rel.DEFAULT_MIRRORS

    def test_empty_items_are_ignored(self, monkeypatch) -> None:
        from frpsctl.core import release as rel

        monkeypatch.delenv("FRPSCTL_MIRROR", raising=False)
        assert rel.resolve_mirrors(["", "  "]) == rel.DEFAULT_MIRRORS


class TestWithFrpc:
    """`--with-frpc` 与"frps 是否已在盘上"必须**相互独立**。

    回归：此前 `install` 在 `dest.exists()` 时直接 `return`，于是"机器上已有 frps、
    现在想补装 frpc"永远装不上——而输出还报着成功（`downloaded=False`、退出码 0）。
    CI 恰好掩盖了它：CI 每次都是全新数据目录，必然走完整路径。

    与 `_place_binary` 的用例共用 `make_fake_asset`：这里把真实 tar 字节流喂给
    `download`，让被测代码走的是**真实的解包与复验路径**。
    """

    @staticmethod
    def _patch_download(monkeypatch, asset_path, rel) -> list[str]:
        """把 download/checksum 换成固定资产，并记录 download 被调用了几次。"""
        import hashlib

        calls: list[str] = []

        def fake_download(asset, version, mirrors=None, *, on_progress=None):
            calls.append(asset)
            if on_progress is not None:
                on_progress(0, None)
            return asset_path.read_bytes()

        digest = hashlib.sha256(asset_path.read_bytes()).hexdigest()
        monkeypatch.setattr(rel, "download", fake_download)
        monkeypatch.setattr(rel, "download_checksums", lambda *_a, **_k: f"{digest}  {asset_path.name}\n")
        return calls

    def test_installs_frpc_when_frps_already_present(self, tmp_path, monkeypatch) -> None:
        """**回归核心**：frps 已在盘上 → 仍必须取出 frpc。"""
        from frpsctl.core import release as rel

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = make_fake_asset(tmp_path)
        (bin_dir / "frps-0.71.0").write_bytes(b"#!/bin/sh\necho 0.71.0\n")
        calls = self._patch_download(monkeypatch, fake, rel)

        result = rel.install(bin_dir=bin_dir, version="0.71.0", with_frpc=True)

        assert calls, "已有 frps 时也必须下载（frpc 在同一个资产里）"
        assert (bin_dir / "frpc-0.71.0").exists(), "frps 已存在时 frpc 没有被安装"
        assert (bin_dir / "frpc").is_symlink()
        assert (bin_dir / "frpc").resolve().name == "frpc-0.71.0"
        assert result.downloaded is True

    def test_both_present_skips_network_entirely(self, tmp_path, monkeypatch) -> None:
        """两边都在盘上 → 一次网络都不该发（`downloaded=False`）。"""
        from frpsctl.core import release as rel

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "frps-0.71.0").write_bytes(b"x")
        (bin_dir / "frpc-0.71.0").write_bytes(b"x")
        calls = self._patch_download(monkeypatch, make_fake_asset(tmp_path), rel)

        result = rel.install(bin_dir=bin_dir, version="0.71.0", with_frpc=True)
        assert calls == [], "两边都已就位却仍然下载了"
        assert result.downloaded is False

    def test_without_flag_does_not_install_frpc(self, tmp_path, monkeypatch) -> None:
        """不带 `--with-frpc` 时不得顺手装 frpc（它是测试专用件，不是运维件）。"""
        from frpsctl.core import release as rel

        bin_dir = tmp_path / "bin"
        self._patch_download(monkeypatch, make_fake_asset(tmp_path), rel)

        rel.install(bin_dir=bin_dir, version="0.71.0")
        assert (bin_dir / "frps-0.71.0").exists()
        assert not (bin_dir / "frpc-0.71.0").exists()

    def test_force_reinstalls_frpc_alongside_frps(self, tmp_path, monkeypatch) -> None:
        """`--force` 对两者同时生效：不能只重装 frps 而漏掉 frpc。"""
        from frpsctl.core import release as rel

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "frps-0.71.0").write_bytes(b"old")
        (bin_dir / "frpc-0.71.0").write_bytes(b"old")
        self._patch_download(monkeypatch, make_fake_asset(tmp_path), rel)

        result = rel.install(bin_dir=bin_dir, version="0.71.0", with_frpc=True, force=True)
        assert result.downloaded is True
        assert (bin_dir / "frps-0.71.0").read_bytes().startswith(b"#!/bin/sh")
        assert (bin_dir / "frpc-0.71.0").read_bytes().startswith(b"#!/bin/sh")

    def test_only_download_does_not_touch_either_symlink(self, tmp_path, monkeypatch) -> None:
        """`--only-download` 对 frps 与 frpc 一视同仁：都只落盘、不切链。

        回归：frpc 软链此前**无条件**切换，同一个参数出现两套语义——"先把
        二进制备到多台机器、再统一切换"的运维节奏会被悄悄破坏。
        """
        from frpsctl.core import release as rel

        bin_dir = tmp_path / "bin"
        self._patch_download(monkeypatch, make_fake_asset(tmp_path), rel)

        result = rel.install(bin_dir=bin_dir, version="0.71.0", with_frpc=True, switch=False)

        assert (bin_dir / "frps-0.71.0").exists()
        assert (bin_dir / "frpc-0.71.0").exists()
        assert not (bin_dir / "frps").exists(), "frps 软链被切换了"
        assert not (bin_dir / "frpc").exists(), "frpc 软链被切换了"
        assert result.switched is False
        assert result.switched_frpc is False

    def test_both_present_repairs_frpc_symlink(self, tmp_path, monkeypatch) -> None:
        """两边都在盘上时，缺失的 frpc 软链会被幂等校正（与 frps 行为一致）。

        此前这条捷径只碰 frps 的链，`--with-frpc` 时手工删掉的 frpc 链
        永远修不回来。
        """
        from frpsctl.core import release as rel

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "frps-0.71.0").write_bytes(b"x")
        (bin_dir / "frpc-0.71.0").write_bytes(b"x")
        calls = self._patch_download(monkeypatch, make_fake_asset(tmp_path), rel)

        result = rel.install(bin_dir=bin_dir, version="0.71.0", with_frpc=True)

        assert calls == [], "已在盘上却下载了"
        assert (bin_dir / "frps").is_symlink()
        assert (bin_dir / "frpc").is_symlink()
        assert result.switched is True
        assert result.switched_frpc is True

    def test_both_present_only_download_keeps_symlinks_absent(self, tmp_path, monkeypatch) -> None:
        """两边在盘上 + `--only-download`：不建立任何软链。"""
        from frpsctl.core import release as rel

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "frps-0.71.0").write_bytes(b"x")
        (bin_dir / "frpc-0.71.0").write_bytes(b"x")
        self._patch_download(monkeypatch, make_fake_asset(tmp_path), rel)

        result = rel.install(bin_dir=bin_dir, version="0.71.0", with_frpc=True, switch=False)
        assert not (bin_dir / "frps").exists()
        assert not (bin_dir / "frpc").exists()
        assert result.switched is False
        assert result.switched_frpc is False


# ---------------------------------------------------------------------------
# core/instance.py —— 快照序号分配
# ---------------------------------------------------------------------------


class TestHistorySlot:
    def test_allocates_increasing_sequence(self, inst) -> None:
        first = inst.next_history_slot()
        second = inst.next_history_slot()
        assert first.name.startswith("0001-")
        assert second.name.startswith("0002-")

    def test_returns_a_created_directory(self, inst) -> None:
        """返回的路径必须**已经被占位**，否则并发调用会算出同一个候选名。"""
        slot = inst.next_history_slot()
        assert slot.is_dir()

    def test_exhaustion_raises_contract_error(self, inst, monkeypatch) -> None:
        """连续冲突耗尽重试 → 必须是契约内异常（不是裸 RuntimeError）。

        裸异常会被 CLI 映射成"未分类错误(1)"，用户看到的是"工具内部出错"，
        而真因是快照目录本身异常。

        构造方式：磁盘上密集存在 1000–1999 这 1000 个目录，但 `glob` **只报告到
        999**——模拟"扫过一次之后、写之前，序号被一段并发写者密集占满"的竞态
        （`next_history_slot` 的注释里就写着这正是 EEXIST 占位要防的事）。
        实现从 1000 开始试，1000 次 `mkdir` 恰好全部落在 1000–1999 上，
        重试耗尽 → 走耗尽分支。时钟同时固定，否则换一秒就换名字了。
        """
        from frpsctl.errors import FrpsctlError

        inst.history_dir.mkdir(parents=True, exist_ok=True)
        dense = [Path(f"{seq:04d}-20260101-000000") for seq in range(1000, 2000)]
        for path in dense:
            (inst.history_dir / path.name).mkdir()
        reported = [Path(f"{seq:04d}-20260101-000000") for seq in range(1, 1000)]

        monkeypatch.setattr(Path, "glob", lambda _self, _pattern: reported)
        monkeypatch.setattr("frpsctl.core.instance.time.strftime", lambda _fmt: "20260101-000000")

        with pytest.raises(FrpsctlError) as excinfo:
            inst.next_history_slot()
        assert int(excinfo.value.exit_code) == 1
        assert "快照序号" in excinfo.value.message
        # 不变量：耗尽时**不得**多创建任何目录（磁盘上仍是那 1000 个）
        assert len(list(inst.history_dir.iterdir())) == 1000

    def test_history_entries_are_newest_first(self, inst) -> None:
        inst.next_history_slot()
        inst.next_history_slot()
        names = [p.name for p in inst.history_entries()]
        assert names == sorted(names, reverse=True)


# ---------------------------------------------------------------------------
# cli —— 日志 tail 轮转检测与 NDJSON 输出
# ---------------------------------------------------------------------------


class TestLogTail:
    def test_reopen_if_rotated_detects_new_inode(self, tmp_path) -> None:
        """日志轮转（rename + 新建）后必须重开新文件——跟路径，不跟旧 inode。"""
        from frpsctl.cli import _reopen_if_rotated

        path = tmp_path / "frps.log"
        path.write_text("old\n", "utf-8")
        handle = path.open("r", encoding="utf-8")
        try:
            assert _reopen_if_rotated(path, handle) is handle  # 未轮转：原样返回

            path.rename(tmp_path / "frps.log.1")
            path.write_text("new\n", "utf-8")

            reopened = _reopen_if_rotated(path, handle)
            assert reopened is not handle, "轮转后没有重开新文件"
            assert reopened.readline() == "new\n"
            reopened.close()
        finally:
            handle.close()

    def test_reopen_if_rotated_survives_missing_file(self, tmp_path) -> None:
        """文件被移走、新文件还没建的窗口里不能抛异常（等待下一轮即可）。"""
        from frpsctl.cli import _reopen_if_rotated

        path = tmp_path / "frps.log"
        path.write_text("x\n", "utf-8")
        handle = path.open("r", encoding="utf-8")
        try:
            path.unlink()
            assert _reopen_if_rotated(path, handle) is handle
        finally:
            handle.close()


class TestParseListen:
    """`status` 的 listen 行来自这里（此前完全没有展示控制端口）。"""

    def test_reads_explicit_values(self, tmp_path) -> None:
        from frpsctl.core.healthcheck import parse_listen

        config = tmp_path / "frps.toml"
        config.write_text('bindAddr = "127.0.0.1"\nbindPort = 17000\n', "utf-8")
        info = parse_listen(config)
        assert info is not None
        assert (info.addr, info.port) == ("127.0.0.1", 17000)
        assert info.display == "127.0.0.1:17000"

    def test_defaults_when_keys_missing(self, tmp_path) -> None:
        """合法空配置的生效值是 frp 的默认 0.0.0.0:7000（附录 A）。"""
        from frpsctl.core.healthcheck import parse_listen

        config = tmp_path / "frps.toml"
        config.write_text("# 空配置\n", "utf-8")
        info = parse_listen(config)
        assert info is not None
        assert (info.addr, info.port) == ("0.0.0.0", 7000)

    def test_bind_port_zero_falls_back_to_default(self, tmp_path) -> None:
        """`bindPort = 0` 回落到默认 7000——**实测确认**（真 frps 0.71.0 的
        启动日志为 `frps tcp listen on 127.0.0.1:7000`）。

        回归：此前把 `<= 0` 当作"无监听"返回 None，而 frp 其实照常监听——
        用户会在 status 里看到一个真实存在却未展示的端口。
        """
        from frpsctl.core.healthcheck import parse_listen

        config = tmp_path / "frps.toml"
        config.write_text('bindAddr = "127.0.0.1"\nbindPort = 0\n', "utf-8")
        info = parse_listen(config)
        assert info is not None
        assert (info.addr, info.port) == ("127.0.0.1", 7000)

    def test_missing_file_returns_none(self, tmp_path) -> None:
        from frpsctl.core.healthcheck import parse_listen

        assert parse_listen(tmp_path / "nope.toml") is None

    def test_broken_file_returns_none(self, tmp_path) -> None:
        from frpsctl.core.healthcheck import parse_listen

        config = tmp_path / "frps.toml"
        config.write_text("bindPort = = =", "utf-8")
        assert parse_listen(config) is None

    def test_ipv6_display_is_bracketed(self, tmp_path) -> None:
        from frpsctl.core.healthcheck import parse_listen

        config = tmp_path / "frps.toml"
        config.write_text('bindAddr = "::"\nbindPort = 7000\n', "utf-8")
        info = parse_listen(config)
        assert info is not None
        assert info.display == "[::]:7000"


class TestIsLoopback:
    """全项目共用的回环判据（schema / doctor / plugin 都指向它）。"""

    def test_loopback_forms(self) -> None:
        from frpsctl.core.healthcheck import is_loopback

        for addr in ("127.0.0.1", "127.0.0.2", "localhost", "localhost:7500", "::1", "[::1]:7500"):
            assert is_loopback(addr) is True, addr

    def test_non_loopback_forms(self) -> None:
        from frpsctl.core.healthcheck import is_loopback

        for addr in ("0.0.0.0", "192.168.1.10", "example.com:7500", "::", "[::]:8080"):
            assert is_loopback(addr) is False, addr

    def test_dangerous_combination_ignores_loopback_network(self) -> None:
        """`127.0.0.2` 属于回环网段：不该被当成"对外暴露"。

        回归：doctor 与 schema 各有一套判据，`127.0.0.2` 会在 doctor 侧
        被误报为"监听非回环"。
        """
        from frpsctl.core.schema import check_dangerous_combination

        assert check_dangerous_combination({"webServer": {"addr": "127.0.0.2", "port": 7500}}) is None


class TestEmitJsonCompact:
    def test_compact_is_single_line(self, capsys) -> None:
        """`--watch --json` 用 NDJSON：每行一个完整对象，可被逐行消费。"""
        from frpsctl.cli import ui

        ui.emit_json({"a": 1, "nested": {"b": 2}}, compact=True)
        out = capsys.readouterr().out
        assert out == '{"a": 1, "nested": {"b": 2}}\n'

    def test_non_compact_is_pretty(self, capsys) -> None:
        from frpsctl.cli import ui

        ui.emit_json({"a": 1})
        out = capsys.readouterr().out
        assert "\n  " in out, "默认输出应当是缩进格式"
        assert out.endswith("\n")


# ---------------------------------------------------------------------------
# core/systemd.py —— unit 渲染与 systemctl 委托
# ---------------------------------------------------------------------------


class TestRenderUnit:
    """`render_unit` 是纯函数，但此前零覆盖——而它渲染出的 unit 决定了
    systemd 托管下的**全部**行为（§12.2）。"""

    def test_renders_all_placeholders(self, tmp_path) -> None:
        from frpsctl.core.systemd import render_unit

        text = render_unit(
            binary="/opt/frps/frps-0.71.0",
            config_dir=Path("/etc/frps/instances"),
            log_dir=Path("/var/log/frps"),
        )
        assert "ExecStart=/opt/frps/frps-0.71.0 -c /etc/frps/instances/%i/frps.toml" in text
        assert "WorkingDirectory=/etc/frps/instances/%i" in text
        # ReadWritePaths 必须同时包含日志目录与**实例目录**：ProtectSystem=strict
        # 下其余路径只读，而 frp 默认要往实例目录写 ./frps.log。
        assert "ReadWritePaths=/var/log/frps /etc/frps/instances/%i" in text
        # 实例名走 systemd 自己的说明符，不在渲染期展开
        assert "%i" in text
        assert "{exec_start}" not in text and "{config_dir}" not in text, "有占位符没被替换"

    def test_execstart_is_a_concrete_path_not_the_symlink(self) -> None:
        """ExecStart 必须是**具体版本路径**：软链换向不会让 systemd 重新读 unit。

        这条差异是 §8.6.1 要求 `install` 输出里显式提醒用户的原因，
        因此它是被测行为而不是实现细节。
        """
        from frpsctl.core.systemd import render_unit

        text = render_unit(
            binary="/data/frpsctl/bin/frps-0.71.0",
            config_dir=Path("/etc/frps/instances"),
            log_dir=Path("/var/log/frps"),
        )
        exec_line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
        assert exec_line.endswith("frps-0.71.0 -c /etc/frps/instances/%i/frps.toml")
        assert "/bin/frps " not in exec_line, "ExecStart 写了软链路径"

    def test_unit_name_uses_instance(self, inst) -> None:
        from frpsctl.core.systemd import Systemd

        assert Systemd(inst).unit_name == "frps@test.service"

    def test_renders_custom_user_and_group(self) -> None:
        """服务用户可配置（`--user`/`--group`）：模板不得把它们写死。"""
        from frpsctl.core.systemd import render_unit

        text = render_unit(
            binary="/opt/frps/frps-0.71.0",
            config_dir=Path("/etc/frps/instances"),
            log_dir=Path("/var/log/frps"),
            user="svc-frps",
            group="svc-frps",
        )
        assert "User=svc-frps" in text
        assert "Group=svc-frps" in text

    def test_group_defaults_to_user(self) -> None:
        from frpsctl.core.systemd import render_unit

        text = render_unit(
            binary="/opt/frps/frps-0.71.0",
            config_dir=Path("/etc/frps/instances"),
            log_dir=Path("/var/log/frps"),
            user="alice",
        )
        assert "User=alice" in text
        assert "Group=alice" in text


class TestSystemdDelegation:
    """委托路径此前零覆盖，而它正是 §15.5.4 里"文档承诺了、代码没做到"的那一处。"""

    @pytest.fixture
    def systemd(self, inst, tmp_path):
        from frpsctl.core.systemd import Systemd

        # unit 的 ExecStart 固定使用实例目录内的 frps.toml：部署体检要求它存在
        inst.config.write_text("bindPort = 17000\n", "utf-8")
        return Systemd(inst, unit_dir=tmp_path / "systemd")

    @pytest.fixture
    def recorded(self, monkeypatch):
        """拦截 systemctl 调用并记录 argv。"""
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    @pytest.fixture
    def as_root(self, monkeypatch):
        """`_require_root` 在方法内部 `import os`，因此 patch 全局 `os.geteuid` 即生效。

        **不能假设测试跑在 root 下**：本项目在开发机上以普通用户运行，而 CI 的
        runner 用户也不是 root——测试必须显式声明"我要 root 身份"，而不是靠环境。
        """
        monkeypatch.setattr(os, "geteuid", lambda: 0)

    @pytest.fixture
    def fake_accounts(self, monkeypatch):
        """把部署体检指向**当前进程的真实账户**。

        测试环境不能依赖系统里恰好有 `frps` 用户；返回真实 uid/gid 后，
        chown 是 no-op、权限位检查也符合预期。
        """
        monkeypatch.setattr(
            "frpsctl.core.systemd._account_ids",
            lambda *_: (os.getuid(), os.getgid()),
        )

    @pytest.fixture
    def foreign_accounts(self, monkeypatch):
        """假装服务用户是"另一个账户"（uid/gid 偏移）。

        用于覆盖"目录/文件对服务用户不可达"的分支：0700 的目录对偏移后的
        身份就是不可进入的。
        """
        monkeypatch.setattr(
            "frpsctl.core.systemd._account_ids",
            lambda *_: (os.getuid() + 12345, os.getgid() + 12345),
        )

    def test_start_stop_restart_delegate_to_systemctl(self, systemd, recorded, as_root) -> None:
        systemd.start()
        systemd.stop()
        systemd.restart()
        assert recorded == [
            ["systemctl", "start", "frps@test.service"],
            ["systemctl", "stop", "frps@test.service"],
            ["systemctl", "restart", "frps@test.service"],
        ]

    def test_install_template_writes_unit_and_enables(
        self, systemd, recorded, tmp_path, as_root, fake_accounts
    ) -> None:
        binary = tmp_path / "frps-0.71.0"
        binary.write_text("#!/bin/sh\ntrue\n", "utf-8")
        binary.chmod(0o755)
        log_dir = tmp_path / "logs"

        path = systemd.install_template(
            binary=binary, log_dir=log_dir, user="frps", group="frps"
        )
        assert path == systemd.template_path
        assert path.exists()
        assert f"ExecStart={binary}" in path.read_text("utf-8")
        assert "User=frps" in path.read_text("utf-8")
        assert log_dir.is_dir(), "ReadWritePaths 的目录必须存在，否则 unit 起不来"
        assert path.stat().st_mode & 0o777 == 0o644
        assert ["systemctl", "daemon-reload"] in recorded
        assert ["systemctl", "enable", "frps@test.service"] in recorded

    def test_install_template_refuses_to_overwrite_without_force(self, systemd, recorded, as_root) -> None:
        from frpsctl.errors import UsageError

        systemd.template_path.parent.mkdir(parents=True, exist_ok=True)
        systemd.template_path.write_text("手写的 unit", "utf-8")

        with pytest.raises(UsageError, match="已存在"):
            systemd.install_template(binary=Path("/opt/frps"), log_dir=Path("/var/log/frps"))
        assert systemd.template_path.read_text("utf-8") == "手写的 unit", "未经 --force 就覆盖了"

    def test_install_template_force_overwrites(
        self, systemd, recorded, tmp_path, as_root, fake_accounts
    ) -> None:
        systemd.template_path.parent.mkdir(parents=True, exist_ok=True)
        systemd.template_path.write_text("旧的", "utf-8")
        binary = tmp_path / "frps-0.71.0"
        binary.write_text("#!/bin/sh\ntrue\n", "utf-8")
        binary.chmod(0o755)

        systemd.install_template(binary=binary, log_dir=tmp_path / "logs", force=True)
        assert "ExecStart=" in systemd.template_path.read_text("utf-8")

    def test_install_template_rejects_missing_service_user(self, systemd, recorded, as_root) -> None:
        """账户不存在必须在**渲染之前**拒绝：unit 装上了也起不来。

        此前模板硬编码 `User=frps` 而安装流程完全不检查——用户按文档
        `sudo frpsctl service install` 成功，`systemctl start` 才报
        "Failed to determine user credentials"，两个动作相隔很远。
        """
        from frpsctl.errors import UsageError

        with pytest.raises(UsageError, match="不存在"):
            systemd.install_template(
                binary=Path("/bin/true"),
                log_dir=Path("/tmp"),
                user="definitely-no-such-user-frpsctl",
            )
        assert not systemd.template_path.exists(), "体检未过却写了 unit"
        assert recorded == [], "体检未过却调用了 systemctl"

    def test_install_template_rejects_unreachable_binary(
        self, systemd, recorded, tmp_path, as_root, foreign_accounts
    ) -> None:
        """二进制对服务用户不可达（如 /root/...）必须拒绝。

        这是最隐蔽的一类部署失败：`sudo frpsctl install` 把二进制放进
        `/root/.local/share`（0700），安装 unit 成功、`systemctl start` 才炸。
        """
        from frpsctl.errors import UsageError

        private_dir = tmp_path / "private"
        private_dir.mkdir()
        private_dir.chmod(0o700)
        binary = private_dir / "frps-0.71.0"
        binary.write_text("#!/bin/sh\ntrue\n", "utf-8")
        binary.chmod(0o755)

        with pytest.raises(UsageError, match="无法执行"):
            systemd.install_template(binary=binary, log_dir=tmp_path / "logs")
        assert not systemd.template_path.exists(), "体检未过却写了 unit"
        assert recorded == [], "体检未过却调用了 systemctl"

    def test_install_template_rejects_unwritable_log_dir(
        self, systemd, recorded, tmp_path, as_root, foreign_accounts
    ) -> None:
        """日志目录不可写必须拒绝（ProtectSystem=strict 下 frp 会写不进日志）。

        用 /tmp 下的独立目录而不是 `tmp_path`：pytest 的临时目录是 0700，
        目标服务用户（uid 偏移后的替身）会先在二进制可达性上失败，测不到
        本用例要覆盖的 log_dir 分支。
        """
        import shutil
        import tempfile

        from frpsctl.errors import UsageError

        base = Path(tempfile.mkdtemp(prefix="frpsctl-uid-test-"))
        try:
            base.chmod(0o755)
            binary = base / "frps-0.71.0"
            binary.write_text("#!/bin/sh\ntrue\n", "utf-8")
            binary.chmod(0o755)
            readonly = base / "readonly"
            readonly.mkdir()
            readonly.chmod(0o555)

            with pytest.raises(UsageError, match="不可写"):
                systemd.install_template(binary=binary, log_dir=readonly)
            assert recorded == [], "体检未过却调用了 systemctl"
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_install_template_requires_instance_config(self, inst, tmp_path, as_root, fake_accounts) -> None:
        """unit 的 ExecStart 固定使用实例内的 frps.toml——缺失时必须当场拒绝。

        用户用 `--config` 指向别处时尤其容易踩：装出来的 unit 不会用那个文件，
        启动必然失败。
        """
        from frpsctl.core.systemd import Systemd
        from frpsctl.errors import UsageError

        inst.config.unlink(missing_ok=True)
        fresh = Systemd(inst, unit_dir=tmp_path / "systemd-fresh")
        binary = tmp_path / "frps-0.71.0"
        binary.write_text("#!/bin/sh\ntrue\n", "utf-8")
        binary.chmod(0o755)

        with pytest.raises(UsageError, match="配置文件不存在"):
            fresh.install_template(binary=binary, log_dir=tmp_path / "logs")
        assert not fresh.template_path.exists(), "体检未过却写了 unit"

    def test_install_template_rejects_home_instance_dir(
        self, tmp_path, as_root, fake_accounts
    ) -> None:
        """家目录下的实例会被 ProtectHome=true 挡住：必须提前拒绝。

        这是挂载隔离（权限位检查看不出来），漏掉的后果同样是
        `systemctl start` 才失败。这里不真创建目录——`_protect_home_conflict`
        在配置存在性检查**之前**触发。
        """
        from frpsctl.core.instance import Instance
        from frpsctl.core.systemd import Systemd
        from frpsctl.errors import UsageError

        home_inst = Instance(
            name="t",
            instances_root=Path("/home/someone/.local/share/frpsctl/instances"),
            data_home=tmp_path / "data",
        )
        binary = tmp_path / "frps-0.71.0"
        binary.write_text("#!/bin/sh\ntrue\n", "utf-8")
        binary.chmod(0o755)
        systemd_home = Systemd(home_inst, unit_dir=tmp_path / "systemd-home")

        with pytest.raises(UsageError, match="ProtectHome"):
            systemd_home.install_template(binary=binary, log_dir=tmp_path / "logs")
        assert not systemd_home.template_path.exists()

    def test_protect_home_conflict_detection(self) -> None:
        from frpsctl.core.systemd import _protect_home_conflict

        assert _protect_home_conflict(Path("/home/u/frps")) == "/home/"
        assert _protect_home_conflict(Path("/root/.local/share/frpsctl")) == "/root/"
        assert _protect_home_conflict(Path("/run/user/1000/frps")) == "/run/user/"
        assert _protect_home_conflict(Path("/opt/frpsctl")) is None
        assert _protect_home_conflict(Path("/etc/frps/instances")) is None

    def test_install_template_hands_instance_to_service_user(
        self, systemd, recorded, tmp_path, as_root, fake_accounts, monkeypatch
    ) -> None:
        """实例目录必须移交给服务用户（否则 frps 读不到 0700 目录里的配置）。

        这是 systemd 部署闭环的最后一环：实例目录由 root 创建、权限 0700，
        服务用户不是属主时连 `frps.toml` 都读不到，unit 必然起不来。
        """
        binary = tmp_path / "frps-0.71.0"
        binary.write_text("#!/bin/sh\ntrue\n", "utf-8")
        binary.chmod(0o755)
        systemd.inst.config.write_text("bindPort = 7000\n", "utf-8")

        chowned: list[tuple[str, int, int]] = []
        real_chown = os.chown

        def spy(path, uid, gid):
            chowned.append((str(path), uid, gid))
            return real_chown(path, uid, gid)

        monkeypatch.setattr(os, "chown", spy)
        systemd.install_template(binary=binary, log_dir=tmp_path / "logs")

        targets = {item[0] for item in chowned}
        assert str(systemd.inst.dir) in targets, "实例目录没有移交给服务用户"
        assert str(systemd.inst.config) in targets, "配置文件没有移交给服务用户"

    def test_uninstall_disables_and_removes_template(self, systemd, recorded, as_root) -> None:
        systemd.template_path.parent.mkdir(parents=True, exist_ok=True)
        systemd.template_path.write_text("unit", "utf-8")
        systemd.uninstall()
        assert not systemd.template_path.exists()
        assert ["systemctl", "disable", "--now", "frps@test.service"] in recorded
        assert ["systemctl", "daemon-reload"] in recorded

    def test_relative_inputs_are_resolved_in_unit(
        self, systemd, recorded, tmp_path, as_root, fake_accounts, monkeypatch
    ) -> None:
        """相对路径的 binary / log_dir 必须渲染为绝对路径（systemd 不接受相对路径）。"""
        monkeypatch.chdir(tmp_path)
        binary = Path("bin/frps-0.71.0")
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\ntrue\n", "utf-8")
        binary.chmod(0o755)

        systemd.install_template(binary=binary, log_dir=Path("logs"))
        text = systemd.template_path.read_text("utf-8")

        resolved_bin = (tmp_path / "bin" / "frps-0.71.0").resolve()
        assert f"ExecStart={resolved_bin} -c" in text
        assert "ExecStart=bin/" not in text, "渲染了相对路径"
        for line in text.splitlines():
            if line.startswith("ReadWritePaths="):
                for token in line.split("=", 1)[1].split():
                    assert Path(token.replace("%i", "t")).is_absolute(), line

    def test_mutation_requires_root(self, systemd, recorded, monkeypatch) -> None:
        """非 root 时三个变更动作都必须拒绝，且**不得**调用 systemctl。"""
        from frpsctl.errors import PermissionRequired

        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        for action in (systemd.start, systemd.stop, systemd.restart):
            with pytest.raises(PermissionRequired) as excinfo:
                action()
            assert int(excinfo.value.exit_code) == 8
        assert recorded == [], "没有权限却调用了 systemctl"

    def test_failed_systemctl_is_an_unclassified_error(self, systemd, monkeypatch, as_root) -> None:
        """systemctl 失败 → 退出码 1（而非 2）：脚本据此判断"重试可能有用"。"""
        from frpsctl.errors import FrpsctlError

        def failing(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Unit not found")

        monkeypatch.setattr(subprocess, "run", failing)
        with pytest.raises(FrpsctlError) as excinfo:
            systemd.start()
        assert int(excinfo.value.exit_code) == 1
        assert "Unit not found" in excinfo.value.message

    def test_main_pid_reads_show_output(self, systemd, monkeypatch) -> None:
        def fake_run(argv, **kwargs):
            out = "4321\n" if "MainPID" in argv else ""
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert systemd.main_pid() == 4321

    def test_main_pid_none_when_stopped(self, systemd, monkeypatch) -> None:
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="0\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert systemd.main_pid() is None

    def test_available_requires_systemctl_and_unit_dir(self, systemd, monkeypatch, tmp_path) -> None:
        """`available` 是"容器里没有 systemd"的守卫：任一条件不满足就必须为 False。"""
        from frpsctl.core.systemd import Systemd

        systemd.unit_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: "/usr/bin/systemctl")
        assert systemd.available is True

        # 条件一：没有 systemctl
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: None)
        assert systemd.available is False

        # 条件二：unit 目录不存在（容器里常见）。用新实例而不是改类属性——
        # `unit_dir` 在 dataclass 实例上，改类属性对已构造的实例无效。
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: "/usr/bin/systemctl")
        assert Systemd(systemd.inst, unit_dir=tmp_path / "missing").available is False


class TestServiceIdentityResolution:
    """服务账户解析（§12.2 v0.3.2）：`frps` 只是候选，任何用户都可用。

    覆盖 `--user` 缺省的三级解析、组回退、数字 UID/GID、`--create-user`
    的完整控制流——它们共同保证"服务用户"不再是写死的假设。
    """

    @staticmethod
    def _pw(uid: int, gid: int, name: str):
        from types import SimpleNamespace

        return SimpleNamespace(pw_uid=uid, pw_gid=gid, pw_name=name)

    @staticmethod
    def _gr(gid: int, name: str):
        from types import SimpleNamespace

        return SimpleNamespace(gr_gid=gid, gr_name=name)

    def test_default_prefers_existing_frps(self, monkeypatch) -> None:
        from frpsctl.core.systemd import resolve_service_identity

        monkeypatch.setattr(
            "frpsctl.core.systemd._user_record",
            lambda name: self._pw(1001, 1002, "frps") if name == "frps" else None,
        )
        monkeypatch.setattr(
            "frpsctl.core.systemd._group_record",
            lambda name: self._gr(1002, "frps") if name == "frps" else None,
        )
        identity = resolve_service_identity(None)
        assert (identity.user, identity.group) == ("frps", "frps")
        assert (identity.uid, identity.gid) == (1001, 1002)
        assert identity.source == "default-frps"
        assert identity.notes == ()

    def test_default_falls_back_to_current_user(self, monkeypatch) -> None:
        import grp
        import pwd

        from frpsctl.core.systemd import resolve_service_identity

        monkeypatch.setattr(
            "frpsctl.core.systemd._user_record",
            lambda name: self._pw(4242, 4343, "tester") if name == "tester" else None,
        )
        monkeypatch.setattr("frpsctl.core.systemd._group_record", lambda _n: None)
        monkeypatch.setattr("frpsctl.core.systemd.os.geteuid", lambda: 4242)
        monkeypatch.setattr(pwd, "getpwuid", lambda _uid: self._pw(4242, 4343, "tester"))
        monkeypatch.setattr(grp, "getgrgid", lambda _gid: self._gr(4343, "testergrp"))

        identity = resolve_service_identity(None)
        assert (identity.user, identity.group) == ("tester", "testergrp")
        assert identity.uid == 4242
        assert identity.source == "default-current"
        assert any("主组" in note for note in identity.notes)

    def test_explicit_numeric_uid_and_gid(self, monkeypatch) -> None:
        import grp
        import pwd

        from frpsctl.core.systemd import resolve_service_identity

        monkeypatch.setattr(pwd, "getpwuid", lambda _uid: self._pw(4242, 4343, "numuser"))
        monkeypatch.setattr(grp, "getgrgid", lambda _gid: self._gr(5555, "numgrp"))
        identity = resolve_service_identity("4242", "5555")
        assert (identity.user, identity.group) == ("numuser", "numgrp")
        assert identity.source == "explicit"

    def test_numeric_uid_missing_rejected(self, monkeypatch) -> None:
        import pwd

        from frpsctl.core.systemd import resolve_service_identity
        from frpsctl.errors import UsageError

        def boom(_uid):
            raise KeyError("nope")

        monkeypatch.setattr(pwd, "getpwuid", boom)
        with pytest.raises(UsageError, match="用户 ID 不存在"):
            resolve_service_identity("4242")

    def test_numeric_gid_missing_rejected(self, monkeypatch) -> None:
        import grp

        from frpsctl.core.systemd import resolve_service_identity
        from frpsctl.errors import UsageError

        def boom(_gid):
            raise KeyError("nope")

        monkeypatch.setattr(grp, "getgrgid", boom)
        with pytest.raises(UsageError, match="组 ID 不存在"):
            resolve_service_identity("root", "5555")

    def test_illegal_token_rejected(self) -> None:
        from frpsctl.core.systemd import resolve_service_identity
        from frpsctl.errors import UsageError

        with pytest.raises(UsageError, match="非法"):
            resolve_service_identity("bad\nname")

    def test_missing_same_name_group_falls_back_to_primary(self, monkeypatch) -> None:
        import grp

        from frpsctl.core.systemd import resolve_service_identity

        monkeypatch.setattr(
            "frpsctl.core.systemd._user_record",
            lambda name: self._pw(1000, 100, "alice") if name == "alice" else None,
        )
        monkeypatch.setattr("frpsctl.core.systemd._group_record", lambda _n: None)
        monkeypatch.setattr(grp, "getgrgid", lambda _gid: self._gr(100, "users"))

        identity = resolve_service_identity("alice")
        assert identity.group == "users"
        assert identity.uid == 1000
        assert any("主组" in note for note in identity.notes)

    def test_create_user_requires_explicit_name(self) -> None:
        from frpsctl.core.systemd import ensure_service_account
        from frpsctl.errors import UsageError

        with pytest.raises(UsageError, match="需要配合 --user"):
            ensure_service_account(None, None, create_user=True)

    def test_missing_user_without_create_rejected(self, monkeypatch) -> None:
        from frpsctl.core.systemd import ensure_service_account
        from frpsctl.errors import UsageError

        monkeypatch.setattr("frpsctl.core.systemd._user_record", lambda _n: None)
        with pytest.raises(UsageError, match="系统用户或组不存在") as info:
            ensure_service_account("ghost", create_user=False)
        assert "--create-user" in (info.value.hint or "")

    def test_missing_explicit_group_rejected_even_with_create(self, monkeypatch) -> None:
        from frpsctl.core.systemd import ensure_service_account
        from frpsctl.errors import UsageError

        monkeypatch.setattr(
            "frpsctl.core.systemd._user_record",
            lambda name: self._pw(1000, 100, "alice") if name == "alice" else None,
        )
        monkeypatch.setattr("frpsctl.core.systemd._group_record", lambda _n: None)
        with pytest.raises(UsageError, match="系统组不存在"):
            ensure_service_account("alice", "missinggrp", create_user=True)

    def test_create_user_with_missing_explicit_group_does_not_create(self, monkeypatch) -> None:
        """显式 --group 缺失时先拒绝：不能为注定失败的安装先创建账户（review 收口）。"""
        from frpsctl.core.systemd import ensure_service_account
        from frpsctl.errors import UsageError

        calls: list = []
        monkeypatch.setattr("frpsctl.core.systemd._user_record", lambda _n: None)
        monkeypatch.setattr(
            "frpsctl.core.systemd.subprocess.run", lambda *a, **_k: calls.append(a)
        )
        monkeypatch.setattr("frpsctl.core.systemd.os.geteuid", lambda: 0)
        with pytest.raises(UsageError, match="系统组不存在"):
            ensure_service_account("ghost", "missinggrp", create_user=True)
        assert calls == [], "显式组缺失时不应执行 useradd"

    def test_create_user_flow(self, monkeypatch) -> None:
        import subprocess

        from frpsctl.core.systemd import ensure_service_account

        state = {"created": False}
        calls: list[list[str]] = []

        def fake_user(name):
            if name == "ghost" and state["created"]:
                return self._pw(4321, 4321, "ghost")
            return None

        def fake_group(name):
            return self._gr(4321, "ghost") if (state["created"] and name == "ghost") else None

        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            state["created"] = True
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr("frpsctl.core.systemd._user_record", fake_user)
        monkeypatch.setattr("frpsctl.core.systemd._group_record", fake_group)
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _n: "/usr/sbin/useradd")
        monkeypatch.setattr("frpsctl.core.systemd.subprocess.run", fake_run)
        monkeypatch.setattr("frpsctl.core.systemd.os.geteuid", lambda: 0)

        identity = ensure_service_account("ghost", create_user=True)
        assert identity.created is True
        assert (identity.uid, identity.gid) == (4321, 4321)
        assert identity.source == "explicit"
        argv = next(call for call in calls if "useradd" in call[0])
        assert argv[1:] == [
            "--system",
            "--no-create-home",
            "--shell",
            "/usr/sbin/nologin",
            "--user-group",
            "ghost",
        ]

    def test_create_user_non_root_rejected(self, monkeypatch) -> None:
        from frpsctl.core.systemd import ensure_service_account
        from frpsctl.errors import PermissionRequired

        monkeypatch.setattr("frpsctl.core.systemd._user_record", lambda _n: None)
        monkeypatch.setattr("frpsctl.core.systemd.os.geteuid", lambda: 1000)
        with pytest.raises(PermissionRequired, match="创建服务账户"):
            ensure_service_account("ghost", create_user=True)

    def test_create_user_invalid_account_name(self, monkeypatch) -> None:
        from frpsctl.core.systemd import ensure_service_account
        from frpsctl.errors import UsageError

        monkeypatch.setattr("frpsctl.core.systemd._user_record", lambda _n: None)
        monkeypatch.setattr("frpsctl.core.systemd.os.geteuid", lambda: 0)
        with pytest.raises(UsageError, match="不是规范的系统账户名"):
            ensure_service_account("Upper", create_user=True)

    def test_create_user_useradd_missing(self, monkeypatch) -> None:
        from frpsctl.core.systemd import ensure_service_account
        from frpsctl.errors import FrpsctlError

        monkeypatch.setattr("frpsctl.core.systemd._user_record", lambda _n: None)
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _n: None)
        monkeypatch.setattr("frpsctl.core.systemd.os.geteuid", lambda: 0)
        with pytest.raises(FrpsctlError, match="找不到 useradd"):
            ensure_service_account("ghost", create_user=True)

    def test_create_user_useradd_failure_surfaces_stderr(self, monkeypatch) -> None:
        import subprocess

        from frpsctl.core.systemd import ensure_service_account
        from frpsctl.errors import FrpsctlError

        monkeypatch.setattr("frpsctl.core.systemd._user_record", lambda _n: None)
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _n: "/usr/sbin/useradd")
        monkeypatch.setattr("frpsctl.core.systemd.os.geteuid", lambda: 0)

        def fake_run(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="useradd: 账户已锁定")

        monkeypatch.setattr("frpsctl.core.systemd.subprocess.run", fake_run)
        with pytest.raises(FrpsctlError, match="创建系统用户失败") as info:
            ensure_service_account("ghost", create_user=True)
        assert "账户已锁定" in info.value.message

    def test_create_user_useradd_timeout(self, monkeypatch) -> None:
        import subprocess

        from frpsctl.core.systemd import ensure_service_account
        from frpsctl.errors import FrpsctlError

        monkeypatch.setattr("frpsctl.core.systemd._user_record", lambda _n: None)
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _n: "/usr/sbin/useradd")
        monkeypatch.setattr("frpsctl.core.systemd.os.geteuid", lambda: 0)

        def fake_run(argv, **_kwargs):
            raise subprocess.TimeoutExpired(argv, 15)

        monkeypatch.setattr("frpsctl.core.systemd.subprocess.run", fake_run)
        with pytest.raises(FrpsctlError, match="超时"):
            ensure_service_account("ghost", create_user=True)


class TestServiceManifest:
    """安装留档 `service.json`（v0.3.2）：安装参数在下游可跟随。"""

    @pytest.fixture
    def systemd(self, inst, tmp_path):
        from frpsctl.core.systemd import Systemd

        inst.config.write_text("bindPort = 17000\n", "utf-8")
        return Systemd(inst, unit_dir=tmp_path / "systemd")

    @pytest.fixture
    def recorded(self, monkeypatch):
        import subprocess

        calls: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    @pytest.fixture
    def as_root(self, monkeypatch):
        monkeypatch.setattr("frpsctl.core.systemd.os.geteuid", lambda: 0)

    @pytest.fixture
    def fake_accounts(self, monkeypatch):
        monkeypatch.setattr(
            "frpsctl.core.systemd._account_ids", lambda *_: (os.getuid(), os.getgid())
        )

    def _write_binary(self, tmp_path) -> Path:
        binary = tmp_path / "frps-0.71.0"
        binary.write_text("#!/bin/sh\ntrue\n", "utf-8")
        binary.chmod(0o755)
        return binary

    def test_install_records_manifest(
        self, systemd, recorded, tmp_path, as_root, fake_accounts
    ) -> None:
        from frpsctl.core.systemd import read_service_manifest

        log_dir = tmp_path / "logs"
        systemd.install_template(
            binary=self._write_binary(tmp_path), log_dir=log_dir, user="frps", group="frps"
        )
        data, error = read_service_manifest(systemd.inst)
        assert error is None
        record = data["frps"]
        assert record["user"] == "frps" and record["group"] == "frps"
        assert record["log_dir"] == str(log_dir.resolve())
        assert record["unit"] == "frps@test.service"
        assert record["installed_at"]
        assert systemd.inst.service_manifest.stat().st_mode & 0o777 == 0o600

    def test_manifest_records_resolved_default(
        self, systemd, recorded, tmp_path, as_root, fake_accounts
    ) -> None:
        import pwd

        from frpsctl.core.systemd import read_service_manifest

        systemd.install_template(binary=self._write_binary(tmp_path), log_dir=tmp_path / "logs")
        data, _ = read_service_manifest(systemd.inst)
        try:
            pwd.getpwnam("frps")
            expected = "frps"
        except KeyError:
            expected = pwd.getpwuid(os.geteuid()).pw_name
        assert data["frps"]["user"] == expected
        assert data["frps"]["group"]

    def test_uninstall_removes_record(
        self, systemd, recorded, tmp_path, as_root, fake_accounts
    ) -> None:
        systemd.install_template(
            binary=self._write_binary(tmp_path), log_dir=tmp_path / "logs", user="frps", group="frps"
        )
        assert systemd.inst.service_manifest.exists()
        systemd.uninstall()
        assert not systemd.inst.service_manifest.exists(), "最后一个服务卸载后留档应删除"

    def test_manifest_keeps_other_services(
        self, systemd, recorded, tmp_path, as_root, fake_accounts
    ) -> None:
        import json

        from frpsctl.core.systemd import read_service_manifest

        systemd.inst.ensure_dirs()
        systemd.inst.service_manifest.write_text(
            json.dumps({"web": {"user": "webuser", "group": "webuser"}}), "utf-8"
        )
        systemd.install_template(
            binary=self._write_binary(tmp_path), log_dir=tmp_path / "logs", user="frps", group="frps"
        )
        data, _ = read_service_manifest(systemd.inst)
        assert set(data) == {"frps", "web"}
        systemd.uninstall()
        data, _ = read_service_manifest(systemd.inst)
        assert set(data) == {"web"}, "卸载 frps 不应影响其它服务的留档"

    def test_manifest_missing_reads_empty(self, inst) -> None:
        from frpsctl.core.systemd import read_service_manifest

        data, error = read_service_manifest(inst)
        assert data == {} and error is None

    def test_manifest_corrupted_reads_error(self, inst) -> None:
        from frpsctl.core.systemd import read_service_manifest

        inst.ensure_dirs()
        inst.service_manifest.write_text("{ not json", "utf-8")
        data, error = read_service_manifest(inst)
        assert data == {}
        assert error is not None and "JSON" in error

    def test_remove_service_record_missing_key_is_noop(self, inst) -> None:
        from frpsctl.core.systemd import remove_service_record

        inst.ensure_dirs()
        remove_service_record(inst, "frps")
        assert not inst.service_manifest.exists()


class TestServeRuntime:
    """后台服务运行时（v0.3.3）：start / stop / probe / 状态文件 / 日志。

    全部用**真进程**（`python -c sleep`，argv 里带独立的 `serve` 元素）——
    三重校验与停止等待只有对真实 /proc 数据才有意义。
    """

    @pytest.fixture(autouse=True)
    def _cleanup(self, inst):
        yield
        import contextlib

        from frpsctl.core import serve_runtime as sr

        for spec in (sr.WEB_SPEC, sr.PLUGIN_SPEC):
            with contextlib.suppress(Exception):
                status = sr.probe(inst, spec)
                if status.running:
                    sr.stop_background(inst, spec, timeout=1)

    @staticmethod
    def _sleeper() -> list[str]:
        import sys

        # 末尾独立的 "serve" 元素：模拟真实 `frpsctl web serve` 的命令行标记
        return [sys.executable, "-c", "import time; time.sleep(60)", "serve"]

    def _start(self, inst, spec, *, argv=None, args=None, host=None, port=None):
        from frpsctl.core import serve_runtime as sr

        return sr.start_background(
            inst,
            spec,
            argv=argv or self._sleeper(),
            args=args or {"bind": "127.0.0.1:8787"},
            host=host,
            port=port,
            wait=0.3,
        )

    def test_start_records_state_and_probes_direct(self, inst) -> None:
        from frpsctl.core import serve_runtime as sr
        from frpsctl.core import platform as plat

        state = self._start(inst, sr.WEB_SPEC)
        assert plat.pid_alive(state.pid)
        assert state.start_time > 0
        assert state.argv[-1] == "serve"
        assert state.args == {"bind": "127.0.0.1:8787"}
        path = sr.state_path(inst, sr.WEB_SPEC)
        assert path.exists()
        assert path.stat().st_mode & 0o777 == 0o600

        status = sr.probe(inst, sr.WEB_SPEC)
        assert status.running
        assert status.state.pid == state.pid
        assert status.uptime_seconds is not None

    def test_start_twice_rejected(self, inst) -> None:
        from frpsctl.core import serve_runtime as sr
        from frpsctl.errors import ServeAlreadyRunning

        first = self._start(inst, sr.WEB_SPEC)
        with pytest.raises(ServeAlreadyRunning, match=str(first.pid)):
            self._start(inst, sr.WEB_SPEC)

    def test_stale_state_is_cleaned(self, inst) -> None:
        import json

        from frpsctl.core import platform as plat
        from frpsctl.core import serve_runtime as sr

        # 一个已经退出的进程：wait() 之后 pid 进入僵尸/消失，start 应清理记录
        import subprocess

        proc = subprocess.Popen(["true"])
        proc.wait()
        inst.ensure_dirs()
        sr.state_path(inst, sr.WEB_SPEC).write_text(
            json.dumps(
                {
                    "pid": proc.pid,
                    "start_time": 1,
                    "binary": "/bin/true",
                    "argv": ["/bin/true", "serve"],
                    "args": {},
                    "log": "",
                    "started_at": "",
                }
            ),
            "utf-8",
        )
        assert plat.process_gone(proc.pid)
        state = self._start(inst, sr.WEB_SPEC)
        assert state.pid != proc.pid
        assert sr.probe(inst, sr.WEB_SPEC).running

    def test_foreign_pid_rejected(self, inst) -> None:
        import json
        import os

        from frpsctl.core import platform as plat
        from frpsctl.core import serve_runtime as sr
        from frpsctl.errors import OwnershipConflict

        me = os.getpid()
        inst.ensure_dirs()
        sr.state_path(inst, sr.WEB_SPEC).write_text(
            json.dumps(
                {
                    "pid": me,
                    "start_time": plat.proc_start_time(me),
                    "binary": __import__("sys").executable,
                    "argv": [__import__("sys").executable, "serve"],
                    "args": {},
                    "log": "",
                    "started_at": "",
                }
            ),
            "utf-8",
        )
        # pytest 进程的 cmdline 不含独立的 "serve" 元素 → 身份不符
        with pytest.raises(OwnershipConflict):
            self._start(inst, sr.WEB_SPEC)
        with pytest.raises(OwnershipConflict):
            sr.stop_background(inst, sr.WEB_SPEC, timeout=0.2)
        assert sr.probe(inst, sr.WEB_SPEC).owner is sr.ServeOwner.FOREIGN

    def test_stop_stops_process_and_cleans(self, inst) -> None:
        from frpsctl.core import platform as plat
        from frpsctl.core import serve_runtime as sr

        state = self._start(inst, sr.WEB_SPEC)
        stopped = sr.stop_background(inst, sr.WEB_SPEC, timeout=5)
        assert stopped.pid == state.pid
        assert plat.process_gone(state.pid)
        assert not sr.state_path(inst, sr.WEB_SPEC).exists()
        assert sr.probe(inst, sr.WEB_SPEC).owner is sr.ServeOwner.NONE

    def test_stop_when_not_running(self, inst) -> None:
        from frpsctl.core import serve_runtime as sr
        from frpsctl.errors import ServeNotRunning

        with pytest.raises(ServeNotRunning, match="Web 管理台"):
            sr.stop_background(inst, sr.WEB_SPEC)

    def test_stale_stop_cleans_and_reports(self, inst) -> None:
        import json

        from frpsctl.core import serve_runtime as sr
        from frpsctl.errors import ServeNotRunning

        inst.ensure_dirs()
        sr.state_path(inst, sr.PLUGIN_SPEC).write_text(
            json.dumps(
                {
                    "pid": 999_999_99,
                    "start_time": 1,
                    "binary": "/bin/true",
                    "argv": ["/bin/true", "serve"],
                    "args": {},
                    "log": "",
                    "started_at": "",
                }
            ),
            "utf-8",
        )
        with pytest.raises(ServeNotRunning):
            sr.stop_background(inst, sr.PLUGIN_SPEC)
        assert not sr.state_path(inst, sr.PLUGIN_SPEC).exists()

    def test_startup_failure_cleans_state_and_keeps_log(self, inst) -> None:
        from frpsctl.core import serve_runtime as sr
        from frpsctl.errors import StartupFailed

        with pytest.raises(StartupFailed, match="启动后立即退出"):
            self._start(inst, sr.WEB_SPEC, argv=["/bin/false", "serve"])
        assert not sr.state_path(inst, sr.WEB_SPEC).exists()
        assert sr.log_path(inst, sr.WEB_SPEC).exists()

    def test_startup_failure_when_port_not_ready(self, inst) -> None:
        from frpsctl.core import serve_runtime as sr
        from frpsctl.errors import StartupFailed

        with pytest.raises(StartupFailed) as info:
            self._start(inst, sr.WEB_SPEC, host="127.0.0.1", port=1)
        assert "端口未就绪" in (info.value.hint or "")
        assert "Web 管理台" in info.value.message
        assert not sr.state_path(inst, sr.WEB_SPEC).exists()
        # 半活进程必须被收拾掉（不能留下"端口没通但进程还在"的孤儿）
        assert sr.probe(inst, sr.WEB_SPEC).owner is sr.ServeOwner.NONE

    def test_port_ready_accepts_listening_process(self, inst) -> None:
        import socket
        import sys

        from frpsctl.core import serve_runtime as sr

        # 只借一个空闲端口号（立刻释放），真正的监听由子进程做
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        argv = [
            sys.executable,
            "-c",
            "import socket, time; s=socket.socket(); "
            f"s.bind(('127.0.0.1', {port})); s.listen(1); time.sleep(60)",
            "serve",
        ]
        state = self._start(inst, sr.WEB_SPEC, argv=argv, host="127.0.0.1", port=port)
        assert sr.probe(inst, sr.WEB_SPEC).running
        assert state.port == port

    def test_corrupted_state_is_reported(self, inst) -> None:
        from frpsctl.core import serve_runtime as sr
        from frpsctl.errors import ConfigError

        inst.ensure_dirs()
        sr.state_path(inst, sr.WEB_SPEC).write_text("{ broken", "utf-8")
        status = sr.probe(inst, sr.WEB_SPEC)
        assert status.owner is sr.ServeOwner.CORRUPTED
        assert status.error
        with pytest.raises(ConfigError):
            self._start(inst, sr.WEB_SPEC)
        with pytest.raises(ConfigError):
            sr.stop_background(inst, sr.WEB_SPEC)

    def test_state_missing_fields_rejected(self, inst) -> None:
        from frpsctl.core import serve_runtime as sr
        from frpsctl.errors import ConfigError

        inst.ensure_dirs()
        sr.state_path(inst, sr.WEB_SPEC).write_text('{"pid": 123}', "utf-8")
        with pytest.raises(ConfigError):
            sr.stop_background(inst, sr.WEB_SPEC)

    def test_sigkill_fallback_for_stubborn_process(self, inst) -> None:
        import sys

        from frpsctl.core import platform as plat
        from frpsctl.core import serve_runtime as sr

        argv = [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
            "serve",
        ]
        state = self._start(inst, sr.WEB_SPEC, argv=argv)
        stopped = sr.stop_background(inst, sr.WEB_SPEC, timeout=0.3)
        assert stopped.pid == state.pid
        assert plat.process_gone(state.pid)

    def test_rotate_log(self, inst) -> None:
        from frpsctl.core import serve_runtime as sr

        inst.ensure_dirs()
        log = sr.log_path(inst, sr.WEB_SPEC)
        log.write_bytes(b"x" * (sr.LOG_MAX_BYTES + 1))
        sr.rotate_log(log)
        assert not log.exists()
        rotated = log.parent / (log.name + ".1")
        assert rotated.exists() and rotated.stat().st_size > sr.LOG_MAX_BYTES

    def test_log_tail_reads_tail(self, inst) -> None:
        from frpsctl.core import serve_runtime as sr

        inst.ensure_dirs()
        log = sr.log_path(inst, sr.WEB_SPEC)
        log.write_text("\n".join(f"line-{i}" for i in range(50)) + "\n", "utf-8")
        assert "line-49" in sr.log_tail(log, lines=5)
        assert "line-0" not in sr.log_tail(log, lines=5)

    def test_probe_none_when_never_started(self, inst) -> None:
        from frpsctl.core import serve_runtime as sr

        assert sr.probe(inst, sr.PLUGIN_SPEC).owner is sr.ServeOwner.NONE


class TestUninstallLocalServices:
    """uninstall / doctor 对 web/plugin direct 后台的防护（v0.3.3，真进程）。

    孤儿状态（服务在跑、数据已删）是这一层要堵的核心：预检拒绝、--force 先停、
    doctor 如实报告。
    """

    @pytest.fixture(autouse=True)
    def _cleanup(self, inst):
        yield
        import contextlib

        from frpsctl.core import serve_runtime as sr

        for spec in (sr.WEB_SPEC, sr.PLUGIN_SPEC):
            with contextlib.suppress(Exception):
                status = sr.probe(inst, spec)
                if status.running:
                    sr.stop_background(inst, spec, timeout=1)

    @staticmethod
    def _start(inst, spec):
        import sys

        from frpsctl.core import serve_runtime as sr

        return sr.start_background(
            inst,
            spec,
            argv=[sys.executable, "-c", "import time; time.sleep(60)", "serve"],
            args={},
            wait=0.3,
        )

    def test_precheck_refuses_running_direct(self, inst) -> None:
        from frpsctl.core import serve_runtime as sr
        from frpsctl.core.uninstall import _precheck
        from frpsctl.errors import OwnershipConflict

        self._start(inst, sr.WEB_SPEC)
        with pytest.raises(OwnershipConflict, match="拒绝卸载"):
            _precheck(inst, force=False)
        _precheck(inst, force=True)  # --force 允许（实际停止在 _ensure_stopped）

    def test_execute_without_force_refuses(self, inst) -> None:
        from frpsctl.core import serve_runtime as sr
        from frpsctl.core.uninstall import execute_uninstall, plan_uninstall
        from frpsctl.errors import OwnershipConflict

        self._start(inst, sr.WEB_SPEC)
        with pytest.raises(OwnershipConflict):
            execute_uninstall(plan_uninstall(inst))

    def test_force_uninstall_stops_direct(self, inst) -> None:
        from frpsctl.core import platform as plat
        from frpsctl.core import serve_runtime as sr
        from frpsctl.core.uninstall import execute_uninstall, plan_uninstall

        state = self._start(inst, sr.PLUGIN_SPEC)
        report = execute_uninstall(plan_uninstall(inst), force=True)
        assert plat.process_gone(state.pid), "direct 插件进程必须被停止"
        assert any("插件服务" in item for item in report.stopped)

    def test_doctor_reports_running_direct(self, inst) -> None:
        from frpsctl.core import serve_runtime as sr
        from frpsctl.core.doctor import Severity, _check_local_services

        state = self._start(inst, sr.WEB_SPEC)
        findings = _check_local_services(inst)
        assert any(
            f.severity is Severity.INFO
            and "Web 管理台" in f.message
            and str(state.pid) in f.message
            for f in findings
        )

    def test_doctor_warns_when_port_unreachable(self, inst) -> None:
        """状态说在跑、端口却连不上 → WARN（服务半死；v0.3.3 P1）。"""
        import json as json_module
        import socket
        import subprocess
        import sys

        from frpsctl.core import platform as plat
        from frpsctl.core import serve_runtime as sr
        from frpsctl.core.doctor import Severity, _check_local_services

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "serve"])
        try:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            inst.ensure_dirs()
            sr.state_path(inst, sr.WEB_SPEC).write_text(
                json_module.dumps(
                    {
                        "pid": proc.pid,
                        "start_time": plat.proc_start_time(proc.pid),
                        "binary": sys.executable,
                        "argv": [sys.executable, "serve"],
                        "args": {},
                        "log": "",
                        "started_at": "",
                        "host": "127.0.0.1",
                        "port": port,
                    }
                ),
                "utf-8",
            )
            findings = _check_local_services(inst)
            assert any(
                f.severity is Severity.WARN and "无法连接" in f.message for f in findings
            )
        finally:
            proc.kill()
            proc.wait()

    def test_doctor_reports_none_silently(self, inst) -> None:
        from frpsctl.core.doctor import _check_local_services

        assert _check_local_services(inst) == []


class TestServeEndToEnd:
    """direct 后台的**真链路**（真 `python -m frpsctl ... serve` 子进程）。

    与 `TestServeRuntime`（sleeper 进程验证生命周期原语）互补：这里验证
    spawn 的命令行真的能对外服务——Web 的 HTTP 200、插件的裁决与审计刷盘。
    """

    @pytest.fixture(autouse=True)
    def _env_and_cleanup(self, inst, monkeypatch):
        import contextlib
        import os

        monkeypatch.setenv("FRPSCTL_ROOT", str(inst.instances_root))
        monkeypatch.setenv("FRPSCTL_DATA_HOME", str(inst.data_home))
        monkeypatch.setenv("FRPSCTL_INSTANCE", inst.name)
        assert os.environ["FRPSCTL_ROOT"]  # 子进程继承同一数据目录
        yield
        from frpsctl.core import serve_runtime as sr

        for spec in (sr.WEB_SPEC, sr.PLUGIN_SPEC):
            with contextlib.suppress(Exception):
                if sr.probe(inst, spec).running:
                    sr.stop_background(inst, spec, timeout=1)

    @staticmethod
    def _exe(*args: str) -> list[str]:
        import sys

        return [sys.executable, "-m", "frpsctl", *args]

    @staticmethod
    def _free_port() -> int:
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def test_web_serve_end_to_end(self, inst) -> None:
        import urllib.request

        from frpsctl.core import platform as plat
        from frpsctl.core import serve_runtime as sr

        port = self._free_port()
        inst.ensure_dirs()
        pw = inst.dir / "web-password"
        pw.write_text("e2e-secret\n", "utf-8")
        pw.chmod(0o600)
        state = sr.start_background(
            inst,
            sr.WEB_SPEC,
            argv=self._exe(
                "web", "serve", "--bind", f"127.0.0.1:{port}", "--password-file", str(pw)
            ),
            args={"bind": f"127.0.0.1:{port}"},
            host="127.0.0.1",
            port=port,
        )
        assert sr.probe(inst, sr.WEB_SPEC).running
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:  # noqa: S310
            body = resp.read().decode()
            status = resp.status
        assert status == 200 and "frpsctl" in body
        sr.stop_background(inst, sr.WEB_SPEC, timeout=5)
        assert plat.process_gone(state.pid)

    def test_plugin_serve_end_to_end_audit_flush(self, inst) -> None:
        import json as json_module
        import urllib.request

        from frpsctl.core import platform as plat
        from frpsctl.core import serve_runtime as sr

        port = self._free_port()
        inst.ensure_dirs()
        policy = inst.dir / "plugin-policy.json"
        policy.write_text(
            json_module.dumps(
                {
                    "users": {"alice": {"allowed_ports": [6000]}},
                    "audit": {
                        "enabled": True,
                        "path": "plugin-audit.jsonl",
                        "flush_every": 1,
                        "flush_interval": 0.05,
                    },
                }
            ),
            "utf-8",
        )
        state = sr.start_background(
            inst,
            sr.PLUGIN_SPEC,
            argv=self._exe(
                "plugin", "serve", "--policy", str(policy), "--bind", f"127.0.0.1:{port}"
            ),
            args={"bind": f"127.0.0.1:{port}", "policy": str(policy)},
            host="127.0.0.1",
            port=port,
        )
        body = json_module.dumps(
            {
                "version": "0.1.0",
                "op": "Login",
                "content": {"user": "alice", "metas": {"client_id": "alice"}},
            }
        ).encode()
        request = urllib.request.Request(  # noqa: S310 - 固定回环
            f"http://127.0.0.1:{port}/handler?version=0.1.0&op=Login",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as resp:  # noqa: S310
            payload = json_module.loads(resp.read())
        assert resp.status == 200
        assert payload.get("reject") in (False, None), f"Login 应放行：{payload}"

        # 停止走 SIGTERM：plugin serve 会先刷审计再退出
        sr.stop_background(inst, sr.PLUGIN_SPEC, timeout=5)
        assert plat.process_gone(state.pid)
        audit = inst.dir / "plugin-audit.jsonl"
        assert audit.exists(), "审计文件未生成"
        assert "alice" in audit.read_text("utf-8"), "停止后审计未落盘"


class TestPluginService:
    """`plugin service install` 的 unit 渲染与部署体检（§11.2 的 systemd 落地）。

    README 硬性要求"插件必须由 systemd 守护且 Restart=always"，但此前工具不提供
    任何 unit 生成——用户只能手写。这里的断言就是那条文档承诺的可执行形式。
    """

    def test_render_plugin_unit(self) -> None:
        from frpsctl.core.systemd import render_plugin_unit

        text = render_plugin_unit(
            exec_start="/usr/local/bin/frpsctl",
            bind="127.0.0.1:8080",
            handler_path="/handler",
            policy=Path("/etc/frps/instances/web/plugin-policy.json"),
            workdir=Path("/etc/frps/instances/web"),
        )
        assert (
            "ExecStart=/usr/local/bin/frpsctl --instance %i plugin serve "
            "--policy /etc/frps/instances/web/plugin-policy.json "
            "--bind 127.0.0.1:8080 --path /handler" in text
        )
        # 插件是登录单点：任何退出都必须被立刻拉起
        assert "Restart=always" in text
        # 策略与审计都落在实例目录
        assert "ReadWritePaths=/etc/frps/instances/web" in text
        assert "ProtectHome=true" in text
        assert "User=frps" in text and "Group=frps" in text

    @pytest.fixture
    def service(self, inst, tmp_path):
        from frpsctl.core.systemd import PluginService

        inst.config.write_text("bindPort = 17000\n", "utf-8")
        (inst.dir / "plugin-policy.json").write_text('{"users": {}}', "utf-8")
        return PluginService(inst, unit_dir=tmp_path / "systemd-plugin")

    @pytest.fixture
    def recorded(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    @pytest.fixture
    def as_root(self, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: 0)

    @pytest.fixture
    def fake_accounts(self, monkeypatch):
        monkeypatch.setattr(
            "frpsctl.core.systemd._account_ids",
            lambda *_: (os.getuid(), os.getgid()),
        )

    def _binary(self, tmp_path) -> Path:
        binary = tmp_path / "frpsctl"
        binary.write_text("#!/bin/sh\ntrue\n", "utf-8")
        binary.chmod(0o755)
        return binary

    def test_install_writes_unit_and_enables(
        self, service, recorded, tmp_path, as_root, fake_accounts
    ) -> None:
        policy = service.inst.dir / "plugin-policy.json"
        path = service.install_template(exec_start=self._binary(tmp_path), policy=policy)

        assert path == service.template_path
        assert path.exists()
        text = path.read_text("utf-8")
        assert "ExecStart=" in text and "plugin serve" in text
        assert ["systemctl", "daemon-reload"] in recorded
        assert ["systemctl", "enable", "frpsctl-plugin@test.service"] in recorded

    def test_install_rejects_non_loopback_bind(
        self, service, recorded, tmp_path, as_root, fake_accounts
    ) -> None:
        from frpsctl.errors import UsageError

        with pytest.raises(UsageError, match="非回环"):
            service.install_template(
                exec_start=self._binary(tmp_path),
                policy=service.inst.dir / "plugin-policy.json",
                bind="0.0.0.0:8080",
            )
        assert not service.template_path.exists()
        assert recorded == [], "体检未过却调用了 systemctl"

    def test_install_rejects_missing_policy(
        self, service, recorded, tmp_path, as_root, fake_accounts
    ) -> None:
        from frpsctl.errors import UsageError

        with pytest.raises(UsageError, match="策略文件不存在"):
            service.install_template(
                exec_start=self._binary(tmp_path),
                policy=tmp_path / "nope.json",
            )
        assert recorded == []

    def test_install_rejects_home_instance_dir(self, tmp_path, as_root, fake_accounts) -> None:
        """家目录下的实例会被 ProtectHome=true 挡住：必须在安装前拒绝。"""
        from frpsctl.core.instance import Instance
        from frpsctl.core.systemd import PluginService
        from frpsctl.errors import UsageError

        home_inst = Instance(
            name="t",
            instances_root=Path("/home/someone/.local/share/frpsctl/instances"),
            data_home=tmp_path / "data",
        )
        service = PluginService(home_inst, unit_dir=tmp_path / "systemd-home")
        policy = tmp_path / "policy.json"
        policy.write_text('{"users": {}}', "utf-8")

        with pytest.raises(UsageError, match="ProtectHome"):
            service.install_template(exec_start=self._binary(tmp_path), policy=policy)
        assert not service.template_path.exists()

    def test_uninstall_disables_and_removes(self, service, recorded, as_root) -> None:
        service.template_path.parent.mkdir(parents=True, exist_ok=True)
        service.template_path.write_text("unit", "utf-8")
        service.uninstall()
        assert not service.template_path.exists()
        assert ["systemctl", "disable", "--now", "frpsctl-plugin@test.service"] in recorded
        assert ["systemctl", "daemon-reload"] in recorded

    def test_relative_paths_are_resolved(
        self, recorded, tmp_path, as_root, fake_accounts, monkeypatch
    ) -> None:
        """相对路径输入必须渲染为绝对路径（systemd 不接受相对路径）。

        回归（第五轮 review 实测复现）：`--root ./instances` 时渲染出
        `ExecStart=bin/frpsctl`、`WorkingDirectory=instances/t`——安装成功，
        `systemctl start` 才报 "Executable path is not absolute"。
        """
        from frpsctl.core.instance import Instance
        from frpsctl.core.systemd import PluginService

        monkeypatch.chdir(tmp_path)
        inst = Instance(name="t", instances_root=Path("instances"), data_home=Path("data"))
        inst.ensure_dirs()
        inst.config.write_text("bindPort = 17000\n", "utf-8")
        policy = inst.dir / "plugin-policy.json"
        policy.write_text('{"users": {}}', "utf-8")
        binary = Path("bin/frpsctl")
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\ntrue\n", "utf-8")
        binary.chmod(0o755)

        service = PluginService(inst, unit_dir=Path("units"))
        path = service.install_template(exec_start=binary, policy=policy)

        text = path.read_text("utf-8")
        resolved_bin = (tmp_path / "bin" / "frpsctl").resolve()
        resolved_work = (tmp_path / "instances" / "t").resolve()
        assert f"ExecStart={resolved_bin}" in text
        assert f"WorkingDirectory={resolved_work}" in text
        assert f"--policy {resolved_work / 'plugin-policy.json'}" in text
        assert "ExecStart=bin/" not in text, "渲染了相对路径"


class TestDoctorBinaryDiagnosis:
    """`doctor` 对"低于门槛"的二进制必须给出**正确**的诊断。

    回归：`_read_version` 曾用带门槛的 `lc.binary_version()`——`< 0.70.0` 抛
    `UnsupportedVersion` 被吞成 None，最终报"无法读取版本 / 可能不是官方
    frps"。真因是"版本太低"，错误诊断会把用户引向完全错误的方向。
    """

    def test_unsupported_version_is_reported_as_such(self, inst) -> None:
        from frpsctl.core.doctor import run_doctor

        inst.config.write_text(
            'bindPort = 17000\n[auth]\ntoken = "x"\n[webServer]\nport = 0\n', "utf-8"
        )
        inst.config.chmod(0o600)
        make_fake_frps(inst.bin_dir, version="0.69.1")

        findings = run_doctor(inst).findings
        version_findings = [f for f in findings if f.check == "二进制版本"]
        assert version_findings, [f.check for f in findings]
        message = version_findings[0].message
        assert "低于最低支持版本" in message, message
        assert "无法读取版本" not in message


class TestDoctorWebPasswordFile:
    """`doctor` 检查 web-password 的权限（它含管理台登录口令）。

    未部署 Web 管理台时文件不存在——那是常态，不该有任何输出噪音。
    """

    def _findings(self, tmp_path, *, mode: int | None):
        from frpsctl.core.doctor import run_doctor
        from frpsctl.core.instance import Instance

        inst = Instance(name="t", instances_root=tmp_path / "instances", data_home=tmp_path / "data")
        inst.ensure_dirs()
        inst.config.write_text(
            'bindPort = 17000\n[auth]\ntoken = "x"\n[webServer]\nport = 0\n', "utf-8"
        )
        inst.config.chmod(0o600)
        if mode is not None:
            path = inst.dir / "web-password"
            path.write_text("pw\n", "utf-8")
            path.chmod(mode)
        return run_doctor(inst).findings

    def test_loose_permissions_are_warned(self, tmp_path) -> None:
        hits = [f for f in self._findings(tmp_path, mode=0o644) if f.check == "Web 口令文件"]
        assert hits and hits[0].severity.value == "WARN"
        assert "0600" in hits[0].message

    def test_0600_is_reported_ok(self, tmp_path) -> None:
        hits = [f for f in self._findings(tmp_path, mode=0o600) if f.check == "Web 口令文件"]
        assert hits and hits[0].severity.value == "INFO"

    def test_absent_file_is_silent(self, tmp_path) -> None:
        hits = [f for f in self._findings(tmp_path, mode=None) if f.check == "Web 口令文件"]
        assert not hits, "未部署管理台时不该产生噪音"


class TestProxyTrafficParsing:
    """`proxy_traffic`：真机形状的解析与名称转义（Web 趋势图数据源）。"""

    def test_parses_shape_and_quotes_name(self) -> None:
        from frpsctl.core.admin import AdminClient

        seen: list[str] = []

        class _Resp:
            status_code = 200

            def json(self):
                return {
                    "data": {
                        "name": "alice/a b",
                        "unit": "bytes",
                        "granularity": "day",
                        "history": [
                            {"date": "2026-09-16", "trafficIn": 1024, "trafficOut": 2048},
                            {"date": "2026-09-17", "trafficIn": 0, "trafficOut": 0},
                        ],
                    }
                }

        class _Client(AdminClient):
            def _get(self, path, *, params=None):
                seen.append(path)
                return _Resp()

        client = _Client("http://127.0.0.1:1")
        history = client.proxy_traffic("alice/a b")

        assert seen == ["/api/v2/proxies/alice%2Fa%20b/traffic"], seen
        assert history == [
            {"date": "2026-09-16", "in": 1024, "out": 2048},
            {"date": "2026-09-17", "in": 0, "out": 0},
        ]

    def test_missing_history_returns_empty(self) -> None:
        from frpsctl.core.admin import AdminClient

        class _Resp:
            status_code = 200

            def json(self):
                return {"data": {"name": "x"}}

        class _Client(AdminClient):
            def _get(self, path, *, params=None):
                return _Resp()

        assert _Client("http://127.0.0.1:1").proxy_traffic("x") == []

    def test_not_found_means_no_data(self) -> None:
        """404 = "这个代理没有数据"（C10 的端点事实），返回空列表而不是报错。

        离线/不存在的代理返回 404——它是**常态**（清理前的离线记录、已下线的
        客户端），不是 dashboard 故障。把它升级成 `AdminUnreachable` 会让
        "查一个离线代理的历史"变成错误；CLI `traffic` 与 Web 趋势图都依赖
        这里返回空。
        """
        from frpsctl.core.admin import AdminClient

        class _Resp:
            status_code = 404

        class _Client(AdminClient):
            def _get(self, path, *, params=None):
                return _Resp()

        assert _Client("http://127.0.0.1:1").proxy_traffic("gone") == []


class TestAdminPaging:
    """`_paged` 必须按 `total` 翻页取全量。

    实测（真 frp 0.71.0）：服务端把 `page_size` 上限 **cap 到 50**——请求 200
    只返回 50 条（`pageSize=50`）。只取一页会静默少数据，因此这里锁定
    "按 total 收敛、与请求页大小无关"的行为。
    """

    @staticmethod
    def _client(*, total: int, cap: int = 50):
        from frpsctl.core.admin import AdminClient

        calls: list[int] = []

        class _Resp:
            status_code = 200

            def __init__(self, payload: dict) -> None:
                self._payload = payload

            def json(self) -> dict:
                return self._payload

        class _Client(AdminClient):
            def _get(self, path, *, params=None):
                page = int(params["page"])
                calls.append(page)
                start = (page - 1) * cap
                end = min(start + cap, total)
                items = [
                    {
                        "name": f"p{index}",
                        "type": "tcp",
                        "spec": {"type": "tcp", "tcp": {"remotePort": 6000 + index}},
                        "status": {"phase": "online"},
                    }
                    for index in range(start, end)
                ]
                return _Resp({"data": {"total": total, "page": page, "pageSize": cap, "items": items}})

        return _Client("http://127.0.0.1:1"), calls

    def test_list_proxies_pages_until_total(self) -> None:
        client, calls = self._client(total=107)
        items = client.list_proxies()
        assert len(items) == 107, f"少拉了 {107 - len(items)} 条"
        assert calls == [1, 2, 3]
        assert items[0].name == "p0"
        assert items[-1].name == "p106"
        assert items[0].remote_port == 6000

    def test_list_clients_pages_until_total(self) -> None:
        client, calls = self._client(total=120)
        items = client.list_clients()
        assert len(items) == 120
        assert calls == [1, 2, 3]

    def test_exact_total_does_not_fetch_extra_page(self) -> None:
        client, calls = self._client(total=100)
        assert len(client.list_clients()) == 100
        assert calls == [1, 2]


class TestUiOutputResilience:
    """stderr 断开（`2>&1 | head` 等）时，所有 stderr 辅助输出必须静默。

    回归（第六轮 review 实测复现）：`note` / `progress` / `warn` / `trace`
    曾直接写 stderr——管道断开时抛 `BrokenPipeError`。挂在启动流程里的进度
    回调抛异常会触发 `_reap_after_failure`：**刚派生的 frps 被误杀**；而
    `warn` 在 `map_exceptions` 的错误报告路径上抛异常会变成 traceback。
    """

    class _Broken:
        def write(self, _text: str) -> None:
            raise BrokenPipeError("EPIPE")

        def flush(self) -> None:
            raise BrokenPipeError("EPIPE")

        def isatty(self) -> bool:
            return False

    def test_stderr_helpers_never_raise(self, monkeypatch) -> None:
        import sys

        from frpsctl.cli import ui
        from frpsctl.core import diagnostics

        monkeypatch.setattr(sys, "stderr", self._Broken())
        ui.note("x")
        ui.progress("y")
        ui.warn("z")
        ui.end_progress()
        monkeypatch.setattr(diagnostics, "_VERBOSE", True)
        ui.trace("t")

    def test_progress_tty_branch_clears_line(self, monkeypatch) -> None:
        """tty 模式下必须带清行尾码（`\\033[K`），否则短文本留残影。"""
        import io
        import sys

        from frpsctl.cli import ui

        captured = io.StringIO()

        class _Tty:
            def write(self, text: str) -> None:
                captured.write(text)

            def flush(self) -> None:
                pass

            def isatty(self) -> bool:
                return True

        monkeypatch.setattr(sys, "stderr", _Tty())
        ui.progress("等待健康检查 9s：L1 ok")
        assert "\r" in captured.getvalue()
        assert "\033[K" in captured.getvalue(), "tty 进度缺清行尾码（可能有残影）"


class TestWebService:
    """`web service install` 的 unit 渲染、口令文件与部署体检。"""

    def test_render_web_unit(self) -> None:
        from frpsctl.core.systemd import render_web_unit

        text = render_web_unit(
            exec_start="/usr/local/bin/frpsctl",
            bind="127.0.0.1:8787",
            password_file=Path("/opt/frpsctl/instances/web/web-password"),
            workdir=Path("/opt/frpsctl/instances/web"),
        )
        assert (
            "ExecStart=/usr/local/bin/frpsctl --instance %i web serve "
            "--bind 127.0.0.1:8787 --password-file /opt/frpsctl/instances/web/web-password" in text
        )
        # 交互工具：崩了拉起、正常停止不自启
        assert "Restart=on-failure" in text
        assert "--allow-non-loopback" not in text
        assert "ReadWritePaths=/opt/frpsctl/instances/web" in text

    def test_render_web_unit_non_loopback_adds_flag(self) -> None:
        from frpsctl.core.systemd import render_web_unit

        text = render_web_unit(
            exec_start="/usr/local/bin/frpsctl",
            bind="0.0.0.0:8787",
            password_file=Path("/x/web-password"),
            workdir=Path("/x"),
            allow_non_loopback=True,
        )
        assert "--allow-non-loopback" in text

    @pytest.fixture
    def service(self, inst, tmp_path):
        from frpsctl.core.systemd import WebService

        inst.config.write_text("bindPort = 17000\n", "utf-8")
        return WebService(inst, unit_dir=tmp_path / "systemd-web")

    @pytest.fixture
    def recorded(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    @pytest.fixture
    def as_root(self, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: 0)

    @pytest.fixture
    def fake_accounts(self, monkeypatch):
        monkeypatch.setattr(
            "frpsctl.core.systemd._account_ids",
            lambda *_: (os.getuid(), os.getgid()),
        )

    def _binary(self, tmp_path) -> Path:
        binary = tmp_path / "frpsctl"
        binary.write_text("#!/bin/sh\ntrue\n", "utf-8")
        binary.chmod(0o755)
        return binary

    def test_install_generates_password_file_once(
        self, service, recorded, tmp_path, as_root, fake_accounts
    ) -> None:
        """首次安装生成 0600 口令文件并返回明文（只显示一次）；重装不重生成。"""
        binary = self._binary(tmp_path)
        path, generated = service.install_template(exec_start=binary)

        assert path.exists()
        assert generated, "首次安装应返回生成的口令"
        assert service.password_file.read_text("utf-8").strip() == generated
        assert service.password_file.stat().st_mode & 0o777 == 0o600
        assert ["systemctl", "enable", "frpsctl-web@test.service"] in recorded

        _, again = service.install_template(exec_start=binary, force=True)
        assert again == "", "已有口令文件时不得重新生成/重打"

    def test_install_unit_never_contains_plaintext_password(
        self, service, recorded, tmp_path, as_root, fake_accounts
    ) -> None:
        """unit 是 0644：口令明文绝不能进 unit（只以文件路径引用）。"""
        _, generated = service.install_template(exec_start=self._binary(tmp_path))
        text = service.template_path.read_text("utf-8")
        assert generated not in text
        assert str(service.password_file) in text

    def test_install_rejects_home_instance_dir(self, tmp_path, as_root, fake_accounts) -> None:
        from frpsctl.core.instance import Instance
        from frpsctl.core.systemd import WebService
        from frpsctl.errors import UsageError

        home_inst = Instance(
            name="t",
            instances_root=Path("/home/someone/.local/share/frpsctl/instances"),
            data_home=tmp_path / "data",
        )
        service = WebService(home_inst, unit_dir=tmp_path / "systemd-home")
        with pytest.raises(UsageError, match="ProtectHome"):
            service.install_template(exec_start=self._binary(tmp_path))
        assert not service.template_path.exists()

    def test_uninstall_keeps_password_file(
        self, service, recorded, tmp_path, as_root, fake_accounts
    ) -> None:
        service.install_template(exec_start=self._binary(tmp_path))
        service.uninstall()
        assert not service.template_path.exists()
        assert service.password_file.exists(), "口令文件是数据，卸载 unit 不该删它"



class TestDoctorStatRace:
    """doctor 与文件系统竞态：`exists()` 之后 `stat()` 可能失败（v0.3.1）。

    真窗口极小（并发删除/权限变化），但 doctor 的承诺是"只报告、不崩"——
    在这个窗口里抛裸 OSError 会让整个体检变成"未分类错误(1)"。
    """

    def test_permissions_check_survives_stat_race(self, inst, monkeypatch) -> None:
        from frpsctl.core import doctor as doc
        from frpsctl.core.doctor import Severity

        inst.config.write_text("bindPort = 17000\n", "utf-8")
        real_stat = Path.stat
        calls: list[int] = []

        def flaky(self, *args, **kwargs):
            if self == inst.config:
                calls.append(1)
                # 第一次（exists 内部）正常，之后（读取权限位时）失败
                if len(calls) >= 2:
                    raise PermissionError("simulated race")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", flaky)
        findings = doc._check_permissions(inst)
        assert findings, "竞态失败必须有可见的 Finding，而不是空列表"
        assert findings[0].severity is Severity.WARN


class TestSystemdOwnerDetection:
    """`is_active()` 是 `resolve_owner()` 判 `Owner.SYSTEMD` 的**唯一依据**（ADR-1）。

    它一旦误判，`start`/`stop` 就会走错分支：要么在 systemd 已托管时又直接派生一个
    frps（双起），要么反过来把 direct 实例交给 systemctl。
    """

    @pytest.fixture
    def systemd(self, inst, tmp_path):
        from frpsctl.core.systemd import Systemd

        unit_dir = tmp_path / "systemd"
        unit_dir.mkdir()
        return Systemd(inst, unit_dir=unit_dir)

    def _runner(self, monkeypatch, *, cat_rc: int = 0, active: str = "active", calls: list | None = None):
        def fake_run(argv, **kwargs):
            if calls is not None:
                calls.append(list(argv))
            if "cat" in argv:
                return subprocess.CompletedProcess(argv, cat_rc, stdout="[Unit]\n", stderr="")
            if "is-active" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=f"{active}\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: "/usr/bin/systemctl")

    def test_active_unit_is_detected(self, systemd, monkeypatch) -> None:
        self._runner(monkeypatch, active="active")
        assert systemd.is_active() is True

    def test_inactive_unit_is_not_active(self, systemd, monkeypatch) -> None:
        self._runner(monkeypatch, active="inactive")
        assert systemd.is_active() is False

    def test_missing_unit_is_not_active(
        self,
        systemd,
        monkeypatch,
    ) -> None:
        """unit 不存在（`systemctl cat` 非零）→ 不得再问 is-active。

        `is-active` 对不存在的 unit 也会返回非零，但先判存在性能给出更稳的语义，
        且避免把"unit 不存在"与"unit 存在但没跑"混为一谈。
        """
        calls: list[list[str]] = []
        self._runner(monkeypatch, cat_rc=1, calls=calls)
        assert systemd.is_active() is False
        assert not any("is-active" in call for call in calls)

    def test_not_available_short_circuits(self, systemd, monkeypatch) -> None:
        """容器里没有 systemctl → 直接 False，且**不执行任何命令**。"""
        calls: list[list[str]] = []
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: None)
        monkeypatch.setattr(subprocess, "run", lambda argv, **_kw: calls.append(argv))
        assert systemd.is_active() is False
        assert calls == []

    def test_systemctl_timeout_is_collected_into_contract_error(
        self, systemd, monkeypatch
    ) -> None:
        """v0.3.1：`subprocess.TimeoutExpired` 必须收口为契约内异常。

        此前它一路裸冒到 CLI 顶层变成"未分类错误(1)"——把"systemd 无响应"
        误报成"工具内部出错"（与 `release._verify_binary` 修过的是同一类缺口）。
        """
        from frpsctl.errors import FrpsctlError

        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: "/usr/bin/systemctl")

        def timeout_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 10))

        monkeypatch.setattr(subprocess, "run", timeout_run)
        with pytest.raises(FrpsctlError) as excinfo:
            systemd.is_active()
        assert "超时" in excinfo.value.message
        assert "systemctl" in excinfo.value.message

    def test_systemctl_oserror_is_collected_into_contract_error(
        self, systemd, monkeypatch
    ) -> None:
        """v0.3.1 review：systemctl 在 available 检查与执行之间被删（罕见竞态）
        → 裸 OSError 会让 resolve_owner/status 崩；必须同样收口为契约异常。"""
        from frpsctl.errors import FrpsctlError

        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: "/usr/bin/systemctl")

        def gone(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", argv[0])

        monkeypatch.setattr(subprocess, "run", gone)
        with pytest.raises(FrpsctlError) as excinfo:
            systemd.is_active()
        assert "systemctl" in excinfo.value.message

    def test_owner_probe_failure_degrades_visibly_and_is_cached(
        self, inst, write_config, monkeypatch
    ) -> None:
        """探测失败时 `resolve_owner` 不抛：降级判定 + 记录错误 + 缓存复用。

        要求三点：① `status()` 永不崩（承诺）；② 降级结果可见
        （`systemd_probe_error` 字段）；③ 错误也进缓存——否则 systemd 卡死时
        每轮 status 都要重打一次 10 秒超时。
        """
        from frpsctl.core.lifecycle import Lifecycle, Owner
        from frpsctl.core.systemd import Systemd

        write_config("bindPort = 17000\n")
        monkeypatch.setattr(Systemd, "available", property(lambda _self: True))
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: "/usr/bin/systemctl")
        calls = 0

        def timeout_run(argv, **kwargs):
            nonlocal calls
            calls += 1
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 10))

        monkeypatch.setattr(subprocess, "run", timeout_run)

        lc = Lifecycle(inst)
        assert lc.resolve_owner() is Owner.NONE, "无 state.json 时降级为 NONE"
        assert lc.owner_probe_error() and "超时" in lc.owner_probe_error()
        first_round = calls
        assert first_round >= 1
        lc.resolve_owner()  # 第二次：2 秒 TTL 内必须命中缓存
        assert calls == first_round, "探测错误没有进缓存（每轮都会重打超时）"

        report = Lifecycle(inst).status()
        assert report.state_corrupted is False
        assert report.systemd_probe_error and "超时" in report.systemd_probe_error

    def test_mutations_refuse_when_owner_probe_failed(
        self, inst, write_config, monkeypatch
    ) -> None:
        """变更类操作在所有权不可判定时 **fail-closed 拒绝**（ADR-7）。

        stop 尤其危险：systemd 托管实例通常没有 state.json，降级判定会把它
        误报成"未运行(5)"——用户以为没在跑，而 unit 其实活着。
        """
        from frpsctl.core.lifecycle import Lifecycle
        from frpsctl.core.systemd import Systemd
        from frpsctl.errors import OwnershipConflict

        write_config("bindPort = 17000\n")
        monkeypatch.setattr(Systemd, "available", property(lambda _self: True))
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: "/usr/bin/systemctl")

        def timeout_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 10))

        monkeypatch.setattr(subprocess, "run", timeout_run)
        lc = Lifecycle(inst)
        with pytest.raises(OwnershipConflict):
            lc.start()
        with pytest.raises(OwnershipConflict):
            lc.stop()

    def test_owner_becomes_systemd_when_unit_active(self, inst, monkeypatch) -> None:
        """端到端：unit active → `Lifecycle.resolve_owner()` 返回 SYSTEMD。

        `resolve_owner()` 内部是**延迟导入**（`from .systemd import Systemd`），因此
        patch 点必须是 `frpsctl.core.systemd.Systemd` 本身，而不是 `lifecycle` 的
        模块属性——否则打的是一个永远不会被读的名字，测试会"通过"却什么都没验证。
        """
        from frpsctl.core import systemd as sd_mod
        from frpsctl.core.lifecycle import Lifecycle, Owner
        from frpsctl.core.systemd import Systemd

        unit_dir = inst.dir / "units"
        unit_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sd_mod, "Systemd", lambda instance, **_kw: Systemd(instance, unit_dir=unit_dir))
        self._runner(monkeypatch, active="active")
        assert Lifecycle(inst).resolve_owner() is Owner.SYSTEMD

        # 反向：unit 未 active 且无 state.json → NONE（不能把"有 unit 文件"当成托管）
        self._runner(monkeypatch, active="inactive")
        assert Lifecycle(inst).resolve_owner() is Owner.NONE

        # 反向：unit 未 active 但存在 state.json → DIRECT
        inst.write_state({"pid": 1, "start_time": 1, "binary": "/x", "config": "/y"})
        assert Lifecycle(inst).resolve_owner() is Owner.DIRECT

    def test_status_survives_corrupted_state_with_systemd(self, inst, monkeypatch) -> None:
        """systemd 托管 + 损坏的残留 state.json：必须照常报告 SYSTEMD_ACTIVE。

        回归（P0）：`status` 的损坏分支直接返回 owner=NONE/STOPPED，**完全不探测
        unit**——而 systemd 托管下 state.json 本就不参与任何判定（ADR-1）。一份
        残留且损坏的 state.json（direct → systemd 迁移的常见遗留）会让 status
        误报"服务没在跑"，用户据此去"重启"，而服务一直在正常服务。
        """
        from frpsctl.core import systemd as sd_mod
        from frpsctl.core.lifecycle import Lifecycle, State
        from frpsctl.core.systemd import Systemd

        inst.config.write_text("bindPort = 17000\n", "utf-8")
        inst.config.chmod(0o600)
        inst.state.write_text("{broken json", "utf-8")

        unit_dir = inst.dir / "units"
        unit_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            sd_mod, "Systemd", lambda instance, **_kw: Systemd(instance, unit_dir=unit_dir)
        )
        self._runner(monkeypatch, active="active")

        report = Lifecycle(inst).status()
        assert report.state is State.SYSTEMD_ACTIVE, report
        assert report.state_corrupted is True
        assert report.systemd_unit == "frps@test.service"

    def test_corrupted_state_without_systemd_is_indeterminate(self, inst, monkeypatch) -> None:
        """对照：没有 systemd 时，损坏 state.json 仍报"不可判定"而不是硬猜。"""
        from frpsctl.core.lifecycle import Lifecycle, Owner, State

        inst.config.write_text("bindPort = 17000\n", "utf-8")
        inst.config.chmod(0o600)
        inst.state.write_text("{broken json", "utf-8")
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: None)

        report = Lifecycle(inst).status()
        assert report.state is State.STOPPED
        assert report.owner is Owner.NONE
        assert report.state_corrupted is True

    def test_same_config_active_matches_execstart_path(self, systemd, monkeypatch) -> None:
        """`same_config_active` 比的是 ExecStart 里的**配置路径**，不是 unit 名。

        用户完全可能自建一个名字与 frps 无关的 unit 指向我们的配置——那样也必须
        被认出来，否则会出现 direct 与 systemd 双起（ADR-1 要防的事）。用例
        特意把 unit 命名为 `my-tunnel.service`：此前按 `frps*` 过滤时它扫不到。
        """
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if "list-units" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, stdout="my-tunnel.service loaded active running\n", stderr=""
                )
            if "show" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=f"{{ path={systemd.inst.config} ; argv[]={systemd.inst.config} ; }}\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: "/usr/bin/systemctl")
        assert systemd.same_config_active() is True

    def test_same_config_active_ignores_other_configs(self, systemd, monkeypatch) -> None:
        def fake_run(argv, **kwargs):
            if "list-units" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, stdout="frps-custom.service loaded active running\n", stderr=""
                )
            if "is-active" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="active\n", stderr="")
            if "show" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, stdout="{ path=/etc/frps/instances/other/frps.toml ; }\n", stderr=""
                )
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: "/usr/bin/systemctl")
        assert systemd.same_config_active() is False

    def test_same_config_active_skips_inactive_units(self, systemd, monkeypatch) -> None:
        """非 active 的 unit 交给 systemd 侧过滤（`--state=active`）。

        修复后不再逐个 `is-active`：过滤在 `list-units` 参数里完成，空列表时
        连 `show` 都不调用。
        """
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: "/usr/bin/systemctl")
        assert systemd.same_config_active() is False
        assert any("--state=active" in call for call in calls), calls
        assert not any("is-active" in call for call in calls), "不需要逐个问 is-active"
        assert not any("show" in call for call in calls), "空列表时不该调用 show"

    def test_same_config_active_batches_show_calls(self, systemd, monkeypatch) -> None:
        """v0.3.1：多个 active unit 只发**一次** `systemctl show`。

        此前逐个 unit 查询：多实例机器上 30 个 active unit 就是 30 次子进程
        （~300ms），而且发生在 start 的实例锁内。
        """
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if "list-units" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=(
                        "a.service loaded active running\n"
                        "b.service loaded active running\n"
                        "c.service loaded active running\n"
                    ),
                    stderr="",
                )
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: "/usr/bin/systemctl")
        assert systemd.same_config_active() is False
        show_calls = [call for call in calls if "show" in call]
        assert len(show_calls) == 1, f"应批量查询（实际 {len(show_calls)} 次）"
        assert "c.service" in show_calls[0], show_calls[0]


class TestSystemdDisableOnly:
    """`disable()`（只停用、不删共享模板）——多实例卸载依赖它。

    `uninstall()` 会删 `frps@.service` 模板，而模板是所有实例共享的：多实例
    机器上卸载一个实例只能 `disable --now`，删模板会连累其他实例。
    """

    @pytest.fixture
    def systemd(self, inst, tmp_path):
        from frpsctl.core.systemd import Systemd

        return Systemd(inst, unit_dir=tmp_path / "systemd")

    @pytest.fixture
    def recorded(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    @pytest.fixture
    def as_root(self, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: 0)

    def test_disable_only_stops_instance_unit(self, systemd, recorded, as_root) -> None:
        systemd.disable()
        assert ["systemctl", "disable", "--now", systemd.unit_name] in recorded
        assert all("daemon-reload" not in call for call in recorded), "disable 不应做 daemon-reload"

    def test_is_enabled_reads_systemd_state(self, systemd, monkeypatch) -> None:
        """`is_enabled()` 判定"本实例的 unit 是否开机自启"（不是看共享模板）。

        systemctl 的返回被 mock 成三态：不存在 / enabled / disabled。
        """
        import subprocess as _sp

        def fake_run(argv, **kwargs):
            if argv[1:2] == ["cat"]:
                exists = fake_run.exists
                return _sp.CompletedProcess(argv, 0 if exists else 1, stdout="", stderr="")
            if argv[1:2] == ["is-enabled"]:
                return _sp.CompletedProcess(argv, 0, stdout=fake_run.state + "\n", stderr="")
            return _sp.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(_sp, "run", fake_run)
        monkeypatch.setattr(
            "shutil.which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None
        )
        unit_dir = systemd.unit_dir
        unit_dir.mkdir(parents=True, exist_ok=True)

        fake_run.exists, fake_run.state = False, "enabled"
        assert systemd.is_enabled() is False, "unit 不存在时必须是 False"
        fake_run.exists, fake_run.state = True, "enabled"
        assert systemd.is_enabled() is True
        fake_run.exists, fake_run.state = True, "disabled"
        assert systemd.is_enabled() is False



# ---------------------------------------------------------------------------
# v0.2.6：日志反向读 / 分页 total / 审计读取 / doctor counts
# ---------------------------------------------------------------------------


class TestTailLinesReverseRead:
    """`core/logs.tail_lines` 从文件尾反向按块读（v0.2.6）。

    旧实现用 `deque(maxlen)` 全量扫描：100MB 日志每看一次读 100MB、Web 每 5 秒
    轮询一次再读一遍。反向读之后读取量与"需要几行"相关，与日志总量解耦。
    """

    def test_basic_tail(self, tmp_path) -> None:
        from frpsctl.core.logs import tail_lines

        path = tmp_path / "a.log"
        path.write_text("1\n2\n3\n4\n5\n", "utf-8")
        assert tail_lines(path, 2) == ["4\n", "5\n"]
        assert tail_lines(path, 5) == ["1\n", "2\n", "3\n", "4\n", "5\n"]

    def test_lines_larger_than_file(self, tmp_path) -> None:
        from frpsctl.core.logs import tail_lines

        path = tmp_path / "a.log"
        path.write_text("x\ny\n", "utf-8")
        assert tail_lines(path, 100) == ["x\n", "y\n"]

    def test_no_trailing_newline(self, tmp_path) -> None:
        from frpsctl.core.logs import tail_lines

        path = tmp_path / "a.log"
        path.write_text("a\nb\nc", "utf-8")
        assert tail_lines(path, 2) == ["b\n", "c"]
        assert tail_lines(path, 1) == ["c"]
        assert tail_lines(path, 4) == ["a\n", "b\n", "c"]

    def test_single_line_without_newline(self, tmp_path) -> None:
        from frpsctl.core.logs import tail_lines

        path = tmp_path / "a.log"
        path.write_text("only", "utf-8")
        assert tail_lines(path, 5) == ["only"]

    def test_empty_missing_and_zero(self, tmp_path) -> None:
        from frpsctl.core.logs import tail_lines

        path = tmp_path / "a.log"
        path.write_text("", "utf-8")
        assert tail_lines(path, 5) == []
        assert tail_lines(tmp_path / "missing.log", 5) == []
        assert tail_lines(path, 0) == []

    def test_crlf_and_multibyte(self, tmp_path) -> None:
        from frpsctl.core.logs import tail_lines

        path = tmp_path / "a.log"
        path.write_text("第一行\r\n第二行\r\n", "utf-8")
        assert tail_lines(path, 1) == ["第二行\r\n"]

    def test_cross_block_boundary(self, tmp_path, monkeypatch) -> None:
        """把块大小改到极小，逼出多块回扫与边界切分路径。"""
        from frpsctl.core import logs

        monkeypatch.setattr(logs, "TAIL_BLOCK_BYTES", 7)
        path = tmp_path / "a.log"
        path.write_text("".join(f"line-{i}\n" for i in range(50)), "utf-8")
        assert logs.tail_lines(path, 3) == ["line-47\n", "line-48\n", "line-49\n"]
        assert logs.tail_lines(path, 50)[0] == "line-0\n"

    def test_multibyte_across_block_boundary(self, tmp_path, monkeypatch) -> None:
        from frpsctl.core import logs

        monkeypatch.setattr(logs, "TAIL_BLOCK_BYTES", 4)
        path = tmp_path / "a.log"
        path.write_text("aaaa\n汉字汉字\n", "utf-8")
        assert logs.tail_lines(path, 1) == ["汉字汉字\n"]

    def test_reads_only_tail_region(self, tmp_path, monkeypatch) -> None:
        """I/O 量必须与"需要几行"相关而不是文件大小（全量扫描的回归守卫）。"""
        import builtins

        from frpsctl.core import logs

        path = tmp_path / "big.log"
        with open(path, "wb") as handle:
            handle.write(b"filler filler filler\n" * 200_000)  # ~4.6 MB
            handle.write("".join(f"line-{i}\n" for i in range(100)).encode("utf-8"))
        stats = {"bytes": 0}
        real_open = builtins.open

        class _Counting:
            def __init__(self, handle) -> None:
                self._handle = handle

            def read(self, size=-1):
                data = self._handle.read(size)
                stats["bytes"] += len(data)
                return data

            def seek(self, *args):
                return self._handle.seek(*args)

            def tell(self):
                return self._handle.tell()

            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> None:
                self._handle.close()

        monkeypatch.setattr(builtins, "open", lambda *a, **k: _Counting(real_open(*a, **k)))
        lines = logs.tail_lines(path, 50)
        assert len(lines) == 50
        assert lines[-1] == "line-99\n"
        assert stats["bytes"] < 300_000, f"读取了 {stats['bytes']} 字节（应只读尾部区域）"


class TestAdminPages:
    """`core/admin`：全量翻页的 total/截断汇报与清理计数（v0.2.6）。"""

    @staticmethod
    def _client(monkeypatch, payloads):
        from frpsctl.core.admin import AdminClient

        client = AdminClient("http://127.0.0.1:1")
        queue = list(payloads)

        class _Resp:
            status_code = 200

            def __init__(self, payload) -> None:
                self._payload = payload

            def json(self):
                return {"code": 0, "data": self._payload}

        def fake_get(path, params=None):  # noqa: ANN001, ARG001
            assert queue, f"多余的请求：{path}"
            return _Resp(queue.pop(0))

        monkeypatch.setattr(client, "_get", fake_get)
        return client

    def test_page_result_total_and_multi_page(self, monkeypatch) -> None:
        client = self._client(
            monkeypatch,
            [
                {"items": [{"key": "a"}, {"key": "b"}], "total": 3},
                {"items": [{"key": "c"}], "total": 3},
            ],
        )
        page = client.page_clients()
        assert [item["key"] for item in page.items] == ["a", "b", "c"]
        assert page.total == 3
        assert page.truncated is False

    def test_truncation_is_reported_when_max_pages_exhausted(self, monkeypatch) -> None:
        """翻页上限用尽仍未拉全 → `truncated` 必须为真（旧实现静默截断）。"""
        client = self._client(
            monkeypatch,
            [
                {"items": [{"key": "a"}], "total": 10},
                {"items": [{"key": "b"}], "total": 10},
            ],
        )
        page = client._paged("/api/v2/clients", max_pages=2)
        assert page.truncated is True
        assert page.total == 10
        assert len(page.items) == 2

    def test_prune_offline_counts_before_and_after(self, monkeypatch) -> None:
        from frpsctl.core.admin import AdminClient, PageResult, V2Proxy

        client = AdminClient("http://127.0.0.1:1")
        deletes = {"count": 0}
        pages = [
            PageResult(
                items=[
                    V2Proxy(name="a", phase="offline"),
                    V2Proxy(name="b", phase="online"),
                    V2Proxy(name="c", phase="offline"),
                ],
                total=3,
            ),
            PageResult(items=[V2Proxy(name="b", phase="online")], total=1),
        ]
        monkeypatch.setattr(client, "page_proxies", lambda **_kw: pages.pop(0))
        monkeypatch.setattr(
            client, "clear_offline_proxies", lambda: deletes.__setitem__("count", deletes["count"] + 1)
        )
        outcome = client.prune_offline_proxies()
        assert (outcome.before, outcome.cleared, outcome.exact) == (2, 2, True)
        assert deletes["count"] == 1


class TestAggregateDays:
    """趋势聚合下沉到 core 后的口径（CLI 与 Web 共用同一实现）。"""

    def test_sums_by_date_sorted(self) -> None:
        from frpsctl.core.admin import aggregate_days, traffic_total

        series = [
            [
                {"date": "2026-09-17", "in": 1, "out": 2},
                {"date": "2026-09-16", "in": 5, "out": 0},
            ],
            [{"date": "2026-09-17", "in": 10, "out": 20}],
        ]
        days = aggregate_days(series)
        assert days == [
            {"date": "2026-09-16", "in": 5, "out": 0},
            {"date": "2026-09-17", "in": 11, "out": 22},
        ]
        assert traffic_total(days) == {"in": 16, "out": 22}

    def test_points_without_date_are_ignored(self) -> None:
        from frpsctl.core.admin import aggregate_days

        assert aggregate_days([[{"date": "", "in": 1, "out": 1}, {"in": 2, "out": 2}]]) == []
        assert aggregate_days([]) == []


class TestFetchHistoriesBudget:
    """趋势查询的**整体预算**（v0.3.1）。

    此前 `pool.map` 无预算：慢 dashboard 下 50 个代理 × 3s 超时最坏要等 ~19 秒，
    单个请求就会长占 Web worker 或让 CLI 干等。现在预算内返回已完成部分，
    并以 `partial=True` 如实标记——部分数据当全量展示是更糟的失败。
    """

    def test_deadline_returns_partial_results(self) -> None:
        import threading
        import time

        from frpsctl.core.admin import fetch_histories

        release = threading.Event()

        class _FakeClient:
            def proxy_traffic(self, name: str) -> list[dict]:
                if name == "slow":
                    release.wait(5)
                    return []
                return [{"date": "2026-09-17", "in": 1, "out": 2}]

        names = [f"proxy-{index}" for index in range(8)] + ["slow"]
        try:
            started = time.monotonic()
            result = fetch_histories(_FakeClient(), names, workers=2, deadline=0.5)
            elapsed = time.monotonic() - started
        finally:
            release.set()
        assert result.partial is True
        assert elapsed < 2.0, f"预算未生效（{elapsed:.1f}s）"
        assert len(result.series) == len(names), "结果必须与输入等长同序"
        assert result.series[0], "已完成的结果必须保留"
        assert result.series[-1] == [], "未完成的记空曲线"

    def test_all_done_is_not_partial(self) -> None:
        from frpsctl.core.admin import fetch_histories

        class _FakeClient:
            def proxy_traffic(self, name: str) -> list[dict]:
                return [{"date": "d", "in": 1, "out": 1}]

        result = fetch_histories(_FakeClient(), ["a", "b", "c"], workers=2, deadline=5.0)
        assert result.partial is False
        assert all(result.series)

    def test_single_name_still_works(self) -> None:
        from frpsctl.core.admin import fetch_histories

        class _FakeClient:
            def proxy_traffic(self, name: str) -> list[dict]:
                return [{"date": "d", "in": 1, "out": 1}]

        result = fetch_histories(_FakeClient(), ["only"], deadline=5.0)
        assert result.partial is False and len(result.series) == 1

    def test_offline_proxy_does_not_fail_the_batch(self) -> None:
        """离线代理（FrpsctlError）照旧记空曲线，不受预算逻辑影响。"""
        from frpsctl.core.admin import fetch_histories
        from frpsctl.errors import AdminUnreachable

        class _FakeClient:
            def proxy_traffic(self, name: str) -> list[dict]:
                if name == "gone":
                    raise AdminUnreachable("404")
                return [{"date": "d", "in": 1, "out": 1}]

        result = fetch_histories(_FakeClient(), ["ok", "gone"], deadline=5.0)
        assert result.partial is False
        assert result.series == [[{"date": "d", "in": 1, "out": 1}], []]


class TestDoctorCounts:
    def test_counts_by_severity(self) -> None:
        from frpsctl.core.doctor import DoctorReport, Finding, Severity

        report = DoctorReport(
            instance="x",
            findings=[
                Finding("a", Severity.ERROR, "e"),
                Finding("b", Severity.WARN, "w"),
                Finding("c", Severity.WARN, "w2"),
                Finding("d", Severity.INFO, "i"),
            ],
        )
        assert report.counts == {"error": 1, "warn": 2, "info": 1}
        assert report.ok is False


class TestAuditlog:
    """`core/auditlog`：策略定位 / 审计路径解析 / tail / stats。"""

    def test_resolve_policy_path_precedence(self, tmp_path, monkeypatch, inst) -> None:
        from frpsctl.core.auditlog import DEFAULT_POLICY_FILE, resolve_policy_path

        override = tmp_path / "custom.json"
        assert resolve_policy_path(inst, override) == override
        monkeypatch.setenv("FRPSCTL_PLUGIN_POLICY", str(tmp_path / "env.json"))
        assert resolve_policy_path(inst, None) == tmp_path / "env.json"
        monkeypatch.delenv("FRPSCTL_PLUGIN_POLICY")
        assert resolve_policy_path(inst, None) == inst.dir / DEFAULT_POLICY_FILE

    def test_resolve_audit_path_relative_to_policy(self, tmp_path) -> None:
        from frpsctl.core.auditlog import resolve_audit_path

        policy = tmp_path / "sub" / "policy.json"
        assert resolve_audit_path(policy, "audit.jsonl") == policy.parent / "audit.jsonl"
        assert resolve_audit_path(policy, "/var/log/a.jsonl") == Path("/var/log/a.jsonl")
        assert resolve_audit_path(policy, None) is None

    def test_load_view_defaults_match_strict_parser(self, inst) -> None:
        """宽容视图与严格解析的默认值必须一致（防两处默认漂移）。"""
        import json

        from frpsctl.core.auditlog import DEFAULT_AUDIT_FILE, load_view
        from frpsctl.plugin.policy import PluginPolicy

        policy_path = inst.dir / "plugin-policy.json"
        policy_path.write_text(json.dumps({"users": {}}), "utf-8")
        view = load_view(inst)
        assert view.available is True and view.enabled is True
        assert view.path == policy_path.parent / DEFAULT_AUDIT_FILE
        strict = PluginPolicy.parse({"users": {}})
        assert strict.audit.enabled is view.enabled
        assert view.path is not None and view.path.name == strict.audit.path.name

    def test_load_view_reports_missing_and_broken(self, inst) -> None:
        from frpsctl.core.auditlog import load_view

        view = load_view(inst)
        assert view.available is False and view.reason
        (inst.dir / "plugin-policy.json").write_text("{broken", "utf-8")
        view = load_view(inst)
        assert view.available is False and "不合法" in view.reason

    def test_load_view_disabled_and_memory_only(self, inst) -> None:
        import json

        from frpsctl.core.auditlog import load_view

        policy = inst.dir / "plugin-policy.json"
        policy.write_text(json.dumps({"users": {}, "audit": {"enabled": False}}), "utf-8")
        view = load_view(inst)
        assert view.available is True and view.enabled is False
        policy.write_text(json.dumps({"users": {}, "audit": {"path": None}}), "utf-8")
        view = load_view(inst)
        assert view.available is True and view.enabled is True and view.path is None

    def test_load_view_invalid_audit_shapes(self, inst) -> None:
        import json

        from frpsctl.core.auditlog import load_view

        policy = inst.dir / "plugin-policy.json"
        policy.write_text(json.dumps({"users": {}, "audit": "false"}), "utf-8")
        assert load_view(inst).enabled is False
        policy.write_text(json.dumps({"users": {}, "audit": {"enabled": "yes"}}), "utf-8")
        assert load_view(inst).enabled is False

    def test_parse_since_formats(self) -> None:
        from datetime import datetime

        from frpsctl.core.auditlog import parse_since
        from frpsctl.errors import UsageError

        now = 1_000_000.0
        assert parse_since("24h", now=now) == now - 86400
        assert parse_since("90m", now=now) == now - 5400
        assert parse_since("123", now=now) == 123.0
        expected = datetime.fromisoformat("2026-09-17T10:00:00").timestamp()
        assert parse_since("2026-09-17T10:00:00") == expected
        for bad in ("", "abc", "24x"):
            with pytest.raises(UsageError):
                parse_since(bad)

    def test_summarize_counts_window_and_bad_lines(self, tmp_path) -> None:
        import json

        from frpsctl.core.auditlog import summarize

        path = tmp_path / "audit.jsonl"
        records = [
            {"at_unix": 100.0, "op": "Login", "user": "alice", "decision": "allow"},
            {"at_unix": 200.0, "op": "Login", "user": "mallory", "decision": "deny", "suppressed": 3},
            {"at_unix": 300.0, "op": "NewProxy", "user": "alice", "decision": "allow"},
        ]
        with open(path, "w", encoding="utf-8") as handle:
            for item in records:
                handle.write(json.dumps(item) + "\n")
            handle.write("{half\n")  # 进程被 kill 时的半截行
        summary = summarize(path)
        assert (summary.total, summary.allow, summary.deny, summary.bad_lines) == (3, 2, 1, 1)
        assert summary.suppressed_total == 3
        assert summary.by_user["alice"] == {"allow": 2, "deny": 0}
        assert summary.by_user["mallory"] == {"allow": 0, "deny": 1}
        assert summary.by_op == {"Login": 2, "NewProxy": 1}
        assert (summary.first_at, summary.last_at) == (100.0, 300.0)

        windowed = summarize(path, since=250.0)
        assert windowed.total == 1 and windowed.by_op == {"NewProxy": 1}

    def test_summarize_bounds_user_table(self, tmp_path) -> None:
        """`user` 是客户端自报的任意字符串：统计表必须有界（输入驱动表纪律）。"""
        import json

        from frpsctl.core.auditlog import MAX_STAT_USERS, summarize

        path = tmp_path / "audit.jsonl"
        with open(path, "w", encoding="utf-8") as handle:
            for i in range(MAX_STAT_USERS + 50):
                handle.write(
                    json.dumps(
                        {"at_unix": float(i), "op": "Login", "user": f"u{i}", "decision": "deny"}
                    )
                    + "\n"
                )
        summary = summarize(path)
        assert len(summary.by_user) == MAX_STAT_USERS + 1  # 上限 + "(其他)"桶
        assert summary.by_user["(其他)"]["deny"] == 50
        assert summary.total == MAX_STAT_USERS + 50

    def test_read_tail_skips_bad_lines(self, tmp_path) -> None:
        import json

        from frpsctl.core.auditlog import read_tail

        path = tmp_path / "audit.jsonl"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"at_unix": 1.0, "op": "Login", "user": "a", "decision": "allow"}) + "\n")
            handle.write("not-json\n")
            handle.write(json.dumps({"at_unix": 2.0, "op": "Login", "user": "b", "decision": "deny"}) + "\n")
        tail = read_tail(path, 10)
        assert [r["user"] for r in tail.records] == ["a", "b"]
        assert tail.bad_lines == 1
        assert read_tail(tmp_path / "missing.jsonl", 5).records == []



class TestAuditlogOpBounds:
    def test_summarize_bounds_op_table(self, tmp_path) -> None:
        """`op` 来自请求 query（恶意请求可任意制造）：统计表同样必须有界。"""
        import json

        from frpsctl.core.auditlog import MAX_STAT_OPS, summarize

        path = tmp_path / "audit.jsonl"
        with open(path, "w", encoding="utf-8") as handle:
            for i in range(MAX_STAT_OPS + 20):
                handle.write(
                    json.dumps(
                        {"at_unix": float(i), "op": f"Evil{i}", "user": "u", "decision": "deny"}
                    )
                    + "\n"
                )
        summary = summarize(path)
        assert len(summary.by_op) == MAX_STAT_OPS + 1
        assert summary.by_op["(其他)"] == 20
        assert summary.total == MAX_STAT_OPS + 20



class TestServiceAccessLogRender:
    """`--access-log` 的 unit 渲染（v0.2.6：此前 systemd 部署无法开启访问日志，
    虽然 `plugin serve --access-log` / `web serve --access-log` 早已存在）。"""

    def test_plugin_unit_flag_only_when_requested(self, tmp_path) -> None:
        from frpsctl.core.systemd import render_plugin_unit

        base = {
            "exec_start": "/opt/frpsctl",
            "bind": "127.0.0.1:8080",
            "handler_path": "/handler",
            "policy": tmp_path / "p.json",
            "workdir": tmp_path,
        }
        plain = render_plugin_unit(**base)
        assert "--access-log" not in plain, "默认渲染不应带访问日志开关"
        line = next(
            item for item in render_plugin_unit(**base, access_log=True).splitlines()
            if item.startswith("ExecStart=")
        )
        assert line.endswith("--access-log"), line

    def test_web_unit_flag_only_when_requested(self, tmp_path) -> None:
        from frpsctl.core.systemd import render_web_unit

        base = {
            "exec_start": "/opt/frpsctl",
            "bind": "127.0.0.1:8787",
            "password_file": tmp_path / "web-password",
            "workdir": tmp_path,
        }
        plain = render_web_unit(**base)
        assert "--access-log" not in plain
        line = next(
            item for item in render_web_unit(**base, access_log=True).splitlines()
            if item.startswith("ExecStart=")
        )
        assert line.endswith("--access-log"), line
        # 与既有旗标可以叠加
        combined = next(
            item for item in render_web_unit(
                **base, access_log=True, trusted_proxy=True
            ).splitlines()
            if item.startswith("ExecStart=")
        )
        assert combined.endswith("--trusted-proxy --access-log"), combined


class TestPasswordGeneratorSingleSource:
    """口令生成器必须单点（v0.2.6 收敛：此前三处各写一份 token_urlsafe(18)，
    而 `generate_web_password` 的 docstring 却声称'单点实现'——注释与事实不符）。"""

    def test_web_generator_delegates_to_core(self) -> None:
        import inspect

        from frpsctl.core.systemd import generate_web_password
        from frpsctl.web import generate_password

        source = inspect.getsource(generate_password)
        assert "generate_web_password" in source, "web 侧生成器没有委托 core 单点"
        assert "secrets" not in source, "web 侧仍在自行实现口令生成"
        assert len(generate_web_password()) >= 20


class TestAuditSummaryElapsed:
    def test_elapsed_avg_max_and_count(self, tmp_path) -> None:
        """审计记录 elapsed_ms 的目的就是回答"插件拖慢了登录吗"——统计出口。"""
        import json

        from frpsctl.core.auditlog import summarize

        path = tmp_path / "audit.jsonl"
        with open(path, "w", encoding="utf-8") as handle:
            for i, elapsed in enumerate((10.0, 20.0, 30.0, None)):
                record = {"at_unix": float(i), "op": "Login", "user": "u", "decision": "allow"}
                if elapsed is not None:
                    record["elapsed_ms"] = elapsed
                handle.write(json.dumps(record) + "\n")
        summary = summarize(path)
        assert summary.elapsed_count == 3
        assert summary.elapsed_avg_ms == 20.0
        assert summary.elapsed_max_ms == 30.0

    def test_elapsed_absent_is_zero(self, tmp_path) -> None:
        from frpsctl.core.auditlog import summarize

        summary = summarize(tmp_path / "missing.jsonl")
        assert (summary.elapsed_count, summary.elapsed_avg_ms, summary.elapsed_max_ms) == (0, 0.0, 0.0)


class TestWebAuditCore:
    """`core/web_audit.py`：JSONL 写入、统计、指纹与失败降级。"""

    def test_record_and_summarize(self, inst) -> None:
        import time as _time

        from frpsctl.core import web_audit

        assert web_audit.record(inst, action="start", source="127.0.0.1", session_id="abc") is True
        assert (
            web_audit.record(inst, action="stop", result="error:NotRunning", source="10.0.0.9")
            is True
        )
        path = web_audit.resolve_path(inst)
        assert path.exists()

        summary = web_audit.summarize(path)
        assert summary.total == 2
        assert summary.ok == 1 and summary.error == 1
        assert summary.by_action == {"start": 1, "stop": 1}
        assert summary.by_source["127.0.0.1"] == 1
        assert summary.first_at is not None and summary.last_at is not None

        assert web_audit.summarize(path, since=_time.time() + 10).total == 0

    def test_summarize_tolerates_bad_lines(self, inst) -> None:
        from frpsctl.core import web_audit

        path = web_audit.resolve_path(inst)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"action": "start", "result": "ok", "at_unix": 1.0}\nnot-json\n', "utf-8")
        summary = web_audit.summarize(path)
        assert summary.total == 1 and summary.bad_lines == 1

    def test_session_fingerprint_is_stable_and_short(self) -> None:
        from frpsctl.core import web_audit

        first = web_audit.session_fingerprint("token-abc")
        assert first == web_audit.session_fingerprint("token-abc")
        assert len(first) == 12
        assert web_audit.session_fingerprint(None) == ""

    def test_record_never_raises_on_unwritable_path(self, inst, monkeypatch) -> None:
        """审计落盘失败 → 返回 False，绝不抛异常（管理台可用性优先）。"""
        from pathlib import Path as _Path

        from frpsctl.core import web_audit

        monkeypatch.setattr(
            web_audit, "resolve_path", lambda _inst: _Path("/proc/1/no-such-dir/web-audit.jsonl")
        )
        assert web_audit.record(inst, action="start") is False


class TestOwnerDetectionCache:
    """owner 探测缓存：TTL 内复用、state.json 戳变化必失效。"""

    def test_detection_is_cached_within_ttl(self, inst, monkeypatch) -> None:
        from frpsctl.core.lifecycle import Lifecycle

        import frpsctl.core.systemd as systemd_mod

        calls = {"n": 0}

        class _Fake:
            def __init__(self, _inst) -> None:
                pass

            def is_active(self) -> bool:
                calls["n"] += 1
                return False

        monkeypatch.setattr(systemd_mod, "Systemd", _Fake)
        lc = Lifecycle(inst)
        lc.resolve_owner()
        lc.resolve_owner()
        assert calls["n"] == 1, "TTL 内第二次探测没有走缓存"

    def test_state_stamp_change_invalidates_cache(self, inst) -> None:
        """start 写 state / stop 删 state 后，缓存不能把旧 owner 藏起来。"""
        from frpsctl.core.lifecycle import Lifecycle, Owner

        lc = Lifecycle(inst)
        assert lc.resolve_owner() is Owner.NONE
        inst.write_state({"pid": 1, "start_time": 1, "binary": "", "config": ""})
        assert lc.resolve_owner() is Owner.DIRECT, "state.json 出现后仍命中 NONE 缓存"
        inst.clear_state()
        assert lc.resolve_owner() is Owner.NONE, "state.json 删除后仍命中 DIRECT 缓存"


class TestAuditRotation:
    """审计轮转：写侧改名保留、读侧跨文件、禁用/失败安全。"""

    def test_rotate_and_read_across_files(self, tmp_path) -> None:
        from frpsctl.core import auditlog

        path = tmp_path / "plugin-audit.jsonl"
        line = '{"at_unix": %s, "user": "u%s", "decision": "allow", "op": "Login"}\n'
        path.write_text((line % (1, "a")) * 5, "utf-8")
        assert auditlog.rotate_if_needed(path, max_bytes=10) is True
        path.write_text((line % (2, "b")) * 5, "utf-8")
        assert auditlog.rotate_if_needed(path, max_bytes=10) is True
        path.write_text((line % (3, "c")) * 5, "utf-8")

        names = [item.name for item in auditlog.rotated_paths(path)]
        assert names == ["plugin-audit.jsonl", "plugin-audit.jsonl.1", "plugin-audit.jsonl.2"]

        summary = auditlog.summarize(path)
        assert summary.total == 15, "统计没有跨轮转文件"
        tail = auditlog.read_tail(path, 7)
        assert len(tail.records) == 7
        assert tail.records[-1]["at_unix"] == 3, "尾部结果必须按时间升序"
        assert tail.records[0]["at_unix"] == 2, "不足的行应从 .1 追溯"

    def test_rotation_disabled_is_noop(self, tmp_path) -> None:
        from frpsctl.core import auditlog

        path = tmp_path / "x.jsonl"
        path.write_text("x" * 100, "utf-8")
        assert auditlog.rotate_if_needed(path, max_bytes=0) is False
        assert not (tmp_path / "x.jsonl.1").exists()

    def test_web_audit_rotation_keeps_summary_complete(self, inst, monkeypatch) -> None:
        """轮转后统计必须等于"落盘可见的行数之和"（保留 2 份，旧批次按设计丢弃）。"""
        from frpsctl.core import auditlog, web_audit

        monkeypatch.setattr(web_audit, "DEFAULT_WEB_AUDIT_MAX_BYTES", 1000)
        for _ in range(30):
            web_audit.record(inst, action="start")
        path = web_audit.resolve_path(inst)
        assert (inst.dir / "web-audit.jsonl.1").exists(), "Web 审计没有轮转"
        on_disk = sum(
            len(item.read_text("utf-8").strip().splitlines())
            for item in auditlog.rotated_paths(path)
        )
        assert web_audit.summarize(path).total == on_disk
        assert on_disk < 30, "老批次按设计被轮转丢弃（保留 2 份）"


class TestPluginAuditRotation:
    def test_flush_rotates_when_over_limit(self, tmp_path) -> None:
        from frpsctl.plugin.audit import AuditLog, AuditRecord

        path = tmp_path / "audit.jsonl"
        log = AuditLog(path, flush_every=1, max_bytes=200)
        try:
            for _ in range(30):
                log.record(AuditRecord(op="Login", user="u", decision="allow"))
                log.flush()
        finally:
            log.close()
        assert (tmp_path / "audit.jsonl.1").exists(), "插件审计没有轮转"
        from frpsctl.core import auditlog

        on_disk = sum(
            len(item.read_text("utf-8").strip().splitlines())
            for item in auditlog.rotated_paths(path)
        )
        assert auditlog.summarize(path).total == on_disk
        assert on_disk < 30, "老批次按设计被轮转丢弃（保留 2 份）"


class TestOidcConfig:
    """OIDC 配置支持：完整性校验（schema）与 doctor 提示。"""

    def test_oidc_requires_issuer_and_audience(self) -> None:
        from frpsctl.core.schema import validate_document
        from frpsctl.errors import ConfigError

        with pytest.raises(ConfigError, match="auth.oidc.issuer"):
            validate_document('[auth]\nmethod = "oidc"\n')
        with pytest.raises(ConfigError, match="auth.oidc.audience"):
            validate_document('[auth]\nmethod = "oidc"\n[auth.oidc]\nissuer = "https://x"\n')
        # 完整配置通过（OIDC 协议本身由 frps 实现，本工具只管配置完整性）
        validate_document(
            '[auth]\nmethod = "oidc"\n[auth.oidc]\nissuer = "https://x"\naudience = "frps"\n'
        )

    def test_doctor_reports_missing_oidc_fields(self, inst, write_config) -> None:
        from frpsctl.core.doctor import Severity, run_doctor

        write_config('[auth]\nmethod = "oidc"\ntoken = "stale"\n')
        report = run_doctor(inst)
        oidc = [f for f in report.findings if f.check == "auth.oidc"]
        assert oidc and oidc[0].severity is Severity.ERROR
        token = [f for f in report.findings if f.check == "auth.token"]
        assert token and token[0].severity is Severity.WARN

    def test_doctor_accepts_complete_oidc(self, inst, write_config) -> None:
        from frpsctl.core.doctor import Severity, run_doctor

        write_config(
            '[auth]\nmethod = "oidc"\n[auth.oidc]\nissuer = "https://x"\naudience = "frps"\n'
        )
        report = run_doctor(inst)
        oidc = [f for f in report.findings if f.check == "auth.oidc"]
        assert oidc and oidc[0].severity is Severity.INFO


class TestAuditRotationByAge:
    """按天轮转（与 frp 日志 maxDays 同语义）：超过年龄的审计归档轮换。"""

    def test_rotation_by_age(self, tmp_path) -> None:
        import os
        import time as _time

        from frpsctl.core import auditlog

        path = tmp_path / "a.jsonl"
        path.write_text("x\n", "utf-8")
        old = _time.time() - 8 * 86400
        os.utime(path, (old, old))
        assert auditlog.rotate_if_needed(path, max_age_seconds=7 * 86400) is True
        assert (tmp_path / "a.jsonl.1").exists()

        # 刚写的文件不触发按天轮转
        path.write_text("y\n", "utf-8")
        assert auditlog.rotate_if_needed(path, max_age_seconds=7 * 86400) is False

    def test_web_audit_rotation_by_age(self, inst, monkeypatch) -> None:
        import os
        import time as _time

        from frpsctl.core import web_audit

        monkeypatch.setattr(web_audit, "DEFAULT_WEB_AUDIT_MAX_AGE_DAYS", 7.0)
        path = web_audit.resolve_path(inst)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"action": "old"}\n', "utf-8")
        old = _time.time() - 30 * 86400
        os.utime(path, (old, old))

        web_audit.record(inst, action="start")
        assert (inst.dir / "web-audit.jsonl.1").exists(), "Web 审计没有按天轮转"
        assert '"action": "start"' in path.read_text("utf-8")

    def test_plugin_audit_rotation_by_age(self, tmp_path) -> None:
        import os
        import time as _time

        from frpsctl.plugin.audit import AuditLog, AuditRecord

        path = tmp_path / "audit.jsonl"
        path.write_text("old\n", "utf-8")
        old = _time.time() - 30 * 86400
        os.utime(path, (old, old))

        log = AuditLog(path, flush_every=1, max_age_seconds=7 * 86400)
        try:
            log.record(AuditRecord(op="Login", user="u", decision="allow"))
            log.flush()
        finally:
            log.close()
        assert (tmp_path / "audit.jsonl.1").exists(), "插件审计没有按天轮转"


class TestRegressionReviewFixes:
    """v0.3.0 发布前回归 review 的修复守卫（每条对应一个实测问题）。"""

    def test_non_ascii_password_works(self) -> None:
        """M6：`hmac.compare_digest` 对非 ASCII **str** 抛 TypeError——中文口令
        会让登录线程断开、管理台完全不可用；比较必须先 encode。"""
        from frpsctl.web.auth import AuthManager

        auth = AuthManager("密码123")
        session = auth.login("密码123", source="s")
        assert session is not None, "非 ASCII 口令无法登录"
        assert auth.login("密码124", source="s2") is None
        assert auth.check_password("密码123") is True
        assert auth.check_password("错误") is False

    def test_metrics_failures_share_login_throttle(self) -> None:
        """H1：`/metrics` 的 Basic auth 失败必须计入同一张来源限速表——
        否则它是绕开登录限速的第二条口令爆破通道。"""
        from frpsctl.web.auth import MAX_FAILURES, AuthManager
        from frpsctl.web.metrics import check_basic_auth

        auth = AuthManager("right")
        bad = "Basic " + __import__("base64").b64encode(b"prom:wrong").decode()
        for _ in range(MAX_FAILURES):
            assert check_basic_auth(auth, bad, source="10.0.0.9") is False
        assert auth.is_throttled("10.0.0.9") is True
        good = "Basic " + __import__("base64").b64encode(b"prom:right").decode()
        assert check_basic_auth(auth, good, source="10.0.0.9") is False, "限速中的来源不能放行"
        assert check_basic_auth(auth, good, source="10.0.0.10") is True

    def test_lone_surrogate_password_does_not_crash(self) -> None:
        """N2：HTTP JSON 可构造 lone surrogate——比较必须用 surrogatepass，
        否则 UnicodeEncodeError 让登录线程断开（与中文口令同类症状）。
        用 chr() 构造，避免源码里出现 surrogate 字面量（会让 pytest 收集崩）。"""
        from frpsctl.web.auth import AuthManager

        auth = AuthManager("密码123")
        assert auth.login(chr(0xD800), source="s") is None
        assert auth.check_password(chr(0xD800)) is False
        assert auth.login("密码123", source="s2") is not None

    def test_doctor_survives_malformed_auth_table(self, inst, write_config) -> None:
        """N3：`auth` 本身不是表时 doctor 也不能崩（与 oidc 分支同规格）。"""
        from frpsctl.core.doctor import Severity, run_doctor

        write_config('auth = "oops"\n')
        report = run_doctor(inst)
        auth_findings = [f for f in report.findings if f.check == "auth"]
        assert auth_findings and auth_findings[0].severity is Severity.ERROR

    def test_read_tail_skips_bad_lines_across_rotation(self, tmp_path) -> None:
        """L1：坏行不应吃掉配额——当前文件全坏时仍要往 `.1` 追溯。"""
        from frpsctl.core import auditlog

        path = tmp_path / "a.jsonl"
        old_file = tmp_path / "a.jsonl.1"
        old_file.write_text(
            "".join(
                f'{{"at_unix": {i}, "user": "u{i}", "decision": "allow", "op": "Login"}}\n'
                for i in range(3)
            ),
            "utf-8",
        )
        path.write_text("not-json\n" * 3, "utf-8")
        tail = auditlog.read_tail(path, 3)
        assert len(tail.records) == 3, f"坏行吃掉了配额：{tail}"
        assert tail.bad_lines == 3
        assert [r["at_unix"] for r in tail.records] == [0, 1, 2]

    def test_doctor_survives_malformed_oidc(self, inst, write_config) -> None:
        """M4：`auth.oidc` 是字符串时 doctor 不能崩（应报 ERROR 而非
        AttributeError → 未分类错误 1，什么都不诊断）。"""
        from frpsctl.core.doctor import Severity, run_doctor

        write_config('[auth]\nmethod = "oidc"\noidc = "oops"\n')
        report = run_doctor(inst)
        oidc = [f for f in report.findings if f.check == "auth.oidc"]
        assert oidc and oidc[0].severity is Severity.ERROR

    def test_ttl_cache_is_bounded(self) -> None:
        """M2：缓存必须有上限（输入驱动的表都要有界）。"""
        from frpsctl.web.cache import DEFAULT_MAX_ENTRIES, TTLCache

        cache = TTLCache()
        for index in range(DEFAULT_MAX_ENTRIES * 3):
            cache.put(f"k{index}", index)
        assert len(cache) == DEFAULT_MAX_ENTRIES
        assert cache.get("k0", 999) is None, "最旧的条目应先被淘汰"
        assert cache.get(f"k{DEFAULT_MAX_ENTRIES * 3 - 1}", 999) is not None

    def test_web_audit_concurrent_writes_do_not_lose_records(self, inst) -> None:
        """M1：并发写（ThreadingHTTPServer）不会因轮转交错丢记录。"""
        from concurrent.futures import ThreadPoolExecutor

        from frpsctl.core import auditlog, web_audit

        total = 400

        def write(_index: int) -> None:
            web_audit.record(inst, action="start")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(total)))
        path = web_audit.resolve_path(inst)
        on_disk = sum(
            len(item.read_text("utf-8").strip().splitlines())
            for item in auditlog.rotated_paths(path)
        )
        assert on_disk == total, f"并发写入丢失记录：{on_disk}/{total}"


class TestDoctorSystemdDeployment:
    """doctor 的 systemd 部署检查（v0.3.2）：账户 / 二进制 / 路径的下游一致性。"""

    def _write_manifest(self, inst, record: dict) -> None:
        import json

        inst.ensure_dirs()
        inst.service_manifest.write_text(json.dumps(record), "utf-8")

    def test_not_installed_produces_nothing(self, inst) -> None:
        from frpsctl.core.doctor import _check_systemd_deployment

        assert _check_systemd_deployment(inst) == []

    def test_missing_service_user_is_error(self, inst) -> None:
        from frpsctl.core.doctor import Severity, _check_systemd_deployment

        self._write_manifest(inst, {"frps": {"user": "ghost-svc-x", "group": "ghost-svc-x"}})
        findings = _check_systemd_deployment(inst)
        errors = [f for f in findings if f.severity is Severity.ERROR]
        assert any("ghost-svc-x" in f.message and "不存在" in f.message for f in errors)

    def test_missing_group_is_error(self, inst) -> None:
        import pwd

        from frpsctl.core.doctor import Severity, _check_systemd_deployment

        me = pwd.getpwuid(os.geteuid()).pw_name
        self._write_manifest(inst, {"frps": {"user": me, "group": "ghost-grp-x"}})
        findings = _check_systemd_deployment(inst)
        assert any(
            f.severity is Severity.ERROR and "ghost-grp-x" in f.message for f in findings
        )

    def test_show_values_take_precedence_over_manifest(self, inst, monkeypatch) -> None:
        import pwd

        from frpsctl.core.doctor import Severity, _check_systemd_deployment

        me = pwd.getpwuid(os.geteuid()).pw_name
        self._write_manifest(inst, {"frps": {"user": me, "group": me}})
        monkeypatch.setattr("frpsctl.core.systemd.Systemd.unit_exists", lambda _self: True)
        monkeypatch.setattr(
            "frpsctl.core.systemd.show_unit_accounts",
            lambda _unit: ("ghost-svc-y", "ghost-svc-y"),
        )
        findings = _check_systemd_deployment(inst)
        assert any(
            f.severity is Severity.ERROR and "ghost-svc-y" in f.message for f in findings
        ), "systemctl show 的实际值必须优先于留档"

    def test_binary_unreachable_is_error(self, inst, monkeypatch) -> None:
        import grp
        import pwd

        from frpsctl.core.doctor import Severity, _check_systemd_deployment

        record = pwd.getpwuid(os.geteuid())
        group = grp.getgrgid(record.pw_gid).gr_name
        self._write_manifest(inst, {"frps": {"user": record.pw_name, "group": group}})
        monkeypatch.setattr("frpsctl.core.systemd.Systemd.unit_exists", lambda _self: True)
        monkeypatch.setattr(
            "frpsctl.core.systemd.show_unit_accounts",
            lambda _unit: (record.pw_name, group),
        )
        monkeypatch.setattr(
            "frpsctl.core.systemd.read_template_exec", lambda _path: "/root/secret/frps"
        )
        findings = _check_systemd_deployment(inst)
        errors = [f for f in findings if f.severity is Severity.ERROR]
        assert any("缺少执行" in f.message or "ProtectHome" in f.message for f in errors)

    def test_probe_failure_warns_not_crashes(self, inst, monkeypatch) -> None:
        from frpsctl.core.doctor import Severity, _check_systemd_deployment
        from frpsctl.errors import FrpsctlError

        def boom(_self):
            raise FrpsctlError("systemd 无响应")

        monkeypatch.setattr("frpsctl.core.systemd.Systemd.unit_exists", boom)
        findings = _check_systemd_deployment(inst)
        assert any(f.severity is Severity.WARN and "无法探测" in f.message for f in findings)

    def test_corrupted_manifest_warns(self, inst) -> None:
        from frpsctl.core.doctor import Severity, _check_systemd_deployment

        inst.ensure_dirs()
        inst.service_manifest.write_text("{ broken", "utf-8")
        findings = _check_systemd_deployment(inst)
        assert any(f.severity is Severity.WARN and f.check == "安装记录" for f in findings)

    def test_legacy_unit_without_manifest_is_checked(self, inst, monkeypatch) -> None:
        """旧部署（unit 存在、无留档）：仍用 `systemctl show` 实际账户检查。"""

        from frpsctl.core.doctor import Severity, _check_systemd_deployment

        monkeypatch.setattr("frpsctl.core.systemd.Systemd.unit_exists", lambda _self: True)
        monkeypatch.setattr(
            "frpsctl.core.systemd.show_unit_accounts",
            lambda _unit: ("ghost-legacy-x", "ghost-legacy-x"),
        )
        findings = _check_systemd_deployment(inst)
        assert any(
            f.severity is Severity.ERROR and "ghost-legacy-x" in f.message for f in findings
        )

    def test_log_dir_missing_is_error(self, inst, monkeypatch) -> None:
        import grp
        import pwd

        from frpsctl.core.doctor import Severity, _check_systemd_deployment

        record = pwd.getpwuid(os.geteuid())
        group = grp.getgrgid(record.pw_gid).gr_name
        self._write_manifest(
            inst,
            {"frps": {"user": record.pw_name, "group": group, "log_dir": "/nonexistent-logs-032"}},
        )
        monkeypatch.setattr("frpsctl.core.systemd.Systemd.unit_exists", lambda _self: True)
        monkeypatch.setattr(
            "frpsctl.core.systemd.show_unit_accounts",
            lambda _unit: (record.pw_name, group),
        )
        findings = _check_systemd_deployment(inst)
        assert any(
            f.severity is Severity.ERROR and "日志目录不可用" in f.message for f in findings
        )

    def test_workdir_not_writable_is_error(self, inst, monkeypatch) -> None:
        import grp
        import pwd

        from frpsctl.core.doctor import Severity, _check_systemd_deployment

        record = pwd.getpwuid(os.geteuid())
        group = grp.getgrgid(record.pw_gid).gr_name
        self._write_manifest(inst, {"frps": {"user": record.pw_name, "group": group}})
        monkeypatch.setattr("frpsctl.core.systemd.Systemd.unit_exists", lambda _self: True)
        monkeypatch.setattr(
            "frpsctl.core.systemd.show_unit_accounts",
            lambda _unit: (record.pw_name, group),
        )
        old_mode = inst.dir.stat().st_mode & 0o777
        inst.dir.chmod(0o500)
        try:
            findings = _check_systemd_deployment(inst)
        finally:
            inst.dir.chmod(old_mode)
        assert any(
            f.severity is Severity.ERROR and "实例目录对服务用户不可用" in f.message
            for f in findings
        )


class TestUninstallServiceWarnings:
    """卸载的服务账户 / 日志目录提示跟随安装留档（v0.3.2）。"""

    def _warnings(
        self, inst, monkeypatch, existing: set[str], log_dir: Path | None = None
    ) -> list[str]:
        import pwd

        from frpsctl.core.uninstall import (
            UninstallReport,
            _collect_manifest_hints,
            _courtesy_warnings,
        )

        def fake(name):
            if name in existing:
                return pwd.struct_passwd(("placeholder", "x", 1000, 1000, "", "/", ""))
            raise KeyError(name)

        monkeypatch.setattr("frpsctl.core.uninstall.pwd.getpwnam", fake)
        users, log_dirs = _collect_manifest_hints((inst,))
        report = UninstallReport()
        _courtesy_warnings(report, users=users, log_dirs=log_dirs, log_dir=log_dir)
        return report.warnings

    def _manifest(self, inst, data: dict) -> None:
        import json

        inst.ensure_dirs()
        inst.service_manifest.write_text(json.dumps(data), "utf-8")

    def test_manifest_users_prompted(self, inst, monkeypatch) -> None:
        import pwd

        me = pwd.getpwuid(os.geteuid()).pw_name
        self._manifest(inst, {"frps": {"user": me}, "web": {"user": me}})
        warnings = self._warnings(inst, monkeypatch, existing={me})
        assert any(f"userdel {me}" in w for w in warnings)

    def test_frps_not_reported_when_unused(self, inst, monkeypatch) -> None:
        import pwd

        me = pwd.getpwuid(os.geteuid()).pw_name
        self._manifest(inst, {"frps": {"user": me}})
        warnings = self._warnings(inst, monkeypatch, existing={me, "frps"})
        assert not any("userdel frps" in w for w in warnings), "未使用的 frps 不应误报"

    def test_log_dir_from_manifest(self, inst, tmp_path, monkeypatch) -> None:
        log_dir = tmp_path / "svclogs"
        log_dir.mkdir()
        self._manifest(inst, {"frps": {"user": "ghost", "log_dir": str(log_dir)}})
        warnings = self._warnings(inst, monkeypatch, existing=set())
        assert any(str(log_dir) in w for w in warnings)
        assert not any("/var/log/frps" in w for w in warnings)

    def test_fallback_without_manifest(self, inst, monkeypatch) -> None:
        warnings = self._warnings(inst, monkeypatch, existing={"frps"})
        assert any("userdel frps" in w for w in warnings)

    def test_execute_uninstall_collects_hints_before_deleting(
        self, inst, tmp_path, monkeypatch
    ) -> None:
        """删除顺序守卫：留档提示必须在数据删除**之前**收集。

        v0.3.2 review 实测复现：`_courtesy_warnings` 原先在 `_remove_data`
        之后才读 `service.json`——那时实例目录已被删除，自定义账户与日志
        路径的提示会静默退化成默认假设。
        """
        import json
        import pwd

        from frpsctl.core.uninstall import execute_uninstall, plan_uninstall

        me = pwd.getpwuid(os.geteuid()).pw_name
        inst.ensure_dirs()
        inst.config.write_text("bindPort = 17000\n", "utf-8")
        inst.service_manifest.write_text(
            json.dumps({"frps": {"user": me, "log_dir": "/nonexistent-review032"}}), "utf-8"
        )
        # unit_dir 注入空目录：与真实 /etc/systemd/system 完全隔离（root 开发机
        # 上也不会误删系统 unit），同时不依赖 systemctl stub。
        report = execute_uninstall(plan_uninstall(inst), unit_dir=tmp_path / "units")
        assert not inst.dir.exists(), "实例数据应已删除"
        assert any(f"userdel {me}" in w for w in report.warnings), (
            "留档中的服务账户提示在删除后丢失（收集顺序回归）"
        )


class TestUninstallCleanUnitsProbeFailure:
    """卸载的 unit 清理阶段：探测失败收进 warnings，而不是崩掉整个流程（v0.3.1）。

    `_precheck` 会拦住"探测失败 + 所有权不明"的实例，但预检与清理之间仍有
    时间窗口——清理阶段必须自己兜住，并按"降级必须可见"汇总给用户。
    """

    def test_probe_failure_becomes_visible_warning(self, inst, monkeypatch, tmp_path) -> None:
        from frpsctl.core.systemd import PluginService, Systemd, WebService
        from frpsctl.core.uninstall import UninstallReport, _clean_units
        from frpsctl.errors import FrpsctlError

        def boom(_self):
            raise FrpsctlError("systemctl 超时未返回（10 秒）")

        for cls in (Systemd, PluginService, WebService):
            monkeypatch.setattr(cls, "available", property(lambda _self: True))
            monkeypatch.setattr(cls, "is_active", boom)
            monkeypatch.setattr(cls, "is_enabled", boom)

        unit_dir = tmp_path / "units"
        unit_dir.mkdir()
        report = UninstallReport()
        _clean_units(inst, remove_templates=False, unit_dir=unit_dir, report=report)
        assert report.warnings, "探测失败必须可见"
        assert all("无法探测" in item for item in report.warnings), report.warnings
        assert len(report.warnings) == 3, "三个 unit 各自汇报：%r" % report.warnings


class TestConfigureStreams:
    """入口 UTF-8 固定（v0.3.1）：对"不寻常"的流对象保持零异常。"""

    def test_tolerates_streams_without_reconfigure(self, monkeypatch) -> None:
        import sys as _sys

        from frpsctl.core.diagnostics import configure_streams

        class _Plain:
            pass

        monkeypatch.setattr(_sys, "stdout", _Plain())
        monkeypatch.setattr(_sys, "stderr", _Plain())
        configure_streams()  # 不抛即可

    def test_reconfigure_failure_is_silent(self, monkeypatch) -> None:
        import sys as _sys

        from frpsctl.core.diagnostics import configure_streams

        class _Boom:
            def reconfigure(self, **_kwargs):
                raise ValueError("I/O operation on closed file")

        monkeypatch.setattr(_sys, "stdout", _Boom())
        monkeypatch.setattr(_sys, "stderr", _Boom())
        configure_streams()  # 不抛即可


# ---------------------------------------------------------------------------
# v0.3.4 新增：增量日志 / 锁三态 / 审计参数 / 守卫 / argv / 安装锁 / 翻页
# ---------------------------------------------------------------------------


class TestTailSince:
    """增量日志 tail（F6）：完整行、offset 推进、轮转重置、半行不丢不重。"""

    def test_first_read_is_full_with_reset(self, tmp_path: Path) -> None:
        from frpsctl.core.logs import tail_since

        log = tmp_path / "a.log"
        log.write_text("l1\nl2\nl3\n", "utf-8")
        lines, offset, reset = tail_since(log, None, max_lines=2)
        assert reset is True
        assert lines == ["l2", "l3"]
        assert offset == log.stat().st_size

    def test_incremental_only_new_lines(self, tmp_path: Path) -> None:
        from frpsctl.core.logs import tail_since

        log = tmp_path / "a.log"
        log.write_text("l1\n", "utf-8")
        _, offset, reset = tail_since(log, None)
        assert reset is True and offset == 3
        with log.open("a", encoding="utf-8") as handle:
            handle.write("l2\nl3\n")
        lines, new_offset, reset = tail_since(log, offset)
        assert reset is False
        assert lines == ["l2", "l3"]
        assert new_offset == log.stat().st_size

    def test_no_new_content_returns_empty(self, tmp_path: Path) -> None:
        from frpsctl.core.logs import tail_since

        log = tmp_path / "a.log"
        log.write_text("l1\n", "utf-8")
        size = log.stat().st_size
        lines, offset, reset = tail_since(log, size)
        assert (lines, offset, reset) == ([], size, False)

    def test_rotation_resets(self, tmp_path: Path) -> None:
        from frpsctl.core.logs import tail_since

        log = tmp_path / "a.log"
        log.write_text("very-long-old-line\n", "utf-8")
        old_offset = log.stat().st_size
        log.write_text("new\n", "utf-8")   # 模拟轮转后的新文件（变小）
        lines, offset, reset = tail_since(log, old_offset)
        assert reset is True
        assert lines == ["new"]
        assert offset == 4

    def test_partial_line_not_returned_then_delivered(self, tmp_path: Path) -> None:
        from frpsctl.core.logs import tail_since

        log = tmp_path / "a.log"
        log.write_text("l1\n", "utf-8")
        offset = log.stat().st_size
        with log.open("a", encoding="utf-8") as handle:
            handle.write("half")   # 无换行
        lines, new_offset, reset = tail_since(log, offset)
        assert lines == []
        assert reset is False
        assert new_offset == offset       # 等完整行到来
        with log.open("a", encoding="utf-8") as handle:
            handle.write("-done\n")
        lines, new_offset, _reset = tail_since(log, new_offset)
        assert lines == ["half-done"]
        assert new_offset == log.stat().st_size

    def test_unicode_and_crlf(self, tmp_path: Path) -> None:
        from frpsctl.core.logs import tail_since

        log = tmp_path / "a.log"
        log.write_bytes("你好\n".encode())
        size = log.stat().st_size
        with log.open("ab") as handle:
            handle.write("世界\r\n".encode())
        lines, new_offset, _ = tail_since(log, size)
        assert lines == ["世界"]
        assert new_offset == log.stat().st_size

    def test_max_lines_truncates_but_advances(self, tmp_path: Path) -> None:
        from frpsctl.core.logs import tail_since

        log = tmp_path / "a.log"
        log.write_text("l1\n", "utf-8")
        offset = log.stat().st_size
        with log.open("a", encoding="utf-8") as handle:
            handle.write("".join(f"x{i}\n" for i in range(10)))
        lines, new_offset, reset = tail_since(log, offset, max_lines=3)
        assert reset is False
        assert lines == ["x7", "x8", "x9"]
        assert new_offset == log.stat().st_size   # 中间被跳过的行不重复投递

    def test_missing_file_resets(self, tmp_path: Path) -> None:
        from frpsctl.core.logs import tail_since

        lines, offset, reset = tail_since(tmp_path / "nope.log", 100)
        assert (lines, offset, reset) == ([], 0, True)


class TestLockTriState:
    """is_locked 三态（R6）：无法探测 ≠ 没有锁（历史假阴性修复）。"""

    def test_unlocked_and_locked(self, tmp_path: Path) -> None:
        lock = tmp_path / ".lock"
        assert is_locked(lock) is False
        with instance_lock(lock):
            assert is_locked(lock) is True
        assert is_locked(lock) is False

    def test_unreadable_lock_file_reports_unknown(self, tmp_path: Path, monkeypatch) -> None:
        lock = tmp_path / ".lock"
        lock.write_text("", "utf-8")
        real_open = os.open

        def fake_open(path, flags, *args, **kwargs):
            if Path(path) == lock and flags == os.O_RDONLY:
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", fake_open)
        assert is_locked(lock) is None


class TestAuditParamSanitize:
    """Web 审计落盘的参数闸门（R5）：标量白名单 / 敏感打码 / 0600。"""

    def test_params_are_sanitized(self, tmp_path: Path) -> None:
        import json

        from frpsctl.core import web_audit
        from frpsctl.core.instance import Instance

        inst = Instance(name="default", instances_root=tmp_path, data_home=tmp_path)
        inst.ensure_dirs()
        assert web_audit.record(
            inst,
            action="config-apply",
            params={
                "password": "super-secret",
                "webServer.password": "super-secret",
                "value": "x" * 500,
                "nested": {"a": 1},
                "count": 3,
                "flag": True,
            },
        )
        record = json.loads(web_audit.resolve_path(inst).read_text("utf-8").strip())
        params = record["params"]
        assert params["password"] == "***"
        assert params["webServer.password"] == "***"
        assert len(params["value"]) == 200
        assert params["nested"] == "<dict>"
        assert params["count"] == 3 and params["flag"] is True

    def test_file_mode_is_0600(self, tmp_path: Path) -> None:
        from frpsctl.core import web_audit
        from frpsctl.core.instance import Instance

        inst = Instance(name="default", instances_root=tmp_path, data_home=tmp_path)
        inst.ensure_dirs()
        assert web_audit.record(inst, action="login")
        mode = web_audit.resolve_path(inst).stat().st_mode & 0o777
        assert mode == 0o600, oct(mode)


class TestServeGuard:
    """互斥守卫下沉 core（R1）：direct ↔ systemd 双向拒绝（Web 启停也走这里）。"""

    def _spec(self):
        from frpsctl.core.serve_runtime import WEB_SPEC

        return WEB_SPEC

    def test_guard_against_direct_rejects_running(self, tmp_path: Path, monkeypatch) -> None:
        from frpsctl.core import serve_guard, serve_runtime
        from frpsctl.core.instance import Instance
        from frpsctl.errors import OwnershipConflict

        inst = Instance(name="default", instances_root=tmp_path, data_home=tmp_path)
        inst.ensure_dirs()
        import types

        status = serve_runtime.ServeStatus(
            spec=self._spec(),
            owner=serve_runtime.ServeOwner.DIRECT,
            state=types.SimpleNamespace(pid=4242),  # type: ignore[arg-type]
        )
        monkeypatch.setattr(serve_runtime, "probe", lambda *_a, **_k: status)
        with pytest.raises(OwnershipConflict):
            serve_guard.guard_against_direct(inst, self._spec())

    def test_guard_against_systemd_rejects_active(self, tmp_path: Path, monkeypatch) -> None:
        from frpsctl.core import serve_guard
        from frpsctl.core.instance import Instance
        from frpsctl.errors import OwnershipConflict

        inst = Instance(name="default", instances_root=tmp_path, data_home=tmp_path)
        inst.ensure_dirs()

        class _Service:
            def unit_exists(self):
                return True

            def is_active(self):
                return True

        monkeypatch.setattr(serve_guard, "_service_for", lambda *_a, **_k: _Service())
        with pytest.raises(OwnershipConflict):
            serve_guard.guard_against_systemd(inst, self._spec())


class TestBuildServeArgv:
    """serve argv 单点（R2）：两种服务各自的选项集与白名单校验。"""

    def test_web_argv(self) -> None:
        from frpsctl.core.serve_runtime import build_serve_argv

        argv = build_serve_argv(
            "/usr/local/bin/frpsctl",
            subcommand="web",
            bind="127.0.0.1:8787",
            password_file="/x/web-password",
            trusted_proxy=True,
        )
        assert argv[:6] == [
            "/usr/local/bin/frpsctl", "web", "serve", "--bind", "127.0.0.1:8787", "--password-file",
        ]
        assert "--trusted-proxy" in argv
        assert "--policy" not in argv

    def test_plugin_argv(self) -> None:
        from frpsctl.core.serve_runtime import build_serve_argv

        argv = build_serve_argv(
            "/usr/local/bin/frpsctl",
            subcommand="plugin",
            bind="127.0.0.1:8080",
            policy="/x/plugin-policy.json",
            handler_path="/handler",
            access_log=True,
        )
        assert argv[:4] == ["/usr/local/bin/frpsctl", "plugin", "serve", "--bind"]
        assert argv[5:7] == ["--policy", "/x/plugin-policy.json"]
        assert "--path" in argv and "/handler" in argv and "--access-log" in argv

    def test_cross_service_options_rejected(self) -> None:
        from frpsctl.core.serve_runtime import build_serve_argv
        from frpsctl.errors import UsageError

        with pytest.raises(UsageError):
            build_serve_argv("/x", subcommand="web", bind="127.0.0.1:1", policy="/p")
        with pytest.raises(UsageError):
            build_serve_argv("/x", subcommand="plugin", bind="127.0.0.1:1", metrics=True)
        with pytest.raises(UsageError):
            build_serve_argv("/x", subcommand="bogus", bind="127.0.0.1:1")


class TestInstallLockAndProgress:
    """安装互斥与进度回调（F11 core）：并发取锁拒绝、阶段序列、进度推进。"""

    def test_install_lock_is_exclusive(self, tmp_path: Path) -> None:
        from frpsctl.core.release import _install_lock
        from frpsctl.errors import FrpsctlError

        with (
            _install_lock(tmp_path, timeout=0.2),
            pytest.raises(FrpsctlError),
            _install_lock(tmp_path, timeout=0.2),
        ):
            pass

    def test_progress_callback_sequence(self, tmp_path: Path, monkeypatch) -> None:
        import hashlib
        import io
        import tarfile

        from frpsctl.core import release as rel

        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode="w:gz") as tar:
            data = b"#!/bin/sh\necho 0.71.0\n"
            info = tarfile.TarInfo("frp_0.71.0/frps")
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
        payload = blob.getvalue()
        digest = hashlib.sha256(payload).hexdigest()

        def fake_download(asset, version, mirrors=None, *, on_progress=None):
            if on_progress is not None:
                on_progress(len(payload), len(payload))
            return payload

        monkeypatch.setattr(rel, "download", fake_download)
        monkeypatch.setattr(
            rel,
            "download_checksums",
            lambda version, mirrors=None: f"{digest}  {rel.asset_name(version)}\n",  # noqa: ARG005
        )
        monkeypatch.setattr(rel, "_verify_binary", lambda path: None)  # noqa: ARG005

        phases: list[str] = []
        result = rel.install(
            bin_dir=tmp_path,
            version="0.71.0",
            mirrors=("https://example.invalid",),
            switch=False,
            on_progress=lambda phase, received=0, total=None: phases.append(phase),  # noqa: ARG005
        )
        assert result.downloaded is True
        assert phases[0] == "checksum"
        assert phases.count("download") >= 1
        assert "verify" in phases and phases[-1] == "place"


class TestPageResultUnknownTotal:
    """翻页在信封缺 total 时的行为（R3）：满页续拉、total_known、如实截断。"""

    def _client(self, pages: list[dict]):
        from frpsctl.core.admin import AdminClient

        client = AdminClient("http://127.0.0.1:1", "u", "p")

        class _Resp:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

        def fake_get(path, *, params=None):
            page = int(params["page"])
            return _Resp(pages[page - 1] if page <= len(pages) else {"items": []})

        client._get = fake_get  # type: ignore[method-assign]
        client._unwrap = lambda resp: resp._payload
        return client

    def test_missing_total_pulls_until_short_page(self) -> None:
        client = self._client([
            {"items": [{"n": i} for i in range(3)]},
            {"items": [{"n": i} for i in range(3, 4)]},
        ])
        result = client._paged("/api/v2/x", page_size=3)
        assert len(result.items) == 4
        assert result.total_known is False
        assert result.total is False or result.total == 4
        assert result.truncated is False

    def test_explicit_total_still_wins(self) -> None:
        client = self._client([{"items": [{"n": 1}], "total": 1}])
        result = client._paged("/api/v2/x", page_size=3)
        assert result.total == 1 and result.total_known is True and result.truncated is False

    def test_max_pages_marks_truncated(self) -> None:
        from frpsctl.core.admin import AdminClient

        client = AdminClient("http://127.0.0.1:1", "u", "p")

        class _Resp:
            status_code = 200
            _payload = {"items": [{"n": 1}, {"n": 2}]}   # 永远满页

        client._get = lambda path, *, params=None: _Resp()   # noqa: ARG005
        client._unwrap = lambda resp: resp._payload
        result = client._paged("/api/v2/x", page_size=2, max_pages=3)
        assert result.truncated is True
        assert result.total_known is False
        assert len(result.items) == 6


class TestDoctorLockUnreadable:
    """doctor 对"锁文件无法探测"的可见降级（R6 的对侧：None → INFO 而非静默）。"""

    def test_unreadable_lock_reports_info(self, inst, monkeypatch) -> None:
        from frpsctl.core import doctor as doc

        inst.lock.write_text("", "utf-8")
        real_open = os.open
        lock_path = inst.lock

        def fake_open(path, flags, *args, **kwargs):
            if Path(path) == lock_path and flags == os.O_RDONLY:
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", fake_open)
        report = doc.run_doctor(inst)
        findings = [item for item in report.findings if item.check == "实例锁"]
        assert findings, "无法探测锁时必须产生发现项（降级可见）"
        assert findings[0].severity is doc.Severity.INFO
        assert "无法探测" in findings[0].message


class TestPortRangesTextPlannable:
    """F8 的序列化文本必须能被服务端 TOML 解析（前后端边界契约）。"""

    def test_serialized_text_is_plannable(self, tmp_path: Path) -> None:
        path = tmp_path / "frps.toml"
        path.write_text("bindPort = 7000\n", "utf-8")
        text = "[ { single = 6000 }, { start = 7000, end = 7100 } ]"
        plan = cfg.plan_change_many(path, [("allowPorts", text)])
        assert not plan.is_noop
        value = plan.after["allowPorts"]
        value = value.unwrap() if hasattr(value, "unwrap") else value
        assert value == [{"single": 6000}, {"start": 7000, "end": 7100}]


class TestConfigPendingRestartHelper:
    """待重启判定单点（RV-2）：不依赖 uptime 字段，pid 与文件系统事实直判。"""

    def test_none_pid_or_missing_config(self, tmp_path: Path) -> None:
        from frpsctl.core.lifecycle import _config_pending_restart

        config = tmp_path / "frps.toml"
        assert _config_pending_restart(config, None) is False
        assert _config_pending_restart(config, os.getpid()) is False  # 文件不存在

    def test_uptime_conversion_none_degrades_safely(self, tmp_path: Path, monkeypatch) -> None:
        """`_uptime_from_ticks` 返回 None 时按"不提示"降级（不得抛 TypeError）。

        最后一轮 review 对修复本身的复审发现：`time.time() - None` 的 TypeError
        不在 suppress(OSError) 内，会击穿 status 的"永不异常"承诺。
        """
        import frpsctl.core.lifecycle as lc_mod
        from frpsctl.core import platform as plat_mod
        from frpsctl.core.lifecycle import _config_pending_restart

        config = tmp_path / "frps.toml"
        config.write_text("x", "utf-8")
        monkeypatch.setattr(plat_mod, "proc_start_time", lambda pid: 12345)  # noqa: ARG005
        monkeypatch.setattr(lc_mod, "_uptime_from_ticks", lambda ticks: None)  # noqa: ARG005
        assert _config_pending_restart(config, os.getpid()) is False

    def test_mtime_compared_with_process_start(self, tmp_path: Path) -> None:
        import time as _time

        from frpsctl.core.lifecycle import _config_pending_restart

        config = tmp_path / "frps.toml"
        config.write_text("bindPort = 7000\n", "utf-8")
        now = _time.time()
        os.utime(config, (now, now + 3600))     # 未来 mtime = 明确比进程新
        assert _config_pending_restart(config, os.getpid()) is True
        os.utime(config, (now, now - 3600))     # 过去的 mtime
        assert _config_pending_restart(config, os.getpid()) is False


class TestTaskRegistryConcurrency:
    """任务注册表（RV-3/RV-4）：单飞行的检查+插入原子、淘汰不碰运行中。"""

    def _inst(self, tmp_path: Path):
        from frpsctl.core.instance import Instance

        inst = Instance(name="t", instances_root=tmp_path, data_home=tmp_path)
        inst.ensure_dirs()
        return inst

    def _fake_install(self, monkeypatch, tmp_path: Path, *, delay: float = 0.0):
        import time as _time

        from frpsctl.core import release as rel

        def fake(**kwargs):
            if delay:
                _time.sleep(delay)
            return rel.InstallResult(
                version=kwargs["version"],
                binary=tmp_path / "frps-x",
                switched=True,
                downloaded=True,
            )

        monkeypatch.setattr(rel, "install", fake)

    def test_concurrent_submits_only_one_wins(self, tmp_path: Path, monkeypatch) -> None:
        """并发提交只有一个成功。

        ⚠️ 语义边界（review 反向验证的诚实记账）：GIL 下"检查→插入"的窄窗口
        无法被这个测试**可靠复现**——临时把检查移出锁，本测试依旧通过。
        它守住的是"第二个提交被拒"这一语义；原子性本身是结构性修复
        （检查+插入同锁），由代码审查保证（§27.5）。
        """
        import threading as _threading

        from frpsctl.errors import UsageError
        from frpsctl.web.tasks import TaskRegistry

        inst = self._inst(tmp_path)
        self._fake_install(monkeypatch, tmp_path, delay=0.3)
        registry = TaskRegistry()
        results: list[str] = []
        barrier = _threading.Barrier(2)

        def submit(version: str) -> None:
            barrier.wait()
            try:
                registry.submit_install(inst, version=version, only_download=False)
                results.append("ok")
            except UsageError:
                results.append("busy")

        threads = [
            _threading.Thread(target=submit, args=(version,))
            for version in ("0.71.0", "0.72.0")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == ["busy", "ok"]

    def test_eviction_skips_running(self) -> None:
        from frpsctl.web.tasks import Task, TaskRegistry

        registry = TaskRegistry(clock=lambda: 1000.0, max_entries=2)
        registry._tasks["running"] = Task(
            id="running", kind="install", version="0.70.0", state="running", started_at=0.0
        )
        registry._tasks["done0"] = Task(
            id="done0", kind="install", version="0.71.0", state="done", started_at=1.0
        )
        registry._tasks["done1"] = Task(
            id="done1", kind="install", version="0.71.0", state="done", started_at=2.0
        )
        registry._evict_locked()
        assert "running" in registry._tasks      # 运行中的任务绝不被淘汰
        assert "done0" not in registry._tasks    # 最旧的已结束任务被淘汰
        assert "done1" in registry._tasks
        assert len(registry._tasks) == 2


class TestAdminDetail404:
    """详情端点的 404 语义（RV-1）：不存在 = 无数据（与 traffic 一致），不是 502。"""

    def test_client_detail_404_is_empty(self) -> None:
        from frpsctl.core.admin import AdminClient

        client = AdminClient("http://127.0.0.1:1", "u", "p")

        class _Resp:
            status_code = 404

        client._get = lambda path, params=None: _Resp()  # type: ignore[method-assign]  # noqa: ARG005
        assert client.client_detail("gone") == {}


class TestAdminUsers:
    """`/api/v2/users` 解析（v0.3.5 F3）：字段是 `clientCount` / `proxyCount`（单数）。

    这是与 `proxyTypeCount` 同类的字段陷阱：写错名字不会报错，只会永远显示 0。
    """

    @staticmethod
    def _client(payload):
        from frpsctl.core.admin import AdminClient

        client = AdminClient("http://127.0.0.1:1", "u", "p")
        seen: dict = {}

        class _Resp:
            status_code = 200

            def json(self):
                return payload

        def _get(path, params=None):
            seen["path"] = path
            seen["params"] = params
            return _Resp()

        client._get = _get  # type: ignore[method-assign]
        return client, seen

    def test_parses_real_payload_shape(self) -> None:
        client, seen = self._client({
            "code": 200,
            "msg": "success",
            "data": {
                "total": 2,
                "page": 1,
                "pageSize": 50,
                "items": [
                    {"user": "", "clientCount": 1, "proxyCount": 1},
                    {"user": "alice", "clientCount": 2, "proxyCount": 3},
                ],
            },
        })
        page = client.users()
        assert [item.user for item in page.items] == ["", "alice"]
        assert page.items[1].client_count == 2
        assert page.items[1].proxy_count == 3
        assert page.total == 2
        assert page.truncated is False  # total == len(items)，没有更多
        assert page.total_known is True
        assert seen["path"] == "/api/v2/users"
        assert seen["params"] == {"page": 1, "page_size": 200}

    def test_wrong_field_names_yield_zero(self) -> None:
        """字段名写成复数（clientCounts / proxyCounts）→ 静默 0（陷阱的守卫）。"""
        client, _seen = self._client({
            "code": 200,
            "data": {"items": [{"user": "a", "clientCounts": 5, "proxyCounts": 7}]},
        })
        page = client.users()
        assert page.items[0].client_count == 0
        assert page.items[0].proxy_count == 0

    def test_malformed_items_skipped_and_empty_envelope(self) -> None:
        client, _seen = self._client({"code": 200, "data": {"items": ["nope", 42]}})
        assert client.users().items == []
        client2, _seen2 = self._client({"code": 200, "data": {}})
        page = client2.users()
        assert page.items == [] and page.total == 0 and page.total_known is False

    def test_bad_field_types_degrade_to_zero(self) -> None:
        """上游字段类型漂移时给 0，而不是让 int("N/A") 冒成 400/500（v0.3.5 review）。"""
        client, _seen = self._client({
            "code": 200,
            "data": {"total": 3, "items": [
                {"user": "a", "clientCount": "N/A", "proxyCount": {"x": 1}},
                {"user": "b", "clientCount": None, "proxyCount": [1]},
            ]},
        })
        page = client.users()
        assert [item.client_count for item in page.items] == [0, 0]
        assert [item.proxy_count for item in page.items] == [0, 0]
        assert page.total == 3
        assert page.truncated is True  # total(3) > len(items)(2)

    def test_non_dict_payload_returns_empty_page(self) -> None:
        """`data` 不是对象（上游形状漂移）→ 空结果，而不是 AttributeError → 500。"""
        client, _seen = self._client({"code": 200, "data": [1, 2]})
        page = client.users()
        assert page.items == [] and page.total == 0

    def test_total_equal_to_limit_is_not_truncated(self) -> None:
        """恰好等于单页上限时不得误报截断（v0.3.5 review：旧实现用 len>=limit）。"""
        items = [{"user": f"u{i}", "clientCount": 1, "proxyCount": 1} for i in range(200)]
        client, _seen = self._client({"code": 200, "data": {"total": 200, "items": items}})
        page = client.users(page_size=200)
        assert len(page.items) == 200
        assert page.truncated is False


class TestSessionManagement:
    """会话管理（v0.3.5 F2）：脱敏快照 + 登出其他所有会话。"""

    def test_snapshot_is_sanitized_and_ordered(self) -> None:
        from frpsctl.core.web_audit import session_fingerprint
        from frpsctl.web import AuthManager

        now = {"t": 1000.0}
        wall = {"t": 1_700_000_000.0}
        # TTL 用单调时钟、created_at 用墙钟（v0.3.5 review：单调值不可作为
        # Unix 时间戳展示，字段语义必须是墙钟）——两个时钟都可注入，便于断言。
        auth = AuthManager("pw", clock=lambda: now["t"], wall_clock=lambda: wall["t"])
        first = auth.login("pw", source="10.0.0.1")
        now["t"] = 1010.0
        wall["t"] = 1_700_000_100.0
        second = auth.login("pw", source="10.0.0.2")
        assert first is not None and second is not None

        items = auth.snapshot()
        assert [item.source for item in items] == ["10.0.0.1", "10.0.0.2"]
        assert items[0].created_at == 1_700_000_000.0  # 墙钟时间戳
        assert items[1].created_at == 1_700_000_100.0
        assert items[0].expires_in > 0
        # 指纹存在但不是 token（token 绝不出现在快照里），且与审计口径一致
        assert items[0].fingerprint == session_fingerprint(first.token)
        assert items[0].fingerprint != first.token
        assert first.token not in {item.fingerprint for item in items}

    def test_revoke_all_keeps_current(self) -> None:
        from frpsctl.core.web_audit import session_fingerprint
        from frpsctl.web import AuthManager

        auth = AuthManager("pw")
        first = auth.login("pw", source="a")
        second = auth.login("pw", source="b")
        third = auth.login("pw", source="c")
        assert first and second and third

        removed = auth.revoke_all(keep_fingerprint=session_fingerprint(second.token))
        assert removed == 2
        assert auth.check_session(second.token) is not None
        assert auth.check_session(first.token) is None
        assert auth.check_session(third.token) is None

    def test_revoke_all_without_keep_removes_everything(self) -> None:
        from frpsctl.web import AuthManager

        auth = AuthManager("pw")
        session = auth.login("pw", source="a")
        assert session is not None
        assert auth.revoke_all() == 1
        assert auth.check_session(session.token) is None

    def test_snapshot_prunes_expired(self) -> None:
        from frpsctl.web import AuthManager

        now = {"t": 1000.0}
        auth = AuthManager("pw", session_ttl=10.0, clock=lambda: now["t"])
        auth.login("pw", source="a")
        now["t"] = 1020.0
        assert auth.snapshot() == []


class TestAuditQuery:
    """审计的过滤 + 分页查询（v0.3.5 R4）。

    与 `read_tail`（"最近 N 条"）的差别是"**符合条件**的第 offset..offset+limit
    条"——Web 审计视图的过滤与"加载更多"都建立在它上面。
    """

    @staticmethod
    def _write(path, records) -> None:
        import json as _json

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for index, record in enumerate(records):
                record.setdefault("at_unix", 1000.0 + index)
                record.setdefault("at", "2026-01-01T00:00:00")
                handle.write(_json.dumps(record, ensure_ascii=False) + "\n")

    def test_filter_by_action_and_source(self, tmp_path) -> None:
        from frpsctl.core import auditlog

        path = tmp_path / "a.jsonl"
        self._write(path, [
            {"action": "login", "result": "ok", "source": "10.0.0.1"},
            {"action": "config-apply", "result": "ok", "source": "10.0.0.2"},
            {"action": "login", "result": "error:Auth", "source": "10.0.0.1"},
        ])
        result = auditlog.query(path, filters={"action": "login"})
        assert result.matched == 2
        assert [item["action"] for item in result.records] == ["login", "login"]

    def test_filter_is_case_insensitive_substring(self, tmp_path) -> None:
        from frpsctl.core import auditlog

        path = tmp_path / "a.jsonl"
        self._write(path, [{"action": "Config-Apply", "result": "ok", "source": "x"}])
        assert auditlog.query(path, filters={"action": "config"}).matched == 1
        assert auditlog.query(path, filters={"result": "OK"}).matched == 1
        assert auditlog.query(path, filters={"action": "nope"}).matched == 0

    def test_paging_newest_first_with_has_more(self, tmp_path) -> None:
        from frpsctl.core import auditlog

        path = tmp_path / "a.jsonl"
        self._write(path, [{"action": f"a{i}", "result": "ok"} for i in range(5)])
        first = auditlog.query(path, limit=2, offset=0)
        assert [item["action"] for item in first.records] == ["a3", "a4"]
        assert first.has_more is True
        second = auditlog.query(path, limit=2, offset=2)
        assert [item["action"] for item in second.records] == ["a1", "a2"]
        third = auditlog.query(path, limit=2, offset=4)
        assert [item["action"] for item in third.records] == ["a0"]
        assert third.has_more is False

    def test_since_and_until_window(self, tmp_path) -> None:
        from frpsctl.core import auditlog

        path = tmp_path / "a.jsonl"
        self._write(path, [{"at_unix": 100.0, "action": "old"}, {"at_unix": 200.0, "action": "new"}])
        assert [item["action"] for item in auditlog.query(path, since=150.0).records] == ["new"]
        assert [item["action"] for item in auditlog.query(path, until=150.0).records] == ["old"]

    def test_summarize_respects_since_and_until(self, tmp_path) -> None:
        """统计与记录列表同口径：`until` 也约束统计（v0.3.5 补）。"""
        from frpsctl.core import auditlog

        path = tmp_path / "a.jsonl"
        self._write(path, [
            {"at_unix": 100.0, "decision": "allow"},
            {"at_unix": 200.0, "decision": "deny"},
        ])
        assert auditlog.summarize(path, since=150.0).total == 1
        assert auditlog.summarize(path, until=150.0).total == 1
        assert auditlog.summarize(path, since=150.0, until=150.0).total == 0
        assert auditlog.summarize(path).total == 2

    def test_truncated_window_semantics(self, tmp_path, monkeypatch) -> None:
        """`truncated`（扫描窗口已满）与 `has_more`（本页之后还有）是两件事。

        复审修正后的语义：
        - `has_more` 只看本页之后是否还有**窗口内**的记录——它决定"加载更多"
          是否显示，绝不能因 `truncated` 而恒真（否则按钮永不消失、每次点击都
          空转并触发一次全量扫描）；
        - `truncated` 表示"可能还有更旧的记录在窗口之外"，由前端用文字提示。
        """
        from frpsctl.core import auditlog

        monkeypatch.setattr(auditlog, "MAX_QUERY_MATCHES", 5)
        path = tmp_path / "a.jsonl"
        self._write(path, [{"action": f"a{index}"} for index in range(20)])

        first = auditlog.query(path, limit=5, offset=0)
        assert first.truncated is True       # 20 条匹配 > 窗口 5
        assert first.has_more is False       # 窗口内已无更旧的可翻
        assert len(first.records) == 5

        beyond = auditlog.query(path, limit=5, offset=5)
        assert beyond.truncated is True
        assert beyond.has_more is False
        assert beyond.records == []          # 超出窗口：空页（前端不应再翻）

    def test_has_more_only_within_window(self, tmp_path, monkeypatch) -> None:
        """未触发截断时，`has_more` 如实反映本页之后是否还有记录。"""
        from frpsctl.core import auditlog

        monkeypatch.setattr(auditlog, "MAX_QUERY_MATCHES", 100)
        path = tmp_path / "a.jsonl"
        self._write(path, [{"action": f"a{index}"} for index in range(5)])
        assert auditlog.query(path, limit=2, offset=0).has_more is True
        assert auditlog.query(path, limit=2, offset=4).has_more is False
        assert auditlog.query(path, limit=2, offset=0).truncated is False

    def test_non_finite_window_is_rejected(self) -> None:
        """`since`/`until` 拒绝 nan/inf：它们会静默关闭时间窗（与"非法值 400"矛盾）。"""
        import pytest

        from frpsctl.core.auditlog import parse_since
        from frpsctl.errors import UsageError

        for bad in ("nan", "inf", "-inf", "1e400"):
            with pytest.raises(UsageError):
                parse_since(bad)

    def test_csv_cell_prefix_rules(self) -> None:
        """CSV 防线的边界：控制字符加前缀，纯数值（含负数）不加。"""
        import csv as _csv
        import io as _io

        from frpsctl.core import auditlog

        text = auditlog.to_csv([{
            "action": "\n=1+1",       # 换行后接公式：加前缀（黑名单会漏）
            "target": "\x00=cmd",     # 控制字符：加前缀
            "user": " =1+1",          # **前导空格后接公式**：必须加前缀（v0.3.5 复审：
                                      # lstrip 曾把 `=+-@` 一起 strip 掉，使这条防线失效）
            "proxy_name": "  =cmd|'/c calc'!A1",
            "source": "-5",           # 纯数值：不加（否则下游把它当文本）
            "result": "1e-3",         # 科学计数：不加
            "op": None,               # None → 空
        }])
        rows = list(_csv.reader(_io.StringIO(text)))
        header, row = rows[0], rows[1]
        assert row[header.index("action")] == "'\n=1+1"
        assert row[header.index("target")].startswith("'")
        assert row[header.index("user")] == "' =1+1"
        assert row[header.index("proxy_name")].startswith("'  =cmd")
        assert row[header.index("source")] == "-5"
        assert row[header.index("result")] == "1e-3"
        assert row[header.index("op")] == ""

    def test_bad_lines_counted_not_fatal(self, tmp_path) -> None:
        from frpsctl.core import auditlog

        path = tmp_path / "a.jsonl"
        path.write_text('{"action":"a"}\nnot-json\n{"action":"b"}\n', "utf-8")
        result = auditlog.query(path)
        assert result.matched == 2
        assert result.bad_lines == 1

    def test_cross_rotation_merged_in_time_order(self, tmp_path) -> None:
        from frpsctl.core import auditlog

        path = tmp_path / "a.jsonl"
        self._write(Path(f"{path}.1"), [{"action": "older"}])
        self._write(path, [{"action": "newer"}])
        assert [item["action"] for item in auditlog.query(path).records] == ["older", "newer"]

    def test_missing_file_is_empty_not_error(self, tmp_path) -> None:
        from frpsctl.core import auditlog

        result = auditlog.query(tmp_path / "nope.jsonl")
        assert result.records == [] and result.matched == 0

    def test_csv_guards_formula_injection(self) -> None:
        """CSV 公式注入防护 + 容器类型 JSON 化（按解析后的单元格值断言）。"""
        import csv as _csv
        import io as _io

        from frpsctl.core import auditlog

        text = auditlog.to_csv([
            {"action": "=cmd|'/c calc'!A1", "source": "+1", "params": {"k": "v"}}
        ])
        rows = list(_csv.reader(_io.StringIO(text)))
        header = rows[0]
        assert header[0] == "at" and "params" in header
        row = rows[1]
        assert row[header.index("action")] == "'=cmd|'/c calc'!A1"
        assert row[header.index("source")] == "'+1"
        assert row[header.index("params")] == '{"k": "v"}'

    def test_jsonl_roundtrip(self) -> None:
        import json as _json

        from frpsctl.core import auditlog

        text = auditlog.to_jsonl([{"action": "a"}, {"action": "b"}])
        assert [_json.loads(line)["action"] for line in text.splitlines()] == ["a", "b"]
