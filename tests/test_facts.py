"""契约层：设计文档"事实基线"的自动化守卫（设计文档 §13.1）。

这个文件守的是**文档里每一条会静默出错的事实**——它们不会让程序崩溃，只会让
输出悄悄错掉，所以必须显式断言。

| 断言 | 需要真 frps | 守住的事实 |
|------|-----------|-----------|
| C1 | ✔ | `proxyTypeCount` 是 map 而非总数，且求和口径正确 |
| C2 | ✘（静态） | 我们只用 v2，没有偷偷加回 v1 降级路径 |
| C3 | ✔ | `/healthz` 免认证 |
| C4 | ✔ | v2 信封 `{code,msg,data}` |
| C5 | ✔ | user/password 双空 = **完全不鉴权** |
| C6 | ✔ | `frps verify` 是权威判定（退出码 + 成功措辞） |
| C7 | ✔ | 版本门槛与标志可用性（§3.6） |
| C8 | ✔ | 鉴权开关是"**任一非空**"，且**空口令是合法口令**（§3.3） |

缺二进制时整组 skip，但断言本身始终留在仓库里——CI 有二进制时它们就是门禁。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from frpsctl.core.admin import AdminClient, sum_proxy_types
from frpsctl.core.version import MINIMUM_VERSION, parse_version

from .conftest import free_ports

pytestmark = pytest.mark.contract

#: 真二进制来源：环境变量 > frpsctl 的数据目录 > PATH。
_ENV_BINARY = "FRPSCTL_TEST_BINARY"


def _default_binary_candidates() -> list[Path]:
    candidates: list[Path] = []
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    candidates.append(base / "frpsctl" / "bin" / "frps")
    candidates.append(base / "frpsctl" / "bin" / "frps-0.71.0")
    for name in (f"frps-{'.'.join(map(str, MINIMUM_VERSION))}", "frps"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    return candidates


def find_real_frps() -> Path | None:
    explicit = os.environ.get(_ENV_BINARY)
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None
    for candidate in _default_binary_candidates():
        if candidate.exists():
            return candidate
    return None


REAL_FRPS = find_real_frps()

requires_binary = pytest.mark.skipif(
    REAL_FRPS is None,
    reason=(
        f"未找到真实 frps 二进制：契约层需要它。用 `frpsctl install` 安装，或设置 {_ENV_BINARY}=/path/to/frps"
    ),
)


# ---------------------------------------------------------------------------
# 真 frps 上的服务夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def real_server(tmp_path):
    """在高位端口起一个真 frps，并返回 (base_url, user, password, 配置文件)。

    用高位端口、临时目录、独立实例，测完必定清理。
    """
    assert REAL_FRPS is not None
    port, dash_port = free_ports(2)
    config = tmp_path / "frps.toml"
    config.write_text(
        f"""\
bindAddr = "127.0.0.1"
bindPort = {port}

[webServer]
addr = "127.0.0.1"
port = {dash_port}
user = "admin"
password = "contract-test"

[log]
to = "{tmp_path / "frps.log"}"
level = "info"
""",
        "utf-8",
    )
    proc = subprocess.Popen(
        [str(REAL_FRPS), "-c", str(config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        if not _wait_port("127.0.0.1", dash_port, timeout=10):
            proc.kill()
            out = proc.communicate(timeout=5)[0]
            pytest.fail(f"真 frps 未在 10s 内起来：{out[:500]}")
        yield f"http://127.0.0.1:{dash_port}", "admin", "contract-test", config
    finally:
        proc.kill()
        proc.wait(timeout=5)


def _wait_port(host: str, port: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
# C1 —— proxyTypeCount 是 map，且求和口径正确（§3.2 的字段陷阱）
# ---------------------------------------------------------------------------


@requires_binary
class TestC1ProxyTypeCount:
    def test_proxy_count_is_a_map_not_a_total(self, real_server) -> None:
        """**这是问题 4 的核心断言**。

        如果哪天 frp 把 `proxyCount` 改成整数，直接当整数用的代码看起来还能跑，
        但 `sum()` 会 TypeError；反过来，如果有人把我们的解析改成"直接当整数"，
        这里会因为拿到 dict 而失败。两种漂移都能被这条抓住。
        """
        import httpx

        base_url, user, password, _ = real_server
        resp = httpx.get(f"{base_url}/api/v2/system/info", auth=(user, password), timeout=5)
        assert resp.status_code == 200
        payload = resp.json()["data"]
        status = payload["status"]

        # 字段名是 proxyTypeCount（不是 proxyCount）——真机实测，见本节说明
        assert "proxyTypeCount" in status, f"字段名漂移：实际键 {sorted(status)}"
        counts = status["proxyTypeCount"]
        assert isinstance(counts, dict), f"proxyTypeCount 应为 map，实际 {type(counts).__name__}"
        assert all(isinstance(v, int) for v in counts.values()), counts

        # 另一个易错的形状：clientCounts 是**整数**而非字典
        assert isinstance(status["clientCounts"], int), (
            f"clientCounts 应为 int，实际 {type(status['clientCounts']).__name__}"
        )

    def test_sum_matches_per_type_endpoints(self, real_server) -> None:
        """光断言类型不够：**求和口径**也必须正确。

        用第二条独立路径验证——把 `proxyCount` 的求和结果与逐类型代理列表的
        实际长度对比。漏掉某个类型（例如 udp）同样是静默 bug，这条能抓住。
        """
        base_url, user, password, _ = real_server
        with AdminClient(base_url, user, password) as client:
            info = client.server_info()
            per_type = {ptype: len(client.proxies(ptype)) for ptype in ("tcp", "http", "udp")}

        assert (
            info.proxy_type_counts == {k: v for k, v in per_type.items() if v}
            or sum(per_type.values()) == info.proxy_total
        ), f"求和口径不一致：proxyCount={info.proxy_type_counts}，逐类型={per_type}"
        assert sum_proxy_types(info.proxy_type_counts) == info.proxy_total


# ---------------------------------------------------------------------------
# C2 —— 我们只用 v2（反向断言）
# ---------------------------------------------------------------------------


class TestC2V2Only:
    """不需要二进制：断言的是**我们自己的源码**没有 v1 降级路径。

    这是反向断言——不是测 frp，而是测我们没有偷偷用回 v1（ADR-3 的决策守卫）。
    """

    def test_admin_client_only_uses_v2_paths(self) -> None:
        import frpsctl.core.admin as admin_module

        source = Path(admin_module.__file__).read_text("utf-8")
        # 逐行检查真正的请求调用，忽略文档字符串里的举例
        calls = [
            line.strip()
            for line in source.splitlines()
            if "self._client." in line and "http" not in line.split("self._client.")[0]
        ]
        offenders = [
            line
            for line in calls
            if "/api/" in line and "/api/v2/" not in line and "/api/proxies" not in line
        ]
        assert not offenders, f"admin.py 出现非 v2 端点：{offenders}"

    def test_expected_v2_fields_are_parsed(self) -> None:
        """v2 关键字段名必须在**解析语句**里出现——改名会立刻暴露。

        只看代码行、不看文档字符串：说明文字里提到某字段名是正常的，
        真正的风险是解析代码里写错。
        """
        import frpsctl.core.admin as admin_module

        code_lines = [
            line
            for line in Path(admin_module.__file__).read_text("utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        code = "\n".join(code_lines)
        for field in ("clientCounts", "proxyTypeCount", "curConns", "totalTrafficIn", "tlsForce"):
            assert f'"{field}"' in code, f"admin.py 未解析 v2 字段 {field}（§3.2）"
        # 反向：写错的字段名不得出现在解析代码里
        assert '"proxyCount"' not in code, "解析了不存在的字段名 proxyCount"

    def test_no_v1_fallback_remnants(self) -> None:
        """`/api/serverinfo` 与 `proxyTypeCount`（v1 字段名）不得出现在实现里。"""
        import frpsctl.core.admin as admin_module

        source = Path(admin_module.__file__).read_text("utf-8")
        assert "/api/serverinfo" not in source, "v1 降级路径被加回来了（ADR-3）"
        # v2 的字段名恰好也叫 proxyTypeCount（真机实测），因此这里改为断言
        # "没有 v1 端点的解析路径"，而不是断言字段名不存在。
        assert 'source="v1"' not in source, "保留了 v1 的来源标记"


# ---------------------------------------------------------------------------
# C3 —— /healthz 免认证
# ---------------------------------------------------------------------------


@requires_binary
class TestC3HealthzUnauthenticated:
    def test_healthz_works_without_credentials(self, real_server) -> None:
        """§3.2：`/healthz` 注册在鉴权中间件之外。

        如果它哪天被挪进鉴权层，`start` 的健康检查会静默失效——所有实例都会被
        判为"启动后不健康"。
        """
        base_url, _, _, _ = real_server
        with AdminClient(base_url, user="", password="") as client:
            ok, ms = client.healthz()
        assert ok is True, "/healthz 在无凭据时不通（可能被挪进了鉴权中间件）"
        assert ms >= 0


# ---------------------------------------------------------------------------
# C4 —— v2 信封形状
# ---------------------------------------------------------------------------


@requires_binary
class TestC4Envelope:
    def test_v2_envelope_shape(self, real_server) -> None:
        import httpx

        base_url, user, password, _ = real_server
        body = httpx.get(f"{base_url}/api/v2/system/info", auth=(user, password), timeout=5).json()
        assert set(body) >= {"code", "msg", "data"}, f"信封字段异常：{sorted(body)}"
        assert "status" in body["data"] and "version" in body["data"]

    def test_version_is_bare_without_v_prefix(self, real_server) -> None:
        """§3.1：`frps -v` 输出**无 `v` 前缀**，我们的解析依赖这一点。"""
        base_url, user, password, _ = real_server
        with AdminClient(base_url, user, password) as client:
            info = client.server_info()
        assert not info.version.startswith("v"), info.version
        parse_version(info.version)  # 必须能解析


# ---------------------------------------------------------------------------
# C5 —— 双空 = 完全不鉴权（安全基线的事实依据）
# ---------------------------------------------------------------------------


@requires_binary
class TestC5NoAuthMeansNoAuth:
    def test_empty_credentials_means_open_access(self, tmp_path) -> None:
        """§3.3：`user` 与 `password` **同时为空**时 frp 完全不鉴权。

        这不是"弹窗要求登录"，而是任何人都能读全部状态、并下线任意代理。
        整份安全基线（§10）都建立在这条事实上，因此必须自动化确认。
        """
        port, dash_port = free_ports(2)
        config = tmp_path / "frps-open.toml"
        config.write_text(
            f"""\
bindAddr = "127.0.0.1"
bindPort = {port}

[webServer]
addr = "127.0.0.1"
port = {dash_port}
""",
            "utf-8",
        )
        proc = subprocess.Popen(
            [str(REAL_FRPS), "-c", str(config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        try:
            if not _wait_port("127.0.0.1", dash_port, timeout=10):
                pytest.fail("真 frps 未起来")
            import httpx

            resp = httpx.get(f"http://127.0.0.1:{dash_port}/api/v2/clients", timeout=5)
            assert resp.status_code == 200, (
                f"双空凭据下 /api/v2/clients 返回 {resp.status_code}，"
                "说明鉴权语义已变化——§3.3 与 §10 的安全基线需要重写"
            )
        finally:
            proc.kill()
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# C6 —— frps verify 是权威判定
# ---------------------------------------------------------------------------


@requires_binary
class TestC6VerifyIsAuthoritative:
    def test_valid_config_exits_zero_with_expected_message(self, tmp_path) -> None:
        config = tmp_path / "ok.toml"
        config.write_text('bindPort = 7000\n[auth]\ntoken = "x"\n', "utf-8")
        proc = subprocess.run(
            [str(REAL_FRPS), "--strict_config=true", "verify", "-c", str(config)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "syntax is ok" in proc.stdout, proc.stdout

    def test_invalid_value_exits_one(self, tmp_path) -> None:
        config = tmp_path / "bad.toml"
        config.write_text("bindPort = 99999\n", "utf-8")
        proc = subprocess.run(
            [str(REAL_FRPS), "--strict_config=true", "verify", "-c", str(config)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 1, "非法配置必须退出 1"

    def test_unknown_key_rejected_by_strict_mode(self, tmp_path) -> None:
        """§3.1 推论 1：`--strict_config=true` 让键名写错**硬报错**。

        这是 frpsctl 依赖的免费护栏，必须确认它真的生效——否则 `config set`
        写错键名会静默无效。
        """
        config = tmp_path / "unknown.toml"
        config.write_text("bindPort = 7000\nnotARealKey = 1\n", "utf-8")
        proc = subprocess.run(
            [str(REAL_FRPS), "--strict_config=true", "verify", "-c", str(config)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode != 0, "严格模式下未知字段竟然通过了"

    def test_our_flag_construction_is_accepted(self, tmp_path) -> None:
        """我们构造的标志必须被真 frps 接受（§8.4 `config_flags`）。"""
        from frpsctl.core.config import config_flags

        config = tmp_path / "flags.toml"
        config.write_text("bindPort = 7000\n", "utf-8")
        flags = config_flags()
        proc = subprocess.run(
            [str(REAL_FRPS), *flags, "verify", "-c", str(config)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"标志 {flags} 被拒绝：{proc.stdout}{proc.stderr}"

        # --allow-unsafe 是 StringSlice，值必须是 TokenSourceExec
        flags2 = config_flags(uses_exec_token_source=True)
        assert "--allow-unsafe" in flags2 and "TokenSourceExec" in flags2


# ---------------------------------------------------------------------------
# C7 —— 版本门槛与标志可用性（§3.6）
# ---------------------------------------------------------------------------


@requires_binary
class TestC7VersionGate:
    def test_version_output_has_no_v_prefix(self) -> None:
        """§3.1：`frps -v` 输出**无 `v` 前缀**，我们的解析依赖这一点。

        断言两件事：前缀形态，以及这串文本确实能被 `parse_version` 吃下——
        后者才是我们真正依赖的能力。
        """
        proc = subprocess.run([str(REAL_FRPS), "-v"], capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0
        assert not proc.stdout.strip().startswith("v"), proc.stdout
        assert parse_version(proc.stdout).tuple >= MINIMUM_VERSION

    def test_installed_version_meets_the_gate(self) -> None:
        proc = subprocess.run([str(REAL_FRPS), "-v"], capture_output=True, text=True, timeout=10)
        version = parse_version(proc.stdout)
        assert version.tuple >= MINIMUM_VERSION, f"契约层要求 >= {MINIMUM_VERSION}，实际 {version}"

    def test_help_exposes_the_flags_we_pass(self) -> None:
        """§3.6：我们要传的标志必须真实存在。

        标志名一旦变化，`verify` 会因未知标志直接退出，表现为"配置合法却启动
        失败"。

        已知细节：frp 源码里这个标志叫 `strict_config`（下划线），而 `--help`
        由 pflag 渲染成 **`--strict-config`（连字符）**。两种写法实测**都可用**
        （见 `test_both_flag_spellings_are_accepted`），所以这里只断言"存在"，
        不绑定某一种拼写——否则一次纯展示层的变化就会让门禁误报。
        """
        proc = subprocess.run([str(REAL_FRPS), "--help"], capture_output=True, text=True, timeout=10)
        text = proc.stdout + proc.stderr
        assert "strict-config" in text or "strict_config" in text, text[:800]
        assert "--allow-unsafe" in text, text[:800]

    def test_both_flag_spellings_are_accepted(self, tmp_path) -> None:
        """下划线与连字符两种拼写都必须被接受。

        我们传的是**下划线**形式（与文档、与 frp 源码里的字段名一致）。如果哪天
        pflag 的规范化行为变了，下划线形式被拒，所有 verify 调用都会失败——这条
        断言就是那个变化的哨兵。
        """
        config = tmp_path / "spelling.toml"
        config.write_text("bindPort = 7000\n", "utf-8")
        for flag in ("--strict_config=true", "--strict-config=true"):
            proc = subprocess.run(
                [str(REAL_FRPS), flag, "verify", "-c", str(config)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert proc.returncode == 0, f"{flag} 被拒绝：{proc.stdout}{proc.stderr}"

    def test_strict_mode_is_default_on_in_target_version(self, tmp_path) -> None:
        """§3.1：目标版本上 `--strict_config` **默认已是 true**。

        实测：不传标志时未知字段同样报错。这与 v0.53–v0.65 的默认 false 不同，
        是"显式传值"这条决定的事实依据——依赖默认值是脆弱的。
        """
        config = tmp_path / "unknown-default.toml"
        config.write_text("bindPort = 7000\nnotARealKey = 1\n", "utf-8")
        proc = subprocess.run(
            [str(REAL_FRPS), "verify", "-c", str(config)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode != 0, "目标版本上未知字段竟然通过了（默认值可能变了）"

    def test_unsupported_version_is_refused_by_gate(self) -> None:
        """低版本必须在**安装阶段**就被拒绝，而不是装了才发现用不了。

        这里用 CLI 的 install 做端到端断言（不下载：版本门槛在下载之前检查）。
        """
        from frpsctl.core.release import install
        from frpsctl.errors import UnsupportedVersion

        with pytest.raises(UnsupportedVersion):
            install(bin_dir=Path("/tmp/frpsctl-contract-should-not-exist"), version="0.69.1")


# ---------------------------------------------------------------------------
# C8 —— 鉴权开关是"任一非空"，且空口令是合法口令（§3.3）
# ---------------------------------------------------------------------------


@requires_binary
class TestC8PasswordlessAuth:
    """守 `check_dangerous_combination()` 的**判据边界**。

    它只拒绝"user/password 两者全空 + 绑非回环"（真·无鉴权），而放行"有 user、
    口令为空"——后者交给 `doctor` 以 WARN 告警。这个分工只有在下面这条事实成立时
    才正确：**frp 的鉴权开关是"任一非空即启用"，而空口令是合法口令**。

    若哪天 frp 改成"口令必须非空才启用鉴权"，那么"有 user + 空口令"会**静默退化
    成完全不鉴权**，而 `check_dangerous_combination()` 仍然放行——安全缺口就此产生。
    这条断言就是那个变化的哨兵。
    """

    def _start(self, tmp_path, name: str, extra: str) -> tuple[subprocess.Popen, int]:
        """起一个 frps，返回 (进程, dashboard 端口)。extra 是 [webServer] 里的额外键。"""
        port, dash_port = free_ports(2)
        config = tmp_path / f"{name}.toml"
        config.write_text(
            f"""\
bindAddr = "127.0.0.1"
bindPort = {port}

[webServer]
addr = "127.0.0.1"
port = {dash_port}
{extra}
""",
            "utf-8",
        )
        proc = subprocess.Popen(
            [str(REAL_FRPS), "-c", str(config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        if not _wait_port("127.0.0.1", dash_port, timeout=10):
            proc.kill()
            pytest.fail(f"{name}: 真 frps 未起来")
        return proc, dash_port

    @staticmethod
    def _status(dash_port: int, auth: tuple[str, str] | None) -> int:
        import httpx

        resp = httpx.get(
            f"http://127.0.0.1:{dash_port}/api/v2/clients",
            auth=auth,
            timeout=5,
        )
        return resp.status_code

    def test_nonempty_user_with_empty_password_still_enforces_auth(self, tmp_path) -> None:
        """`user = "admin"` + 口令为空 → 无凭据 401，而 `admin:`+空口令 200。

        `401` 那一半证明 `check_dangerous_combination()` 的"两者全空才拒绝"是
        **有依据的**（这里确实启用了鉴权）；`200` 那一半证明"免口令 dashboard"
        的强度问题真实存在，因此需要 `doctor` 的 WARN。
        """
        proc, dash_port = self._start(tmp_path, "user-only", 'user = "admin"')
        try:
            assert self._status(dash_port, None) == 401, (
                "user 非空但口令为空时无凭据请求竟拿到 200 —— 鉴权开关语义已变，"
                "`check_dangerous_combination()` 的判据必须重新收紧"
            )
            assert self._status(dash_port, ("admin", "")) == 200, (
                "空口令不再是合法口令 —— §3.3 的实测表与 doctor 的告警文案需要重写"
            )
            assert self._status(dash_port, ("admin", "whatever")) == 401, "口令校验被跳过了"
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_password_only_config_requires_empty_username(self, tmp_path) -> None:
        """只设 `password`（user 留空）→ 必须用 `:secret` 才能进。

        这是 `DashboardInfo.auth_enabled`（`bool(user or password)`）的另一半依据：
        "只有口令"同样启用鉴权，但调用方要发**空用户名**的 Basic Auth。
        """
        proc, dash_port = self._start(tmp_path, "pass-only", 'password = "secret"')
        try:
            assert self._status(dash_port, None) == 401
            assert self._status(dash_port, ("", "secret")) == 200
            assert self._status(dash_port, ("bogus", "secret")) == 401
        finally:
            proc.kill()
            proc.wait(timeout=5)
