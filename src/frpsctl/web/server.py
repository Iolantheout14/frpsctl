"""Web 管理台的 HTTP 服务（设计文档 §18.2）。

标准库 `ThreadingHTTPServer`（与插件服务同栈）：零依赖、故障面小。职责边界：

- **server**：HTTP 细节——静态文件、Cookie、CSRF 强制、安全响应头、body 上限、
  优雅退出（SIGTERM → KeyboardInterrupt，与 `plugin/server.py` 同一模式）；
- **api**：业务分发（`dispatch`），只调 `core/`。

**认证边界**：除 `POST /api/login` 与登录页（`GET /`）外，一切 API 都要求有效
会话；一切 POST（除 login）都要求 `X-CSRF-Token` 头。

**CSP 说明**：单文件前端的内联样式与脚本需要 `'unsafe-inline'`；但
`default-src 'none'` + `connect-src 'self'` 仍封死了"加载外部资源/外发数据"
的通道——XSS 面只剩我们自己那一个文件。
"""

from __future__ import annotations

import json
import signal
import threading
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..core.healthcheck import parse_bind
from .api import WebContext, dispatch
from .auth import SESSION_COOKIE

__all__ = ["WebServer", "WebSettings", "STATIC_INDEX"]

#: 单文件前端（package data，随 wheel 分发）。
STATIC_INDEX = Path(__file__).parent / "static" / "index.html"

#: 请求体上限：管理台的请求都很小，限制它防止有人拿它当上传口。
MAX_BODY_BYTES = 1 << 20  # 1 MiB

#: 安全响应头（所有响应都带）。
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

_INDEX_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'; img-src data:"
)


@dataclass(frozen=True)
class WebSettings:
    bind: str = "127.0.0.1:8787"
    #: 是否打访问日志（默认关闭：每请求两行太吵；排查时打开）。
    access_log: bool = False
    #: 在反向代理后运行时，用 `X-Forwarded-For` 的**最后一跳**作为登录限速来源。
    #: **默认关闭**：不开启时该头完全不被读取——伪造它既不能绕开限速，也不能
    #: 制造新来源。开启的前提是"前面确实有一层会重写该头的可信代理"。
    trusted_proxy: bool = False

    @property
    def host(self) -> str:
        return parse_bind(self.bind, default_port=8787)[0]

    @property
    def port(self) -> int:
        return parse_bind(self.bind, default_port=8787)[1]


def _make_handler(ctx: WebContext, settings: WebSettings, static_index: Path):
    """构造请求处理器（闭包传状态，避免类属性在多次启动间串味）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "frpsctl-web/0.1"
        protocol_version = "HTTP/1.1"
        #: keep-alive 连接的空闲超时（同插件服务：防止线程停在 readline）。
        timeout = 30

        # --- 入口 ------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._send_index()
                return
            if parsed.path == "/favicon.ico":
                # 页面内嵌 data URI 图标（CSP 零外部资源）；这一条是给仍会请求
                # 站点图标的工具/旧浏览器收尾的——204 比 404 JSON 干净。
                self._send_no_content()
                return
            if not parsed.path.startswith("/api/"):
                self._send_json(404, {"error": "not found"})
                return
            session = self._session()
            if parsed.path == "/api/session":
                # 会话状态查询：刷新页面后 Cookie 还在、但前端内存里的 CSRF
                # 丢了——用它恢复（否则刷新后的所有变更操作都会 403）。
                if session is None:
                    self._send_json(401, {"error": "未登录"})
                    return
                self._send_json(200, {"csrf": session.csrf, "ttl": ctx.auth.session_ttl})
                return
            status, payload = dispatch(
                ctx,
                method="GET",
                path=parsed.path,
                query={key: values[-1] for key, values in parse_qs(parsed.query).items()},
                body={},
                authenticated=session is not None,
            )
            self._send_json(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self._send_json(404, {"error": "not found"})
                return
            payload = self._read_body()
            if payload is None:
                # 读不出/超限时必须断开连接（HTTP/1.1 下未排空的请求体会串味）
                self.close_connection = True
                self._send_json(400, {"error": "请求体不是合法 JSON"})
                return

            # 登录/登出由 server 直接处理（涉及 Set-Cookie 的 HTTP 细节）
            if parsed.path == "/api/login":
                self._handle_login(payload)
                return
            session = self._session()
            if session is None:
                self._send_json(401, {"error": "未登录"})
                return
            if parsed.path == "/api/logout":
                if not ctx.auth.check_csrf(session, self.headers.get("X-CSRF-Token")):
                    self._send_json(403, {"error": "CSRF 校验失败"})
                    return
                ctx.auth.logout(session.token)
                self._send_json(
                    200,
                    {"logged_out": True},
                    extra_headers={
                        "Set-Cookie": f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
                    },
                )
                return
            if not ctx.auth.check_csrf(session, self.headers.get("X-CSRF-Token")):
                self._send_json(403, {"error": "CSRF 校验失败", "hint": "刷新页面后重试"})
                return

            status, body = dispatch(
                ctx,
                method="POST",
                path=parsed.path,
                query={},
                body=payload,
                authenticated=True,
            )
            self._send_json(status, body)

        # --- 认证细节 --------------------------------------------------

        def _handle_login(self, payload: dict[str, Any]) -> None:
            session = ctx.auth.login(
                str(payload.get("password") or ""),
                source=self._login_source(),
            )
            if session is None:
                # 口令错误与被限速的响应**完全一致**（不给爆破者信号）
                self._send_json(401, {"error": "口令错误", "hint": ""})
                return
            max_age = int(session.expires_at - ctx.clock())
            cookie = (
                f"{SESSION_COOKIE}={session.token}; HttpOnly; SameSite=Strict; "
                f"Path=/; Max-Age={max_age}"
            )
            self._send_json(
                200,
                {"csrf": session.csrf, "ttl": ctx.auth.session_ttl},
                extra_headers={"Set-Cookie": cookie},
            )

        def _session(self):
            raw = self.headers.get("Cookie", "")
            if not raw:
                return None
            cookie = SimpleCookie()
            try:
                cookie.load(raw)
            except Exception:  # noqa: BLE001 - 畸形 Cookie 视为未登录
                return None
            morsel = cookie.get(SESSION_COOKIE)
            return ctx.auth.check_session(morsel.value if morsel is not None else None)

        def _login_source(self) -> str:
            """登录限速的来源标识。

            **为什么只取 XFF 的最后一跳**：该头是链式的，最左端可被客户端伪造；
            最后一跳是最靠近我们的代理写入的——反代的两种标准写法
            （`$proxy_add_x_forwarded_for` 追加、`$remote_addr` 覆盖）都保证
            它就是真实来源。默认（trusted_proxy=False）**完全不读该头**。
            """
            if settings.trusted_proxy:
                forwarded = self.headers.get("X-Forwarded-For", "")
                hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
                if hops:
                    return hops[-1]
            return f"{self.client_address[0]}"

        # --- 请求体 ----------------------------------------------------

        def _read_body(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0:
                return {}
            if length > MAX_BODY_BYTES:
                self.close_connection = True
                return None
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return data if isinstance(data, dict) else None

        # --- 输出 ------------------------------------------------------

        def _send_json(
            self, status: int, payload: dict[str, Any], *, extra_headers: dict | None = None
        ) -> None:
            # default=str：配置里可能有 TOML datetime 这类非原生类型
            # （`json.dumps` 直接抛 TypeError 会让整个配置页连接被断开）。
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for key, value in _SECURITY_HEADERS.items():
                    self.send_header(key, value)
                for key, value in (extra_headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self._note("客户端在响应写出前断开连接")

        def _send_no_content(self) -> None:
            """204：没有响应体（favicon 等"无需内容"的请求）。"""
            try:
                self.send_response(204)
                for key, value in _SECURITY_HEADERS.items():
                    self.send_header(key, value)
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                self._note("客户端在响应写出前断开连接")

        def _send_index(self) -> None:
            try:
                html = static_index.read_bytes()
            except OSError:
                self._send_json(500, {"error": "前端资源缺失（static/index.html）"})
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                for key, value in _SECURITY_HEADERS.items():
                    self.send_header(key, value)
                self.send_header("Content-Security-Policy", _INDEX_CSP)
                self.end_headers()
                self.wfile.write(html)
            except (BrokenPipeError, ConnectionResetError):
                self._note("客户端在页面加载中断开连接")

        def _note(self, message: str) -> None:
            import sys

            sys.stderr.write(f"[web] {message}\n")
            sys.stderr.flush()

        def log_message(self, *args: object) -> None:
            """默认访问日志太吵（每请求两行）；只在诊断需要时打开。"""
            if settings.access_log:
                super().log_message(*args)  # type: ignore[arg-type]

    return Handler


class WebServer:
    """可启动/可停止的 Web 管理台。测试与 CLI 共用。"""

    def __init__(
        self,
        ctx: WebContext,
        settings: WebSettings,
        *,
        static_index: Path | None = None,
    ) -> None:
        self.ctx = ctx
        self.settings = settings
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._static = static_index

    # --- 生命周期 ------------------------------------------------------

    def start(self) -> None:
        """绑定并开始服务。绑定失败（端口占用）原样抛 `OSError`。"""
        handler = _make_handler(self.ctx, self.settings, self._static or STATIC_INDEX)
        self._httpd = ThreadingHTTPServer((self.settings.host, self.settings.port), handler)
        self._httpd.daemon_threads = True
        # 未 accept 的连接队列上限（backlog）。管理台不面向高并发，设一个小值
        # 让过载时的行为可预期（多出的连接被内核拒绝，而不是无限排队）。
        # 注意这是**唯一**的并发护栏：请求线程数没有上限（单机管理工具，
        # 见 README"已知边界"）。
        self._httpd.request_queue_size = 64
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="frpsctl-web", daemon=True
        )
        self._thread.start()

    def serve_forever(self) -> None:
        """前台阻塞运行（CLI 用）。SIGTERM（systemd stop）→ 优雅退出。"""
        if self._httpd is None:
            self.start()
        assert self._httpd is not None

        previous = signal.getsignal(signal.SIGTERM)
        try:
            signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
            self._httpd.serve_forever()
        finally:
            signal.signal(signal.SIGTERM, previous)
            self.stop()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def close(self) -> None:
        self.stop()

    def __enter__(self) -> WebServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- 观测 ----------------------------------------------------------

    @property
    def address(self) -> tuple[str, int]:
        if self._httpd is None:
            return (self.settings.host, self.settings.port)
        host, port = self._httpd.server_address[:2]
        return (str(host), int(port))

    def url(self) -> str:
        host, port = self.address
        shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        return f"http://{shown}:{port}/"


def _raise_keyboard_interrupt(_signum: int, _frame: object) -> None:
    """把 SIGTERM 转成 KeyboardInterrupt（与插件服务同一模式）。"""
    raise KeyboardInterrupt
