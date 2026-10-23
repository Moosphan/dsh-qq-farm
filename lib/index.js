/**
 * dsh-qq-farm 宿主半（cordis 插件）。
 *
 * 组装顺序：
 *   config → AgentClient（协议） / Supervisor（进程）→ routes（浏览器面）→ tools（LLM 面）
 *
 * 这一层刻意保持"无业务"：所有农场逻辑都在 Agent 里，
 * 插件只负责鉴权、转发、聚合与进程监督。
 */

import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import fap from '../shared/fap.cjs'
import { AgentClient } from './agent-client.js'
import { Supervisor } from './supervisor.js'
import { createRoutes } from './routes.js'
import { registerTools } from './tools.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..')

export const name = 'dsh-qq-farm'
/** 依赖 DSH 的 Web 路由服务（web profile 必然具备）。 */
export const inject = ['webServer', 'skills']

function defaultAgentCommand(url) {
  let port = 4571
  try {
    port = Number(new URL(url).port || 4571)
  } catch {
    // url 非法时用默认端口，真正的错误会在首次请求时暴露
  }
  // macOS 且编好了 macd：优先用原生后端（Vision OCR + 系统点击）
  if (process.platform === 'darwin') {
    const macd = path.join(packageRoot, 'agent', 'mac', 'bin', 'macd')
    if (fs.existsSync(macd)) {
      // 用绝对路径的 python3：桌面版 DSH 的 PATH 只有 /usr/bin，
      // 而 macagent 只用标准库 + macOS 自带的 sips，所以 /usr/bin/python3 足够。
      return ['/usr/bin/python3', path.join(packageRoot, 'agent', 'mac', 'macagent.py'), '--port', String(port)]
    }
  }
  // 其余平台（或 macd 未编译）：用内置 Mock，保证面板始终能跑起来
  return [process.execPath, path.join(packageRoot, 'agent', 'mock', 'index.js'), '--port', String(port)]
}

/**
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {any} config cordis.patch.yml 中该 entry 的 config
 */
export function apply(ctx, config) {
  const log = (...args) => {
    const line = args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ')
    try {
      if (ctx.logger && typeof ctx.logger.info === 'function') ctx.logger.info(line)
      else console.log(`[dsh-qq-farm] ${line}`)
    } catch {
      // 日志失败不得影响插件
    }
  }

  const cfg = {
    agent: {
      mode: config?.agent?.mode === 'remote' ? 'remote' : 'managed',
      url: String(config?.agent?.url || 'http://127.0.0.1:4571'),
      token: String(config?.agent?.token || ''),
      command: Array.isArray(config?.agent?.command) && config.agent.command.length > 0
        ? config.agent.command.map(String)
        : null,
    },
    ui: {
      defaultFps: Number(config?.ui?.defaultFps ?? 4),
      maxFrameWidth: Number(config?.ui?.maxFrameWidth ?? 720),
    },
  }
  if (!cfg.agent.command) cfg.agent.command = defaultAgentCommand(cfg.agent.url)
  // 支持配置文件里写 {packageRoot}，避免绝对路径写死
  cfg.agent.command = cfg.agent.command.map((part) => String(part).replaceAll('{packageRoot}', packageRoot))

  /** 浏览器写操作的一次性令牌（HttpOnly Cookie 下发，JS 读不到）。 */
  const token = crypto.randomBytes(24).toString('hex')

  // Agent 可能因端口避让换端口：读 ~/.dsh-farm/agent-port.json 跟随
  const resolveAgentUrl = () => {
    try {
      const p = path.join(os.homedir(), '.dsh-farm', 'agent-port.json')
      if (!fs.existsSync(p)) return null
      const info = JSON.parse(fs.readFileSync(p, 'utf8'))
      if (!info || !info.port) return null
      const u = new URL(cfg.agent.url)
      u.port = String(info.port)
      return u.toString().replace(/\/+$/, '')
    } catch {
      return null
    }
  }
  const discovered = resolveAgentUrl()
  if (discovered && discovered !== cfg.agent.url) {
    log(`agent 端口跟随端口文件：${cfg.agent.url} → ${discovered}`)
    cfg.agent.url = discovered
  }

  // ── 技能：领域知识随插件打包（不依赖工作区 AGENTS.md）──────────────
  if (ctx.skills && typeof ctx.skills.registerProvider === 'function') {
    const skillFile = path.join(packageRoot, 'skills', 'qq-farm', 'SKILL.md')
    // DSH 校验要求（见 @deepseek-ai/dsh-skill validateCandidate）：
    //   name 匹配 /^[a-z0-9]+(-[a-z0-9]+)*$/、description 非空、
    //   source 字符串、rank 有限数字、provider 必须与 provider.name 一致
    const PROVIDER_NAME = 'dsh-qq-farm'
    const candidate = {
      name: 'qq-farm',
      description: '操作 QQ 经典农场小程序：截图判断地块、点「手」收获、多轮采摘、安全红线',
      source: 'bundled',
      provider: PROVIDER_NAME,
      rank: 300,                    // 低于项目级(100/200)，允许项目覆盖
      invocation: { modelInvocable: true, userInvocable: true },
      directory: path.dirname(skillFile),
      locator: skillFile,
    }
    try {
      ctx.skills.registerProvider(() => ({
        name: PROVIDER_NAME,
        list: () => Promise.resolve([candidate]),
        async get(target) {
          const content = await fs.promises.readFile(target.locator, 'utf8')
          return {
            ...target,
            source: target.source || 'bundled',
            provider: PROVIDER_NAME,
            content,
          }
        },
      }))
      log('skills: registered qq-farm')
    } catch (error) {
      log(`skills: register failed ${String(error && error.message)}`)
    }
  }

  const agent = new AgentClient({
    url: cfg.agent.url,
    token: cfg.agent.token,
    log,
    resolveUrl: resolveAgentUrl,
  })

  /** @type {Supervisor|null} */
  let supervisor = null
  if (cfg.agent.mode === 'managed') {
    supervisor = new Supervisor({
      command: cfg.agent.command,
      cwd: packageRoot,
      log,
    })
    supervisor.start()
    log(`managed agent: ${cfg.agent.command.join(' ')}`)
  } else {
    log(`remote agent: ${cfg.agent.url}`)
  }

  // health 路由需要读进程信息，用 getter 保持实时
  Object.defineProperty(cfg, 'supervisorInfo', {
    enumerable: true,
    get() {
      if (!supervisor) return null
      return { mode: 'managed', pid: supervisor.pid, running: supervisor.running, restarts: supervisor.restarts }
    },
  })

  const routeHandler = createRoutes({ agent, config: cfg, token, log })

  // 路由注册返回 disposer；用 ctx.effect 让插件卸载时自动回收
  ctx.effect(() => {
    const disposeRoute = ctx.webServer.register({
      kind: 'prefix',
      path: fap.BASE,
      handler: routeHandler,
    })
    log(`routes mounted at ${fap.BASE}/*`)
    return () => {
      try {
        disposeRoute()
      } catch {
        // 已回收
      }
    }
  })

  // LLM 工具（可选服务：没有 tools 服务时静默跳过）
  const tools = typeof ctx.get === 'function' ? ctx.get('tools') : undefined
  if (tools) {
    try {
      ctx.effect(() => registerTools(tools, agent, log))
    } catch (error) {
      log(`tools wiring failed: ${String(error && error.message)}`)
    }
  } else {
    log('ctx.tools 不可用，跳过 LLM 工具注册')
  }

  // 卸载：先断协议，再停进程（顺序反了会留下僵尸 SSE）
  ctx.effect(() => {
    return () => {
      log('disposing: stop agent client')
      agent.stop()
      if (supervisor) {
        log('disposing: stop managed agent process')
        supervisor.stop()
      }
    }
  })

  log(`ready — protocol v${fap.PROTOCOL}, mode ${cfg.agent.mode}`)
}
