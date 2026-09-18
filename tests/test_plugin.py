"""M5 插件测试（设计文档 §11）。

分两组，职责不重叠：

| 组 | 驱动方式 | 验证什么 |
|----|---------|---------|
| **协议/策略**（本文件主体） | 直接向插件发 HTTP 请求 | 报文形状、裁决逻辑、审计、fail-closed |
| **真机契约**（`TestRealFrpcContract`） | 真 frpc → 真 frps → 插件 | 接得住 frp 的调用，拒绝真的生效 |

第二组是这一层唯一无法用假件替代的部分：`op` 在 query、`content.user` 在 Login 与
NewProxy 上不同型、`unchange` 语义——这些都只有真 frp 能证明。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from frpsctl.plugin.audit import AuditLog, AuditRecord
from frpsctl.plugin.engine import DecisionEngine
from frpsctl.plugin.policy import PluginPolicy, decide_login, decide_new_proxy
from frpsctl.plugin.server import PluginServer, ServerSettings
from .conftest import free_port, free_ports
from frpsctl.plugin.types import (
    LoginContent,
    NewProxyContent,
    PluginRequest,
    PluginResponse,
    UnknownOp,
)

pytestmark = pytest.mark.integration

FRPS_BIN = os.environ.get("FRPSCTL_TEST_BINARY") or str(Path.home() / ".local/share/frpsctl/bin/frps-0.71.0")


def _default_frpc() -> str:
    """frpc 的位置：环境变量 > 与 frps 同目录 > PATH。

    它来自同一次 `frpsctl install --with-frpc`（frpc 与 frps 在同一个 tar 包里），
    因此正常情况下不需要单独指定。
    """
    explicit = os.environ.get("FRPSCTL_TEST_FRPC")
    if explicit:
        return explicit
    beside_frps = Path(FRPS_BIN).parent / "frpc"
    if beside_frps.exists():
        return str(beside_frps)
    import shutil as _shutil

    return _shutil.which("frpc") or "frpc"


FRPC_BIN = _default_frpc()


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def policy(tmp_path: Path) -> PluginPolicy:
    return PluginPolicy.parse(
        {
            "audit": {"path": str(tmp_path / "audit.jsonl")},
            "users": {
                "alice": {"allowed_ports": ["6000-6010"], "note": "普通用户"},
                "bob": {"allowed_ports": ["7000"], "allow_random_port": True},
                "carol": {"allowed_proxy_types": ["http"]},
            },
        }
    )


@pytest.fixture
def plugin(policy: PluginPolicy, tmp_path: Path):
    """起一个真实的插件服务（随机端口），测完关闭。"""
    server = PluginServer(
        policy,
        ServerSettings(bind="127.0.0.1:0", path="/handler"),
    )
    server.start()
    try:
        yield server
    finally:
        server.close()


def post(
    server: PluginServer, op: str, content: dict, *, path: str = "/handler", reqid: str = "REQ-TEST"
) -> tuple[int, dict]:
    """按 frp 的真实报文格式发一次请求（op 在 query，content 嵌在 body 里）。"""
    url = f"http://127.0.0.1:{server.address[1]}{path}?version=0.1.0&op={op}"
    body = json.dumps({"version": "0.1.0", "op": op, "content": content}).encode()
    request = urllib.request.Request(  # noqa: S310 - 目标固定为 127.0.0.1
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-Frp-Reqid": reqid},
    )
    try:
        # noqa: S310 - 目标固定为测试自己起的 127.0.0.1 服务
        with urllib.request.urlopen(request, timeout=5) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# ---------------------------------------------------------------------------
# §11.1 协议事实：报文形状
# ---------------------------------------------------------------------------


class TestProtocolFacts:
    def test_pass_response_must_carry_unchange_true(self, plugin: PluginServer) -> None:
        """**全篇最危险的坑**：只回 `{"reject": false}` 会让 frps 清空内容。

        frp 侧 `manager.go:99` 在 `!Unchange` 时会做 `content = retContent.(*T)`，
        而 Go 零值是 `false`——于是 user 变空串、proxy_name 变空。因此放行时
        **必须**显式带 `unchange: true`。
        """
        _, body = post(plugin, "Login", {"user": "alice", "metas": {"client_id": "alice"}})
        assert body == {"reject": False, "unchange": True}

    def test_reject_response_has_reason(self, plugin: PluginServer) -> None:
        _, body = post(plugin, "Login", {"user": "nobody", "metas": {"client_id": "nobody"}})
        assert body["reject"] is True
        assert body["unchange"] is False
        assert body["reject_reason"]

    def test_login_user_is_string(self) -> None:
        """Login 的 `content.user` 是**字符串**（`types.go:34-38`）。"""
        content = LoginContent.parse({"user": "alice", "metas": {"client_id": "alice"}})
        assert content.user == "alice"
        assert content.metas["client_id"] == "alice"

    def test_newproxy_user_is_object(self) -> None:
        """NewProxy 的 `content.user` 是**对象**（`types.go:41-51`）——不同型。"""
        content = NewProxyContent.parse(
            {
                "user": {"user": "alice", "run_id": "r1", "metas": {}},
                "proxy_name": "p",
                "proxy_type": "tcp",
                "remote_port": 6001,
            }
        )
        assert content.user.user == "alice"
        assert content.user.run_id == "r1"
        assert content.remote_port == 6001

    def test_login_parser_is_defensive_if_user_is_object(self) -> None:
        """万一上游把 user 换成对象，也必须降级成空串而不是崩掉。

        插件崩掉 = fail-closed = 所有人登录不了，所以解析必须比协议更宽容。
        """
        assert LoginContent.parse({"user": {"user": "alice"}}).user == ""

    def test_unknown_op_raises(self) -> None:
        with pytest.raises(UnknownOp):
            PluginRequest.from_payload(op="EvilOp", version="0.1.0", body={"content": {}})

    def test_response_factories_always_set_unchange(self) -> None:
        """三个工厂方法都不会产出"忘记写 unchange"的响应。"""
        assert PluginResponse.pass_through().to_payload()["unchange"] is True
        assert PluginResponse.reject_op("x").to_payload()["unchange"] is False
        assert PluginResponse.replace({"a": 1}).to_payload()["unchange"] is False


# ---------------------------------------------------------------------------
# §11.3 裁决逻辑
# ---------------------------------------------------------------------------


class TestLoginDecisions:
    def test_known_user_with_matching_client_id(self, policy: PluginPolicy) -> None:
        assert decide_login(policy, user="alice", client_id="alice").allowed is True

    def test_unknown_user_denied_by_default(self, policy: PluginPolicy) -> None:
        decision = decide_login(policy, user="mallory", client_id="mallory")
        assert decision.allowed is False
        assert "未知用户" in decision.reason

    def test_missing_client_id_denied(self, policy: PluginPolicy) -> None:
        decision = decide_login(policy, user="alice", client_id="")
        assert decision.allowed is False
        assert "client_id" in decision.reason

    def test_mismatched_client_id_denied(self, policy: PluginPolicy) -> None:
        """user 与 client_id 不一致 = 冒用身份，必须拒绝。"""
        decision = decide_login(policy, user="alice", client_id="bob")
        assert decision.allowed is False
        assert "不一致" in decision.reason

    def test_empty_user_denied(self, policy: PluginPolicy) -> None:
        assert decide_login(policy, user="", client_id="").allowed is False

    def test_allow_unknown_user_opt_in(self) -> None:
        relaxed = PluginPolicy.parse({"users": {"alice": {}}, "allow_unknown_user": True})
        assert decide_login(relaxed, user="anyone", client_id="anyone").allowed is True


class TestNewProxyDecisions:
    def test_port_inside_range_allowed(self, policy: PluginPolicy) -> None:
        assert decide_new_proxy(
            policy, user="alice", proxy_name="p", proxy_type="tcp", remote_port=6005
        ).allowed

    def test_port_outside_range_denied_with_actionable_reason(self, policy: PluginPolicy) -> None:
        """拒绝理由必须让客户端知道**怎么办**，而不是只说"不行"。"""
        decision = decide_new_proxy(policy, user="alice", proxy_name="p", proxy_type="tcp", remote_port=9999)
        assert decision.allowed is False
        assert "6000-6010" in decision.reason, "拒绝理由里应给出许可范围"

    def test_random_port_denied_unless_explicitly_allowed(self, policy: PluginPolicy) -> None:
        """`remote_port = 0` 默认拒绝：放行随机端口等于白名单形同虚设。"""
        denied = decide_new_proxy(policy, user="alice", proxy_name="p", proxy_type="tcp", remote_port=0)
        assert denied.allowed is False
        allowed = decide_new_proxy(policy, user="bob", proxy_name="p", proxy_type="tcp", remote_port=0)
        assert allowed.allowed is True

    def test_domain_based_proxy_skips_port_check(self, policy: PluginPolicy) -> None:
        """http/https 走域名，不带端口——不该被"随机端口"规则误杀。"""
        assert decide_new_proxy(
            policy, user="carol", proxy_name="p", proxy_type="http", remote_port=0
        ).allowed

    def test_proxy_type_restriction(self, policy: PluginPolicy) -> None:
        decision = decide_new_proxy(policy, user="carol", proxy_name="p", proxy_type="tcp", remote_port=6001)
        assert decision.allowed is False
        assert "tcp" in decision.reason

    def test_user_with_no_ports_can_only_do_domain_proxies(self) -> None:
        p = PluginPolicy.parse({"users": {"dave": {}}})
        assert (
            decide_new_proxy(p, user="dave", proxy_name="p", proxy_type="tcp", remote_port=6000).allowed
            is False
        )
        assert (
            decide_new_proxy(p, user="dave", proxy_name="p", proxy_type="https", remote_port=0).allowed
            is True
        )


class TestProxyNamePolicy:
    def test_glob_prefix(self) -> None:
        p = PluginPolicy.parse(
            {"users": {"alice": {"allowed_ports": ["6000"], "allowed_proxy_names": ["alice-*"]}}}
        )
        assert decide_new_proxy(
            p, user="alice", proxy_name="alice-web", proxy_type="tcp", remote_port=6000
        ).allowed
        denied = decide_new_proxy(p, user="alice", proxy_name="bob-web", proxy_type="tcp", remote_port=6000)
        assert denied.allowed is False
        assert "代理名" in denied.reason


# ---------------------------------------------------------------------------
# HTTP 层行为
# ---------------------------------------------------------------------------


class TestHttpBehaviour:
    def test_wrong_path_is_rejected(self, plugin: PluginServer) -> None:
        """路径不匹配说明 frps 配置写错；必须 fail-closed 而不是默默放行。"""
        status, body = post(plugin, "Login", {"user": "alice"}, path="/wrong")
        assert status == 404
        assert body["reject"] is True

    def test_unknown_op_is_rejected(self, plugin: PluginServer) -> None:
        """我们只注册了 Login/NewProxy；收到别的 op 说明请求来路不明。"""
        status, body = post(plugin, "EvilOp", {})
        assert status == 200
        assert body["reject"] is True

    def test_healthz_is_available_and_unauthenticated(self, plugin: PluginServer) -> None:
        with urllib.request.urlopen(  # noqa: S310 - 固定回环地址
            f"http://127.0.0.1:{plugin.address[1]}/healthz", timeout=3
        ) as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["status"] == "ok"

    @pytest.mark.parametrize("bind", ["0.0.0.0:8080", "192.168.1.10:9000", "[::]:8080"])
    def test_non_loopback_bind_is_refused(self, policy: PluginPolicy, bind: str) -> None:
        """§11.2 硬约束：frp 插件协议没有认证，绑非回环就是把鉴权决定权交给网络。

        校验发生在**构造期**（fail-fast）：配置错误应当在启动前就报出来，
        而不是等绑定了端口、服务"看起来起来了"之后再出问题。
        """
        from frpsctl.errors import UsageError

        with pytest.raises(UsageError, match="非回环"):
            PluginServer(policy, ServerSettings(bind=bind))

    def test_malformed_body_is_rejected_not_crashed(self, plugin: PluginServer) -> None:
        url = f"http://127.0.0.1:{plugin.address[1]}/handler?version=0.1.0&op=Login"
        request = urllib.request.Request(
            url,
            data=b"{not json",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            # noqa: S310 - 目标固定为测试自己起的回环服务
            with urllib.request.urlopen(request, timeout=3) as resp:  # noqa: S310
                status, body = resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            status, body = exc.code, json.loads(exc.read())
        assert status == 400
        assert body["reject"] is True

    def test_other_ops_pass_through_with_unchange(self, plugin: PluginServer) -> None:
        """CloseProxy/Ping/NewWorkConn/NewUserConn 不涉及权限边界，必须放行且不改内容。"""
        for op in ("CloseProxy", "Ping", "NewWorkConn", "NewUserConn"):
            _, body = post(plugin, op, {"user": {"user": "alice"}, "proxy_name": "p"})
            assert body == {"reject": False, "unchange": True}, op


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


class TestAudit:
    def test_decisions_are_recorded(self, tmp_path: Path, policy: PluginPolicy) -> None:
        log = AuditLog(tmp_path / "audit.jsonl", flush_every=1, flush_interval=0.1)
        engine = DecisionEngine(policy, log)

        engine.handle(
            PluginRequest.from_payload(
                op="Login",
                version="0.1.0",
                body={"content": {"user": "alice", "metas": {"client_id": "alice"}}},
            ),
            source="127.0.0.1",
        )
        engine.handle(
            PluginRequest.from_payload(
                op="NewProxy",
                version="0.1.0",
                body={
                    "content": {
                        "user": {"user": "alice"},
                        "proxy_name": "p",
                        "proxy_type": "tcp",
                        "remote_port": 9999,
                    }
                },
            )
        )
        log.close()

        lines = (tmp_path / "audit.jsonl").read_text("utf-8").strip().split("\n")
        assert len(lines) == 2
        first, second = (json.loads(line) for line in lines)
        assert first["op"] == "Login" and first["decision"] == "allow"
        assert second["op"] == "NewProxy" and second["decision"] == "deny"
        assert second["remote_port"] == 9999
        assert "6000-6010" in second["reason"]

    def test_record_is_non_blocking_by_construction(self, tmp_path: Path) -> None:
        """`record()` 必须只入队——§11.2 要求 handler 内不做慢速 I/O。"""
        log = AuditLog(tmp_path / "audit.jsonl", flush_every=1000, flush_interval=999)
        for index in range(50):
            log.record(AuditRecord(op=f"Login{index}", user="u", decision="allow"))
        assert log.pending == 50, "record() 不该触发写盘"
        assert log.written == 0
        log.flush()
        assert log.written == 50
        log.close()

    def test_buffer_overflow_drops_oldest(self, tmp_path: Path) -> None:
        """磁盘不可用时不能把内存吃光；丢**最旧**的，因为最近的最有用。"""
        log = AuditLog(tmp_path / "audit.jsonl", flush_every=10_000, flush_interval=999, max_buffer=5)
        for i in range(8):
            log.record(AuditRecord(op=f"op{i}", user="u", decision="allow"))
        assert log.pending == 5
        assert log.dropped == 3
        log.close()

    def test_file_write_failure_does_not_raise(self, tmp_path: Path) -> None:
        """写盘失败不能让登录链路失败——服务可用性优先于审计完整性。"""
        blocker = tmp_path / "blocked"
        blocker.write_text("我是文件，不是目录", "utf-8")
        log = AuditLog(blocker / "sub" / "audit.jsonl", flush_every=1, flush_interval=0.1)
        # 目录建不出来时降级为"仅内存"，而不是让插件起不来
        assert "仅内存" in log.describe()
        log.record(AuditRecord(op="Login", user="u", decision="allow"))
        log.flush()  # 不该抛
        assert log.pending == 1, "记录应留在内存中"
        log.close()


class TestPolicyStrictTypes:
    """策略 JSON 的类型错误必须显式拒绝，而不是宽松转换。

    最危险的一条：JSON 里写 `"false"`（字符串）时，Python 的 `bool("false")`
    是 **True**——`allow_unknown_user` 会被静默打开（fail-open：以为关了鉴权
    后门，实际全开），`allow_random_port` 同理（白名单形同虚设）。
    """

    def test_string_false_is_not_false(self) -> None:
        from frpsctl.errors import ConfigError

        with pytest.raises(ConfigError, match="allow_unknown_user"):
            PluginPolicy.parse({"allow_unknown_user": "false", "users": {"a": {}}})

    def test_string_false_on_random_port_is_rejected(self) -> None:
        from frpsctl.errors import ConfigError

        with pytest.raises(ConfigError, match="allow_random_port"):
            PluginPolicy.parse({"users": {"alice": {"allow_random_port": "false"}}})

    def test_audit_must_be_an_object(self) -> None:
        """`"audit": "false"` 此前会走到 `"false".get(...)` → 裸 AttributeError。"""
        from frpsctl.errors import ConfigError

        with pytest.raises(ConfigError, match="audit 必须是对象"):
            PluginPolicy.parse({"audit": "false"})

    def test_audit_numeric_fields_are_validated(self) -> None:
        from frpsctl.errors import ConfigError

        with pytest.raises(ConfigError, match="flush_every"):
            PluginPolicy.parse({"audit": {"flush_every": "abc"}})
        with pytest.raises(ConfigError, match="flush_interval"):
            PluginPolicy.parse({"audit": {"flush_interval": "soon"}})

    def test_bool_is_not_an_int(self) -> None:
        """`bool` 是 `int` 的子类：`{"max_proxies": true}` 不得被当作 1。"""
        from frpsctl.errors import ConfigError

        with pytest.raises(ConfigError, match="max_proxies"):
            PluginPolicy.parse({"users": {"alice": {"max_proxies": True}}})

    def test_string_is_not_a_list(self) -> None:
        """字符串会被逐字符迭代——那种"看似能跑"的行为必须变成明确报错。"""
        from frpsctl.errors import ConfigError

        with pytest.raises(ConfigError, match="allowed_ports"):
            PluginPolicy.parse({"users": {"alice": {"allowed_ports": "6000"}}})

    def test_valid_booleans_still_work(self) -> None:
        policy = PluginPolicy.parse(
            {
                "allow_unknown_user": True,
                "require_client_id": False,
                "audit": {"enabled": False},
                "users": {"alice": {"allow_random_port": True}},
            }
        )
        assert policy.allow_unknown_user is True
        assert policy.require_client_id is False
        assert policy.audit.enabled is False
        assert policy.users["alice"].allow_random_port is True

    def test_negative_numbers_are_clamped_not_rejected(self) -> None:
        """类型必须严格，但负数的**钳制**语义保持不变（flush_every 最小 1）。"""
        policy = PluginPolicy.parse({"audit": {"flush_every": -5, "flush_interval": -1}})
        assert policy.audit.flush_every == 1
        assert policy.audit.flush_interval == 0.1


class TestRejectRateLimiting:
    """`reject_log_burst` / `reject_log_window` 必须真的生效（此前是零引用死配置）。

    语义：拒绝风暴期间请求**仍然被拒绝**，只是审计停止逐条刷写；被抑制的条数
    累计到下一条记录上（`suppressed` 字段），降采样必须可见。
    """

    def test_reject_storm_is_rate_limited_but_still_denied(self, tmp_path: Path) -> None:
        policy = PluginPolicy.parse(
            {
                "users": {"alice": {"allowed_ports": ["6000"]}},
                "audit": {"path": str(tmp_path / "storm.jsonl")},
                "reject_log_burst": 2,
                "reject_log_window": 100,
            }
        )
        log = AuditLog(tmp_path / "storm.jsonl", flush_every=1, flush_interval=0.1)
        engine = DecisionEngine(policy, log)

        responses = [
            engine.handle(
                PluginRequest.from_payload(
                    op="NewProxy",
                    version="0.1.0",
                    body={
                        "content": {
                            "user": {"user": "alice"},
                            "proxy_name": "p",
                            "proxy_type": "tcp",
                            "remote_port": 9999,  # 越界 → 每次都拒绝
                        }
                    },
                )
            )
            for _ in range(5)
        ]
        log.close()

        assert all(item.reject is True for item in responses), "限速不得改变拒绝语义"
        lines = [
            line for line in (tmp_path / "storm.jsonl").read_text("utf-8").splitlines() if line.strip()
        ]
        assert len(lines) == 2, f"拒绝风暴只应写 burst(2) 条，实际 {len(lines)}"

    def test_suppressed_count_is_reported_after_window(self) -> None:
        """窗口过后，被抑制的条数必须如实汇报在 `suppressed` 字段上。"""
        from frpsctl.plugin.engine import RejectLimiter

        now = {"t": 1000.0}
        limiter = RejectLimiter(burst=1, window=10.0, clock=lambda: now["t"])

        keep1, extra1 = limiter.check("alice")
        assert (keep1, extra1) == (True, 0)
        keep2, _ = limiter.check("alice")
        keep3, _ = limiter.check("alice")
        assert keep2 is False and keep3 is False

        now["t"] += 11.0  # 窗口滑过
        keep4, extra4 = limiter.check("alice")
        assert keep4 is True
        assert extra4 == 2, "窗口期间被抑制的 2 条没有汇报"

    def test_limiter_is_per_user(self) -> None:
        from frpsctl.plugin.engine import RejectLimiter

        limiter = RejectLimiter(burst=1, window=100.0, clock=lambda: 1000.0)
        assert limiter.check("alice")[0] is True
        assert limiter.check("bob")[0] is True, "alice 的拒绝风暴不该影响 bob"


# ---------------------------------------------------------------------------
# 真机契约：真 frpc → 真 frps → 我们的插件
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.skipif(
    not Path(FRPS_BIN).exists() or not Path(FRPC_BIN).exists(),
    reason="需要真实 frps 与 frpc（见 FRPSCTL_TEST_BINARY / FRPSCTL_TEST_FRPC）",
)
class TestRealFrpcContract:
    """**唯一能证明"我们接得住 frp 调用"的一组测试。**

    用真 frpc 发起登录，断言：
    1. 未授权用户被拒 → 它真的连不上（fail-closed 生效）；
    2. 授权用户被放行 → frpc 能拿到 run_id，说明我们的 `unchange: true` 是对的
       （若忘了它，user 会被清空，登录必然失败）。
    """

    @pytest.fixture
    def stack(self, tmp_path: Path, policy: PluginPolicy):
        """起 插件 + frps，返回连接参数。"""
        bind_port, dash_port = free_ports(2)
        server = PluginServer(policy, ServerSettings(bind="127.0.0.1:0", path="/handler"))
        server.start()

        config = tmp_path / "frps.toml"
        config.write_text(
            f"""bindAddr = "127.0.0.1"
bindPort = {bind_port}
[webServer]
addr = "127.0.0.1"
port = {dash_port}
user = "admin"
password = "contract"
[[httpPlugins]]
name = "frpsctl"
addr = "http://127.0.0.1:{server.address[1]}"
path = "/handler"
ops = ["Login", "NewProxy"]
""",
            "utf-8",
        )
        # 先确认这份配置本身合法（顺带验证 §11.1 的 httpPlugins 键名）
        check = subprocess.run(
            [FRPS_BIN, "--strict_config=true", "verify", "-c", str(config)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert check.returncode == 0, check.stdout + check.stderr

        proc = subprocess.Popen(
            [FRPS_BIN, "-c", str(config)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", bind_port), timeout=0.3):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                pytest.fail("frps 未起来")
            yield {"bind_port": bind_port, "tmp": tmp_path, "plugin": server}
        finally:
            proc.kill()
            proc.wait(timeout=5)
            server.close()

    def _run_frpc(self, stack, *, user: str, proxy_port: int, tmp: Path) -> tuple[int, str]:
        cfg = tmp / f"frpc-{user}.toml"
        cfg.write_text(
            f'''serverAddr = "127.0.0.1"
serverPort = {stack["bind_port"]}
user = "{user}"
metadatas = {{ client_id = "{user}" }}

[[proxies]]
name = "{user}-tcp"
type = "tcp"
localIP = "127.0.0.1"
localPort = {free_port()}
remotePort = {proxy_port}
''',
            "utf-8",
        )
        proc = subprocess.Popen(
            [FRPC_BIN, "-c", str(cfg)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            # 20 秒而不是 8 秒：v0.3.0 验收在全量运行（高负载）下抓到过 flaky——
            # frpc 还没来得及完成第一次登录尝试就被 kill，被拒用例因此拿到
            # 空白输出。被拒路径正常在 1-2 秒内退出，20 秒只是给慢机器兜底。
            out, _ = proc.communicate(timeout=20)
            return proc.returncode, out
        except subprocess.TimeoutExpired:
            # 登录成功的话 frpc 会一直跑（正常），此时杀掉并返回已捕获输出
            proc.kill()
            out, _ = proc.communicate(timeout=5)
            return 0, out

    def test_authorized_user_can_login_and_create_proxy(self, stack) -> None:
        """授权用户 alice 申请 6005（在白名单里）→ 应当成功。

        这条同时验证了 `unchange: true`：若我们回 `{"reject": false}` 而不带
        `unchange`，frps 会把 Login 的 user 清空，登录必然失败。
        """
        code, out = self._run_frpc(stack, user="alice", proxy_port=6005, tmp=stack["tmp"])
        assert "login to server success" in out, out
        assert "start proxy success" in out, out
        assert "error" not in out.lower() or "success" in out, out

    def test_unauthorized_user_is_rejected_end_to_end(self, stack) -> None:
        """未授权用户 mallory → 插件拒绝 → frpc 登录失败（fail-closed 真的生效）。"""
        code, out = self._run_frpc(stack, user="mallory", proxy_port=6005, tmp=stack["tmp"])
        assert "login to server failed" in out or "未知用户" in out, out
        assert "login to server success" not in out, out

    def test_port_outside_whitelist_is_rejected_end_to_end(self, stack) -> None:
        """授权用户申请白名单外的端口 → 登录可以成功，但建代理必须失败。"""
        code, out = self._run_frpc(stack, user="alice", proxy_port=9999, tmp=stack["tmp"])
        assert "login to server success" in out, out
        assert "6000-6010" in out, f"拒绝理由没有回到客户端：{out}"
        assert "start proxy success" not in out, out

    def test_plugin_audit_records_real_calls(self, stack) -> None:
        """审计里应当留下真 frp 发来的调用记录（含 op 与用户）。"""
        self._run_frpc(stack, user="alice", proxy_port=6005, tmp=stack["tmp"])
        server: PluginServer = stack["plugin"]
        server.audit.close()  # 刷盘
        assert server.audit.written >= 1


# ---------------------------------------------------------------------------
# doctor 的插件暴露面检查（§11.2）
# ---------------------------------------------------------------------------


class TestDoctorPluginExposure:
    """`doctor` 必须把"插件回调指向非回环"报成 ERROR。

    这是 M5 阶段新识别出的攻击面：frp 的插件协议没有认证，回调地址一旦离开
    回环，任何人都能伪造 Login/NewProxy，从而决定谁能登录 frps。
    """

    def _run(self, tmp_path: Path, addr: str) -> list:
        from frpsctl.core.doctor import run_doctor
        from frpsctl.core.instance import Instance

        inst = Instance(name="t", instances_root=tmp_path / "instances", data_home=tmp_path / "data")
        inst.ensure_dirs()
        inst.config.write_text(
            f'''bindPort = 17000
[auth]
token = "x"
[[httpPlugins]]
name = "frpsctl"
addr = "{addr}"
path = "/handler"
ops = ["Login"]
''',
            "utf-8",
        )
        inst.config.chmod(0o600)
        return run_doctor(inst).findings

    def test_non_loopback_plugin_address_is_an_error(self, tmp_path: Path) -> None:
        findings = self._run(tmp_path, "http://10.0.0.5:8080")
        exposure = [f for f in findings if f.check == "插件暴露面"]
        assert exposure, [f.check for f in findings]
        assert exposure[0].severity.value == "ERROR"
        assert "没有任何认证" in exposure[0].hint

    def test_loopback_plugin_address_is_not_flagged(self, tmp_path: Path) -> None:
        findings = self._run(tmp_path, "http://127.0.0.1:8080")
        assert not [f for f in findings if f.check == "插件暴露面"]


# ---------------------------------------------------------------------------
# doctor 的 dashboard 口令强度检查（§3.3 的实测边界）
# ---------------------------------------------------------------------------


class TestDoctorDashboardCredentials:
    """`doctor` 必须把"口令为空但 user 非空"报出来。

    这一条来自实现期的实测（真 frps 0.71.0，见 `test_facts.py::TestC8PasswordlessAuth`）：
    frp 的鉴权开关是"**任一非空即启用**"，而 **Basic Auth 的空口令是合法口令**——
    `user = "admin"` + 空口令时无凭据请求得 401，但 `admin:` + 空口令得 200。

    因此 `check_dangerous_combination()` 放行它是对的（确实启用了鉴权），但**不能不
    提示**：此时用户名是唯一防护，而它通常就是 `admin`。修复前这条路径完全静默。
    """

    def _findings(self, tmp_path: Path, web_block: str) -> list:
        from frpsctl.core.doctor import run_doctor
        from frpsctl.core.instance import Instance

        inst = Instance(name="t", instances_root=tmp_path / "instances", data_home=tmp_path / "data")
        inst.ensure_dirs()
        inst.config.write_text(
            f'bindPort = 17000\n[auth]\ntoken = "x"\n[webServer]\n{web_block}',
            "utf-8",
        )
        inst.config.chmod(0o600)
        return run_doctor(inst).findings

    @staticmethod
    def _weak(findings: list) -> list:
        return [f for f in findings if f.check == "dashboard 弱口令"]

    def test_empty_password_with_user_is_flagged(self, tmp_path: Path) -> None:
        findings = self._findings(
            tmp_path, 'addr = "127.0.0.1"\nport = 17500\nuser = "admin"\npassword = ""\n'
        )
        weak = self._weak(findings)
        assert weak, [f.check for f in findings]
        assert weak[0].severity.value == "WARN"
        assert "空口令" in weak[0].message
        assert "admin" in weak[0].message

    def test_missing_password_key_is_flagged(self, tmp_path: Path) -> None:
        """口令键**缺失**（而不是显式空串）的语义完全相同，必须同样报出。"""
        findings = self._findings(tmp_path, 'addr = "127.0.0.1"\nport = 17500\nuser = "admin"\n')
        assert self._weak(findings), [f.check for f in findings]

    def test_empty_password_on_non_loopback_is_still_only_a_warning(self, tmp_path: Path) -> None:
        """非回环 + 有 user + 空口令 → 仍然是 WARN，不是 ERROR。

        判据边界是刻意的：它**启用了鉴权**，属于强度不足；ERROR 留给"两者全空"
        那种真·完全无鉴权（见 `test_empty_credentials_on_non_loopback_is_an_error`）。
        """
        findings = self._findings(tmp_path, 'addr = "0.0.0.0"\nport = 17500\nuser = "admin"\npassword = ""\n')
        assert not [f for f in findings if f.check == "dashboard 暴露面"]
        assert self._weak(findings)

    def test_empty_credentials_on_non_loopback_is_an_error(self, tmp_path: Path) -> None:
        """对照：两者全空 + 非回环 = 完全不鉴权 → ERROR。"""
        findings = self._findings(tmp_path, 'addr = "0.0.0.0"\nport = 17500\n')
        exposure = [f for f in findings if f.check == "dashboard 暴露面"]
        assert exposure and exposure[0].severity.value == "ERROR"

    def test_strong_credentials_are_not_flagged(self, tmp_path: Path) -> None:
        findings = self._findings(
            tmp_path, 'addr = "0.0.0.0"\nport = 17500\nuser = "admin"\npassword = "s3cret"\n'
        )
        assert not self._weak(findings), [f.message for f in self._weak(findings)]

    def test_admin_admin_is_still_flagged(self, tmp_path: Path) -> None:
        findings = self._findings(
            tmp_path, 'addr = "127.0.0.1"\nport = 17500\nuser = "admin"\npassword = "admin"\n'
        )
        weak = self._weak(findings)
        assert weak and "admin/admin" in weak[0].message

    def test_disabled_dashboard_is_not_flagged(self, tmp_path: Path) -> None:
        """`port = 0` = 不启动 dashboard，不存在暴露面（口令为空也无所谓）。"""
        findings = self._findings(tmp_path, 'addr = "0.0.0.0"\nport = 0\nuser = "admin"\npassword = ""\n')
        assert not self._weak(findings)
        assert not [f for f in findings if f.check == "dashboard 暴露面"]

    def test_loopback_network_address_is_not_flagged_as_exposure(self, tmp_path: Path) -> None:
        """`127.0.0.2`（回环网段）不是"非回环暴露面"。

        回归：doctor 此前用 `addr in ("127.0.0.1", "::1", "localhost")` 判回环，
        而 schema 的"危险组合"拦截用 ipaddress 判据——`127.0.0.2` 上两套标准
        结论相反（doctor 误报、schema 放行）。现已统一到 `is_loopback`。
        """
        findings = self._findings(
            tmp_path, 'addr = "127.0.0.2"\nport = 17500\nuser = ""\npassword = ""\n'
        )
        assert not [f for f in findings if f.check == "dashboard 暴露面"]


# ---------------------------------------------------------------------------
# 配额（max_proxies）
# ---------------------------------------------------------------------------


class TestQuota:
    """`max_proxies` 的判定与计数来源。

    配额只在 `NewProxy` 上做——`NewUserConn` 落在**每次用户连接**的关键路径上，
    且其错误只以 info 级记录、content 里也没有连接 id，无法可靠统计并发连接。
    详见 `frpsctl/plugin/quota.py` 的模块文档。
    """

    def _engine(self, tmp_path: Path, limit: int) -> tuple[DecisionEngine, AuditLog]:
        policy = PluginPolicy.parse(
            {
                "users": {"alice": {"allowed_ports": ["6000-6100"], "max_proxies": limit}},
                "audit": {"path": str(tmp_path / "q.jsonl")},
            }
        )
        log = AuditLog(tmp_path / "q.jsonl", flush_every=1000)
        return DecisionEngine(policy, log), log

    def _new_proxy(self, engine: DecisionEngine, port: int, name: str = "p") -> dict:
        return engine.handle(
            PluginRequest.from_payload(
                op="NewProxy",
                version="0.1.0",
                body={
                    "content": {
                        "user": {"user": "alice"},
                        "proxy_name": name,
                        "proxy_type": "tcp",
                        "remote_port": port,
                    }
                },
            )
        ).to_payload()

    def test_quota_blocks_after_limit(self, tmp_path: Path) -> None:
        engine, log = self._engine(tmp_path, limit=2)
        assert self._new_proxy(engine, 6000, "p1")["reject"] is False
        assert self._new_proxy(engine, 6001, "p2")["reject"] is False
        third = self._new_proxy(engine, 6002, "p3")
        assert third["reject"] is True
        assert "上限" in third["reject_reason"]
        log.close()

    def test_close_proxy_frees_a_slot(self, tmp_path: Path) -> None:
        """建了又删不能白占配额——否则重启客户端就再也建不出代理。"""
        engine, log = self._engine(tmp_path, limit=1)
        assert self._new_proxy(engine, 6000, "p1")["reject"] is False
        assert self._new_proxy(engine, 6001, "p2")["reject"] is True

        engine.handle(
            PluginRequest.from_payload(
                op="CloseProxy",
                version="0.1.0",
                body={"content": {"user": {"user": "alice"}, "proxy_name": "p1", "proxy_type": "tcp"}},
            )
        )
        assert self._new_proxy(engine, 6001, "p2")["reject"] is False
        log.close()

    def test_zero_limit_means_unlimited(self, tmp_path: Path) -> None:
        engine, log = self._engine(tmp_path, limit=0)
        for index in range(5):
            assert self._new_proxy(engine, 6000 + index, f"p{index}")["reject"] is False
        log.close()

    def test_quota_source_is_recorded_in_audit(self, tmp_path: Path) -> None:
        """审计必须写明计数来源——退化模式的计数不准，运维得看得出来。"""
        engine, log = self._engine(tmp_path, limit=1)
        self._new_proxy(engine, 6000, "p1")
        log.close()

        record = json.loads((tmp_path / "q.jsonl").read_text("utf-8").strip().split("\n")[0])
        assert record["quota_source"] == "local", "未配 admin_url 时应标注为 local"

    def test_dashboard_unreachable_degrades_instead_of_blocking(self, tmp_path: Path) -> None:
        """dashboard 不可达时**不能**让所有人都建不了代理。

        配额是治理手段，不是可用性前提。降级为本地计数并如实标注来源。
        """
        from frpsctl.plugin.quota import QuotaChecker

        checker = QuotaChecker(admin_url="http://127.0.0.1:1", timeout=0.2)
        count, source = checker.current("alice")
        assert (count, source) == (0, "local")
        assert checker.check("alice", 1).allowed is True

    def test_dashboard_mode_uses_authoritative_total(self) -> None:
        """配了 admin_url 时计数来源应标记为 dashboard（用假客户端验证装配）。"""
        from frpsctl.plugin.quota import QuotaChecker

        checker = QuotaChecker(admin_url="http://example.invalid:7500")
        checker._cache["alice"] = (checker._clock(), 7, "dashboard")
        assert checker.current("alice") == (7, "dashboard")
        result = checker.check("alice", 7)
        assert result.allowed is False
        assert "dashboard" in result.reason

    def test_slow_dashboard_query_does_not_block_other_users(self, monkeypatch) -> None:
        """一个用户的 dashboard 查询不得阻塞其他用户。

        回归：预占路径此前持**全局**锁做网络调用，而 frp 侧对插件 HTTP
        客户端没有超时（§11.2）——一个用户的慢查询会串行挂住所有用户的
        登录链路。重构后每个用户一把锁，网络调用不碰全局锁。
        """
        import threading

        from frpsctl.plugin.quota import QuotaChecker

        checker = QuotaChecker(admin_url="http://example.invalid:7500", timeout=2.0)
        alice_querying = threading.Event()
        release_alice = threading.Event()

        def fake_query(user: str) -> int:
            if user == "alice":
                alice_querying.set()
                release_alice.wait(5)
            return 0

        monkeypatch.setattr(checker, "_query_dashboard", fake_query)

        thread = threading.Thread(target=lambda: checker.check("alice", 5, reserve=True))
        thread.start()
        try:
            assert alice_querying.wait(2.0), "alice 的查询没有开始"
            started = time.monotonic()
            result = checker.check("bob", 5, reserve=True)
            elapsed = time.monotonic() - started
        finally:
            release_alice.set()
            thread.join(timeout=5)

        assert result.allowed is True
        assert elapsed < 0.5, f"bob 被 alice 的网络查询阻塞了 {elapsed:.2f}s"

    def test_concurrent_reserve_does_not_exceed_limit(self) -> None:
        """同一用户的并发预占不得突破上限（"读-判-占"必须原子）。"""
        import threading

        from frpsctl.plugin.quota import QuotaChecker

        checker = QuotaChecker()  # 本地模式：不涉及网络
        results: list[bool] = []
        guard = threading.Lock()

        def worker() -> None:
            verdict = checker.check("alice", 3, reserve=True)
            with guard:
                results.append(verdict.allowed)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for item in threads:
            item.start()
        for item in threads:
            item.join()

        assert sum(results) == 3, f"并发预占突破了上限：{sum(results)} 个通过"


# ---------------------------------------------------------------------------
# 进程级生命周期：SIGTERM 必须优雅退出并刷盘（systemd stop 的真实路径）
# ---------------------------------------------------------------------------


def _wait_sigterm_handled(pid: int, *, timeout: float = 5.0) -> bool:
    """等待子进程**真正捕获** SIGTERM（读 `/proc/<pid>/status` 的 `SigCgt` 掩码）。

    为什么不能"起了就发信号"：`serve_forever()` 内部先安装 handler 再阻塞，
    抢在安装之前发 SIGTERM 会直接杀死进程（审计不落盘）——这是该用例偶发
    失败的真因。用内核记录的事实做就绪判定，比定时猜测可靠。
    """
    import signal as _signal

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = Path(f"/proc/{pid}/status").read_text()
        except OSError:
            return False
        for line in status.splitlines():
            if line.startswith("SigCgt:"):
                mask = int(line.split(":", 1)[1].strip(), 16)
                if mask & (1 << (_signal.SIGTERM - 1)):
                    return True
                break
        time.sleep(0.02)
    return False


class TestServeForeverLifecycle:
    """`plugin serve` 的部署要求是"由 systemd 守护"（§11.2），而 **systemd 停止
    服务时发的是 SIGTERM**。默认处置下进程立即死亡：`finally` 不执行、审计缓冲里
    未刷盘的记录全部丢失——而审计正是"谁在什么时候申请了什么端口"的唯一记录，
    停机时丢掉它是最不该丢的时候。

    这条不变量（"SIGTERM 也要落盘"）只有在**真正独立的进程**里才测得出来：
    同进程内发信号只会打断主线程，走不到"进程被终止"这条路径。
    """

    def test_sigterm_flushes_audit_and_exits_cleanly(self, tmp_path: Path) -> None:
        policy_file = tmp_path / "plugin-policy.json"
        policy_file.write_text(
            json.dumps(
                {
                    "users": {"alice": {"allowed_ports": ["6000-6010"]}},
                    "audit": {"path": str(tmp_path / "audit.jsonl"), "flush_interval": 999},
                }
            ),
            "utf-8",
        )
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from frpsctl.plugin.policy import PluginPolicy\n"
            "from frpsctl.plugin.server import PluginServer, ServerSettings\n"
            "from frpsctl.plugin.types import PluginRequest\n"
            "policy = PluginPolicy.load(Path(sys.argv[1]))\n"
            "server = PluginServer(policy, ServerSettings(bind='127.0.0.1:0', path='/handler'))\n"
            "server.start()\n"
            "print(server.address[1], flush=True)\n"
            # 积压一条记录但**不刷盘**（flush_interval=999），随后前台阻塞
            "server.engine.handle(\n"
            "    PluginRequest.from_payload(\n"
            "        op='Login', version='0.1.0',\n"
            "        body={'content': {'user': 'alice', 'metas': {'client_id': 'alice'}}},\n"
            "    )\n"
            ")\n"
            "server.serve_forever()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(policy_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")},
        )
        try:
            port = int((proc.stdout.readline() or "0").strip())
            assert port > 0, proc.stderr.read()[:400]
            # 必须等 handler **真正装好**再发信号：serve_forever() 里
            # "安装 handler" 与 "开始阻塞" 之间存在窗口，抢跑会直接杀死进程。
            assert _wait_sigterm_handled(proc.pid), "子进程未安装 SIGTERM 处理器"
            # 记录此刻只在内存里
            audit_file = tmp_path / "audit.jsonl"
            assert not audit_file.exists() or audit_file.read_text("utf-8") == ""

            proc.terminate()  # SIGTERM：systemd stop 的真实信号
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

        assert audit_file.exists(), "SIGTERM 后审计记录没有落盘（进程被直接杀死）"
        lines = [line for line in audit_file.read_text("utf-8").splitlines() if line.strip()]
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["op"] == "Login" and record["user"] == "alice"



class TestAuditPathResolution:
    """审计相对路径相对**策略文件**解析（v0.2.6）。

    旧行为：`audit.path` 相对进程 CWD——`plugin serve` 手工前台运行与 systemd
    托管（WorkingDirectory=实例目录）会写到两个地方，同一份策略因启动方式
    不同而"审计消失"。现在写入与读取都相对策略文件目录。
    """

    def test_server_settings_policy_path_resolves_audit(self, tmp_path) -> None:
        policy_path = tmp_path / "conf" / "plugin-policy.json"
        policy_path.parent.mkdir()
        policy_path.write_text(json.dumps({"users": {}}), "utf-8")
        policy = PluginPolicy.load(policy_path)
        server = PluginServer(policy, ServerSettings(bind="127.0.0.1:0", policy_path=policy_path))
        try:
            assert server.audit.path == policy_path.parent / "plugin-audit.jsonl"
        finally:
            server.close()

    def test_without_policy_path_relative_stays_relative(self) -> None:
        """未提供策略路径时保持旧语义（原样相对路径）——不引入意外行为。"""
        policy = PluginPolicy.parse({"users": {}, "audit": {"path": "audit.jsonl"}})
        server = PluginServer(policy, ServerSettings(bind="127.0.0.1:0"))
        try:
            assert server.audit.path == Path("audit.jsonl")
        finally:
            server.close()
