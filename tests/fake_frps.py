#!/usr/bin/env python3
"""假 frps —— 集成测试用的可控二进制（不是 frp 的替代品，只模拟其接口）。

行为由环境变量控制：

| 变量 | 取值 | 效果 |
|------|------|------|
| `FRPS_FAKE_VERSION` | 如 `0.71.0` | `-v` 输出（**无 `v` 前缀**，与真 frps 一致） |
| `FRPS_FAKE_MODE` | `ok` | 起一个最小 HTTP 服务并响应 `/healthz` |
| | `exit` | 打印一行错误后立即退出（模拟端口被占/证书缺失） |
| | `hang` | 忽略 SIGTERM，只能被 SIGKILL 干掉（模拟卡死） |
| | `slow` | 延迟 3 秒后再响应 `/healthz`（模拟健康检查超时） |

真 frps 的关键语义在假实现里逐条对齐，否则集成测试就失去意义：

1. `-v` 输出裸版本号；
2. `verify -c <file>` 合法时打印 `frps: the configuration file X syntax is ok` 并退出 0，
   非法时退出 1 —— 假实现检查文件是否存在且能被 `tomllib` 解析；
3. 收到 SIGTERM **立即终止**（frps 没有信号处理器，§3.5），`hang` 模式除外；
4. `/healthz` **免认证**返回 200。
"""

from __future__ import annotations

import http.server
import json
import os
import signal
import sys
import threading
import time


def _detect_version() -> str:
    """版本号来源优先级：环境变量 > **自身文件名**。

    文件名回退很关键：真 frps 的版本来自二进制本身，而 frpsctl 也会用
    `read_binary_version()` 以**干净环境**直接跑这个文件。若只认环境变量，
    那种调用就会退化成默认值，于是"运行 frps-0.70.1"被报成 0.71.0——
    测试会查出一个生产代码里并不存在的问题。
    """
    override = os.environ.get("FRPS_FAKE_VERSION")
    if override:
        return override
    import re

    match = re.search(r"frps-(\d+\.\d+\.\d+)", os.path.basename(sys.argv[0]))
    return match.group(1) if match else "0.71.0"


VERSION = _detect_version()
MODE = os.environ.get("FRPS_FAKE_MODE", "ok")


def _parse_config_path(argv: list[str]) -> str | None:
    for i, arg in enumerate(argv):
        if arg == "-c" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
    return None


def _verify(argv: list[str]) -> int:
    path = _parse_config_path(argv)
    if not path:
        print("frps: the configuration file is not specified")
        return 0
    if not os.path.exists(path):
        print("open " + path + ": no such file or directory")
        return 1
    try:
        import tomllib

        with open(path, "rb") as handle:
            tomllib.load(handle)
    except Exception as exc:  # noqa: BLE001 - 模拟 frps 的报错输出
        print(str(exc))
        return 1
    print(f"frps: the configuration file {path} syntax is ok")
    return 0


def _read_port(config_path: str | None) -> int:
    if not config_path or not os.path.exists(config_path):
        return 0
    try:
        import tomllib

        with open(config_path, "rb") as handle:
            data = tomllib.load(handle)
    except Exception:  # noqa: BLE001
        return 0
    return int((data.get("webServer") or {}).get("port") or 0)


class _Handler(http.server.BaseHTTPRequestHandler):
    slow = False
    #: 是否提供 v2 Admin API。置 False 可模拟"二进制与预期不符"（ADR-3 的 404 路径）。
    v2_api = True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        if self.path == "/healthz":
            if self.slow:
                time.sleep(3)
            # 免认证：故意**不**检查 Authorization 头（与真 frps 一致，§3.2）
            body = b"ok"
            self._send(200, body, "text/plain")
            return

        # v2 Admin API：字段名与真 frps 0.71.0 实测一致（§3.2）。
        # 集成层要覆盖 ADR-3（启动后确认 v2 可用），所以这里必须真的实现它，
        # 而不是让被测代码绕过检查。
        if self.path.startswith("/api/v2/"):
            if not self.v2_api:
                self.send_error(404)
                return
            if self.path.startswith("/api/v2/system/info"):
                payload = {
                    "code": 200,
                    "msg": "success",
                    "data": {
                        "version": VERSION,
                        "status": {
                            "clientCounts": 0,
                            "proxyTypeCount": {},
                            "curConns": 0,
                            "totalTrafficIn": 0,
                            "totalTrafficOut": 0,
                        },
                        "config": {"tlsForce": False},
                    },
                }
            else:
                payload = {
                    "code": 200,
                    "msg": "success",
                    "data": {"total": 0, "page": 1, "pageSize": 50, "items": []},
                }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return

        self.send_error(404)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass  # 保持启动日志干净，便于断言


def _serve(port: int) -> None:
    _Handler.slow = MODE == "slow"
    # 允许用 FRPS_FAKE_NO_V2=1 模拟"该二进制没有 v2 API"（ADR-3 的 404 路径）
    _Handler.v2_api = os.environ.get("FRPS_FAKE_NO_V2") != "1"
    if port <= 0:
        # 未启用 dashboard：阻塞等待信号
        while True:
            time.sleep(3600)
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError as exc:
        # 端口被占（例如测试里多个假 frps 撞到同一端口）：如实打印一行，
        # 然后安静地当作"dashboard 不可用"继续跑——真 frps 也是这个结果。
        # 不能让线程抛异常，那会把启动日志污染成一段回溯。
        print(f"frps: dashboard listen 127.0.0.1:{port} failed: {exc}")
        sys.stdout.flush()
        while True:
            time.sleep(3600)
    server.serve_forever()


def main() -> int:
    argv = sys.argv[1:]

    # 1) 版本查询（Cobra 持久标志，位置随意）
    if "-v" in argv or "--version" in argv:
        print(VERSION)
        return 0

    # 2) 配置校验
    if "verify" in argv:
        return _verify(argv)

    # 3) 启动
    if MODE == "exit":
        print("frps: listen tcp :17000: bind: address already in use")
        return 1

    if MODE == "hang":
        # 忽略 SIGTERM，模拟卡死；只有 SIGKILL 能结束它
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("frps: started (hang mode)")
        sys.stdout.flush()
        while True:
            time.sleep(3600)

    port = _read_port(_parse_config_path(argv))
    print(f"frps: started, dashboard port {port}")
    sys.stdout.flush()

    # 忽略 SIGTERM？不——frps 没有信号处理器，默认行为即终止（§3.5）。
    thread = threading.Thread(target=_serve, args=(port,), daemon=True)
    thread.start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0) from None
