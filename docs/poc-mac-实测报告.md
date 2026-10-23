# POC 实测报告 · macOS 原生路线

> 实测环境：macOS 14.1.2 (arm64)，QQ 6.9.98 / QQ 小程序容器 `QQEXMiniProgram`，
> 游戏：《QQ经典农场》Version 1.15.1.4，窗口 411×771 @(551,50)，Retina @2x
> 结论日期：本次实测（在你的机器上真机跑的）

---

## 0. 一句话结论

**"识别"这一半在 Mac 上今天就能用，而且比 Windows 的模板匹配更强；"控制"这一半能点得动，但纯后台（不动光标）还差一步。**

| 能力 | 结论 | 证据 |
|---|---|---|
| 找到游戏窗口 | ✅ | `CGWindowListCopyWindowInfo` → `QQ小程序 / QQ经典农场 / pid 23459` |
| 后台截图（窗口被遮挡也能截） | ✅ | `screencapture -x -o -l <wid>` → 822×1542，窗口被 Safari 遮住仍成功 |
| 图像识别 | ✅ | Apple Vision OCR 一次读 24–81 个文本块，带像素坐标，600–1100 ms |
| 坐标换算 | ✅ | 窗口内图像 px ÷ 2 + 窗口原点；与全屏 OCR 交叉验证一致 |
| 点击（系统级） | ✅ | `CGEventPost + CGWarpMouseCursorPosition` → 成功打开仓库页 |
| 拖拽/滑动 | ✅ | 系统级 drag 序列生效 |
| 点击（纯后台，不动光标） | ⚠️ 未成功 | `CGEventPostToPid` 与 SkyLight `SLEventPostToPid` 单独使用均被丢弃 |
| 键盘（纯后台） | ⚠️ 未成功 | `postToPid` 发 Escape 无反应 |

---

## 1. 窗口与截图

```
wid=64132 pid=23459 layer=0 411x771 @(551,50) owner=QQ小程序 title=QQ经典农场
宿主进程: .../QQ/versions/6.9.98-51102/QQUpdate.app/.../QQEXMiniProgram
```

- 抓到的是 **822×1542**（正好是 411×771 的 @2x），`-o` 去掉了窗口阴影，所以图像坐标与窗口坐标严格成 2:1 关系。
- 抓取耗时 < 200 ms（含进程启动），**可以做到 5–10 fps 的连续抓帧**（Windows 那边 `PrintWindow` 的同等能力在 Mac 上不需要私有 API）。

## 2. 识别（Vision OCR 实测输出）

首屏（主页）识别结果节选 —— 注意它连坐标一起给：

```
[ 317,  21] QQ经典农场            ← 标题
[ 721, 196] 3.4亿                 ← 金币
[ 230, 198] 沐风  [208,237] 91  [255,238] 3.4万/33万   ← 昵称/等级/经验
[ 136, 342] 送福利  [718, 374] 商城  [687, 497] 奶茶热销中
[ 360,1178] 一键务农              ← 核心按钮 87×25
[ 141,1264] 等级提升至92级   [140,1298] （91/92）
[  47,1447] 仓库 [170,1447] 商店 [295,1447] 宠物 [423,1447] 图鉴 [546,1447] 装扮 [703,1447] 好友
[ 443,1503] ID:1261383285| Version:1.15.1.4
```

切到仓库页后识别到 81 个块：`超变果实/果实/种子/道具` 四个 tab、`格子上限：66/210`、`批量锁定`、`出售/批量出售` 等。

**这意味着 Mac 上不必依赖模板匹配做"文字类 UI"** —— 按钮的点击目标可以直接由 OCR 文本 + 坐标给出，比 `cv2.matchTemplate` 更抗分辨率与 DPI 变化。模板匹配只留给纯图标（无文字）的元素。

## 3. 坐标换算（两种方法交叉验证）

| 方法 | 仓库按钮坐标 |
|---|---|
| 窗口抓帧：图像 (77,1464) ÷2 + 原点 (551,50) | 屏幕 **(589.5, 782)** |
| 全屏抓帧 OCR：图像 (1147,1546) ÷2 | 屏幕 **(589.8, 781.8)** |

两法一致 → 坐标体系可信。

## 4. 输入投递（四组对照实验）

| # | 方式 | 结果 | 判定 |
|---|---|---|---|
| A | `CGEvent.postToPid` 点击「仓库」 | 底部导航区像素差 **0**，页面未变 | ❌ 被丢弃 |
| B | SkyLight `SLEventPostToPid`（符号 `0x193646d44` 已定位）点击「仓库」 | 页面未变（OCR 无增减） | ❌ 单独使用无效 |
| C | `CGEventPostToPid` 键盘 Escape | 无反应 | ❌ 被丢弃 |
| D | `CGEvent.post(tap:.cghidEventTap)` + `CGWarpMouseCursorPosition` | **成功打开仓库页**（81 块） | ✅ 生效 |

**观察到的关键细节**：macOS 默认 `acceptsFirstMouse = false`，**第一次点击只激活窗口**，需要对同一目标连点两次（或先激活再点）。实测中第一次系统点击只让标题栏控件变化，第二次才真正导航。

### 4.1 为什么 A/B 失败（与 Cua 的记录对照）

Cua 团队逆向的结论是：Chromium 渲染层在 renderer IPC 边界做"用户手势白名单"校验，纯 `postToPid` 缺三样东西：

1. `mouseEventSubtype` 字节
2. `clickState` 计数
3. **由私有 SPI 写入的窗口本地坐标戳**

另外还需要 yabai 的 **"激活不置前"**（`SLPSPostEventRecordTo`）先让目标进程成为事件路由的焦点，`SLEventPostToPid` 才起作用。我们的 B 实验**只做了后半步**，所以仍然失败 —— 这不是"不可能"，而是"还差一个前置步骤"。

## 5. 对方案的影响

### 5.1 立即可以交付的形态：「半后台」模式

```
抓帧（后台，不动用户） → Vision OCR 定位 → 光标 warp + 系统点击 → 再抓帧验证
```

- **能做的事**：一键务农、收获、播种、偷菜、领奖…… 全部功能都成立。
- **代价**：鼠标会跳到按钮上再点击；多开时会抢焦点（每次点击把目标窗口带到前台）。
- **适用**：单账号挂机、人不在电脑前、或愿意"让机器自己点一会儿"的场景。

这个形态 **今天就能跑通**，而且不需要任何私有 API、不需要第三方库。

### 5.2 真后台模式（多开必需）需要补的一步

两条路，都值得做 POC：

| 路线 | 做法 | 成本 | 风险 |
|---|---|---|---|
| **B-1 自研补齐** | 复刻 yabai 的 `SLPSPostEventRecordTo`（激活不置前）+ `SLEventPostToPid` + primer click | 1–2 天 | 私有 API，随 macOS 版本变 |
| **B-2 用 cua-driver** | MIT 许可，已封装好上述三件套，对外给 CLI/MCP/TS SDK | 0.5 天接入 | v0.1 早期预览，接口可能变 |

### 5.3 识别层选型（Mac）

| 用途 | 方案 |
|---|---|
| 文字类按钮/数值（等级、金币、倒计时、tab 名） | **Vision OCR**（实测最优，零模型） |
| 纯图标（无文字元素） | OpenCV 模板匹配（需重新采集 Mac 模板，Windows 模板不可用） |
| 地块状态（成熟/空地/可升级） | OCR 倒计时 + 地块底色（HSV 聚类），不需要模板 |

> 结论：Mac 上 **OpenCV 只承担"图标模板匹配"这一小块**，主力是 Vision OCR + 颜色分析。这比 Windows 版纯模板匹配的鲁棒性更好。

## 6. 本次实测遗留

- 游戏当前停在**仓库页**（点击测试的副作用），需要人工点一次返回——我没有找到页面内的返回控件（横扫了头部左右区域、试过容器标题栏、试过右滑手势，均未触发返回）。
- 这也是一条**有用的负面情报**：这个游戏的子页面返回控件不是文字、也不是常见位置，M1 做"页面收敛"时必须先把它找出来（建议：采集"从子页回主页"的操作录屏，逐帧比对定位）。

## 7. 第二阶段：真机跑通（Agent 已落地）

在 Agent 里把上面这些原语串成闭环（`agent/mac/macagent.py`），并实测了真实游戏动作：

| 步骤 | 观测 |
|---|---|
| 窗口发现 | `wid=64312 pid=26341 owner=QQ小程序 title=QQ经典农场` |
| 首次 OCR | 31 块；识别出「游戏更新公告」弹窗 |
| 关闭弹窗 | 点左侧留白（OCR 证明无文字）→ 弹窗消失 |
| 页面切换 | 判定 `popup_notice → main` |
| 主页面识别 | 22 个地块标「可摘」，按钮文案为「**一键收获**」 |
| 点击收获 | 双点（绕过 acceptsFirstMouse）→ **可摘地块 22 → 0** |
| 数值验证 | 经验 `3757/4.1万` → `2.9万/4.1万`；OCR 复核无「可摘」残留 |

### 7.1 新增的两个关键坑（都已修复）

1. **`acceptsFirstMouse = false`** —— 第一次点击只激活窗口。
   Agent 的 `click(repeat=2)` 连点两次（间隔 250 ms）后才真正落到控件上。
2. **按钮文案随状态变** —— 有作物可摘时是「一键收获」，没有时是「一键务农」。
   必须用标签列表 `HARVEST_LABELS` 匹配，不能写死。

### 7.2 端到端联调（14 项断言全绿）

`node scripts/check-mac.mjs`：真实 mac Agent + 插件路由 + 鉴权 + SSE，
验证了 `/api/health` 上报 `agentId=mac`、`/api/instances`、`state` 含 24 格地块、
`frame` 返回 72 KB 真实 JPEG、无 cookie 的 POST 401、SSE 同时推送 state 与真实截图帧。

## 8. 第三阶段：真后台点击攻防（结论：Chromium 可行，QQ 容器不行）

复刻 cua-driver（MIT）的 `click_at_xy_chromium` 配方后，做了严格对照实验。
**判定标准**：目标窗口在后台（另一个 App 是前台）、光标位置前后一致、页面/计数器有变化。

| 目标 | 系统级 warp 点击 | **幽灵点击**（瞬移→点→还原光标） | **SkyLight 真后台** |
|---|---|---|---|
| Google Chrome（Chromium/Electron） | ✅ | ✅ | **✅ 成立** |
| QQ 小程序容器 `QQEXMiniProgram` | ✅ | **✅ 成立** | ❌ 不响应 |

### 8.1 Chromium 真后台点击成立（实验证据）

```
前台 = 访达/tests        ← 必须不是 Chrome
bgclick → CLICKS:3 → CLICKS:4   送达 ✅
光标 (326,459) 前后一致 ✅      前台仍是 访达/tests ✅
```

### 8.2 QQ 小程序容器不接受 SkyLight 注入

同一条配方、同一坐标、同一个 wid，在游戏上**页面没有任何变化**；
换成 `--system --ghost` 立刻生效（仓库页打开）。

推测原因：该容器不是标准 Chromium 渲染管线（binary 只链 AppKit/Foundation，
含 `ElectronMain` 字符串，渲染很可能在独立 helper 进程），对合成事件有额外校验。

**对方案的影响**：Mac 上多开"真后台"对这款游戏暂不可得，除非使用 cua-driver 本体
（其内部还有一层 `prepare_background_pixel_click` + 可能的 AX 兜底）或换目标（微信容器未测）。

### 8.3 幽灵点击：游戏上的可用最优解

`macd click <pid> <x> <y> --system --ghost`：记下用户光标 → 瞬移 → 点击 → 移回原位。

```
页面: 主页 → 仓库页（格子上限：27/210 / 超变果实 / 批量锁定）
光标: (428,561) → 点击瞬间 → (428,561)   还原 ✅
```

代价：约 100 ms 内光标会闪一下；窗口仍会被激活（抢焦点）。
多开时仍需要一个窗口接一个窗口地跑，但**用户的光标不会被留在别处**。

### 8.4 复刻要点（照抄 cua，逐条都有坑）

1. **事件源必须是 `CGEventSourceStateID.hidSystemState`**（`CGEventSource(stateID:)`），用 `nil` 无效。
2. **五段事件流**，`f0` 是手势阶段标记：
   `mouseMoved@目标(f0=2)` → `down@(-1,-1)(f0=1)` → `up@(-1,-1)(f0=2)` → `down@目标(f0=3)` → `up@目标(f0=3)`
3. **必填字段**（`SLEventSetIntegerValueField`）：
   `f0`=阶段、`f1`=clickState、`f3`=按键号、`f7`=3(NSEventSubtypeTouch)、
   **`f40`=目标 pid**（Chromium 合成事件过滤器）、
   **`f51`/`f91`/`f92`=CGWindowID**（窗口路由）、`f58`=同一个 gesture id。
4. **`CGEventSetWindowLocation` 传屏幕坐标**（不是窗口本地坐标）——我最先传错，白试了半小时。
5. **前置 `activate_without_raise`**：0xF8 字节记录，`[0x04]=0xF8`、`[0x08]=0x0D`、
   `wid` 小端写在 `0x3C..0x3F`、`[0x8A]=0x01`(focus) / `0x02`(defocus)，
   用 `SLPSPostEventRecordTo(psn, buf)` 投递，之后等 50 ms。

### 8.5 过程中踩的三个坑（都已进 Agent）

1. `acceptsFirstMouse = false`：首次点击只激活窗口，必须连点两次。
2. **窗口会在两次操作之间移动/缩放**（激活时尤其明显），坐标必须**点击前重取窗口矩形**。
   实测中因此有 3 次点击落在窗口外。Agent 现在每轮 `refresh_windows()` 并做边界校验。
3. 窗口曾被误缩放到 160×324（点击扫到了缩放边），用 AX API 恢复：
   `axwin <pid> --size 411,771 --pos 551,50`。

## 9. 复现方式

```bash
cd poc/mac
swiftc -O winlist.swift   -o winlist
swiftc -O ocr.swift       -o ocr
swiftc -O postclick.swift -o postclick
swiftc -O sendkey.swift   -o sendkey
swiftc -O drag.swift      -o drag
swiftc -O hittest.swift   -o hittest

./winlist                                  # 找窗口
./ocr frame.png                            # 识别
./hittest 590 782                           # 该点最上层是谁
./postclick <pid> 590 782 --system --warp   # 系统级点击（会动光标）
./postclick <pid> 590 782                   # 纯后台点击（当前无效）
./postclick <pid> 590 782 --sky             # SkyLight 通道（单独无效）
python3 probe_click.py --label 仓库 [--system|--sky]   # 完整闭环探针
```
