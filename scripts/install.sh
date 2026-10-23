#!/usr/bin/env bash
# 把本包安装进指定的 DSH profile（默认 web），并提示重启。
#
#   ./scripts/install.sh          # 装进 web profile
#   ./scripts/install.sh myprof   # 装进自定义 profile
#
# 说明：本地开发用 pnpm 的 link: 协议，源码改动后无需重装，
# 但**宿主半（lib/index.js）改动需要重启 DSH**，浏览器半（lib/client.js）改动靠 HMR 自动重载。
set -euo pipefail

PROFILE="${1:-web}"
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DSH_BIN="dsh"
if ! command -v "$DSH_BIN" >/dev/null 2>&1; then
  if command -v npx >/dev/null 2>&1; then
    DSH_BIN="npx --yes @deepseek-ai/dsh"
    echo "⚠️  未找到全局 dsh，回退到: $DSH_BIN"
  else
    echo "✗ 既没有 dsh 也没有 npx，请先安装 Node.js 20+" >&2
    exit 1
  fi
fi

echo "→ profile : $PROFILE"
echo "→ 包目录  : $PKG_DIR"
echo "→ 安装命令: $DSH_BIN plugin --profile $PROFILE add link:$PKG_DIR"
echo

$DSH_BIN plugin --profile "$PROFILE" add "link:$PKG_DIR"

cat <<'EOF'

✅ 已写入 profile 的 package.json（dsh.profile.bundles 会自动带上本包的 patch 层）。

下一步（必须重启宿主，loader entry 只在启动时读取）：
  1) 停掉当前正在跑的 DSH（如果是在终端里跑的，Ctrl-C）
  2) 重新启动：dsh web   （或 dsh --profile web）

验证：
  · 工作台右下角应出现「🌾 农场 0/0」胶囊（首次启动插件会自动拉起 Mock Agent）
  · 点开应能看到 2 个 Mock 实例与合成画面
  · 若只有胶囊没有实例，检查日志里的 dsh-qq-farm 输出
EOF
