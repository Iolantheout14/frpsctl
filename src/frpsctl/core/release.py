"""二进制获取与校验（设计文档 §8.6、§8.6.1、ADR-4）。

**信任模型**：镜像只影响可用性，不影响信任。信任锚是**官方校验和文件**
（`frp_sha256_checksums.txt`，已实测存在；`frp_0.71.0_checksums.txt` 不存在）。
校验不通过绝不落盘（ADR-7）。

**升级语义**：`install` 拆成两个阶段（§8.6.1）——

| 阶段 | 行为 | 是否影响运行中的实例 |
|------|------|-------------------|
| 落盘 | 写入 `bin/frps-<version>`，**不碰软链** | 否 |
| 切换 | 原子替换软链 `bin/frps` → `frps-<version>` | **否**（只影响下一次 start） |

运行中的进程已把可执行映像绑定到 inode，换软链不影响它。
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import threading
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..errors import (
    BinaryError,
    ChecksumMismatch,
    ChecksumUnavailable,
    UsageError,
)
from .version import ensure_supported, parse_version

__all__ = [
    "CHECKSUM_ASSET",
    "InstallResult",
    "DEFAULT_MIRRORS",
    "asset_name",
    "download",
    "download_checksums",
    "expected_sha256",
    "install",
    "resolve_mirrors",
    "switch_symlink",
    "arch_tag",
    "os_tag",
]

#: 官方校验和资产名。注意**不是** `frp_<version>_checksums.txt`（那个不存在）。
CHECKSUM_ASSET = "frp_sha256_checksums.txt"

#: 下载源。多个源按顺序尝试，任一成功即停——镜像只影响可用性，不影响信任。
DEFAULT_MIRRORS: tuple[str, ...] = (
    "https://github.com/fatedier/frp/releases/download",
    "https://ghproxy.net/https://github.com/fatedier/frp/releases/download",
)

_ARCH_MAP = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
    "i386": "386",
    "i686": "386",
}

_OS_MAP = {"linux": "linux"}


def os_tag() -> str:
    """frp 发布资产的 OS 标签。只支持 Linux（§3.4）。"""
    name = platform.system().lower()
    if name not in _OS_MAP:
        raise UsageError(
            f"不支持的平台：{platform.system()}",
            hint="frpsctl 仅支持 Linux（§3.4）",
        )
    return _OS_MAP[name]


def arch_tag() -> str:
    machine = platform.machine().lower()
    if machine not in _ARCH_MAP:
        raise UsageError(
            f"不支持的架构：{platform.machine()}",
            hint="可用 --asset-url 手工指定下载地址",
        )
    return _ARCH_MAP[machine]


def asset_name(version: str) -> str:
    """发布资产名，如 `frp_0.71.0_linux_amd64.tar.gz`。"""
    return f"frp_{version}_{os_tag()}_{arch_tag()}.tar.gz"


def resolve_mirrors(extra: Sequence[str] = ()) -> tuple[str, ...]:
    """解析下载源列表。优先级：**CLI 参数 > `FRPSCTL_MIRROR`（逗号分隔）> 内置**。

    CLI 给的是**替换**而不是追加：用户指定镜像通常意味着"内置源在我这里不通"，
    此时他要的是"用我给的这几个"，而不是"再试一遍刚才失败的"。

    空项被忽略、尾斜杠被规整（URL 拼接时统一补 `rstrip('/')`，避免 `//v0.71.0`）。
    """
    cleaned = tuple(item.strip().rstrip("/") for item in extra if item.strip())
    if cleaned:
        return cleaned
    env = os.environ.get("FRPSCTL_MIRROR", "")
    from_env = tuple(item.strip().rstrip("/") for item in env.split(",") if item.strip())
    return from_env or DEFAULT_MIRRORS


@dataclass(frozen=True)
class InstallResult:
    version: str
    binary: Path
    #: `bin/frps` 软链**本次是否真的被切换过**。
    #: 它必须与"是否要求切换"（`switch` 参数）区分开：frps 已在盘上时切换是空操作，
    #: 混为一谈会让 CLI 打印出"未切换软链"，而实际可能刚切了 frpc 的软链。
    switched: bool
    downloaded: bool
    #: `bin/frpc` 软链本次是否被建立/切换（`--with-frpc` 才可能为真）。
    switched_frpc: bool = False


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------


def _fetch(url: str, *, timeout: float = 60.0) -> bytes:
    """取回一个 URL 的内容。优先用 curl——它自带镜像/代理/重定向的成熟处理。"""
    if shutil.which("curl"):
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            proc = subprocess.run(
                ["curl", "-fsSL", "--max-time", str(int(timeout)), "-o", str(tmp_path), url],
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                return tmp_path.read_bytes()
            raise OSError(proc.stderr.strip() or f"curl 退出码 {proc.returncode}")
        finally:
            tmp_path.unlink(missing_ok=True)

    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def download(asset: str, version: str, mirrors: tuple[str, ...] = DEFAULT_MIRRORS) -> bytes:
    """按镜像顺序尝试下载资产；全部失败抛 `BinaryError`。"""
    errors: list[str] = []
    for mirror in mirrors:
        url = f"{mirror.rstrip('/')}/v{version}/{asset}"
        try:
            return _fetch(url)
        except Exception as exc:  # 网络层异常种类繁多，统一记录后继续下一个源
            errors.append(f"{url} → {exc}")
    raise BinaryError(
        f"无法下载 {asset}（已尝试 {len(mirrors)} 个源）",
        hint="\n  ".join(errors[:3]),
    )


def download_checksums(version: str, mirrors: tuple[str, ...] = DEFAULT_MIRRORS) -> str:
    """取回官方校验和文件内容（文本）。"""
    blob = download(CHECKSUM_ASSET, version, mirrors)
    return blob.decode("utf-8", "replace")


def expected_sha256(checksums_text: str, asset: str) -> str | None:
    """从官方校验和文件里摘出目标资产的 sha256。

    官方格式是 `<sha256>  <asset>` 每行一条。摘不到返回 None——由调用方
    决定是 fail-closed（默认）还是显式 `--insecure` 放行。
    """
    for line in checksums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset:
            digest = parts[0].strip().lower()
            if len(digest) == 64:
                return digest
    return None


# ---------------------------------------------------------------------------
# 解包
# ---------------------------------------------------------------------------


def extract_frps(blob: bytes, *, member_name: str = "frps") -> bytes:
    """从 tar.gz 里取出某个二进制（默认 frps）。

    只接受**普通文件**且路径 basename 精确匹配：防止压缩包里的
    `../../bin/sh` 之类路径穿越（tarfile 的 filter 在 3.12 才有，
    这里手工判定，3.11 同样安全）。
    """
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            if os.path.basename(member.name) != member_name:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            return handle.read()
    raise BinaryError(
        f"压缩包中找不到 {member_name}",
        hint="发布资产结构可能已变化，请核对手工下载的包内容",
    )


# ---------------------------------------------------------------------------
# 落盘与切换
# ---------------------------------------------------------------------------


def switch_symlink(dest: Path, target: Path) -> None:
    """原子替换软链：临时链 + `os.rename`（同目录内 rename 是原子的）。

    临时名带上线程 id：只用 pid 的话，同一进程内两个线程并发切换会互相
    unlink 对方刚建的临时链。`finally` 保证 rename 失败时不留残留。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.new-{os.getpid()}-{threading.get_ident()}")
    tmp.unlink(missing_ok=True)
    try:
        tmp.symlink_to(target.name)
        os.rename(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)  # 成功路径上 rename 已把它移走，这里是 no-op


def install(
    *,
    bin_dir: Path,
    version: str,
    mirrors: tuple[str, ...] = DEFAULT_MIRRORS,
    insecure: bool = False,
    force: bool = False,
    switch: bool = True,
    with_frpc: bool = False,
) -> InstallResult:
    """下载 → 强校验 → 落盘 → `-v` 复验 → （可选）换软链。

    `switch=False` 对应 `install --only-download`：先把二进制备到多台机器，
    再统一切换的运维节奏（§8.6.1）。

    `with_frpc=True` 额外取出同一 tar 包里的 **frpc**（`bin/frpc-<version>` +
    软链）。它不服务于 frps 的日常运维，而是为了让**契约测试**能跑起来——
    插件（M5）的唯一真实调用方是 frpc，没有它就只能靠手工拼报文验证协议。
    由于 frpc 与 frps 在同一个发布资产里，这一步不产生额外下载。

    `with_frpc` 与"frps 是否已在盘上"**相互独立**：此前 `dest.exists()` 那条捷径
    直接 `return`，于是"机器上已有 frps、现在想补装 frpc"永远装不上（而输出还
    报着成功）。CI 恰好掩盖了它——CI 每次都是全新数据目录，必然走完整路径。
    """
    ensure_supported(parse_version(version))  # 门槛 >= 0.70.0（§3.6）
    bin_dir.mkdir(parents=True, exist_ok=True)
    asset = asset_name(version)
    dest = bin_dir / f"frps-{version}"
    frpc_dest = bin_dir / f"frpc-{version}"

    place_frps = force or not dest.exists()
    place_frpc = with_frpc and (force or not frpc_dest.exists())

    if not place_frps and not place_frpc:
        # 两边都已在盘上：不碰网络。但**换链仍要照做**——用户没加 `--only-download`
        # 时，"让 bin/frps 指向这个版本"就是他要的结果（幂等，且修得回手工改歪的链）。
        # `switched` / `switched_frpc` 如实反映"这次是否真的建立了指向"。
        if switch:
            switch_symlink(bin_dir / "frps", dest)
            switched_frpc = with_frpc and frpc_dest.exists()
            if switched_frpc:
                switch_symlink(bin_dir / "frpc", frpc_dest)
        else:
            switched_frpc = False
        return InstallResult(
            version=version,
            binary=dest,
            switched=switch,
            downloaded=False,
            switched_frpc=switched_frpc,
        )

    from ..cli.ui import trace

    trace(f"目标资产：{asset}（镜像数 {len(mirrors)}）")

    # 1) 校验和：拿不到就拒绝（fail-closed，ADR-7）。
    #    必须先于下载：否则会先花一次网络把二进制拉回来，再因为"没有校验和"
    #    把它丢掉——既慢，又让 `--insecure` 的判断发生在下载之后，读起来像
    #    "下载完了才发现不该装"。
    expected: str | None = None
    try:
        expected = expected_sha256(download_checksums(version, mirrors), asset)
    except BinaryError:
        if not insecure:
            raise ChecksumUnavailable(version) from None
    if expected is None and not insecure:
        raise ChecksumUnavailable(version)

    # 2) 下载（frps 与 frpc 同一个资产，因此只下一次）
    blob = download(asset, version, mirrors)

    # 3) 强校验——不通过绝不落盘
    actual = hashlib.sha256(blob).hexdigest()
    if expected is not None and actual != expected:
        raise ChecksumMismatch(asset, expected, actual)

    # 4) 解包 → 先落到临时文件，复验通过后才原子就位
    if place_frps:
        _place_binary(blob, member="frps", dest=dest)

    # 4b) 可按需附带 frpc（供插件契约测试使用）。它与 frps 的落盘判定互相独立，
    #     因此"已有 frps"时也能补装。
    #     `--only-download`（switch=False）对 frps 与 frpc **一视同仁**：都只落盘、
    #     不动软链——同一命令两套语义会让"先备好、再统一切换"的运维节奏失效。
    if place_frpc:
        _place_binary(blob, member="frpc", dest=frpc_dest)
        if switch:
            switch_symlink(bin_dir / "frpc", frpc_dest)

    # 5) 切换（不影响运行中的进程）。`--only-download` 时 place_frps 只由 force 决定，
    #    此时不该动链，因此这里再判一次 switch。
    if switch and place_frps:
        switch_symlink(bin_dir / "frps", dest)
    return InstallResult(
        version=version,
        binary=dest,
        switched=switch and place_frps,
        downloaded=True,
        switched_frpc=place_frpc and switch,
    )


def _place_binary(blob: bytes, *, member: str, dest: Path) -> None:
    """把 tar 里的一个成员解出来、复验、原子就位。"""
    payload = extract_frps(blob, member_name=member)
    tmp = dest.with_name(f".{dest.name}.dl-{os.getpid()}")
    try:
        tmp.write_bytes(payload)
        tmp.chmod(0o755)
        _verify_binary(tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _verify_binary(path: Path) -> None:
    """`-v` 复验：确认这是能跑起来的 frps，且版本达标。

    三种失败都要收口成 `BinaryError`（退出码 4），不能漏出裸异常：

    - 非零退出 → 架构不匹配之类的"能执行但不是我"；
    - **超时** → 二进制卡住。这是最容易被漏掉的一条：`subprocess.run` 抛的是
      `TimeoutExpired`，它既不是 `FrpsctlError` 也不是 `OSError`，冒到 CLI 会
      变成"未分类错误(1)"，把"二进制有问题"误报成"工具内部出错"；
    - `OSError` → 不可执行（EACCES/ENOEXEC）。
    """
    try:
        proc = subprocess.run([str(path), "-v"], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise BinaryError(
            f"复验超时：{path} -v 在 10 秒内没有返回",
            hint="二进制可能卡住或依赖缺失的动态库；请手工运行它确认真实行为",
        ) from None
    except OSError as exc:
        raise BinaryError(
            f"无法执行下载的二进制 {path}：{exc}",
            hint="架构可能不匹配（例如在 arm64 上取了 amd64 包），或文件系统不允许执行",
        ) from None
    if proc.returncode != 0:
        raise BinaryError(
            f"下载的二进制无法执行：{(proc.stderr or proc.stdout).strip()[:200]}",
            hint="架构可能不匹配（例如在 arm64 上取了 amd64 包）",
        )
    # 版本门槛（§3.6）：不达标即抛 UnsupportedVersion；解析不出即抛
    # VersionParseError —— 两者都是 BinaryError 的子类，退出码 4。
    ensure_supported(parse_version(proc.stdout or proc.stderr))
