#!/usr/bin/env bash
# =============================================================================
# pa-data-source 服务器部署脚本（Linux x86_64 / arm64）
# -----------------------------------------------------------------------------
# 用法:
#   bash server_setup.sh                       # 自动装依赖 + 下载最新 Linux gate-cli
#   bash server_setup.sh --gate-cli-version=v0.7.9   # 指定 gate-cli 版本
#
# 功能:
#   1. 创建 Python 虚拟环境 .venv 并安装依赖 (pyyaml websocket-client)
#   2. 下载对应平台的 gate-cli（官方 GitHub Releases）并校验 SHA256
#   3. 创建运行时目录 data/ logs/
#   4. 输出后续启动步骤
#
# 详细部署说明见 SERVER.md
# =============================================================================
set -e

cd "$(dirname "$0")"
REPO="gate/gate-cli"

echo "[1/4] 安装 Python 依赖（虚拟环境 .venv）..."
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet pyyaml websocket-client

echo "[2/4] 安装 gate-cli（Linux）..."
OS="linux"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)        ARCH="amd64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  *) echo "不支持的架构: $ARCH（仅支持 x86_64 / arm64）"; exit 1 ;;
esac

VERSION=""
for arg in "$@"; do
  case "$arg" in
    --gate-cli-version=*) VERSION="${arg#*=}" ;;
  esac
done
if [ -z "$VERSION" ]; then
  VERSION="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | grep '"tag_name"' | sed 's/.*"tag_name": *"\([^"]*\)".*/\1/')"
fi
[ -z "$VERSION" ] && { echo "无法获取 gate-cli 最新版本号"; exit 1; }

BARE="${VERSION#v}"
ARCHIVE="gate-cli_${BARE}_${OS}_${ARCH}.tar.gz"
BASE="https://github.com/${REPO}/releases/download/${VERSION}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "  下载 ${ARCHIVE} ..."
curl -fsSL "${BASE}/${ARCHIVE}" -o "${TMP}/${ARCHIVE}"
curl -fsSL "${BASE}/checksums.txt" -o "${TMP}/checksums.txt"

EXPECTED="$(grep -F "  ${ARCHIVE}" "${TMP}/checksums.txt" | awk '{print $1}')"
ACTUAL="$(sha256sum "${TMP}/${ARCHIVE}" | awk '{print $1}')"
if [ -z "$EXPECTED" ] || [ "$EXPECTED" != "$ACTUAL" ]; then
  echo "SHA256 校验失败，已中止"; exit 1
fi

tar -xzf "${TMP}/${ARCHIVE}" -C "${TMP}" "gate-cli"
install -m 755 "${TMP}/gate-cli" "./gate-cli"
echo "  gate-cli ${VERSION} 已安装 → ./gate-cli"

echo "[3/4] 创建运行时目录..."
mkdir -p data logs

echo "[4/4] 部署完成。"
echo "------------------------------------------------------------"
echo "启动（密钥用环境变量，不落盘）:"
echo "  export GATE_API_KEY=your_api_key"
echo "  export GATE_API_SECRET=your_api_secret"
echo "  ./.venv/bin/python watchdog.py"
echo ""
echo "验证:"
echo "  curl http://127.0.0.1:18080/health"
echo ""
echo "systemd 开机自启示例见 SERVER.md"
echo "------------------------------------------------------------"
