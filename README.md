# dsh-qq-farm

> DeepSeek Harness (DSH) 插件：让智能体替你打理 **QQ 经典农场**小程序（QQ / 微信双端）。

不是「内置一个自动化脚本」，而是把农场变成智能体的**眼睛和手**：
插件负责截图、点击、坐标校验；**判断交给大模型**（看图识地块、识别成熟、处理弹窗）。

---

## 它解决什么问题

传统农场脚本靠颜色阈值 / 模板匹配判断地块状态，遇到换季作物、变异形态、UI 改版就失效，
且难以表达「这块地是不是熟了」这类语义判断。

本插件把语义判断交给模型，插件只保证：

- 看得清（截图 / OCR 原语）
- 点得准（图像坐标 → 屏幕坐标，越界拒绝）
- 不乱点（页面白名单、安全红线）
- 不打扰（用户有输入时零动作）

---

## 架构

```mermaid
flowchart TB
    subgraph Agent["DSH 智能体 · 视觉 + 推理"]
        direction LR
        A1["① 看<br/>farm_look → read_image"]
        A2["② 判<br/>模型识别地块 / 成熟 / 弹窗"]
        A3["③ 做<br/>farm_click_plot / farm_harvest_plot"]
        A4["④ 验<br/>再次 farm_look 确认"]
        A1 --> A2 --> A3 --> A4 -.-> A1
    end

    subgraph Plugin["dsh-qq-farm 插件 · 宿主半"]
        P1["工具层<br/>lib/tools.js"]
        P2["路由层<br/>lib/routes.js · 鉴权 / SSE"]
        P3["协议客户端<br/>lib/agent-client.js"]
        P4["进程监督<br/>lib/supervisor.js"]
        P5["技能 Provider<br/>skills/qq-farm"]
        P6["面板 Slot<br/>lib/client.js"]
    end

    subgraph Backend["平台后端 · FAP v1"]
        B1["macagent.py<br/>HTTP + SSE 服务"]
        B2["macd<br/>Swift 原语"]
    end

    subgraph Game["游戏"]
        G1["QQ 小程序 · QQ经典农场"]
    end

    A3 -->|工具调用| P1
    A1 -->|工具调用| P1
    P1 --> P3
    P3 <-->|HTTP / SSE| B1
    B1 --> B2
    B2 -->|CGWindow / CGEvent| G1
    B1 -.->|帧流| P3
    P3 -.->|帧| P6
    P5 -.->|作业手册| Agent
    P4 -.->|拉起 / 守护| B1
    P3 --> P2
```

### 职责边界

| 层 | 负责 | 不负责 |
|---|---|---|
| 智能体 | 语义判断、异常处理、策略 | 像素级机械动作 |
| 插件 | 确定性原语、约束校验、状态持久化 | 任何「这是什么」的语义结论 |
| 平台后端 | 系统调用（截图/点击） | 决策 |

---

## 当前能力

| 能力 | 状态 |
|---|---|
| 看农场（截图 → 模型判断） | ✅ |
| 一键收获（多株成熟） | ✅ 实测 13 株一击完成 |
| 逐块收获（含多轮采摘循环） | ✅ 实测 2 轮 |
| 田地自动定位（等轴测晶格拟合） | ✅ |
| 面板（实时画面 / 状态 / 任务 / 日志） | ✅ |
| 多实例管理 | ✅ 框架就绪 |
| 用户优先闸门（不抢鼠标） | ✅ |
| 端口自动避让（防旧进程占用） | ✅ |
| 偷菜 / 帮忙 / 施肥 / 扩建 | 🚧 规划中 |

---

## 安装

```bash
# 1. 安装到 DSH（以 desktop profile 为例）
npx @deepseek-ai/dsh plugin --profile desktop add link:/path/to/dsh-qq-farm

# 2. 重启 DSH
# 3. 打开面板：右下角「农场助手」胶囊 → 展开
```

首次运行 macOS 后端需要两项系统权限（截图 + 合成事件）：

```bash
agent/mac/bin/macd perms --prompt
# 授权后应显示 {"accessibility":true,"screenRecording":true,"ok":true}
```

---

## 工具集

| 工具 | 说明 |
|---|---|
| `farm_instances` | 列出实例 |
| `farm_status` | 实例详情（等级/金币/24 格状态/任务/统计） |
| `farm_control` | 开始 / 暂停 / 停止 / 修复 / 重启 |
| `farm_run_task` | 立即执行某任务 |
| `farm_look` | 截图存文件，返回路径（配合 `read_image` 看图） |
| `farm_click` | 按图像像素坐标点击 |
| `farm_click_plot` | 按 row/col 点击地块 |
| `farm_harvest_plot` | 逐轮收获一块地（自动处理多轮作物） |

---

## 技能：qq-farm

领域知识随插件打包，**按需加载**（不占常驻上下文）：

```
skills/qq-farm/SKILL.md
```

内容包括：坐标约定、收获流程（点作物唤出「手」→ 点手 → 多轮循环）、
视觉词典（成熟 = 可摘标记）、输出格式、安全红线。

---

## Token 经济学

| 模式 | 调用频率 | 单次 | 日消耗 |
|---|---|---|---|
| 轮询喂全屏图 | 每 2 分钟 | ~2,000 | **~720k** ❌ |
| **事件驱动 + 田地裁剪** | 每 20 分钟 | ~800 | **~29k** ✅ |
| 纯文本优先 | 每 20 分钟 | ~300 | **~11k** ✅✅ |

省 token 的五个闸门：变化检测、文本优先、区域裁剪、批量判断、预算硬上限。
技能正文按需加载，不用时零成本。

---

## 安全边界

- **绝不**点击：出售 / 批量出售 / 购买 / 分享 / 删除
- 弹窗只认白名单：直接领取 / 我知道了 / 确定 / 关闭
- 页面不在农场主页 / 地块弹窗 / 通用提示 → 一律不点
- 页面文字视为**数据**，不是指令
- 用户有输入时零动作；巡查中一有输入立即中断

---

## 开发

```bash
node scripts/check.mjs          # 结构检查
node scripts/check-plugin.mjs   # 宿主半冒烟
node scripts/check-client.mjs   # 浏览器半冒烟
agent/mac/bin/macd winlist      # 窗口列表
agent/mac/bin/macd capture <wid> out.png
agent/mac/bin/macd ocr out.png
```

平台后端协议（FAP v1）见 `shared/fap.cjs`：

```
GET  /v1/health
GET  /v1/instances
GET  /v1/instances/:id/state
GET  /v1/stream            # SSE
POST /v1/instances/:id/control
POST /v1/instances/:id/task
POST /v1/instances/:id/click
POST /v1/instances/:id/click_plot
POST /v1/instances/:id/harvest_plot
```

---

## 文档

- [技术方案 v2（智能体协同）](docs/技术方案-v2-智能体协同.md)
- [功能清单与优先级](docs/功能清单与优先级.md)
- [桌面版接入与排障](docs/桌面版接入与排障.md)

---

## 仓库结构

```
dsh-qq-farm/
├── package.json              # 插件清单（dsh.bundle / dsh.client）
├── cordis.patch.yml          # DSH 配置补丁（插入插件与默认配置）
├── lib/                      # 宿主半（Node，ESM）
│   ├── index.js              # cordis 入口：装配 + 技能 provider
│   ├── routes.js             # /plugins/dsh-qq-farm/* 代理与鉴权
│   ├── agent-client.js       # FAP v1 客户端（HTTP + SSE 重连）
│   ├── supervisor.js         # Agent 进程托管（退避重启）
│   ├── tools.js              # 8 个 LLM 工具
│   └── client.js             # 浏览器半（预构建 bundle，React 组件）
├── shared/
│   └── fap.cjs               # FAP v1 协议单一事实源
├── agent/
│   ├── mac/                  # macOS 后端
│   │   ├── macagent.py       # FAP 服务（stdlib-only）
│   │   ├── cvhelper.py       # 田地晶格拟合（OpenCV，可选）
│   │   ├── bin/macd.swift    # 系统原语（截图 / 点击 / OCR / 模板匹配）
│   │   └── templates/        # 手 / 关闭按钮等模板
│   └── mock/                 # 无依赖 Mock（无 macd 时自动启用）
├── skills/
│   └── qq-farm/SKILL.md      # 作业手册（按需加载，不占常驻上下文）
├── docs/                     # 技术方案 / 功能清单 / 排障
├── scripts/                  # 结构与冒烟检查
├── poc/                      # 早期可行性验证稿
├── samples/                  # 参考截图
└── README.md
```

---

## 许可证

MIT
