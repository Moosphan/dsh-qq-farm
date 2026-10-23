# QQ 经典农场 · macOS 自动化方案调研报告

> 目的：回答「有没有其他开源方案能在 Mac 上自动化执行 QQ 经典农场」
> 结论日期：本次调研（基于公开仓库与文档）
> 关联文档：[技术方案.md](./技术方案.md)

---

## 0. 结论先行

**有，而且不止一条。按"能否在 macOS 原生长跑"排序：**

| 排名 | 路线 | macOS 原生可行 | 工作量 | 风险 | 与 DSH 插件的契合度 |
|---|---|---|---|---|---|
| ① | **A. 协议层（Node.js + WebSocket/Protobuf）** | ✅ 完美，纯 Node 进程 | 中 | 高（封号/协议变） | ⭐⭐⭐⭐⭐ 与插件同语言、多开天然 |
| ② | **D. Android 真机/模拟器（uiautomator2 / Airtest）** | ✅ 好，跨 OS 一致 | 中 | 中 | ⭐⭐⭐⭐ 输入天然后台，多开成熟 |
| ③ | **C. 视觉层 macOS（cua-driver / SkyLight 私有 API）** | ⚠️ 技术上成立，v0.1 生态 | 中高 | 中高（私有框架） | ⭐⭐⭐⭐ 面板/协议完全复用 |
| ④ | **B. 视觉层 Windows（现方案，后台 Windows Agent）** | ❌ 需远端 Windows | — | 低 | ⭐⭐⭐ 面板复用，跨机部署 |

**关键更正**：上一版方案把 macOS 判定为"⚠️ 需要 POC 才能定论"。调研后发现 **D1 风险可以从"🔴 未知"降级为"🟡 已知可解"** —— 因为 Cua 团队已经用 Apple 私有框架 SkyLight 打通了"不抢焦点、不动光标地给 Chromium 系窗口投递点击"，而这正是小程序宿主的技术类型。详见 §3。

**同时要警惕一个反向发现**：协议路线（最省事的跨平台方案）与"真实客户端在线"是**互斥**的 —— 两者会互相踢下线。这直接影响产品定位（挂机 vs 边玩边挂）。详见 §2.4。

---

## 1. 项目全景（本轮检索到的开源实现）

### 1.1 协议层（Node.js，天然跨平台）

| 项目 | ★ | 状态 | 许可 | 说明 |
|---|---|---|---|---|
| [Penty-d/qq-farm-bot-ui](https://github.com/Penty-d/qq-farm-bot-ui) | 948 | 活跃 | MIT | **当前最能打的一个**。Node 20 + Express + Socket.io + Vue3 面板；Docker Compose 部署；**提供 macOS arm64/x64 预编译二进制** |
| [linguo2625469/qq-farm-bot](https://github.com/linguo2625469/qq-farm-bot) | 490 | 已归档 | — | 元老项目，协议细节文档最全（PB 解码工具、经验效率计算） |
| [XyhTender/qq-farm-automation-bot](https://github.com/XyhTender/qq-farm-automation-bot) | 442 | 半维护 | — | 是上面那个重构版的前身，README 里自己说"去看别人的更新版" |
| [maile456/qq-farm-bot](https://github.com/maile456/qq-farm-bot) | 209 | 停维护 | MIT | 多用户权限（JWT + AES 存储），支持微信/QQ（含 QQ 端 `SyncAll` 协议） |
| [QianChenJun/qq-farm-bot](https://github.com/QianChenJun/qq-farm-bot) | 233 | 停维护 | MIT | Electron + Vue3 桌面版，**源码已不公开**（仅发布仓库） |
| [sulimu2/qq-farm-bot](https://github.com/sulimu2/qq-farm-bot) | — | 停维护 | MIT | 同上系谱 |

**共同的实现方式**（读源码 README 得到的确证）：

1. 抓包获取小程序 WebSocket 连接 URL 中的 `code` 参数，用 Protobuf（`gatepb.Message`）承载业务消息；
2. 建立 WS 长连接 + 心跳保活，直接调用服务端接口：收获、播种、施肥、除草、除虫、浇水、偷菜、帮忙、领任务、出售；
3. 自己维护一份"作物效率表"算最优种植（`tools/calc-exp-yield.js`：经验效率 / 普通肥经验效率 / 净利润效率）；
4. 提供 PB 解码器做协议自检（`node client.js --decode`）。

**登录方式（关键差异）**：

- **QQ 端**：支持**扫码登录**自动获取 code（借 [lkeme/QRLib](https://github.com/lkeme/QRLib)），可长期自动续期；社区还有"通过 NapCat 自动更新 code"的玩法（NapCat 是 QQ NT 协议端）。
- **微信端**：作者明确写「wx 不会支持，不需要再提问（wx 无此类漏洞）」→ **只能手动抓包取 code**，过期即失效。
- Penty-d 版本改为"面板手动输入 Code" + "账号被踢下线自动删除" + "离线超时自动删除" —— 说明 code 生命周期短、会话易冲突。

### 1.2 视觉层（Windows）

| 项目 | ★ | 开源 | 特点 |
|---|---|---|---|
| [megumiss/qq-farm-copilot](https://github.com/megumiss/qq-farm-copilot) / [rainnight6 复刻](https://github.com/rainnight6/qq-farm-copilot) | 201 / 90 | ✅ GPLv3 | 我们已选定的参考实现：`PrintWindow` 后台截图 + `SendMessage` 后台点击 + `m.dll` 防抢焦点 |
| [flh155/qq-farm-bot-vision](https://github.com/flh155/qq-farm-bot-vision) | 206 | ✅ | OpenCV 版；**要求窗口在前台（可被遮挡）**，有 silence-click 静默点击分支；仍需 Windows |
| [hacker-frok/qq-farm-bot-ai](https://github.com/hacker-frok/qq-farm-bot-ai) | 399 | ❌ 仅发布 | "多模板 + 多尺度匹配"，支持隐藏小程序窗口；闭源，无法复用 |

这三者都是 Windows-only：`pywin32` / `user32` / `mss` 无 macOS 等价物。

### 1.3 桌面级"macOS 后台操作"基础设施（不是农场项目，但是解题关键）

| 项目 | ★量级 | 许可 | 能力 |
|---|---|---|---|
| [trycua/cua](https://github.com/trycua/cua)（Cua Driver） | 知名 | **MIT**（Driver 本体） | **跨 macOS / Windows / Linux 的后台 computer-use 驱动**；macOS 用 SkyLight 私有框架；提供 CLI + MCP(stdio) + TypeScript SDK `@trycua/cua-driver` + Python SDK |
| [koekeishiya/yabai](https://github.com/koekeishiya/yabai) | 很高 | MIT | 窗口管理；其 `window_manager_focus_window_without_raise` 正是 Cua 借鉴的"激活不置前"范式 |

### 1.4 设备侧（绕开桌面 OS 的第三条路）

| 项目 | 说明 |
|---|---|
| [wdf-wyh/wechat-farm](https://github.com/wdf-wyh/wechat-farm) | **真机微信 + uiautomator2 + EasyOCR**，明确"不用模拟器/Hook"，用于 Android 性能 benchmark |

这只证明了一件事：**在 Android 上用 ADB 驱动微信小程序是成熟路线**，Mac 只是宿主，不参与 UI 自动化。Airtest / scrcpy / uiautomator2 在 macOS 上都是一等公民。

---

## 2. 路线 A：协议层（强烈建议纳入）

### 2.1 为什么它在"Mac 支持"这件事上几乎无解可打

```
协议 bot = Node.js 进程 ⇄ 腾讯服务端 WebSocket
```

- **零 OS 依赖**：不截图、不点鼠标、不要窗口。Docker 能跑就能跑，macOS arm64 原生二进制已有成品（Penty-d 的 `package:release` 直接产出 `qq-farm-bot-macos-arm64`）。
- **多开是免费的**：一个账号一条 WS 连接，100 开也不占一个窗口。
- **"后台运行"概念消失**：它本来就不需要前台。
- **与 DSH 完美同构**：DSH 宿主半是 Node，协议 bot 也是 Node —— 可以**直接 in-process 集成**，连 Agent 进程都不用起。

### 2.2 与我们已有架构的对接方式（改动很小）

在 FAP 协议里加一种后端类型即可：

```
platform: "protocol"      # 新增：无窗口后端
```

- 实例模型从"窗口 + 调度"变成"账号 + WS 会话"；
- 面板的"实时截图"退化为"状态快照 + 日志流"（没有画面了，但地块地图/等级/金币/背包数据比截图更准）；
- 地块地图反而**更好**：协议层拿的是真实结构化数据，不用 OCR 猜倒计时；
- LLM 工具能力**更强**：可以直接读背包、作物效率表，做"帮我算最优种植方案"这类事。

### 2.3 优点 / 缺点

| 优点 | 缺点 |
|---|---|
| 跨平台零成本（Mac/Win/Linux/容器/树莓派） | **封号风险最高**：无真实客户端行为特征，服务端可识别 |
| 不需要窗口，CPU/内存占用极低 | 协议逆向，游戏更新即失效（多个项目已停维护就是证据） |
| 数据是结构化的（不是像素），可做深度分析 | 微信端**无法自动登录**，只能手动抓 code |
| 多开成本≈0 | 与"真实客户端在线"**互斥**（见 §2.4） |
| 现成 MIT 实现可参考/复用 | 生态在萎缩（大量项目停更、闭源化、转付费） |

### 2.4 ⚠️ 致命约束：协议 bot 与真实客户端互斥

从 Penty-d 的功能列表可以反推出事实：

- 「手动输入 Code」→ code 来自客户端登录后的一次性凭证；
- 「账号被踢下线自动删除」→ **客户端一登录，bot 就被踢；bot 一登录，客户端就被踢**。

也就是说，**你不能一边在 Mac 上开着农场窗口玩，一边让协议 bot 挂着同一个号**。

这决定了产品定位必须二选一：
- **挂机党**（不看画面，只要收益）→ 协议路线完美；
- **边玩边挂 / 想看画面 / 想手动干预**→ 必须视觉路线（窗口自动化）。

> 而参考项目（copilot）走的正是后者，所以它才会花大力气做后台截图 + 后台点击 + 多开窗口管理。

### 2.5 合规提示

协议层直接调用游戏服务端接口，比"视觉模拟操作"在证据链上更"显性"，**封号与追责风险显著更高**。如果做进插件，必须：
- 默认关闭、默认隐藏；
- 首次启用需二次确认并明示「此模式不模拟客户端行为，封号风险更高」；
- 不提供批量导入账号的能力。

---

## 3. 路线 C：视觉层 macOS —— 上一版方案的 D1 风险已解

### 3.1 上一版的担心是对的

我在原方案里写的担心（"小程序宿主是 Chromium/CEF，`CGEventPostToPid` 可能被渲染层过滤掉"）**被 Cua 的工程记录精确命中**：

> "`CGEvent.postToPid` … Works great on everything except Chrome. **Chrome filters synthesized events at the renderer IPC boundary.** If your click arrives without the telemetry the HID pipeline normally attaches to a real user gesture (a specific `mouseEventSubtype` byte, a `clickState` counter, a window-local coordinate stamp set by a private SPI), the renderer treats it as untrusted and **drops it silently**."
> —— [Inside macOS window internals](https://cua.ai/blog/inside-macos-window-internals)

### 3.2 但他们找到了解法：SkyLight 私有框架

三个关键原语（`dlopen` + `dlsym` 拿 `/System/Library/PrivateFrameworks/SkyLight.framework`）：

| 原语 | 作用 |
|---|---|
| `SLEventPostToPid` | 走 WindowServer 的"信任信封"投递事件，**Chromium 渲染层接受**（而 `CGEvent.postToPid` 被丢） |
| `SLPSPostEventRecordTo` | 翻转某个进程的 AppKit-active 状态但**不置前窗口**（yabai 用了多年的 `focus_window_without_raise` 范式） |
| `_AXObserverAddNotificationAndCheckRemote` | 让被遮挡的 Electron/Chromium 应用的 AX 树保持活跃（否则 Blink 认为"没人看"就暂停更新） |

还有一个非常巧的 trick —— **primer click**：先向 `(-1, -1)`（屏幕外任意窗口都不认领的坐标）发一对 down/up，Chromium 会丢弃它但**推进 user-activation 门禁**，随后真正的点击就被当作"可信手势的延续"而生效。

**这三件事合起来 = macOS 上的"后台点击且不抢焦点"，正是 Windows 上 `m.dll + SendMessage` 的等价物。**

### 3.3 可用性与成熟度

- **许可**：Driver 本体 **MIT**（注意：可选的 `cua-perception` 扩展是 AGPL，**不要装**，我们不需要它）；
- **接口**：CLI（`cua-driver call`）、MCP over stdio、TypeScript SDK `@trycua/cua-driver`（UniFFI 绑定，无 daemon 直连）——对我们非常友好，**Node 直接调**；
- **版本**：作者自述 "v0.1 早期预览"。这是主要风险；
- **平台**：macOS / Windows / Linux 三套，语义统一（Windows 侧同样以 `background` 为默认 dispatch，遇到 Chromium 坐标点击会**诚实返回 `background_unavailable`** 让你选择是否付前台代价）。

### 3.4 我们的选择：依赖它，还是抄它的 20 行 Swift？

两条路：

| 方案 | 成本 | 风险 |
|---|---|---|
| **C-1 依赖 cua-driver**（Node 调 CLI/SDK） | 低（约 2–3 人日接入） | v0.1 可能改接口；我们被它的 bug 拖住 |
| **C-2 自己写 20 行 Swift**（`dlopen` SkyLight + `dlsym` 三个符号） | 中（1 人日 POC + 少量加固） | 私有 API 随 macOS 版本变；需要自己踩 primer click 的坑 |

**建议：先 C-1 快速验证可行性，POC 通过后评估是否切 C-2 自持**（因为农场是长期项目，把命门押在 v0.1 的第三方上有风险；而 Cua 已经把"怎么做"写成了公开文章，自持成本不高）。

### 3.5 macOS 视觉层剩下的问题

| 问题 | 结论 |
|---|---|
| 后台截图 | `ScreenCaptureKit`（macOS 14+）可按窗口抓，遮挡可截、最小化不可截；需屏幕录制权限 |
| OCR | Apple **Vision framework** 中文识别质量好于 RapidOCR，且**无需模型文件**（省掉 onnxruntime + 几十 MB 模型） |
| 模板匹配 | `opencv-python` 有 macOS 轮子；或直接靠 Vision 的结构化输出减少模板依赖 |
| 多开 | 多个微信/QQ 账号实例（是否支持取决于用户环境）；每个窗口按 pid+windowId 独立定位 |
| 权限 | 需要「屏幕录制」+「辅助功能」两个 TCC 授权，首次配置要让用户点两次系统弹窗 |

---

## 4. 路线 D：设备侧（被低估的选项）

### 4.1 为什么它可能是最省心的"Mac 方案"

```
Mac（DSH + Agent）──adb/scrcpy──▶ Android 模拟器或真机上的微信/QQ 小程序
```

- **输入天然后台**：`adb shell input tap` 不经过宿主机焦点系统，Mouse 不会被抢 —— 这恰恰是 macOS 桌面方案最难的部分，在 Android 上根本不存在；
- **多开成熟**：MuMu Pro（Apple Silicon 版）、Genymotion、Android Studio AVD 都支持多实例；
- **截图免费**：`adb exec-out screencap -p`；
- **OCR 成熟**：EasyOCR / PaddleOCR，`wdf-wyh/wechat-farm` 就是这么做的；
- **与宿主机 OS 完全解耦**：Windows 与 Mac 用同一套代码。

### 4.2 缺点

- 需要一台 Android 环境（模拟器吃 CPU/内存，多开更吃）；
- 小程序要在 Android 微信/QQ 里跑（可用，但版本要与游戏匹配）；
- 画面链路多一跳（编码/传输），实时性略差。

### 4.3 什么时候选它

**当"必须视觉路线" + "必须在 Mac 上" + "不想碰私有 API" 同时成立时**，这是唯一无风险解。

---

## 5. 横向对比（决策表）

| 维度 | A 协议 (Node) | B 视觉 Windows | C 视觉 macOS (SkyLight) | D Android 设备 |
|---|---|---|---|---|
| Mac 原生 | ✅ 完美 | ❌ 需远端 Win | ✅ 可行 | ✅ 可行 |
| 后台运行 | 无需窗口 | ✅ 已实现 | ✅ 可行（不抢焦点） | ✅ 天然 |
| 多开 | ✅ 极廉价 | ✅ 已验证 | ⚠️ 取决于多实例 | ✅ 成熟 |
| 画面/人工干预 | ❌ 无画面 | ✅ 有 | ✅ 有 | ✅ 有 |
| 与真实客户端共存 | ❌ **互斥** | ✅ 共存 | ✅ 共存 | ✅ 共存 |
| 数据精度 | ✅ 结构化 | ⚠️ OCR 猜 | ⚠️ OCR 猜 | ⚠️ OCR 猜 |
| 封号风险 | 🔴 最高 | 🟡 中 | 🟡 中 | 🟡 中 |
| 游戏更新影响 | 🔴 协议变即废 | 🟡 模板变 | 🟡 模板变 | 🟡 模板变 |
| 开发工作量 | 中 | 中（已选） | 中高 | 中高 |
| 许可 | MIT（多数） | GPLv3（copilot） | MIT | 各自 |
| 与 DSH 插件契合 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ |

---

## 6. 对原技术方案的影响（修订建议）

### 6.1 D1 风险降级

原方案：

> D1 后台输入的平台鸿沟 … 🔴 高 … M0 必做 POC；不通则 Mac 定位控制台/前台模式

**修订为**：

> D1 … 🟡 中 … 技术路线已知（SkyLight `SLEventPostToPid` + `SLPSPostEventRecordTo` + primer click），风险转移到"私有 API 随 macOS 版本变动"与"cua-driver v0.1 成熟度"。

### 6.2 建议把架构从"双引擎"扩展为"三引擎"

```
DSH 插件（面板 + 控制面 + LLM 工具）
   ⇄ FAP 协议（REST + WS）
      ├── Agent: win-vision      （参考项目内核，复用）
      ├── Agent: mac-vision      （SkyLight / cua-driver + Vision OCR）
      └── Agent: protocol        （Node，无窗口，Mac/Win 通吃）   ← 新增
```

三个后端都实现同一份 FAP，面板按 `capabilities` 自适应（`hasFrames` / `hasPlots` / `hasInventory` / `canCoexistWithClient`）。

### 6.3 分期建议（修订）

| 里程碑 | 原计划 | 修订 |
|---|---|---|
| M0 | 骨架 + POC | **+ macOS SkyLight POC（1 天）**、**+ 协议 bot 跑通验证（0.5 天）** |
| M1 | Windows 视觉全功能 | 不变；**若 POC 顺利，并行起 mac-vision** |
| M1.5 | — | **新增：protocol 后端 MVP**（收获/种植/施肥/偷菜/帮忙/领奖 + 状态面板）。工作量比视觉版小得多，且能立刻在 Mac 上演示 |
| M2 | 体验与观测 | 不变 |
| M3 | macOS 视觉 | 风险已降，工作量从 10–18 天 → 8–14 天 |

### 6.4 需要你决策的新问题

1. **要不要引入协议后端？**（我建议要 —— 它是"Mac 上立刻可用"的最短路径；但必须默认关闭 + 强风险提示）
2. **产品定位**：挂机优先（协议）还是"边玩边挂"（视觉）？还是两条线都留、由用户选？
3. **macOS 视觉层**：依赖 `cua-driver`（快，被 v0.1 绑架）还是自持 20 行 Swift（慢一点，自主）？
4. **是否考虑 Android 设备路线**作为"Mac 无风险"兜底？（若你有 Android 手机/能跑模拟器，这是最稳的一条）

---

## 7. 参考链接

**协议层**
- https://github.com/Penty-d/qq-farm-bot-ui （948★，MIT，有 macOS arm64 二进制）
- https://github.com/linguo2625469/qq-farm-bot （490★，协议细节最全，已归档）
- https://github.com/maile456/qq-farm-bot （209★，微信/QQ 双平台 + SyncAll）
- https://github.com/lkeme/QRLib （QQ 扫码登录取 code）

**视觉层（Windows）**
- https://github.com/megumiss/qq-farm-copilot （原始实现，GPLv3）
- https://github.com/rainnight6/qq-farm-copilot （我们参考的 fork）
- https://github.com/flh155/qq-farm-bot-vision （需前台可遮挡）
- https://github.com/hacker-frok/qq-farm-bot-ai （多模板多尺度，闭源）

**macOS 后台操作基础设施**
- https://cua.ai/blog/inside-macos-window-internals （SkyLight 逆向细节，**本文最关键的一篇**）
- https://github.com/trycua/cua （MIT Driver）
- https://cua.ai/docs/cua-driver/concepts/platform-support （平台能力边界）
- https://github.com/koekeishiya/yabai （focus-without-raise 范式来源）

**设备侧**
- https://github.com/wdf-wyh/wechat-farm （Android 真机 + uiautomator2 + EasyOCR）
- https://airtest-chinese.readthedocs.io/zh-cn/latest/README_MORE.html （Airtest 多端支持）

**背景**
- https://it.ithome.com/archiver/0/934/172.htm （QQ 经典农场 PC 版：基于微信/QQ 桌面版小程序，支持 Win/Mac）

---

*调研结束。若你认可 §6.4 的方向，我会把本文结论合并进 `技术方案.md` 并出 v1.0。*
