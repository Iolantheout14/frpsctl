#!/usr/bin/env bash
#
# frpsctl 一键安装：从源码安装并注册为全局命令。
#
#   ./install.sh                 # 装到 ~/.local（不需要 sudo）
#   sudo ./install.sh --system   # 装到 /usr/local（全局，需要 root）
#   ./install.sh --uninstall     # 卸载
#   ./install.sh --help
#
# 设计要点：
#
#  1. **不依赖 pipx / uv / sudo**。主路径是"自建 venv + 包装脚本"——这三样在
#     目标机器上不一定有，而 venv 是标准库自带的。
#  2. **幂等**。重复运行等于升级：重新装依赖、重写包装脚本，不做多余动作。
#  3. **不下载 frps 二进制**。那是 `frpsctl install` 的职责，且会写入用户数据
#     目录、需要网络与校验和。安装器只负责让 `frpsctl` 这个命令可用。
#  4. **失败即停**（set -euo pipefail），并且每步都有可读的说明。
#  5. **支持在线一键**（`curl … | bash`）：没有脚本同目录的源码时自动下载
#     tarball（FRPSCTL_INSTALL_REF / FRPSCTL_INSTALL_URL 可固定版本或换镜像）；
#     机器没有 Python >= 3.11 时打印 uv 一键安装指引（uv 自带 Python）。
#
set -euo pipefail

# ---------------------------------------------------------------- 常量

PROG_NAME="frpsctl"
VENV_DIRNAME="frpsctl-src"          # 与 pip/pipx 安装的目录区分开
MIN_PY_MINOR=11                     # 需要 Python >= 3.11（标准库 tomllib）
#: 管道安装（`curl … | bash`）时的源码下载源。可用环境变量固定版本或换镜像：
#:   FRPSCTL_INSTALL_REF=v0.2.5   固定 tag / 分支 / commit（默认 main）
#:   FRPSCTL_INSTALL_URL=https://…/x.tar.gz  完整覆盖（镜像 / 离线内网）
#: URL 用 GitHub 的通用形态 `archive/<ref>.tar.gz`——它会自动解析 tag / 分支 /
#: commit；写成 `refs/heads/<ref>` 只能取分支，传 tag 会 404（实测）。
REPO_REF="${FRPSCTL_INSTALL_REF:-main}"
REPO_URL="${FRPSCTL_INSTALL_URL:-https://github.com/ThzxxArt/frpsctl/archive/${REPO_REF}.tar.gz}"
#: 脚本自身所在目录。`curl … | bash` 管道方式下没有自身路径——此时 SRC_DIR 为空，
#: 由后面的"获取源码"阶段下载（见 fetch_source）。
SELF_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
fi
SRC_DIR="$SELF_DIR"

# ---------------------------------------------------------------- 输出

if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'
else
  C_RESET=''; C_BOLD=''; C_DIM=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''
fi

info()  { printf '%s\n' "${C_BLUE}==>${C_RESET} $*"; }
ok()    { printf '%s\n' "${C_GREEN} ✓${C_RESET} $*"; }
warn()  { printf '%s\n' "${C_YELLOW} ⚠${C_RESET} $*" >&2; }
die()   { printf '%s\n' "${C_RED} ✗${C_RESET} $*" >&2; exit 1; }
step()  { printf '\n%s\n' "${C_BOLD}$*${C_RESET}"; }

usage() {
  cat <<EOF
${C_BOLD}frpsctl 安装器${C_RESET} —— 从源码安装并注册为全局命令

用法：
  ./install.sh [选项]                       # 源码目录内运行
  curl -fsSL https://raw.githubusercontent.com/ThzxxArt/frpsctl/main/install.sh | bash
                                            # 在线一键（自动下载源码）

选项：
  --system        安装到 /usr/local（全局，需要 root）
  --prefix DIR    自定义安装前缀（默认：普通用户 ~/.local，root /usr/local）
  --uninstall     卸载（删除 venv 与命令，不动实例数据；不需要 Python）
  --no-verify     跳过安装后的自检
  -h, --help      显示本帮助

环境变量：
  FRPSCTL_INSTALL_REF   固定源码版本（tag / 分支，默认 main）
  FRPSCTL_INSTALL_URL   覆盖源码 tarball 地址（镜像 / 离线内网用）

说明：
  · 默认装到 ~/.local/share/${VENV_DIRNAME}/venv，命令注册到 ~/.local/bin/${PROG_NAME}
  · 管道安装会把源码放到 ~/.local/share/${VENV_DIRNAME}/src（重复运行即更新）
  · 没有 Python >= 3.${MIN_PY_MINOR} 时会给出 uv 一键安装指引（uv 自带 Python，无需系统 Python）
  · 不下载 frps 二进制；装完后自行运行：${PROG_NAME} install && ${PROG_NAME} init
  · 反复运行本脚本即为升级（会重新安装依赖并重写命令）

支持的平台：Linux；Python >= 3.${MIN_PY_MINOR}
EOF
}

# ---------------------------------------------------------------- 参数

# 两个维度分开：MODE 决定"安装还是卸载"，LAYOUT 决定"装到哪"。此前它们挤在
# 一个变量里，`--uninstall --prefix DIR` 会让后解析的 `--prefix` 把 MODE 改成
# custom——"装到自定义前缀后想卸载"这个最常见的组合实际会走安装路径（实测）。
MODE="install"                      # install / uninstall
LAYOUT="user"                       # user / system / custom
PREFIX=""
DO_VERIFY=1

while [ $# -gt 0 ]; do
  case "$1" in
    --system)     LAYOUT="system"; shift ;;
    --prefix)     PREFIX="${2:-}"; [ -n "$PREFIX" ] || die "--prefix 需要一个目录参数"; LAYOUT="custom"; shift 2 ;;
    --prefix=*)   PREFIX="${1#*=}"; LAYOUT="custom"; shift ;;
    --uninstall)  MODE="uninstall"; shift ;;
    --no-verify)  DO_VERIFY=0; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            die "未知参数：$1（用 --help 查看用法）" ;;
  esac
done

# ---------------------------------------------------------------- 前置检查

step "检查运行环境"

[ "$(uname -s)" = "Linux" ] || die "${PROG_NAME} 仅支持 Linux（依赖 /proc 做进程身份校验）"
ok "操作系统：Linux"

case "$LAYOUT" in
  user)    PREFIX="${PREFIX:-${HOME}/.local}" ;;
  system)  PREFIX="${PREFIX:-/usr/local}" ;;
  custom)  : ;;
esac

# root 但没给 --system：给出提示而不是默默装到 /root
if [ "$LAYOUT" = "user" ] && [ "$(id -u)" -eq 0 ]; then
  warn "当前是 root，但默认装到 ${PREFIX}（仅 root 可用）"
  warn "想做系统级安装请用：sudo ./install.sh --system"
fi

# 是否需要 root 由**目标目录是否可写**决定，而不是由模式决定：
# 自定义前缀（例如装到自己的家目录）完全不需要 root，而 --system 通常需要。
# 这样判断既不会误拦自定义前缀，也不会在真正需要权限时给出难懂的 EACCES。
if [ "$MODE" != "uninstall" ]; then
  probe="${PREFIX}"
  while [ ! -e "$probe" ] && [ "$probe" != "/" ]; do probe="$(dirname -- "$probe")"; done
  if [ ! -w "$probe" ]; then
    if [ "$(id -u)" -eq 0 ]; then
      die "目标前缀不可写：${probe}"
    fi
    die "没有权限写入 ${PREFIX}（${probe} 不可写）
   请改用 sudo，或换一个可写前缀：./install.sh --prefix \"\$HOME/.local\""
  fi
fi

# 找到可用的 python3
find_python() {
  local candidate
  for candidate in python3 "python3.${MIN_PY_MINOR}" python3.12 python3.13 python3.14; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, ${MIN_PY_MINOR}) else 1)" 2>/dev/null; then
      printf '%s' "$candidate"; return 0
    fi
  done
  return 1
}

# 下载源码 tarball 并就位到 $1（管道安装用；失败即 die）。
fetch_source() {
  local dest="$1" tmp extracted="" candidate
  command -v tar >/dev/null 2>&1 || die "找不到 tar，无法解压源码"
  tmp="$(mktemp -d)"
  trap 'rm -rf -- "$tmp"' EXIT
  info "下载源码：${REPO_URL}"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --max-time 180 -o "$tmp/src.tar.gz" "$REPO_URL" \
      || die "下载失败：${REPO_URL}
   可固定版本：FRPSCTL_INSTALL_REF=v0.2.5；或换镜像：FRPSCTL_INSTALL_URL=…；
   也可以 git clone 后在源码目录运行 ./install.sh"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 180 -O "$tmp/src.tar.gz" "$REPO_URL" \
      || die "下载失败：${REPO_URL}（可用 FRPSCTL_INSTALL_REF / FRPSCTL_INSTALL_URL）"
  else
    die "需要 curl 或 wget 下载源码；或 git clone 后在源码目录运行 ./install.sh"
  fi
  tar -xzf "$tmp/src.tar.gz" -C "$tmp" || die "解压失败：$tmp/src.tar.gz"
  # tarball 顶层目录名形如 frpsctl-main：用 pyproject.toml 认它（不猜目录名）。
  for candidate in "$tmp"/*/; do
    if [ -f "${candidate}pyproject.toml" ]; then extracted="${candidate%/}"; break; fi
  done
  [ -n "$extracted" ] || die "下载的源码里没有 pyproject.toml（发布包结构变化？）"
  mkdir -p -- "$(dirname -- "$dest")"
  rm -rf -- "$dest"
  mv -- "$extracted" "$dest"
  rm -rf -- "$tmp"
  trap - EXIT
}

# ---------------------------------------------------------------- 路径

VENV_DIR="${PREFIX}/share/${VENV_DIRNAME}/venv"
BIN_DIR="${PREFIX}/bin"
CMD_PATH="${BIN_DIR}/${PROG_NAME}"

# ---------------------------------------------------------------- 卸载

if [ "$MODE" = "uninstall" ]; then
  step "卸载"
  removed=0
  if [ -e "$CMD_PATH" ]; then rm -f "$CMD_PATH"; ok "已删除命令：${CMD_PATH}"; removed=1; fi
  if [ -d "$VENV_DIR" ]; then rm -rf "$VENV_DIR"; ok "已删除虚拟环境：${VENV_DIR}"; removed=1; fi
  if [ -d "${PREFIX}/share/${VENV_DIRNAME}" ] && [ -z "$(ls -A "${PREFIX}/share/${VENV_DIRNAME}" 2>/dev/null)" ]; then
    rmdir "${PREFIX}/share/${VENV_DIRNAME}" 2>/dev/null || true
  fi
  [ "$removed" -eq 1 ] || warn "没找到已安装的 ${PROG_NAME}（前缀：${PREFIX}）"
  printf '\n'
  ok "卸载完成"
  printf '%s\n' "${C_DIM}实例数据（配置、日志、快照）保留在 ~/.local/share/frpsctl/，如需清理请手工删除${C_RESET}"
  exit 0
fi

# ---------------------------------------------------------------- 安装

step "检查运行环境"

if ! PYTHON="$(find_python)"; then
  cat >&2 <<EOF
 ${C_RED}✗${C_RESET} 找不到 Python >= 3.${MIN_PY_MINOR}。
   两条出路（推荐第一条——uv 自带 Python，无需系统 Python）：

     ${C_BOLD}curl -LsSf https://astral.sh/uv/install.sh | sh && uv tool install ${PROG_NAME}${C_RESET}

   或先装一个 Python 再回来：
     sudo apt install python3 python3-venv     # Debian / Ubuntu
     sudo dnf install python3                  # Fedora / RHEL
     sudo apk add python3                      # Alpine
EOF
  exit 1
fi
ok "Python：$("$PYTHON" -V 2>&1)（$(command -v "$PYTHON")）"

if ! "$PYTHON" -c "import venv, ensurepip" 2>/dev/null; then
  die "该 Python 的 venv/ensurepip 组件不完整（venv 建出来会没有 pip）。
   两条出路：
     sudo apt install python3-venv         # Debian / Ubuntu（按实际 Python 版本装对应包）
     curl -LsSf https://astral.sh/uv/install.sh | sh && uv tool install ${PROG_NAME}"
fi
ok "venv 模块可用"

# 源码：脚本同目录里有就用它（clone 方式）；没有（`curl … | bash` 管道方式）
# 就下载到数据目录——两种方式走完全相同的后续流程。
if [ -f "${SRC_DIR}/pyproject.toml" ]; then
  ok "源码目录：${SRC_DIR}（脚本同目录）"
else
  step "获取源码"
  SRC_DIR="${PREFIX}/share/${VENV_DIRNAME}/src"
  fetch_source "$SRC_DIR"
  ok "源码目录：${SRC_DIR}（已下载，ref=${REPO_REF}）"
fi

step "创建虚拟环境"
mkdir -p "$(dirname -- "$VENV_DIR")" "$BIN_DIR"
# 复用前必须验证 pip 可用：venv 创建中断过一次就会留下"有 python 没有 pip"的
# 半残目录，无条件复用会让之后每次安装都在同一个坑里失败（实测踩过）。
if [ -x "${VENV_DIR}/bin/python" ] && "${VENV_DIR}/bin/python" -c "import pip" 2>/dev/null; then
  info "复用已存在的 venv：${VENV_DIR}"
else
  rm -rf -- "$VENV_DIR"
  if ! "$PYTHON" -m venv "$VENV_DIR"; then
    rm -rf -- "$VENV_DIR"
    die "创建虚拟环境失败：${VENV_DIR}
   若报 'ensurepip is not available'：该系统 Python 缺少完整 venv 组件——
   Debian/Ubuntu 请装 python3-venv；或改用 uv（自带完整 Python）：
     curl -LsSf https://astral.sh/uv/install.sh | sh && uv tool install ${PROG_NAME}"
  fi
  ok "已创建：${VENV_DIR}"
fi
VENV_PY="${VENV_DIR}/bin/python"
[ -x "$VENV_PY" ] || die "venv 创建失败：${VENV_PY} 不可执行"

step "安装 ${PROG_NAME} 及其依赖"
# 用 --upgrade 保证重复运行 = 升级；-e 让源码改动立即生效
"$VENV_PY" -m pip install --quiet --upgrade pip 2>/dev/null || warn "pip 自升级失败，继续"
if ! "$VENV_PY" -m pip install --quiet --upgrade -e "$SRC_DIR"; then
  die "依赖安装失败。若网络受限，可试：
   ${VENV_PY} -m pip install -e '$SRC_DIR' -i https://pypi.tuna.tsinghua.edu.cn/simple"
fi
ok "依赖就绪（typer / pydantic / tomlkit / httpx）"

step "注册全局命令"
# 用包装脚本而不是软链：软链指向 venv 内的 console script，venv 换位置就会断；
# 包装脚本显式写死解释器路径，且可读、可调试。
cat > "$CMD_PATH" <<EOF
#!/usr/bin/env bash
# ${PROG_NAME} 启动器 —— 由 install.sh 生成，请勿手工编辑
# 源码目录：${SRC_DIR}
exec "${VENV_PY}" -m ${PROG_NAME} "\$@"
EOF
chmod 0755 "$CMD_PATH"
ok "已注册：${CMD_PATH}"

step "自检"
if [ "$DO_VERIFY" -eq 1 ]; then
  if version_out="$("$CMD_PATH" --version 2>&1)"; then
    ok "命令可用：${version_out}"
  else
    die "自检失败，${CMD_PATH} --version 输出：${version_out}"
  fi
else
  info "已按 --no-verify 跳过自检"
fi

# ---------------------------------------------------------------- PATH 提示

case ":${PATH}:" in
  *":${BIN_DIR}:"*) : ;;
  *)
    printf '\n'
    warn "${BIN_DIR} 不在当前 PATH 中，因此还不能直接敲 ${PROG_NAME}"
    printf '%s\n' "   把下面这行加到 ~/.bashrc（或 ~/.zshrc）后重开终端："
    printf '%s\n' "     ${C_BOLD}export PATH=\"${BIN_DIR}:\$PATH\"${C_RESET}"
    ;;
esac

# ---------------------------------------------------------------- 下一步

printf '\n%s\n' "${C_GREEN}${C_BOLD}${PROG_NAME} 安装完成${C_RESET}"
cat <<EOF

接下来（二选一）：

  ${C_BOLD}快速开始${C_RESET}
    ${PROG_NAME} install       # 下载官方 frps 二进制（sha256 强校验）
    ${PROG_NAME} init          # 生成安全基线配置（会打印 token 与口令）
    ${PROG_NAME} start
    ${PROG_NAME} status

  ${C_BOLD}先看看自己会怎么做${C_RESET}
    ${PROG_NAME} doctor        # 体检：二进制 / 配置 / 权限 / 端口 / 所有权

其他常用：
    ${PROG_NAME} --help              # 全部命令
    ${PROG_NAME} status --json       # 机器可读状态
    ${PROG_NAME} config set K V      # 改配置（自动重启 + 失败回滚）

升级：重新运行本脚本即可   卸载：./install.sh --uninstall
EOF
