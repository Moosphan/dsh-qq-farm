#!/usr/bin/env bash
# 编译 macOS 自动化原语（需要 Xcode Command Line Tools）
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/bin"
xcrun --find swiftc >/dev/null || { echo "✗ 需要 Xcode Command Line Tools: xcode-select --install"; exit 1; }
swiftc -O macd.swift -o macd 2>&1 | grep -v "deprecated" || true
chmod +x macd
echo "✅ 已编译 $(pwd)/macd"
./macd winlist | head -3
