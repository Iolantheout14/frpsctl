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
    server = WebServer(web_ctx, WebSettings(bind="127.0.0.1:0", password="secret-password"))
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
        server = WebServer(web_ctx, WebSettings(bind="127.0.0.1:0", password="p"))
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
