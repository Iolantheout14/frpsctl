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
        """单个代理的流量查询失败不拖垮整张趋势图（记为空曲线）。"""
        from frpsctl.core.admin import PageResult, V2Proxy
        from frpsctl.errors import AdminUnreachable

        class _FakeAdmin:
            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

            def page_proxies(self, **_kwargs):
                items = [V2Proxy(name="bad", type="tcp"), V2Proxy(name="good", type="tcp")]
                return PageResult(items=items, total=2)

            def proxy_traffic(self, name: str):
                if name == "bad":
                    raise AdminUnreachable("boom")
                return [{"date": "2026-09-17", "in": 1, "out": 2}]

        monkeypatch.setattr("frpsctl.web.api._admin", lambda _ctx: _FakeAdmin())
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/traffic")
        assert status == 200
        # 汇总只包含 good 的数据（bad 记空曲线，不拖垮整体）
        assert payload["days"] == [{"date": "2026-09-17", "in": 1, "out": 2}]
        assert payload["proxies"] == 2
        # 新形状：明细不再随汇总下发（按需走 /api/traffic/{name}）
        assert "proxies_history" not in payload
        status, one, _ = client.call("/api/traffic/good")
        assert status == 200
        assert one["history"][0]["in"] == 1
        assert one["total"] == {"in": 1, "out": 2}

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

            def page_proxies(self, **_kwargs):
                from frpsctl.core.admin import PageResult

                items = [V2Proxy(name=f"p{i}", type="tcp") for i in range(TRAFFIC_MAX_PROXIES + 5)]
                return PageResult(items=items, total=len(items))

            def proxy_traffic(self, name: str):
                return []

        monkeypatch.setattr("frpsctl.web.api._admin", lambda _ctx: _FakeAdmin())
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/traffic")
        assert status == 200
        assert payload["truncated"] is True
        assert payload["total"] == TRAFFIC_MAX_PROXIES + 5
        assert payload["proxies"] == TRAFFIC_MAX_PROXIES

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



# ---------------------------------------------------------------------------
# v0.2.6：doctor / audit / traffic 缓存 / 动作成功路径 / 参数边界
# ---------------------------------------------------------------------------


class TestDoctorEndpoint:
    def test_doctor_endpoint_reports_counts_and_findings(self, web) -> None:
        """只读体检：counts 三键 + findings 形状（与 CLI `doctor --json` 同源）。"""
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/doctor")
        assert status == 200, payload
        assert set(payload["counts"]) == {"error", "warn", "info"}
        # 实例目录里没有 frps 二进制 → 至少一条 ERROR（二进制不存在）
        assert payload["counts"]["error"] >= 1
        assert payload["ok"] is False
        assert payload["findings"], "没有任何发现项"
        assert all({"check", "severity", "message", "hint"} <= set(f) for f in payload["findings"])


class TestAuditEndpoint:
    def test_audit_endpoint_reads_policy_and_log(self, web) -> None:
        """审计视图：路径按"相对策略文件目录"解析，stats/tail 来自真实 JSONL。"""
        import json as _json

        inst = web.ctx.inst
        (inst.dir / "plugin-policy.json").write_text(_json.dumps({"users": {}}), "utf-8")
        audit_path = inst.dir / "plugin-audit.jsonl"
        with open(audit_path, "w", encoding="utf-8") as handle:
            handle.write(
                _json.dumps(
                    {
                        "at_unix": 100.0,
                        "at": "2026-09-17T10:00:00",
                        "op": "Login",
                        "user": "alice",
                        "decision": "deny",
                        "reason": "未知用户",
                    }
                )
                + "\n"
            )
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/audit")
        assert status == 200, payload
        assert payload["available"] is True and payload["enabled"] is True
        assert payload["path"] == str(audit_path), "审计路径没有相对策略文件目录解析"
        assert payload["stats"]["deny"] == 1
        assert payload["tail"][0]["user"] == "alice"

    def test_audit_endpoint_without_policy_is_available_false(self, web) -> None:
        """策略缺失不是 4xx/5xx：视图如实说明"为什么看不到审计"。"""
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/audit")
        assert status == 200
        assert payload["available"] is False and payload["reason"]
        assert payload["stats"] is None and payload["tail"] == []


class TestTrafficEndpointCache:
    def test_repeated_calls_hit_server_side_cache(self, web, monkeypatch) -> None:
        """30s 服务端缓存：浏览器 5 秒轮询不会每轮重放 dashboard 查询。"""
        from frpsctl.core.admin import PageResult, V2Proxy

        calls = {"pages": 0}

        class _FakeAdmin:
            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

            def page_proxies(self, **_kwargs):
                calls["pages"] += 1
                return PageResult(items=[V2Proxy(name="p1", type="tcp")], total=1)

            def proxy_traffic(self, name: str):
                return [{"date": "2026-09-17", "in": 1, "out": 1}]

        clock = {"t": 1000.0}
        monkeypatch.setattr(web.ctx, "clock", lambda: clock["t"])
        monkeypatch.setattr("frpsctl.web.api._admin", lambda _ctx: _FakeAdmin())
        client = Client(web)
        client.login()
        assert client.call("/api/traffic")[0] == 200
        assert client.call("/api/traffic")[0] == 200
        assert calls["pages"] == 1, "第二次请求没有走服务端缓存"
        clock["t"] += 31  # 超过 TTL
        assert client.call("/api/traffic")[0] == 200
        assert calls["pages"] == 2, "缓存过期后没有重新查询"


class TestLogsParameterBounds:
    def test_lines_are_clamped_and_bad_values_fall_back(self, web) -> None:
        client = Client(web)
        client.login()
        log_file = web.ctx.inst.dir / "frps.log"
        log_file.write_text("".join(f"line-{i}\n" for i in range(3000)), "utf-8")
        cases = (("lines=abc", 200), ("lines=99999", 2000), ("lines=0", 1), ("lines=-5", 1))
        for query, expected in cases:
            status, payload, _ = client.call(f"/api/logs?{query}")
            assert status == 200, query
            assert len(payload["lines"]) == expected, (query, len(payload["lines"]))

    def test_unnormalized_paths_do_not_serve_files(self, web) -> None:
        """未规范化的路径片段不会穿越到文件系统（配置文件内容绝不出现在响应里）。"""
        import http.client

        client = Client(web)
        client.login()
        # 服务端每响应关闭连接（v0.3.0：worker 槽位按请求占用）——每个请求
        # 各开一条连接，这也正是浏览器的行为。
        def _raw(path: str, *, cookie: str | None) -> tuple[int, bytes]:
            conn = http.client.HTTPConnection("127.0.0.1", web.address[1], timeout=5)
            try:
                headers = {"Cookie": cookie} if cookie else {}
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                return resp.status, resp.read()
            finally:
                conn.close()

        status, body = _raw("/api/../frps.toml", cookie=client.cookie)
        assert status in (401, 404), status
        assert b"test-token" not in body

        status, body = _raw("/../frps.toml", cookie=None)
        assert status == 404, status
        assert b"test-token" not in body


class TestActionSuccessPaths:
    """`/api/actions/restart|rollback` 的成功路径（v0.2.6 补齐）。

    此前 Web 测试只覆盖了拒绝路径（409/400）——"动作真的能做到"没有得到证明。
    """

    def test_restart_success_path(self, web) -> None:
        make_fake_frps(web.ctx.inst.bin_dir)
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/actions/start", method="POST", body={})
        assert status == 200, payload
        try:
            status, payload, _ = client.call("/api/actions/restart", method="POST", body={})
            assert status == 200, payload
            assert payload["pid"]
            assert payload["healthy"] is True
        finally:
            client.call("/api/actions/stop", method="POST", body={})

    def test_rollback_success_path(self, web) -> None:
        """回滚：快照 → 改配置 → 回滚 → 文件恢复且响应含打码 diff。"""
        from frpsctl.core.transaction import config_snapshot

        make_fake_frps(web.ctx.inst.bin_dir)
        inst = web.ctx.inst
        config_snapshot(inst, action="set bindPort")
        original = inst.config.read_text("utf-8")
        inst.config.write_text(original.replace("bindPort = 17000", "bindPort = 17001"), "utf-8")

        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/actions/rollback", method="POST", body={"steps": 1})
        assert status == 200, payload
        assert "bindPort = 17000" in inst.config.read_text("utf-8")
        assert payload["diff"]
        # 回滚不泄露机密（diff 打码；快照里带 test-token）
        assert "test-token" not in payload["diff"]


class TestConditionalRequests:
    """ETag/304：只读 GET 允许条件请求，写/错误/会话响应绝不缓存。"""

    def test_etag_roundtrip_and_304(self, web) -> None:
        client = Client(web)
        client.login()
        status, _payload, headers = client.call("/api/status")
        assert status == 200, headers
        etag = headers.get("ETag")
        assert etag, headers
        assert headers.get("Cache-Control") == "no-cache"

        status2, _payload2, headers2 = client.call(
            "/api/status", headers={"If-None-Match": etag}
        )
        assert status2 == 304, (status2, headers2)
        # 304 仍必须带 ETag 与缓存策略（RFC 9110）
        assert headers2.get("ETag") == etag
        assert headers2.get("Cache-Control") == "no-cache"

        status3, _payload3, _ = client.call(
            "/api/status", headers={"If-None-Match": '"bogus"'}
        )
        assert status3 == 200, "ETag 不匹配时必须重新发送完整响应"

    def test_unauthenticated_errors_are_not_cacheable(self, web) -> None:
        client = Client(web)  # 未登录
        status, _payload, headers = client.call("/api/status")
        assert status == 401
        assert "ETag" not in headers
        assert headers.get("Cache-Control") == "no-store"

    def test_post_responses_are_not_cacheable(self, web) -> None:
        client = Client(web)
        client.login()
        # dashboard 未启用（port=0）：动作会失败，但"不缓存"与状态无关
        _status, _payload, headers = client.call("/api/actions/prune", method="POST", body={})
        assert "ETag" not in headers
        assert headers.get("Cache-Control") == "no-store"

    def test_session_payload_is_not_cached(self, web) -> None:
        """`/api/session` 下发 CSRF——绝不进任何缓存。"""
        client = Client(web)
        client.login()
        status, payload, headers = client.call("/api/session")
        assert status == 200 and payload["csrf"]
        assert "ETag" not in headers
        assert headers.get("Cache-Control") == "no-store"


class TestResponseCacheInvalidation:
    """服务端只读缓存：重复请求不打 dashboard；写操作前显式失效。"""

    def test_clients_cache_served_once_and_invalidated(self, web, monkeypatch) -> None:
        from frpsctl.core.admin import PageResult

        calls = {"n": 0}

        class _FakeAdmin:
            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

            def page_clients(self, **_kwargs):
                calls["n"] += 1
                return PageResult(items=[{"key": "c1"}], total=1)

        monkeypatch.setattr("frpsctl.web.api._admin", lambda _ctx: _FakeAdmin())
        client = Client(web)
        client.login()
        assert client.call("/api/clients")[0] == 200
        assert client.call("/api/clients")[0] == 200
        assert calls["n"] == 1, "第二次请求没有走服务端缓存"

        web.ctx.cache.invalidate()
        assert client.call("/api/clients")[0] == 200
        assert calls["n"] == 2, "失效后没有重新查询"

    def test_action_invalidates_before_doing_work(self, web) -> None:
        """action_payload 在做任何事**之前**清缓存：动作失败也不留旧数据。"""
        web.ctx.cache.put("clients", {"stale": True})
        client = Client(web)
        client.login()
        client.call("/api/actions/prune", method="POST", body={})  # 502（dashboard 未启用）
        assert web.ctx.cache.get("clients", 999) is None

    def test_logs_cache_key_includes_line_count(self, web, monkeypatch) -> None:
        from frpsctl.web import api as api_mod

        calls = {"n": 0}
        real_tail = api_mod.tail_lines

        def counting(path, lines):
            calls["n"] += 1
            return real_tail(path, lines)

        monkeypatch.setattr(api_mod, "tail_lines", counting)
        (web.ctx.inst.dir / "frps.log").write_text("a\nb\n", "utf-8")
        client = Client(web)
        client.login()
        assert client.call("/api/logs?lines=100")[0] == 200
        assert client.call("/api/logs?lines=100")[0] == 200
        assert calls["n"] == 1, "同参数第二次请求没有走缓存"
        assert client.call("/api/logs?lines=200")[0] == 200
        assert calls["n"] == 2, "不同行数是不同的缓存键"


class TestWebOperationAudit:
    """Web 操作审计：动作与登录留痕、来源/会话指纹、读接口 scope。"""

    @pytest.fixture(autouse=True)
    def _reset_login_limiter(self):
        """失败登录审计限速是模块级状态：每个用例前后都要清（测试隔离）。"""
        from frpsctl.web import server as web_server

        web_server._login_fail_limiter._last.clear()
        yield
        web_server._login_fail_limiter._last.clear()

    def _summary(self, web):
        from frpsctl.core import web_audit

        return web_audit.summarize(web_audit.resolve_path(web.ctx.inst))

    def test_action_success_and_failure_are_recorded(self, web, monkeypatch) -> None:
        from frpsctl.core.admin import PruneOutcome

        client = Client(web)
        client.login()

        # 失败：dashboard 未启用（port=0）→ 502，但审计必须已记录 error
        # （登录事件也在审计里，因此按动作计数而不是 total）
        client.call("/api/actions/prune", method="POST", body={})
        summary = self._summary(web)
        assert summary.by_action.get("prune") == 1
        assert summary.error == 1

        class _Admin:
            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

            def prune_offline_proxies(self):
                return PruneOutcome(before=2, cleared=2)

        monkeypatch.setattr("frpsctl.web.api._admin", lambda _ctx: _Admin())
        status, _payload, _ = client.call("/api/actions/prune", method="POST", body={})
        assert status == 200
        summary = self._summary(web)
        assert summary.by_action.get("prune") == 2, "成功动作没有留痕"
        assert summary.error == 1, "此前那次失败的记录不应消失"

    def test_login_events_are_recorded_with_fingerprint(self, web) -> None:
        from frpsctl.core import auditlog, web_audit

        client = Client(web)
        client.login()
        bad = Client(web)
        bad.login("wrong")

        path = web_audit.resolve_path(web.ctx.inst)
        records = auditlog.read_tail(path, 10).records
        actions = {r["action"] for r in records}
        assert actions == {"login"}
        results = sorted(r["result"] for r in records)
        assert results == ["error", "ok"]
        ok_record = next(r for r in records if r["result"] == "ok")
        assert len(ok_record["session_id"]) == 12, "会话只存指纹（不落 token）"

    def test_login_failure_audit_is_rate_limited(self, web) -> None:
        """同来源 60 秒内的重复失败只记一条（防审计文件被爆破刷爆）。"""
        from frpsctl.web import server as web_server

        web_server._login_fail_limiter._last.clear()
        for _ in range(4):
            Client(web).login("wrong")
        summary = self._summary(web)
        assert summary.error == 1, f"失败登录被逐条记录：{summary}"

    def test_audit_scope_web_payload(self, web) -> None:
        client = Client(web)
        client.login()
        client.call("/api/actions/prune", method="POST", body={})
        status, payload, _ = client.call("/api/audit?scope=web")
        assert status == 200, payload
        assert payload["scope"] == "web"
        assert payload["available"] is True
        assert payload["stats"]["error"] == 1
        # 登录 + 失败 prune 各一条；tail 与 stats 同口径（此前是恒真的弱断言）
        assert payload["stats"]["total"] == 2
        assert {r["action"] for r in payload["tail"]} == {"login", "prune"}
        assert len(payload["tail"]) == 2

    def test_audit_scope_web_without_file(self, web) -> None:
        client = Client(web)
        client.login()
        # 删掉审计文件（登录已写）→ available=false 且给理由
        from frpsctl.core import web_audit

        web_audit.resolve_path(web.ctx.inst).unlink()
        status, payload, _ = client.call("/api/audit?scope=web")
        assert status == 200
        assert payload["available"] is False and payload["reason"]

    def test_audit_invalid_scope_is_400(self, web) -> None:
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/api/audit?scope=bogus")
        assert status == 400 and "审计范围" in payload["error"]


class TestCspNonce:
    """CSP nonce 化：每请求 nonce、无 unsafe-inline、无内联 style 属性。"""

    def test_index_injects_per_request_nonce(self, web) -> None:
        import re

        base = f"http://127.0.0.1:{web.address[1]}"
        with urllib.request.urlopen(base + "/", timeout=5) as resp:  # noqa: S310
            html = resp.read().decode("utf-8")
            csp = resp.headers["Content-Security-Policy"]

        assert "unsafe-inline" not in csp, csp
        match = re.search(r"script-src 'nonce-([^']+)'", csp)
        assert match, csp
        nonce = match.group(1)
        assert f'style-src \'nonce-{nonce}\'' in csp
        # 两个 <script> + 一个 <style> 都必须带同一个 nonce
        assert html.count(f'nonce="{nonce}"') == 3
        assert "__CSP_NONCE__" not in html
        # 无内联 style 属性（CSP 去掉 unsafe-inline 的前提）
        assert not re.search(r"\sstyle=\"", html), "还有内联 style 属性"

        # 每次请求的 nonce 不同（防重放/固定）
        with urllib.request.urlopen(base + "/", timeout=5) as resp2:  # noqa: S310
            csp2 = resp2.headers["Content-Security-Policy"]
        assert nonce not in csp2


class TestMetricsEndpoint:
    def _server(self, web_ctx, **kwargs):
        server = WebServer(web_ctx, WebSettings(bind="127.0.0.1:0", **kwargs))
        server.start()
        return server

    def test_disabled_by_default_404(self, web) -> None:
        client = Client(web)
        client.login()
        status, payload, _ = client.call("/metrics")
        assert status == 404, payload

    def test_basic_failures_are_throttled(self, web_ctx) -> None:
        """H1：Basic auth 失败与登录共用限速——错误口令试满后，正确口令
        也必须被拒（否则 /metrics 是绕开限速的爆破通道）。"""
        import base64

        server = self._server(web_ctx, metrics=True)
        try:
            base = f"http://127.0.0.1:{server.address[1]}"
            bad = base64.b64encode(b"prom:wrong").decode()
            good = base64.b64encode(b"prom:secret-password").decode()

            def _call(token: str) -> int:
                req = urllib.request.Request(  # noqa: S310
                    base + "/metrics", headers={"Authorization": f"Basic {token}"}
                )
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                        return resp.status
                except urllib.error.HTTPError as exc:
                    return exc.code

            for _ in range(5):
                assert _call(bad) == 401
            assert _call(good) == 401, "正确口令在限速窗口内也必须被拒"
            assert _call(bad) == 401
        finally:
            server.stop()

    def test_enabled_requires_basic_auth(self, web_ctx) -> None:
        server = self._server(web_ctx, metrics=True)
        try:
            self._assert_basic_auth(server)
        finally:
            server.stop()

    def _assert_basic_auth(self, server) -> None:
        import base64

        base = f"http://127.0.0.1:{server.address[1]}"

        try:
            urllib.request.urlopen(base + "/metrics", timeout=5)  # noqa: S310
            raise AssertionError("无凭据不应 200")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
            assert exc.headers["WWW-Authenticate"].startswith("Basic")

        token = base64.b64encode(b"prometheus:secret-password").decode()
        req = urllib.request.Request(  # noqa: S310
            base + "/metrics", headers={"Authorization": f"Basic {token}"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            text = resp.read().decode("utf-8")
        assert 'frpsctl_up{instance="test"} 1' in text  # 实例名来自 conftest fixture
        assert "frpsctl_instance_state{" in text
        assert "# TYPE frpsctl_dashboard_clients gauge" not in text  # 未运行：无 dashboard 段

        bad = base64.b64encode(b"prometheus:wrong").decode()
        req_bad = urllib.request.Request(  # noqa: S310
            base + "/metrics", headers={"Authorization": f"Basic {bad}"}
        )
        try:
            urllib.request.urlopen(req_bad, timeout=5)  # noqa: S310
            raise AssertionError("错误口令不应 200")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

        # 已登录会话同样可访问
        client = Client(server)
        assert client.login()[0] == 200
        req_cookie = urllib.request.Request(  # noqa: S310
            base + "/metrics", headers={"Cookie": client.cookie or ""}
        )
        with urllib.request.urlopen(req_cookie, timeout=5) as resp:  # noqa: S310
            assert b"frpsctl_up" in resp.read()


class TestBoundedConcurrency:
    """请求线程有界：worker 用尽时立即 503（不无限排队）。"""

    def test_exhausted_workers_get_503(self, web_ctx) -> None:
        from frpsctl.web.server import _BoundedThreadingHTTPServer

        server = _BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), _NullHandler, max_workers=1
        )
        try:

            class _FakeSock:
                def __init__(self) -> None:
                    self.sent = b""
                    self.closed = False

                def sendall(self, data: bytes) -> None:
                    self.sent += data

                def shutdown(self, _how: int) -> None:  # noqa: ARG002
                    pass

                def close(self) -> None:
                    self.closed = True

            assert server._workers.acquire(blocking=False), "测试自占 worker"
            sock = _FakeSock()
            server.process_request(sock, ("127.0.0.1", 1))
            assert b"503 Service Unavailable" in sock.sent
            assert sock.closed, "超限连接必须被关闭"
        finally:
            server._workers.release()
            server.server_close()


class _NullHandler:
    """占位 handler（`_BoundedThreadingHTTPServer` 只要求可被引用）。"""


class TestFinalReviewGuards:
    """v0.3.0 最终 review 的守卫（连接声明 / TTL 契约 / metrics 来源 / 去重）。"""

    def test_responses_declare_connection_close(self, web) -> None:
        """N9：每响应关闭连接必须**显式声明** `Connection: close`（HTTP/1.1
        客户端据此不误复用）——防重构把 close_connection 改回去。"""
        import http.client

        client = Client(web)
        client.login()
        conn = http.client.HTTPConnection("127.0.0.1", web.address[1], timeout=5)
        try:
            conn.request("GET", "/api/status", headers={"Cookie": client.cookie})
            resp = conn.getresponse()
            resp.read()
            values = [v for k, v in resp.getheaders() if k.lower() == "connection"]
            assert values == ["close"], values
        finally:
            conn.close()

    def test_send_error_path_has_single_connection_header(self, web) -> None:
        """N4：stdlib 的 `send_error`（如 501 未知方法）不重复发 Connection。"""
        import http.client

        client = Client(web)
        client.login()
        conn = http.client.HTTPConnection("127.0.0.1", web.address[1], timeout=5)
        try:
            conn.request("PATCH", "/api/status", headers={"Cookie": client.cookie})
            resp = conn.getresponse()
            resp.read()
            values = [v for k, v in resp.getheaders() if k.lower() == "connection"]
            assert len(values) <= 1, f"Connection 头重复：{values}"
        finally:
            conn.close()

    def test_cache_ttls_cover_polling_interval(self) -> None:
        """N9：列表 TTL 必须 ≥ 浏览器轮询间隔（5s）——2 秒 TTL 等于没有缓存
        （v0.3.0 量化验证的教训），防重构改回。"""
        from frpsctl.web import api as api_mod

        assert api_mod.CACHE_TTL_LISTS >= 5.0
        assert api_mod.TRAFFIC_CACHE_TTL == 30.0
        assert api_mod.CACHE_TTL_LOGS > 0

    def test_metrics_source_matches_login_source_with_trusted_proxy(self, web_ctx) -> None:
        """N1：trusted_proxy 下 `/metrics` 的失败限速必须按同一来源（XFF 最后
        一跳）——否则两条限速桶互不相干，爆破通道重新分裂。"""
        import base64

        server = WebServer(
            web_ctx, WebSettings(bind="127.0.0.1:0", metrics=True, trusted_proxy=True)
        )
        server.start()
        try:
            base = f"http://127.0.0.1:{server.address[1]}"
            bad = base64.b64encode(b"prom:wrong").decode()
            good = base64.b64encode(b"prom:secret-password").decode()

            def _call(token: str, forwarded: str) -> int:
                req = urllib.request.Request(  # noqa: S310
                    base + "/metrics",
                    headers={"Authorization": f"Basic {token}", "X-Forwarded-For": forwarded},
                )
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                        return resp.status
                except urllib.error.HTTPError as exc:
                    return exc.code

            for _ in range(5):
                assert _call(bad, "203.0.113.7") == 401
            assert _call(good, "203.0.113.7") == 401, "同一 XFF 来源应被限速"
            assert _call(good, "203.0.113.8") == 200, "不同来源不应被连带限速"
        finally:
            server.stop()
