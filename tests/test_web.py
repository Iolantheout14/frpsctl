"""Web 管理台的测试（设计文档 §18、§13 的"web 层"）。

覆盖三层：

| 层 | 对象 | 重点 |
|----|------|------|
| 认证单元 | `AuthManager` | 口令、会话 TTL、CSRF、失败限速、空口令拒绝 |
| API 集成 | 真实 `WebServer` + HTTP | 认证边界、CSRF 强制、配置预览/应用（含 CAS）、打码、错误映射 |
| 服务生命周期 | `WebServer` | 启动/停止/端口冲突/静态资源 |

安全断言与功能断言同等重要：401/403 的**每一条**都对应"如果漏了会怎样"。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from frpsctl.web import AuthManager, WebContext, WebServer, WebSettings
from frpsctl.web.auth import MAX_FAILURES

from .conftest import make_fake_frps

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def auth() -> AuthManager:
    return AuthManager("secret-password")


@pytest.fixture
def web_ctx(inst) -> WebContext:
    inst.config.write_text(
        'bindPort = 17000\n[auth]\ntoken = "test-token"\n[webServer]\naddr = "127.0.0.1"\nport = 0\n',
        "utf-8",
    )
    inst.config.chmod(0o600)
    return WebContext(inst=inst, auth=AuthManager("secret-password"))


@pytest.fixture
def web(web_ctx):
    server = WebServer(web_ctx, WebSettings(bind="127.0.0.1:0"))
    server.start()
    try:
        yield server
    finally:
        server.stop()


class Client:
    """极简 HTTP 客户端：Cookie 与 CSRF 由测试显式管理（正是要断言的东西）。"""

    def __init__(self, server: WebServer) -> None:
        self.base = f"http://127.0.0.1:{server.address[1]}"
        self.cookie: str | None = None
        self.csrf: str | None = None

    def call(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict, dict]:
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if self.cookie:
            request_headers["Cookie"] = self.cookie
        if method != "GET" and self.csrf and "X-CSRF-Token" not in request_headers:
            request_headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(  # noqa: S310 - 固定回环地址
            self.base + path,
            method=method,
            data=json.dumps(body or {}).encode() if method == "POST" else None,
            headers=request_headers,
        )
        try:
            # noqa: S310 - 固定回环地址
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                payload = json.loads(resp.read() or b"{}")
                return resp.status, payload, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}"), dict(exc.headers)

    def login(self, password: str = "secret-password") -> tuple[int, dict]:  # noqa: S107 - 测试口令
        status, payload, headers = self.call("/api/login", method="POST", body={"password": password})
        if status == 200:
            self.cookie = headers["Set-Cookie"].split(";")[0]
            self.csrf = payload["csrf"]
        return status, payload


# ---------------------------------------------------------------------------
# 认证单元
# ---------------------------------------------------------------------------


class TestAuthManager:
    def test_empty_password_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="空口令"):
            AuthManager("")

    def test_login_success_and_session_roundtrip(self, auth: AuthManager) -> None:
        session = auth.login("secret-password", source="127.0.0.1")
        assert session is not None
        assert auth.check_session(session.token) is not None

    def test_wrong_password_returns_none(self, auth: AuthManager) -> None:
        assert auth.login("wrong", source="127.0.0.1") is None

    def test_session_expires(self) -> None:
        now = {"t": 1000.0}
        auth = AuthManager("pw", session_ttl=100.0, clock=lambda: now["t"])
        session = auth.login("pw", source="s")
        assert session is not None
        now["t"] += 101
        assert auth.check_session(session.token) is None

    def test_logout_invalidates(self, auth: AuthManager) -> None:
        session = auth.login("secret-password", source="s")
        assert session is not None
        auth.logout(session.token)
        assert auth.check_session(session.token) is None

    def test_csrf_check(self, auth: AuthManager) -> None:
        session = auth.login("secret-password", source="s")
        assert session is not None
        assert auth.check_csrf(session, session.csrf) is True
        assert auth.check_csrf(session, "bogus") is False
        assert auth.check_csrf(session, None) is False

    def test_failure_rate_limit_then_recovery(self) -> None:
        """失败限速：窗口内 N 次失败后冷却；窗口过后恢复。

        冷却中的响应与"口令错误"完全一致（不给爆破者信号）。
        """
        now = {"t": 1000.0}
        auth = AuthManager("pw", clock=lambda: now["t"])
        for _ in range(MAX_FAILURES):
            assert auth.login("wrong", source="evil") is None
        # 冷却中：即使口令正确也拒绝（来源被限速）
        assert auth.login("pw", source="evil") is None
        # 另一个来源不受影响
        assert auth.login("pw", source="other") is not None
        # 窗口过后恢复
        now["t"] += 61
        assert auth.login("pw", source="evil") is not None

    def test_expired_session_is_pruned(self) -> None:
        now = {"t": 1000.0}
        auth = AuthManager("pw", session_ttl=10.0, clock=lambda: now["t"])
        first = auth.login("pw", source="s")
        assert first is not None
        now["t"] += 11
        second = auth.login("pw", source="s")
        assert second is not None
        assert auth.check_session(first.token) is None

    def test_failure_sources_are_bounded(self) -> None:
        """失败来源表必须有上限：大量不同来源的失败不能把内存撑大。

        窗口清理只在 login 时发生——持续、分散的失败请求会在窗口内堆积任意多
        的来源条目（慢速内存放大）。上限是最基本的内存护栏。
        """
        from frpsctl.web.auth import MAX_TRACKED_SOURCES

        auth = AuthManager("pw")
        for index in range(MAX_TRACKED_SOURCES + 400):
            auth.login("wrong", source=f"10.{(index // 65536) % 256}.{(index // 256) % 256}.{index % 256}")
        with auth._lock:
            assert len(auth._failures) <= MAX_TRACKED_SOURCES

    def test_sessions_are_bounded(self) -> None:
        """会话表必须有上限：反复用正确口令登录不能持续推高内存。

        失败的登录不会创建会话，因此这条的触发前提是"持有口令"——威胁模型低，
        但它与来源表是同一类"输入驱动的表必须有界"问题，护栏成本只有几行。
        """
        from frpsctl.web.auth import MAX_SESSIONS

        auth = AuthManager("pw")
        seen = []
        for _ in range(MAX_SESSIONS + 10):
            session = auth.login("pw", source="127.0.0.1")
            assert session is not None
            seen.append(session.token)
        with auth._lock:
            assert len(auth._sessions) <= MAX_SESSIONS
        # 驱逐的是最早到期的会话（TTL 相同 → 先开先出），最新的一定还在
        assert auth.check_session(seen[-1]) is not None
        assert auth.check_session(seen[0]) is None


class TestTrustedProxy:
    """反向代理部署下的来源识别（文档推荐的部署方式，此前完全不可用）。

    默认模式下所有请求都来自代理（127.0.0.1）→ 攻击者 5 次失败即可把管理员
    锁在冷却之外（60 秒内口令正确也拒绝）。`--trusted-proxy` 显式开启后，
    来源取 `X-Forwarded-For` 的**最后一跳**（最靠近我们的代理写入的；左侧
    可被客户端伪造）。
    """

    MAX_FAILURES_LOCAL = MAX_FAILURES

    def test_forwarded_header_ignored_by_default(self, web) -> None:
        """默认不读 X-Forwarded-For：伪造它不能绕开限速，也不能制造新来源。"""
        client = Client(web)
        for _ in range(self.MAX_FAILURES_LOCAL):
            status, _, _ = client.call(
                "/api/login",
                method="POST",
                body={"password": "wrong"},
                headers={"X-Forwarded-For": "1.1.1.1"},
            )
            assert status == 401
        # 伪造另一个"来源"用正确口令 → 仍应被限速（真实来源一直是 127.0.0.1）
        status, _, _ = client.call(
            "/api/login",
            method="POST",
            body={"password": "secret-password"},
            headers={"X-Forwarded-For": "2.2.2.2"},
        )
        assert status == 401, "默认模式读取了 X-Forwarded-For（伪造头不应生效）"

    def test_trusted_proxy_limits_by_last_forwarded_hop(self, web_ctx) -> None:
        server = WebServer(
            web_ctx,
            WebSettings(bind="127.0.0.1:0", trusted_proxy=True),
        )
        server.start()
        try:
            client = Client(server)
            for _ in range(self.MAX_FAILURES_LOCAL):
                status, _, _ = client.call(
                    "/api/login",
                    method="POST",
                    body={"password": "wrong"},
                    headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"},
                )
                assert status == 401
            # 同一最后一跳（9.9.9.9）+ 正确口令 → 冷却生效
            status, _, _ = client.call(
                "/api/login",
                method="POST",
                body={"password": "secret-password"},
                headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"},
            )
            assert status == 401, "可信代理模式下没有按最后一跳限速"
            # 不同最后一跳 + 正确口令 → 放行（证明来源确实按最后一跳区分）
            status, payload, _ = client.call(
                "/api/login",
                method="POST",
                body={"password": "secret-password"},
                headers={"X-Forwarded-For": "5.5.5.5"},
            )
            assert status == 200, payload
        finally:
            server.stop()

    def test_trusted_proxy_falls_back_to_peer_address(self, web_ctx) -> None:
        """可信代理模式但没有 X-Forwarded-For 头 → 退回对端地址。"""
        server = WebServer(
            web_ctx,
            WebSettings(bind="127.0.0.1:0", trusted_proxy=True),
        )
        server.start()
        try:
            client = Client(server)
            status, _, _ = client.call(
                "/api/login", method="POST", body={"password": "secret-password"}
            )
            assert status == 200
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# API 集成（真实 HTTP）
# ---------------------------------------------------------------------------


class TestApiAuthBoundary:
    @pytest.mark.parametrize(
        ("path", "method"),
        [
            ("/api/status", "GET"),
            ("/api/clients", "GET"),
            ("/api/proxies", "GET"),
            ("/api/traffic", "GET"),
            ("/api/config", "GET"),
            ("/api/config/history", "GET"),
            ("/api/config/history/1/diff", "GET"),
            ("/api/logs", "GET"),
            ("/api/session", "GET"),
            ("/api/config/preview", "POST"),
            ("/api/config/apply", "POST"),
            ("/api/actions/start", "POST"),
            ("/api/actions/stop", "POST"),
            ("/api/actions/restart", "POST"),
            ("/api/actions/rollback", "POST"),
            ("/api/actions/prune", "POST"),
            ("/api/logout", "POST"),
            ("/api/nonexistent", "GET"),
        ],
    )
    def test_all_api_routes_require_auth(self, web, path, method) -> None:
        """**全路由认证扫描**：除 login 外的每条 API 在未登录时都必须 401。

        路由清单与 `api.py`/`server.py` 手工同步——新增路由若忘了挡认证，
        这条会失败（未知路由也返回 401，不泄露其存在性）。
        """
        status, payload, _ = Client(web).call(
            path, method=method, body={} if method == "POST" else None
        )
        assert status == 401, (path, method, status, payload)

    def test_session_endpoint_returns_csrf_for_refresh(self, web) -> None:
        """刷新页面后的会话恢复：GET /api/session 归还 CSRF。

        回归（第八轮 review 复现）：刷新后 Cookie 还在但前端内存里的 CSRF
        丢了，所有变更操作 403 且无恢复路径。
        """
        client = Client(web)
        client.login()
        original_csrf = client.csrf
        status, payload, _ = client.call("/api/session")
        assert status == 200
        assert payload["csrf"] == original_csrf
        assert payload["ttl"] > 0

    def test_session_endpoint_unauthenticated_is_401(self, web) -> None:
        status, _, _ = Client(web).call("/api/session")
        assert status == 401

    def test_config_with_datetime_is_serializable(self, web) -> None:
        """配置含 TOML datetime 时配置接口必须正常返回。

        回归（第八轮 review 复现）：`json.dumps` 对 datetime 抛 TypeError →
        连接被断开（整个配置页不可用）。现在统一 `default=str` 兜底。
        """
        client = Client(web)
        client.login()
        config = web.ctx.inst.config
        # 顶层键必须写在任何 [table] 之前（TOML 位置语义）
        config.write_text("expires_at = 2026-12-31T23:59:59\n" + config.read_text("utf-8"), "utf-8")
        status, payload, _ = client.call("/api/config")
        assert status == 200, payload
        entry = next(item for item in payload["entries"] if item["key"] == "expires_at")
        assert "2026-12-31" in str(entry["value"])

    def test_unauthenticated_api_is_401(self, web) -> None:
        status, payload, _ = Client(web).call("/api/status")
        assert status == 401
        assert "未登录" in payload["error"]

    def test_index_is_served_without_auth(self, web) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{web.address[1]}/", timeout=5) as resp:  # noqa: S310
            html = resp.read().decode("utf-8")
        assert resp.status == 200
        assert "frpsctl 管理台" in html

    def test_login_wrong_password_is_401(self, web) -> None:
        client = Client(web)
        status, payload = client.login("wrong")
        assert status == 401
        assert "口令错误" in payload["error"]

    def test_login_sets_cookie_and_csrf(self, web) -> None:
        client = Client(web)
        status, payload, headers = client.call(
            "/api/login", method="POST", body={"password": "secret-password"}
        )
        assert status == 200
        cookie = headers["Set-Cookie"]
        assert "frpsctl_session=" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        assert len(payload["csrf"]) > 20

    def test_authenticated_status_works(self, web) -> None:
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/status")
        assert status == 200
        assert payload["instance"] == "test"

    def test_post_without_csrf_is_403(self, web) -> None:
        client = Client(web)
        client.login()
        status, payload, _ = client.call(
            "/api/actions/restart", method="POST", body={}, headers={"X-CSRF-Token": ""}
        )
        assert status == 403
        assert "CSRF" in payload["error"]

    def test_post_with_valid_csrf_passes_boundary(self, web) -> None:
        """CSRF 通过后进入业务层：stop 一个未运行的实例 → 409（不是 403）。"""
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/actions/stop", method="POST", body={})
        assert status == 409
        assert "未运行" in payload["error"]

    def test_logout_clears_cookie_and_session(self, web) -> None:
        client = Client(web)
        client.login()
        status, _, headers = client.call("/api/logout", method="POST", body={})
        assert status == 200
        assert "Max-Age=0" in headers["Set-Cookie"]
        # 旧 cookie 再访问：401
        status, _, _ = client.call("/api/status")
        assert status == 401

    def test_unknown_route_is_404(self, web) -> None:
        client = Client(web)
        client.login()
        status, _, _ = client.call("/api/nope")
        assert status == 404


class TestApiData:
    def test_config_tree_masks_secrets(self, web) -> None:
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/config")
        assert status == 200
        entries = {item["key"]: item for item in payload["entries"]}
        assert entries["auth.token"]["masked"] is True
        assert "test-token" not in json.dumps(payload)
        assert entries["auth.token"]["value"] == "te***en"
        assert entries["bindPort"]["masked"] is False
        assert entries["bindPort"]["value"] == 17000

    def test_status_survives_missing_dashboard(self, web) -> None:
        """dashboard 未启用（port=0）时状态照常返回，统计为 None。"""
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/status")
        assert status == 200
        assert payload["dashboard"] is None

    def test_dashboard_disabled_error_for_stats_endpoints(self, web) -> None:
        client = Client(web)
        client.login()
        for path in ("/api/clients", "/api/proxies", "/api/traffic"):
            status, payload, _ = client.call(path)
            assert status == 502, path
            assert "dashboard 未启用" in payload["error"]
        # 清理离线记录同样依赖 dashboard
        status, payload, _ = client.call("/api/actions/prune", method="POST", body={})
        assert status == 502
        assert "dashboard 未启用" in payload["error"]

    def test_traffic_tolerates_single_proxy_failure(self, web, monkeypatch) -> None:
        """单个代理的流量查询失败不拖垮整张趋势图（记为空 history）。"""
        from frpsctl.core.admin import V2Proxy
        from frpsctl.errors import AdminUnreachable

        class _FakeAdmin:
            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

            def list_proxies(self):
                return [V2Proxy(name="bad", type="tcp"), V2Proxy(name="good", type="tcp")]

            def proxy_traffic(self, name: str):
                if name == "bad":
                    raise AdminUnreachable("boom")
                return [{"date": "2026-09-17", "in": 1, "out": 2}]

        monkeypatch.setattr("frpsctl.web.api._admin", lambda _ctx: _FakeAdmin())
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/traffic")
        assert status == 200
        assert [item["name"] for item in payload["proxies"]] == ["bad", "good"]
        assert payload["proxies"][0]["history"] == []
        assert payload["proxies"][1]["history"][0]["in"] == 1

    def test_logs_endpoint_returns_lines(self, web) -> None:
        client = Client(web)
        client.login()
        log_file = web.ctx.inst.dir / "frps.log"
        log_file.write_text("line-1\nline-2\nline-3\n", "utf-8")
        status, payload, _ = client.call("/api/logs?lines=2")
        assert status == 200
        assert payload["lines"] == ["line-2\n", "line-3\n"]

    def test_traffic_reports_truncation(self, web, monkeypatch) -> None:
        """超过 `TRAFFIC_MAX_PROXIES` 时如实汇报截断——CLI 会告警，Web 不能静默。"""
        from frpsctl.core.admin import TRAFFIC_MAX_PROXIES, V2Proxy

        class _FakeAdmin:
            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

            def list_proxies(self):
                return [V2Proxy(name=f"p{i}", type="tcp") for i in range(TRAFFIC_MAX_PROXIES + 5)]

            def proxy_traffic(self, name: str):
                return []

        monkeypatch.setattr("frpsctl.web.api._admin", lambda _ctx: _FakeAdmin())
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/traffic")
        assert status == 200
        assert payload["truncated"] is True
        assert payload["total"] == TRAFFIC_MAX_PROXIES + 5
        assert len(payload["proxies"]) == TRAFFIC_MAX_PROXIES

    def test_favicon_is_no_content(self, web) -> None:
        """`/favicon.ico` 返回 204（页面内嵌 data URI 图标；这里是旧工具收尾）。"""
        status, payload, _ = Client(web).call("/favicon.ico")
        assert status == 204

    def test_health_payload_carries_plugin_warning(self) -> None:
        """`_health` 必须透出 L3 告警：插件 fail-closed，客户端将无法登录。"""
        from frpsctl.core.health import HealthLayer, HealthReport
        from frpsctl.web.api import _health

        report = HealthReport(
            l1_process=HealthLayer.OK,
            l2_control=HealthLayer.OK,
            l3_plugin=HealthLayer.FAIL,
            detail="frpsctl 127.0.0.1:8080",
        )
        payload = _health(report)
        assert payload is not None
        assert payload["plugin_warning"], "L3 失败没有下发告警文本"
        assert _health(None) is None


class TestActionParameterValidation:
    """动作接口的参数范围校验。

    回归（P0）：`/api/actions/stop` 的 `timeout` 走 `_float` 宽松解析，负值一路
    传到 `Lifecycle.stop` —— `_wait_gone(ref, -1)` 的 deadline 落在过去，循环
    一次都不执行、直接返回"未退出"，于是**立刻升级 SIGKILL**。这与 CLI 侧
    `stop --timeout -1`（v0.2.0 修复、Click min 约束）是同一个缺陷的镜像。
    """

    def test_out_of_range_parameters_are_rejected(self, web) -> None:
        client = Client(web)
        client.login()
        cases = (
            ("start", {"health_timeout": -1}),
            ("restart", {"health_timeout": 99999}),
            ("stop", {"timeout": -1}),
            ("stop", {"timeout": -0.5}),
            ("stop", {"timeout": "abc"}),
            ("stop", {"timeout": True}),  # bool 是 float 的子类：不得当作 1.0
            ("rollback", {"steps": 0}),
            ("rollback", {"steps": -3}),
        )
        for action, body in cases:
            status, payload, _ = client.call(f"/api/actions/{action}", method="POST", body=body)
            assert status == 400, (action, body, status, payload)

    def test_negative_stop_timeout_does_not_kill_the_process(self, web) -> None:
        """负 timeout 必须在请求边界被拒，**进程绝不能**被这一步碰掉。"""
        from frpsctl.core.platform import pid_alive

        make_fake_frps(web.ctx.inst.bin_dir)
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/actions/start", method="POST", body={})
        assert status == 200, payload
        pid = payload["pid"]
        try:
            status, payload, _ = client.call(
                "/api/actions/stop", method="POST", body={"timeout": -1}
            )
            assert status == 400, payload
            assert pid_alive(pid), "负 timeout 的请求把进程杀掉了（应为 400 拒绝）"
        finally:
            client.call("/api/actions/stop", method="POST", body={})


class TestConfigHistory:
    """`GET /api/config/history`：快照列表（Web"历史与回滚"卡片的数据源）。

    `steps` 与 `config rollback N` / `/api/actions/rollback` 的语义完全一致：
    最新快照是 1（= 回滚一步）。接口只读 meta.json，**不读快照里的配置原文**
    （快照是完整配置副本，含机密，没有理由读进内存再考虑打码）。
    """

    def test_empty_history(self, web) -> None:
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/config/history")
        assert status == 200
        assert payload["entries"] == []

    def test_lists_snapshots_newest_first_with_steps(self, web) -> None:
        from frpsctl.core.transaction import config_snapshot

        inst = web.ctx.inst
        config_snapshot(inst, action="set bindPort")
        config_snapshot(inst, action="rollback 1")

        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/config/history")
        assert status == 200, payload
        entries = payload["entries"]
        assert len(entries) == 2
        assert entries[0]["steps"] == 1
        assert entries[0]["action"] == "rollback 1"
        assert entries[0]["name"].startswith("0002")
        assert entries[1]["steps"] == 2
        assert entries[1]["name"].startswith("0001")
        assert entries[0]["at"], "meta.json 里的时间没有透出"
        assert entries[0]["has_config"] is True

    def test_tolerates_corrupted_meta(self, web) -> None:
        from frpsctl.core.transaction import config_snapshot

        inst = web.ctx.inst
        slot = config_snapshot(inst, action="set x")
        (slot / "meta.json").write_text("{broken", "utf-8")

        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/config/history")
        assert status == 200
        assert payload["entries"][0]["steps"] == 1  # 元数据坏了不影响列表

    def test_diff_endpoint_masks_secrets(self, web) -> None:
        """`GET /api/config/history/{steps}/diff`：回滚前看差异（打码后下发）。

        回滚是危险操作，看不到"会改什么"就确认等于盲操作——CLI 的
        `config diff --steps` 一直有这个能力，Web 必须同等。
        """
        from frpsctl.core.transaction import config_snapshot

        inst = web.ctx.inst
        config_snapshot(inst, action="set bindPort")
        inst.config.write_text(
            'bindPort = 18000\n[auth]\ntoken = "rotated-secret-xyz"\n'
            '[webServer]\naddr = "127.0.0.1"\nport = 0\n',
            "utf-8",
        )

        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/config/history/1/diff")
        assert status == 200, payload
        assert payload["steps"] == 1
        assert payload["snapshot"].startswith("0001")
        assert "-bindPort = 17000" in payload["diff"]
        assert "+bindPort = 18000" in payload["diff"]
        # 机密（新 token）绝不出现在差异里
        assert "rotated-secret-xyz" not in payload["diff"]
        assert "test-token" not in payload["diff"]

    def test_diff_endpoint_rejects_bad_steps(self, web) -> None:
        client = Client(web)
        client.login()
        for bad in ("abc", "0", "-1"):
            status, payload, _ = client.call(f"/api/config/history/{bad}/diff")
            assert status == 400, (bad, status, payload)

    def test_diff_endpoint_without_snapshots_is_400(self, web) -> None:
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/config/history/1/diff")
        assert status == 400
        assert "快照" in payload["error"]


class TestConfigEditFlow:
    """预览 → 应用（含 CAS）——Web 配置编辑的核心链路。"""

    def test_preview_then_apply_changes_config(self, web) -> None:
        make_fake_frps(web.ctx.inst.bin_dir)
        client = Client(web)
        client.login()

        status, preview, _ = client.call(
            "/api/config/preview",
            method="POST",
            body={"changes": [["bindPort", "18000"], ["maxPortsPerClient", "30"]]},
        )
        assert status == 200, preview
        assert preview["noop"] is False
        assert "+bindPort = 18000" in preview["diff"]

        status, applied, _ = client.call(
            "/api/config/apply", method="POST", body={"preview_id": preview["preview_id"]}
        )
        assert status == 200, applied
        assert applied["applied"] is True
        text = web.ctx.inst.config.read_text("utf-8")
        assert "bindPort = 18000" in text
        assert "maxPortsPerClient = 30" in text

    def test_cas_rejects_when_file_changed_after_preview(self, web) -> None:
        """预览之后配置文件被 CLI 改过 → 应用被拒（不覆盖别人的改动）。"""
        from frpsctl.core import config as cfg

        make_fake_frps(web.ctx.inst.bin_dir)
        client = Client(web)
        client.login()

        status, preview, _ = client.call(
            "/api/config/preview", method="POST", body={"changes": [["bindPort", "18001"]]}
        )
        assert status == 200

        # 模拟并发的 CLI 操作
        doc = cfg.load_config(web.ctx.inst.config)
        doc["maxPortsPerClient"] = 42
        web.ctx.inst.config.write_text(cfg.tomlkit.dumps(doc), "utf-8")

        status, payload, _ = client.call(
            "/api/config/apply", method="POST", body={"preview_id": preview["preview_id"]}
        )
        assert status == 400
        assert "预览期间" in payload["error"]
        assert "bindPort = 18001" not in web.ctx.inst.config.read_text("utf-8")

    def test_expired_preview_is_rejected(self, web_ctx) -> None:
        make_fake_frps(web_ctx.inst.bin_dir)
        clock = {"t": 1000.0}
        web_ctx.clock = lambda: clock["t"]
        server = WebServer(web_ctx, WebSettings(bind="127.0.0.1:0"))
        server.start()
        try:
            client = Client(server)
            client.login("secret-password")
            status, preview, _ = client.call(
                "/api/config/preview", method="POST", body={"changes": [["bindPort", "18002"]]}
            )
            assert status == 200
            clock["t"] += 601  # 超过 PREVIEW_TTL
            status, payload, _ = client.call(
                "/api/config/apply", method="POST", body={"preview_id": preview["preview_id"]}
            )
            assert status == 400
            assert "过期" in payload["error"]
        finally:
            server.stop()

    def test_invalid_changes_shape_is_400(self, web) -> None:
        client = Client(web)
        client.login()
        for bad in ({"changes": "nope"}, {"changes": []}, {"changes": [{"x": 1}]}):
            status, payload, _ = client.call("/api/config/preview", method="POST", body=bad)
            assert status == 400, bad
            assert payload["error"]

    def test_preview_noop_reports_noop(self, web) -> None:
        client = Client(web)
        client.login()
        status, payload, _ = client.call(
            "/api/config/preview", method="POST", body={"changes": [["bindPort", "17000"]]}
        )
        assert status == 200
        assert payload["noop"] is True

    def test_preview_and_apply_with_unsets(self, web) -> None:
        """删除键与修改可以在同一次预览/应用里（`config unset` 的 Web 形态）。"""
        import tomllib

        make_fake_frps(web.ctx.inst.bin_dir)
        client = Client(web)
        client.login()

        status, preview, _ = client.call(
            "/api/config/preview",
            method="POST",
            body={"changes": [["bindPort", "18030"]], "unsets": ["webServer.port"]},
        )
        assert status == 200, preview
        deleted = [item for item in preview["keys"] if item["deleted"]]
        assert [item["key"] for item in deleted] == ["webServer.port"]
        assert deleted[0]["after"] is None

        status, applied, _ = client.call(
            "/api/config/apply", method="POST", body={"preview_id": preview["preview_id"]}
        )
        assert status == 200, applied
        assert applied["applied"] is True
        parsed = tomllib.loads(web.ctx.inst.config.read_text("utf-8"))
        assert parsed["bindPort"] == 18030
        assert "port" not in parsed["webServer"], "删除的键仍在配置里"

    def test_preview_requires_something_to_change(self, web) -> None:
        """changes 与 unsets 都空 → 400（空事务没有意义）。"""
        client = Client(web)
        client.login()
        for body in ({"changes": []}, {"unsets": []}, {}):
            status, payload, _ = client.call("/api/config/preview", method="POST", body=body)
            assert status == 400, (body, status, payload)

    def test_preview_rejects_conflicting_set_and_unset(self, web) -> None:
        """同一键既赋值又删除 → 400，且线上文件零影响。"""
        client = Client(web)
        client.login()
        status, payload, _ = client.call(
            "/api/config/preview",
            method="POST",
            body={"changes": [["bindPort", "18040"]], "unsets": ["bindPort"]},
        )
        assert status == 400
        assert "同时赋值与删除" in payload["error"]
        assert "bindPort = 17000" in web.ctx.inst.config.read_text("utf-8")


# ---------------------------------------------------------------------------
# 预览存储（TTL 与上限）
# ---------------------------------------------------------------------------


class TestPreviewStore:
    def test_previews_are_capped_and_expire(self) -> None:
        """预览条目：TTL 过期清理 + 超过上限丢弃最旧（防已登录用户堆内存）。"""
        from frpsctl.web import AuthManager
        from frpsctl.web.api import MAX_PENDING_PREVIEWS, WebContext, _Preview

        clock = {"t": 1000.0}
        ctx = WebContext(inst=object(), auth=AuthManager("pw"), clock=lambda: clock["t"])  # type: ignore[arg-type]
        for index in range(MAX_PENDING_PREVIEWS + 5):
            clock["t"] += 0.1
            ctx.previews[f"p{index}"] = _Preview("x", (), clock["t"])

        ctx.prune_previews()
        assert len(ctx.previews) == MAX_PENDING_PREVIEWS
        assert "p0" not in ctx.previews, "最旧的预览应被丢弃"

        clock["t"] += 601
        ctx.prune_previews()
        assert ctx.previews == {}


# ---------------------------------------------------------------------------
# 服务生命周期
# ---------------------------------------------------------------------------


class TestWebServerLifecycle:
    def test_port_conflict_raises(self, web_ctx) -> None:
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        server = WebServer(web_ctx, WebSettings(bind=f"127.0.0.1:{port}"))
        try:
            with pytest.raises(OSError):
                server.start()
        finally:
            blocker.close()

    def test_start_stop_idempotent(self, web_ctx) -> None:
        server = WebServer(web_ctx, WebSettings(bind="127.0.0.1:0"))
        server.start()
        assert server.address[1] > 0
        server.stop()
        server.stop()  # 幂等

    def test_url_shows_loopback_for_wildcard_bind(self, web_ctx) -> None:
        server = WebServer(web_ctx, WebSettings(bind="0.0.0.0:0"))
        server.start()
        try:
            assert server.url().startswith("http://127.0.0.1:")
        finally:
            server.stop()

    def test_missing_static_index_reports_500(self, web_ctx, tmp_path: Path) -> None:
        server = WebServer(
            web_ctx,
            WebSettings(bind="127.0.0.1:0"),
            static_index=tmp_path / "missing.html",
        )
        server.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.address[1]}/", timeout=5) as resp:  # noqa: S310
                assert resp.status == 200
        except urllib.error.HTTPError as exc:
            assert exc.code == 500
            assert "前端资源缺失" in exc.read().decode("utf-8")
        finally:
            server.stop()
