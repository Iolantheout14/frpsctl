"""Web 管理台的 HTTP 服务（设计文档 §18.2）。

标准库 `ThreadingHTTPServer`（与插件服务同栈）：零依赖、故障面小。职责边界：

- **server**：HTTP 细节——静态文件、Cookie、CSRF 强制、安全响应头、body 上限、
  优雅退出（SIGTERM → KeyboardInterrupt，与 `plugin/server.py` 同一模式）；
- **api**：业务分发（`dispatch`），只调 `core/`。

**认证边界**：除 `POST /api/login` 与登录页（`GET /`）外，一切 API 都要求有效
会话；一切 POST（除 login）都要求 `X-CSRF-Token` 头。

**CSP 说明**（v0.3.0 起）：脚本/样式只认**每请求 nonce**，无 `'unsafe-inline'`；
配合 `default-src 'none'` + `connect-src 'self'` + `base-uri`/`form-action`/
`frame-ancestors` 限制——前端本就没有注入点，这层把"万一"的出口也封死。

**并发**：`BoundedThreadingHTTPServer`（`core/httpserver.py`，与插件服务共用）
——worker 上限内每请求一线程；超限时 HTTP 503（或在未读请求体上交由内核
TCP 重置，两者对客户端都是明确失败）。见类 docstring 的过载语义说明。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import signal
import threading
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ..core.healthcheck import parse_bind
from ..core.httpserver import BoundedThreadingHTTPServer
from ..core import web_audit
from .api import RequestInfo, WebContext, dispatch
from .auth import SESSION_COOKIE

__all__ = ["STATIC_DIR", "STATIC_INDEX", "WebServer", "WebSettings"]

#: 前端静态资源目录（package data，随 wheel 分发）。
STATIC_DIR = Path(__file__).parent / "static"

#: 单文件入口（shell：HTML 结构 + 主题内联脚本 + 模块引用）。
STATIC_INDEX = STATIC_DIR / "index.html"

#: 静态资源白名单（扩展名 → MIME）。**拒绝一切白名单外的类型**。
_STATIC_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}

#: 单个静态资源的字节上限（前端模块都是几十 KB 级；防误放巨物）。
MAX_STATIC_BYTES = 1 << 20  # 1 MiB


def resolve_static(rel: str) -> Path | None:
    """把 `/static/<rel>` 解析为磁盘文件；越界/类型不符/不存在一律 None。

    防穿越：URL 解码后必须不含 `..` 路径段，且 `resolve()` 后仍位于
    `STATIC_DIR` 之内（symlink 逃逸同样被这层拦住）——静态路由是新增的
    攻击面，宁可多一道检查。
    """
    rel = unquote(rel)
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None
    candidate = (STATIC_DIR / rel).resolve()
    base = STATIC_DIR.resolve()
    if not str(candidate).startswith(str(base) + os.sep):
        return None
    if candidate.suffix not in _STATIC_TYPES or not candidate.is_file():
        return None
    return candidate


#: 请求体上限：管理台的请求都很小，限制它防止有人拿它当上传口。
MAX_BODY_BYTES = 1 << 20  # 1 MiB

#: 安全响应头（所有响应都带）。Cache-Control 不在这里——它按响应类型动态
#: 决定（见 `_send_json` 的 cacheable 参数）。
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

def _index_csp(nonce: str) -> str:
    """单文件前端的 CSP：脚本/样式只认**每请求 nonce**。

    0.3.0 起无 `'unsafe-inline'`：`<script>` / `<style>` 标签与全部内联 style
    属性已改造为 nonce / class（见 index.html 与前端守卫测试）。这层是纵深
    防御——前端本就没有 innerHTML 注入点，但"万一"的出口现在被 CSP 也堵上。
    """
    return (
        "default-src 'none'; "
        f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
        "connect-src 'self'; img-src data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


@dataclass(frozen=True)
class WebSettings:
    bind: str = "127.0.0.1:8787"
    #: 是否打访问日志（默认关闭：每请求两行太吵；排查时打开）。
    access_log: bool = False
    #: 是否暴露 `/metrics`（Prometheus 文本；需 Basic auth，用户名任意、
    #: 口令 = 管理台口令）。默认关闭：没有监控系统时少一个信息出口。
    metrics: bool = False
    #: 请求线程上限：超限的连接立即 503，而不是无限排队（慢 dashboard 下
    #: 请求线程堆积会吃内存；明确的失败比让浏览器转圈诚实）。
    max_workers: int = 32
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
        #: 读请求/空闲超时。管理台每响应关闭连接（见 `_send_json`），这里主要
        #: 防慢速客户端占住 worker（v0.3.0 review 收紧：30 → 5 秒）。
        timeout = 5

        # --- 入口 ------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._send_index()
                return
            if parsed.path.startswith("/static/"):
                self._send_static(parsed.path[len("/static/"):])
                return
            if parsed.path == "/metrics":
                self._handle_metrics()
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
                    self._send_json(401, {"error": "未登录"}, cacheable=False)
                    return
                # 含 CSRF：绝不进缓存（否则共享缓存的另一端能拿到）
                self._send_json(
                    200,
                    {"csrf": session.csrf, "ttl": ctx.auth.session_ttl},
                    cacheable=False,
                )
                return
            if parsed.path == "/api/diagnostics":
                # 诊断导出（v0.3.4 F9）：文本附件下载（配置已打码；日志需自行检查）
                if session is None:
                    self._send_json(401, {"error": "未登录"}, cacheable=False)
                    return
                from .api import diagnostics_text

                self._send_attachment(
                    200,
                    diagnostics_text(ctx),
                    f"frpsctl-diagnostics-{ctx.inst.name}.txt",
                )
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
                self._send_json(400, {"error": "请求体不是合法 JSON"}, cacheable=False)
                return

            # 登录/登出由 server 直接处理（涉及 Set-Cookie 的 HTTP 细节）
            if parsed.path == "/api/login":
                self._handle_login(payload)
                return
            session = self._session()
            if session is None:
                self._send_json(401, {"error": "未登录"}, cacheable=False)
                return
            if parsed.path == "/api/logout":
                if not ctx.auth.check_csrf(session, self.headers.get("X-CSRF-Token")):
                    self._send_json(403, {"error": "CSRF 校验失败"}, cacheable=False)
                    return
                ctx.auth.logout(session.token)
                self._send_json(
                    200,
                    {"logged_out": True},
                    extra_headers={
                        "Set-Cookie": f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
                    },
                    cacheable=False,
                )
                return
            if not ctx.auth.check_csrf(session, self.headers.get("X-CSRF-Token")):
                self._send_json(403, {"error": "CSRF 校验失败", "hint": "刷新页面后重试"}, cacheable=False)
                return

            status, body = dispatch(
                ctx,
                method="POST",
                path=parsed.path,
                query={},
                body=payload,
                authenticated=True,
                request_info=RequestInfo(
                    source=self._login_source(),
                    session_id=web_audit.session_fingerprint(session.token),
                ),
            )
            self._send_json(status, body, cacheable=False)

        # --- 认证细节 --------------------------------------------------

        def _handle_login(self, payload: dict[str, Any]) -> None:
            source = self._login_source()
            session = ctx.auth.login(str(payload.get("password") or ""), source=source)
            if session is None:
                # 失败留痕（含被限速的请求——限速器让"每来源每分钟记一条"成立，
                # 攻击者的海量重试不会把审计文件刷爆，但第一次一定看得见）。
                if ctx.login_audit.allow(source) and not web_audit.record(
                    ctx.inst, action="login", result="error", source=source
                ):
                    self._note("⚠ Web 操作审计写入失败（登录失败本身已拒绝）：检查实例目录权限与磁盘空间")
                # 口令错误与被限速的响应**完全一致**（不给爆破者信号）
                self._send_json(401, {"error": "口令错误", "hint": ""}, cacheable=False)
                return
            if not web_audit.record(
                ctx.inst,
                action="login",
                result="ok",
                source=source,
                session_id=web_audit.session_fingerprint(session.token),
            ):
                self._note("⚠ Web 操作审计写入失败（登录本身已成功）：检查实例目录权限与磁盘空间")
            max_age = int(session.expires_at - ctx.clock())
            cookie = (
                f"{SESSION_COOKIE}={session.token}; HttpOnly; SameSite=Strict; "
                f"Path=/; Max-Age={max_age}"
            )
            self._send_json(
                200,
                {"csrf": session.csrf, "ttl": ctx.auth.session_ttl},
                extra_headers={"Set-Cookie": cookie},
                cacheable=False,
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

        _connection_header_sent = False

        def send_header(self, keyword, value) -> None:  # noqa: ANN001
            if keyword.lower() == "connection":
                self._connection_header_sent = True
            super().send_header(keyword, value)

        def end_headers(self) -> None:
            """按 `close_connection` 显式声明 `Connection: close`。

            我们每响应关闭连接（见 `_send_json`）——但裸 `BaseHTTPRequestHandler`
            不会自动添加该响应头，HTTP/1.1 客户端会误以为连接可复用（v0.3.0
            review 冒烟实测）。`send_error` 等 stdlib 路径会自己发该头，这里
            做去重（v0.3.0 最终 review N4：实测重复两个头）。
            """
            if self.close_connection and not self._connection_header_sent:
                self.send_header("Connection", "close")
            super().end_headers()

        def _send_json(
            self,
            status: int,
            payload: dict[str, Any],
            *,
            extra_headers: dict | None = None,
            cacheable: bool = True,
        ) -> None:
            """JSON 响应。`cacheable=True`（只读 GET）时带 ETag 并允许条件请求：
            `Cache-Control: no-cache` 让浏览器存下但每次验证，命中 `If-None-Match`
            直接回 304（零 body）——5 秒轮询在"数据没变"时省掉响应体与序列化。

            写响应 / 错误 / 登录（`cacheable=False`）一律 `no-store`：变更结果与
            鉴权失败不能被任何缓存留存。
            """
            # v0.3.0 review：每个响应后关闭连接——worker 槽位按**请求**占用而
            # 非按 keep-alive 连接。少数标签页的常驻连接因此不会把并发额度
            # 占满（回环上重建连接的开销可忽略）。
            self.close_connection = True
            # default=str：配置里可能有 TOML datetime 这类非原生类型
            # （`json.dumps` 直接抛 TypeError 会让整个配置页连接被断开）。
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            etag = f'"{hashlib.sha256(body).hexdigest()[:16]}"'
            if cacheable and status == 200:
                if self.headers.get("If-None-Match", "").strip() == etag:
                    self._send_not_modified(etag)
                    return
                cache_headers = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Cookie"}
            else:
                cache_headers = {"Cache-Control": "no-store"}
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for key, value in {**_SECURITY_HEADERS, **cache_headers, **(extra_headers or {})}.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self._note("客户端在响应写出前断开连接")

        def _send_not_modified(self, etag: str) -> None:
            """304：无 body，但 ETag / 缓存策略 / 安全头必须保持（RFC 9110）。"""
            try:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Vary", "Cookie")
                for key, value in _SECURITY_HEADERS.items():
                    self.send_header(key, value)
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                self._note("客户端在响应写出前断开连接")

        def _send_no_content(self) -> None:
            """204：没有响应体（favicon 等"无需内容"的请求）。"""
            self.close_connection = True
            try:
                self.send_response(204)
                self.send_header("Cache-Control", "no-store")
                for key, value in _SECURITY_HEADERS.items():
                    self.send_header(key, value)
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                self._note("客户端在响应写出前断开连接")

        def _send_static(self, rel: str) -> None:
            """下发静态资源（白名单扩展名 + 防穿越 + ETag 条件请求）。

            `no-cache` + ETag：每次验证、命中 304 零 body。不设 immutable，
            也不做版本查询串——回环场景没有传输成本，而"升级后浏览器混用
            旧模块"是不可接受的故障面（v0.3.4 明确决策，记账于 §27）。
            """
            path = resolve_static(rel)
            if path is None:
                self._send_json(404, {"error": "not found"}, cacheable=False)
                return
            try:
                body = path.read_bytes()
            except OSError:
                self._send_json(404, {"error": "not found"}, cacheable=False)
                return
            if len(body) > MAX_STATIC_BYTES:
                self._send_json(500, {"error": "静态资源超出上限"}, cacheable=False)
                return
            self.close_connection = True
            etag = f'"{hashlib.sha256(body).hexdigest()[:16]}"'
            try:
                if self.headers.get("If-None-Match", "").strip() == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", "no-cache")
                    for key, value in _SECURITY_HEADERS.items():
                        self.send_header(key, value)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", _STATIC_TYPES[path.suffix])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                for key, value in _SECURITY_HEADERS.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self._note("客户端在响应写出前断开连接")

        def _send_index(self) -> None:
            """下发前端 shell 并注入**每请求 nonce**（CSP 无 unsafe-inline）。

            shell 里的内联主题脚本与全部 `<link>`/`<script src>` 都带
            `nonce="__CSP_NONCE__"` 占位，这里一次性替换为同一个随机值——
            CSP 保持 nonce-only（不放宽到 `'self'`）。
            """
            self.close_connection = True
            try:
                template = static_index.read_text("utf-8")
            except OSError:
                self._send_json(500, {"error": "前端资源缺失（static/index.html）"}, cacheable=False)
                return
            nonce = secrets.token_urlsafe(16)
            html = template.replace("__CSP_NONCE__", nonce).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                for key, value in _SECURITY_HEADERS.items():
                    self.send_header(key, value)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", _index_csp(nonce))
                self.end_headers()
                self.wfile.write(html)
            except (BrokenPipeError, ConnectionResetError):
                self._note("客户端在页面加载中断开连接")

        def _send_attachment(self, status: int, text: str, filename: str) -> None:
            """下载型文本响应（诊断导出）：带 Content-Disposition，绝不缓存。

            文件名做头部注入消毒（实例名已受字符集校验，这里是纵深防御）。
            """
            self.close_connection = True
            filename = filename.replace('"', "_").replace("\r", "").replace("\n", "")
            body = text.encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                for key, value in _SECURITY_HEADERS.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self._note("客户端在响应写出前断开连接")

        def _send_text(self, status: int, text: str, *, content_type: str) -> None:
            self.close_connection = True
            body = text.encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for key, value in _SECURITY_HEADERS.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self._note("客户端在响应写出前断开连接")

        # --- /metrics --------------------------------------------------

        def _handle_metrics(self) -> None:
            """Prometheus 指标：未开启 404；未授权 401（Basic auth）。"""
            if not settings.metrics:
                self._send_json(404, {"error": "not found"}, cacheable=False)
                return
            if not self._metrics_authorized():
                self._send_json(
                    401,
                    {"error": "未授权"},
                    extra_headers={"WWW-Authenticate": 'Basic realm="frpsctl"'},
                    cacheable=False,
                )
                return
            from .metrics import render_cached

            self._send_text(
                200,
                render_cached(ctx),
                content_type="text/plain; version=0.0.4; charset=utf-8",
            )

        def _metrics_authorized(self) -> bool:
            """有效会话 Cookie 或 Basic auth（用户名任意、口令 = 管理台口令）。"""
            if self._session() is not None:
                return True
            from .metrics import check_basic_auth

            return check_basic_auth(
                ctx.auth,
                self.headers.get("Authorization", ""),
                # 与登录**同一来源解析**（trusted_proxy 下用 XFF 最后一跳）：
                # 否则两条限速桶互不相干，爆破通道在反代部署下重新分裂
                # （v0.3.0 最终 review N1）。
                source=self._login_source(),
            )

        def handle_error(self, request, client_address) -> None:  # noqa: ANN001, ARG002
            """连接类异常静默（v0.3.0 review）。

            浏览器正常关闭 keep-alive 连接时异常发生在 `rfile.readline`，默认
            实现会把整段 traceback 打进 stderr/journald——那些不是错误，只是
            客户端走了。其余异常仍走默认（保留诊断）。
            """
            import sys as _sys

            exc = _sys.exc_info()[1]
            if isinstance(exc, (ConnectionError, TimeoutError)):
                return
            super().handle_error(request, client_address)

        def _note(self, message: str) -> None:
            # stderr 是辅助通道：断开（`2>&1 | head` 之类）时静默放弃——
            # 告警写失败绝不该让响应路径抛异常（v0.3.1；与 cli.ui 同纪律）。
            import contextlib
            import sys

            with contextlib.suppress(OSError):
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
        self._httpd: BoundedThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._static = static_index

    # --- 生命周期 ------------------------------------------------------

    def start(self) -> None:
        """绑定并开始服务。绑定失败（端口占用）原样抛 `OSError`。"""
        handler = _make_handler(self.ctx, self.settings, self._static or STATIC_INDEX)
        self._httpd = BoundedThreadingHTTPServer(
            (self.settings.host, self.settings.port),
            handler,
            max_workers=self.settings.max_workers,
        )
        # 未 accept 的连接队列上限（backlog）。管理台不面向高并发，设一个小值
        # 让过载时的行为可预期（多出的连接被内核拒绝，而不是无限排队）。
        # 请求线程另有 worker 上限（`max_workers`，超限 503）——
        # 两者共同构成过载护栏（v0.3.0 起；README"已知边界"同步）。
        self._httpd.request_queue_size = 64
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="frpsctl-web", daemon=True
        )
        self._thread.start()

    def serve_forever(self) -> None:
        """前台阻塞运行（CLI 用）。SIGTERM（systemd stop）→ 优雅退出。

        `start()` 已经在后台线程服务；本方法只负责安装信号处理器并**等待**
        ——此前这里对同一个 httpd 再调一次 `serve_forever()`，两个 select
        循环并存（结构隐患，v0.3.0 review 修正）。
        """
        if self._httpd is None:
            self.start()
        assert self._httpd is not None

        previous = signal.getsignal(signal.SIGTERM)
        try:
            signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
            while True:
                thread = self._thread
                if thread is None or not thread.is_alive():
                    break
                thread.join(timeout=0.5)
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
