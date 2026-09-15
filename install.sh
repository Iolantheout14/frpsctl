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
#
set -euo pipefail

# ---------------------------------------------------------------- 常量

PROG_NAME="frpsctl"
VENV_DIRNAME="frpsctl-src"          # 与 pip/pipx 安装的目录区分开
MIN_PY_MINOR=11                     # 需要 Python >= 3.11（标准库 tomllib）
SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

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
  ./install.sh [选项]

选项：
  --system        安装到 /usr/local（全局，需要 root）
  --prefix DIR    自定义安装前缀（默认：普通用户 ~/.local，root /usr/local）
  --uninstall     卸载（删除 venv 与命令，不动实例数据）
  --no-verify     跳过安装后的自检
  -h, --help      显示本帮助

说明：
  · 默认装到 ~/.local/share/${VENV_DIRNAME}/venv，命令注册到 ~/.local/bin/${PROG_NAME}
  · 不下载 frps 二进制；装完后自行运行：${PROG_NAME} install && ${PROG_NAME} init
  · 反复运行本脚本即为升级（会重新安装依赖并重写命令）

支持的平台：Linux；Python >= 3.${MIN_PY_MINOR}
EOF
}

# ---------------------------------------------------------------- 参数

MODE="user"
PREFIX=""
DO_VERIFY=1

while [ $# -gt 0 ]; do
  case "$1" in
    --system)     MODE="system"; shift ;;
    --prefix)     PREFIX="${2:-}"; [ -n "$PREFIX" ] || die "--prefix 需要一个目录参数"; MODE="custom"; shift 2 ;;
    --prefix=*)   PREFIX="${1#*=}"; MODE="custom"; shift ;;
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

case "$MODE" in
  user)    PREFIX="${HOME}/.local" ;;
  system)  PREFIX="/usr/local" ;;
  custom)  : ;;
esac

# root 但没给 --system：给出提示而不是默默装到 /root
if [ "$MODE" = "user" ] && [ "$(id -u)" -eq 0 ]; then
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

if [ "$MODE" != "uninstall" ]; then
  PYTHON="$(find_python)" || die "找不到 Python >= 3.${MIN_PY_MINOR}。请先安装（例如 apt install python3.11）"
  ok "Python：$("$PYTHON" -V 2>&1)（$(command -v "$PYTHON")）"

  [ -f "${SRC_DIR}/pyproject.toml" ] || die "当前目录看不到 pyproject.toml：${SRC_DIR}
   请在源码根目录运行本脚本"
  ok "源码目录：${SRC_DIR}"

  "$PYTHON" -c "import venv" 2>/dev/null || die "该 Python 缺少 venv 模块。Debian/Ubuntu 上请装：apt install python3-venv"
  ok "venv 模块可用"
fi

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

step "创建虚拟环境"
mkdir -p "$(dirname -- "$VENV_DIR")" "$BIN_DIR"
if [ -x "${VENV_DIR}/bin/python" ]; then
  info "复用已存在的 venv：${VENV_DIR}"
else
  "$PYTHON" -m venv "$VENV_DIR"
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
