# Farm Agent · macOS 后端

基于 **Apple Vision OCR + CoreGraphics 事件注入** 的 macOS 原生自动化后端，实现 FAP v1，
与插件侧协议一致，面板零改动即可切换。

## 为什么不用 OpenCV 做主力

实测结论（见 [`docs/poc-mac-实测报告.md`](../../docs/poc-mac-实测报告.md)）：

- mac 上 **Vision OCR 一次就能读出整屏 20–80 个文本块，带像素坐标**（600–1100 ms），零模型依赖；
- 按钮的点击目标直接由"文字 + 坐标"给出，比模板匹配更抗分辨率/DPI/皮肤变化；
- OpenCV 只在"纯图标、无文字"的元素上才需要，且必须重新采集 Mac 模板（Windows 模板不可用）。

所以这里的策略是：**OCR 定位为主，颜色/模板为辅**。

## 依赖

```bash
agent/mac/build.sh          # 编译 bin/macd（需要 Xcode Command Line Tools）
python3 -c "import PIL"     # Pillow（缩帧用）
```

系统授权（一次性，在「系统设置 → 隐私与安全性」里给你的终端/DSH 宿主勾选）：

- **屏幕录制** —— 截图窗口
- **辅助功能** —— 注入鼠标事件

`bin/macd --trust` 可自查（本仓库用 `macd` 直接跑即可，见下）。

## 运行

```bash
# 只识别不点击（安全模式，用于回归）
python3 agent/mac/macagent.py --port 4571 --dry-run

# 正常模式
python3 agent/mac/macagent.py --port 4571
```

插件在 macOS 上会**自动选择**这个后端（`lib/index.js: defaultAgentCommand`）：
检到 `agent/mac/bin/macd` 存在就用它，否则回落到 Mock。
也可以显式指定：

```yaml
# cordis.patch.yml
command: ['python3', '{packageRoot}/agent/mac/macagent.py', '--port', '4571']
```

## 已实现的能力

| 能力 | 状态 | 说明 |
|---|---|---|
| 窗口发现（含窗口重建后的重绑定） | ✅ | 每次循环重新 `winlist`，wid/pid 变了自动跟上 |
| 后台截图（窗口被遮挡可截） | ✅ | `CGWindowListCreateImage`，Retina @2x |
| OCR + 数值解析（等级/金币/经验） | ✅ | 万/亿单位会换算成整数 |
| 页面判定（主页/仓库/公告弹窗） | ✅ | 见 `PAGE_SIGNATURES`，判不出就只报错不动手 |
| 关闭公告弹窗 | ✅ | 点 OCR 证明无文字的留白区 |
| **一键收获 / 一键务农** | ✅ | **实测生效**：可摘地块 22 → 0，经验 3757 → 2.9万 |
| 播种 / 施肥 / 偷菜 / 帮忙 / 巡查 | ❌ M1 | 需要更多页面签名与地块定位 |
| 拖拽/滑动（地块翻页） | 🟡 原语已就绪 | `macd drag`，尚未接入业务 |

## 关键实现细节（踩过的坑）

1. **`acceptsFirstMouse = false`**：macOS 窗口第一次点击只激活窗口，不触发控件。
   Agent 的 `click(repeat=2)` 会连点两次（间隔 250 ms）。**这是"点了没反应"最常见的原因。**
2. **点击有三种模式，按目标选**（`macd` 已全部实现）：

   | 模式 | 命令 | 适用 | 代价 |
   |---|---|---|---|
   | `ghost`（**Agent 默认**） | `click --system --ghost` | 任何窗口，含 QQ/微信小程序容器 | 约 100ms 光标闪一下；窗口会被激活 |
   | `bg` | `bgclick <pid> <wid> x y` | 仅 Chromium 系（Chrome/Electron 实测可） | 无（光标不动、前台不变） |
   | `system` | `click --system --warp` | 兜底 | 光标留在目标处 |

   **实测结论**：QQ 小程序容器不接受 SkyLight 注入（同配方在 Chrome 上成立），
   所以游戏走 `ghost`。真后台要点见 `docs/poc-mac-实测报告.md` §8.4。
3. **窗口会在两步之间移动/缩放**，坐标必须点击前重取窗口矩形（Agent 每轮 `refresh_windows()`）。
   窗口被误缩放时可用 `agent/mac/bin/axwin <pid> --size 411,771 --pos 551,50` 恢复。
4. **按钮文案会变**：有作物可摘时是「一键收获」，没有时是「一键务农」，所以 `HARVEST_LABELS` 是一个列表。
5. **动作后必须验证**：点击后重新抓帧对比 OCR 指纹，没变化就记 `E_STUCK`，绝不无限重点。
6. **白名单点击**：只有 `CLICKABLE` 里的文字才会被点击；「出售 / 批量出售」这类破坏性按钮
   永远不在白名单内。

## 安全边界

- 默认 `state = idle`，**不会自动开始**；必须由面板或 API 显式 `start` / `run_once`。
- `--dry-run` 只识别不点击，可用于在陌生账号上先验证识别质量。
- 单实例单 worker 线程，动作串行，不存在并发点击。
- 日志把每一次点击的目标、坐标、置信度都打出来（`action` / `conf` 字段），可回溯。

## 已知限制

- 子页面（如仓库）返回主页的控件尚未定位，遇到会报 `E_PAGE_UNKNOWN` 并停手。
- 地块倒计时/成熟状态未实现（M1：需要地块定位 + OCR ROI）。
- 多开：每个窗口一条实例，但系统级点击会抢焦点，体验差（真后台解决后消失）。
