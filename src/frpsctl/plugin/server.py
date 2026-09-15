"""插件 HTTP 服务（设计文档 §11.2、§11.3）。

用**标准库** `http.server` 而不是 ASGI 框架，理由不是"省事"：

1. 插件是**全部客户端登录的单点**，且 frp 侧对插件 HTTP 客户端**没有设置任何
   timeout**——插件越轻，启动越快、故障面越小、越不容易在异常路径上卡住；
2. 协议只有"一个 POST，收 JSON，回 JSON"，不需要路由、模板、WebSocket；
3. 零新增依赖：任何有 Python 3.11 的机器可以直接跑，不必先装一套 ASGI 栈。

**必须守住的四条**（§11.2）：

| 约束 | 实现方式 |
|------|---------|
| 绑回环 | `PluginPolicy.validate(bind=...)` 硬拒绝非回环地址 |
| handler 轻量 | 引擎里没有任何 I/O；审计走异步队列（audit.py） |
| 慢请求可观测 | 记录每个请求耗时，超阈值打警告（不静默拖慢登录） |
| fail-closed 语义正确 | 内部异常一律回 `reject`，绝不"出错就放行" |

线程模型：`ThreadingHTTPServer`，每个请求一个线程。这是刻意的——插件被调用时
客户端线程正在等服务端回包，串行处理会让一个慢请求阻塞所有人。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .audit import AuditLog
from .engine import DecisionEngine
from .policy import PluginPolicy
from .types import PluginRequest, PluginResponse, UnknownOp

__all__ = ["PluginServer", "ServerSettings", "serve"]

#: 单个请求超过这个耗时就在日志里点名——它是"插件拖慢登录"的唯一线索。
SLOW_REQUEST_MS = 200.0

#: 请求体上限。插件收到的都是小 JSON；限制它能防止有人拿它当上传口。
MAX_BODY_BYTES = 1 << 20  # 1 MiB


@dataclass(frozen=True)
class ServerSettings:
    bind: str = "127.0.0.1:8080"
    path: str = "/handler"
    #: 是否把每个请求打进 stderr。默认关闭以保持日志干净，审计另有 JSONL。
    access_log: bool = False

    @property
    def host(self) -> str:
        text = self.bind
        if text.startswith("["):
            return text[1 : text.index("]")] if "]" in text else text
        return text.rsplit(":", 1)[0] if ":" in text else text

    @property
    def port(self) -> int:
        text = self.bind
        if text.startswith("[") and "]" in text:
            rest = text[text.index("]") + 1 :]
            return int(rest.lstrip(":")) if rest.lstrip(":") else 8080
        if ":" in text:
            tail = text.rsplit(":", 1)[1]
            return int(tail) if tail else 8080
        return 8080


def _make_handler(engine: DecisionEngine, settings: ServerSettings):
    """构造请求处理器。

    用闭包而不是类属性传状态：`ThreadingHTTPServer` 会为每个请求实例化 handler，
    类属性容易在多次启动之间串味（尤其测试里）。
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "frpsctl-plugin/0.1"
        protocol_version = "HTTP/1.1"

        # --- 入口 ------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            started = time.monotonic()
            try:
                response, status = self._process()
            except Exception as exc:  # noqa: BLE001 - 任何内部异常都必须变成拒绝
                # fail-closed：出错**绝不放行**。放行等于"插件坏了就没人管了"，
                # 而 fail-closed 至少是安全的（§11.2）。
                response = PluginResponse.reject_op(
                    f"frpsctl-plugin: 内部错误（{type(exc).__name__}）"
                )
                status = 200
                self._note(f"内部异常 {type(exc).__name__}: {exc}")

            body = json.dumps(response.to_payload(), ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # 客户端（frps）已经不在了——不是我们的问题，但值得记一笔
                self._note("frps 在响应写出前断开连接")

            elapsed_ms = (time.monotonic() - started) * 1000
            if elapsed_ms > SLOW_REQUEST_MS:
                self._note(f"慢请求 {elapsed_ms:.0f}ms（阈值 {SLOW_REQUEST_MS:.0f}ms）")
            elif settings.access_log:
                self._note(f"{elapsed_ms:.1f}ms")

        def do_GET(self) -> None:  # noqa: N802
            """只暴露 `/healthz`，方便 doctor/负载均衡探活。

            **刻意不提供任何"列出用户"之类的查询接口**：插件绑在回环上，多一个
            信息出口就多一分被本地无关进程读到的风险。
            """
            if urlparse(self.path).path != "/healthz":
                self._send_json(404, {"error": "not found"})
                return
            self._send_json(200, {"status": "ok"})

        # --- 协议 ------------------------------------------------------

        def _process(self) -> tuple[PluginResponse, int]:
            parsed = urlparse(self.path)
            if parsed.path != settings.path:
                # 路径不匹配通常是 frps 配置写错（addr/path 拼错）。回 404 会让
                # frp 直接判定该操作失败——正确的 fail-closed 行为。
                return PluginResponse.reject_op(
                    f"frpsctl-plugin: 未知路径 {parsed.path!r}（期望 {settings.path!r}）"
                ), 404

            query = parse_qs(parsed.query)
            op = (query.get("op") or [""])[0]
            version = (query.get("version") or [""])[0]
            if not op:
                # `op` 在 URL query 里，这是 frp 的实现细节（http.go:93-97）。
                return PluginResponse.reject_op("frpsctl-plugin: 缺少 op 参数"), 400

            payload = self._read_body()
            if payload is None:
                return PluginResponse.reject_op("frpsctl-plugin: 请求体不是合法 JSON"), 400

            try:
                request = PluginRequest.from_payload(
                    op=op,
                    version=version,
                    body=payload,
                    reqid=self.headers.get("X-Frp-Reqid", ""),
                )
            except UnknownOp as exc:
                # 我们只注册了受支持的 op；收到别的说明请求不是来自预期的 frps
                return PluginResponse.reject_op(f"frpsctl-plugin: {exc}"), 200

            response = engine.handle(request, source=self.client_address[0])
            return response, 200

        def _read_body(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0:
                return {}
            if length > MAX_BODY_BYTES:
                return None
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return data if isinstance(data, dict) else None

        # --- 输出 ------------------------------------------------------

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _note(self, message: str) -> None:
            import sys

            sys.stderr.write(f"[plugin] {message}\n")
            sys.stderr.flush()

        def log_message(self, *args: object) -> None:
            """屏蔽默认的访问日志（它会把每个请求写两行到 stderr）。"""
            if settings.access_log:
                super().log_message(*args)  # type: ignore[arg-type]

    return Handler


class PluginServer:
    """可启动/可停止的插件服务。测试与 CLI 共用。"""

    def __init__(
        self,
        policy: PluginPolicy,
        settings: ServerSettings | None = None,
        *,
        audit: AuditLog | None = None,
    ) -> None:
        self.policy = policy
        self.settings = settings or ServerSettings()
        self.policy.validate(bind=self.settings.bind)
        self.audit = audit if audit is not None else AuditLog(
            policy.audit.path,
            enabled=policy.audit.enabled,
            flush_every=policy.audit.flush_every,
            flush_interval=policy.audit.flush_interval,
        )
        self.engine = DecisionEngine(policy, self.audit)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # --- 生命周期 ------------------------------------------------------

    def start(self) -> None:
        """绑定并开始服务。绑定失败（端口占用）会原样抛出 `OSError`。"""
        handler = _make_handler(self.engine, self.settings)
        self._httpd = ThreadingHTTPServer(
            (self.settings.host, self.settings.port), handler
        )
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="frpsctl-plugin", daemon=True
        )
        self._thread.start()

    def serve_forever(self) -> None:
        """前台阻塞运行（CLI 用）。Ctrl-C 由上层捕获。"""
        if self._httpd is None:
            self.start()
        assert self._httpd is not None
        try:
            self._httpd.serve_forever()
        finally:
            self.close()

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
        self.audit.close()

    def __enter__(self) -> PluginServer:
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
        return f"http://{host}:{port}{self.settings.path}"


def serve(policy: PluginPolicy, settings: ServerSettings) -> None:
    """CLI 入口：前台运行直到中断。"""
    server = PluginServer(policy, settings)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


def wait_ready(host: str, port: int, *, timeout: float = 5.0) -> bool:
    """探活辅助（CLI `plugin check` 与测试共用）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.05)
    return False
