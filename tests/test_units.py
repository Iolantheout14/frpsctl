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


class TestSecrets:
    def test_secret_detection(self) -> None:
        assert cfg.is_secret_key("webServer.password") is True
        assert cfg.is_secret_key("auth.token") is True
        assert cfg.is_secret_key("bindPort") is False
        assert cfg.is_secret_key("auth.oidc.clientSecret") is True


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

        def fake_download(asset, version, mirrors=None):
            calls.append(asset)
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
        assert "ReadWritePaths=/var/log/frps" in text
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


class TestSystemdDelegation:
    """委托路径此前零覆盖，而它正是 §15.5.4 里"文档承诺了、代码没做到"的那一处。"""

    @pytest.fixture
    def systemd(self, inst, tmp_path):
        from frpsctl.core.systemd import Systemd

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

    def test_start_stop_restart_delegate_to_systemctl(self, systemd, recorded, as_root) -> None:
        systemd.start()
        systemd.stop()
        systemd.restart()
        assert recorded == [
            ["systemctl", "start", "frps@test.service"],
            ["systemctl", "stop", "frps@test.service"],
            ["systemctl", "restart", "frps@test.service"],
        ]

    def test_install_template_writes_unit_and_enables(self, systemd, recorded, tmp_path, as_root) -> None:
        path = systemd.install_template(
            binary=Path("/opt/frps/frps-0.71.0"),
            log_dir=Path("/var/log/frps"),
        )
        assert path == systemd.template_path
        assert path.exists()
        assert "ExecStart=/opt/frps/frps-0.71.0" in path.read_text("utf-8")
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

    def test_install_template_force_overwrites(self, systemd, recorded, as_root) -> None:
        systemd.template_path.parent.mkdir(parents=True, exist_ok=True)
        systemd.template_path.write_text("旧的", "utf-8")
        systemd.install_template(
            binary=Path("/opt/frps/frps-0.71.0"),
            log_dir=Path("/var/log/frps"),
            force=True,
        )
        assert "ExecStart=" in systemd.template_path.read_text("utf-8")

    def test_uninstall_disables_and_removes_template(self, systemd, recorded, as_root) -> None:
        systemd.template_path.parent.mkdir(parents=True, exist_ok=True)
        systemd.template_path.write_text("unit", "utf-8")
        systemd.uninstall()
        assert not systemd.template_path.exists()
        assert ["systemctl", "disable", "--now", "frps@test.service"] in recorded
        assert ["systemctl", "daemon-reload"] in recorded

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

    def test_same_config_active_matches_execstart_path(self, systemd, monkeypatch) -> None:
        """`same_config_active` 比的是 ExecStart 里的**配置路径**，不是 unit 名。

        用户完全可能自建一个名字不同的 unit 指向我们的配置——那样也必须被认出来，
        否则会出现 direct 与 systemd 双起（ADR-1 要防的事）。
        """
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if "list-units" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, stdout="frps-custom.service loaded active running\n", stderr=""
                )
            if "is-active" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="active\n", stderr="")
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
        def fake_run(argv, **kwargs):
            if "list-units" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, stdout="frps-custom.service loaded inactive dead\n", stderr=""
                )
            if "is-active" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="inactive\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("frpsctl.core.systemd.shutil.which", lambda _name: "/usr/bin/systemctl")
        assert systemd.same_config_active() is False
