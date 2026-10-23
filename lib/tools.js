/**
 * LLM 工具：让 DSH 的 Agent 能用自然语言查询/操作农场。
 *
 * 设计约束（见 docs/技术方案.md §4.5）：
 *   - 只读工具默认可用，写操作（control）依赖面板已完成的鉴权会话；
 *   - 输出有界：不返回图片字节，只返回结构化摘要；
 *   - 错误码透传给模型，便于它给出"去把窗口拉出来"这类可执行建议。
 *
 * 注意：这里使用标准 JSON Schema 描述参数（不用 DSH 内部的 schema DSL），
 * 因为树外插件拿不到 @deepseek-ai/dsh-tools。若注册被拒绝，只告警不阻断插件加载。
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const CONTROL_ACTIONS = ['start', 'pause', 'resume', 'stop', 'run_once', 'repair', 'restart']

/**
 * @param {any} tools ctx.tools 服务
 * @param {import('./agent-client.js').AgentClient} agent
 * @param {(line: string) => void} log
 * @returns {() => void} 用于卸载时反注册
 */
export function registerTools(tools, agent, log) {
  const disposers = []

  /** @param {any} definition */
  const safeRegister = (definition) => {
    try {
      const dispose = tools.register(definition)
      if (typeof dispose === 'function') disposers.push(dispose)
      log(`tools: registered ${definition.name}`)
    } catch (error) {
      log(`tools: register ${definition.name} failed: ${String(error && error.message)}`)
    }
  }

  const text = (value) => [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value, null, 2) }]

  safeRegister({
    name: 'farm_instances',
    description:
      'List QQ Farm automation instances known to the local farm agent: id, name, platform (qq/wechat), state, current task and next run time. Use this first to discover which instance ids exist before calling other farm tools.',
    parameters: { type: 'object', additionalProperties: false, properties: {}, required: [] },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        required: ['ok'],
        properties: {
          ok: { type: 'boolean' },
          instances: { type: 'array', items: { type: 'object' } },
          error: { type: 'object' },
        },
      },
      render: (_args, value) =>
        text(
          value.ok
            ? value.instances.length === 0
              ? '农场 Agent 在线，但没有实例。'
              : value.instances
                  .map((i) => `${i.id} · ${i.name} · ${i.platform} · ${i.state} · 任务 ${i.currentTask || '—'}`)
                  .join('\n')
            : `读取失败：${value.error?.code} ${value.error?.message}`,
        ),
    },
    async execute() {
      const r = await agent.listInstances()
      return r.ok ? { ok: true, instances: r.data } : { ok: false, error: r.error }
    },
  })

  safeRegister({
    name: 'farm_status',
    description:
      'Read one farm instance in detail: profile (level/gold/coupon/exp), all 24 land plots (level, maturity countdown, needs-planting/needs-upgrade flags), task schedule and daily stats. Read-only.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['instanceId'],
      properties: {
        instanceId: { type: 'string', description: 'Instance id from farm_instances, e.g. "qq1".' },
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        required: ['ok'],
        properties: { ok: { type: 'boolean' }, state: { type: 'object' }, error: { type: 'object' } },
      },
      render: (_args, value) => {
        if (!value.ok) return text(`读取失败：${value.error?.code} ${value.error?.message}`)
        const s = value.state
        const plots = s.plots || []
        const needPlant = plots.filter((p) => p.needPlanting).length
        const growing = plots.filter((p) => typeof p.countdownSec === 'number' && p.countdownSec > 0).length
        return text(
          [
            `实例 ${s.summary.name}（${s.summary.id}）状态 ${s.summary.state}，当前任务 ${s.summary.currentTask || '—'}`,
            `等级 ${s.profile.level} · 金币 ${s.profile.gold} · 点券 ${s.profile.coupon} · 经验 ${s.profile.exp}`,
            `地块：${growing} 格生长中，${needPlant} 格待播种，共 ${plots.length} 格`,
            `今日：收获 ${s.stats.harvest} · 偷菜 ${s.stats.steal} · 帮忙 ${s.stats.help}`,
            s.summary.lastError ? `最近错误：${s.summary.lastError.code} ${s.summary.lastError.message}` : '无未处理错误',
          ].join('\n'),
        )
      },
    },
    async execute(args) {
      const id = String((args && args.instanceId) || '')
      if (!id) return { ok: false, error: { code: 'E_BAD_REQUEST', message: 'instanceId 必填' } }
      const r = await agent.instanceState(id)
      return r.ok ? { ok: true, state: r.data } : { ok: false, error: r.error }
    },
  })

  safeRegister({
    name: 'farm_control',
    description:
      'Control one farm instance. Actions: start, pause, resume, stop, run_once (execute one round now), repair (one-click fix), restart (restart the game window). These actions move the mouse and keyboard of the host machine through the farm agent, so prefer read-only tools unless the user explicitly asked to start or stop automation.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['instanceId', 'action'],
      properties: {
        instanceId: { type: 'string' },
        action: { type: 'string', enum: CONTROL_ACTIONS },
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        required: ['ok'],
        properties: { ok: { type: 'boolean' }, result: { type: 'object' }, error: { type: 'object' } },
      },
      render: (_args, value) =>
        text(value.ok ? `已执行：${value.result.action} → ${value.result.state}` : `操作失败：${value.error?.code} ${value.error?.message}`),
    },
    async execute(args) {
      const id = String((args && args.instanceId) || '')
      const action = String((args && args.action) || '')
      if (!id || !CONTROL_ACTIONS.includes(action)) {
        return { ok: false, error: { code: 'E_BAD_REQUEST', message: 'instanceId / action 非法' } }
      }
      const r = await agent.control(id, action, crypto.randomUUID())
      return r.ok ? { ok: true, result: r.data } : { ok: false, error: r.error }
    },
  })

  safeRegister({
    name: 'farm_run_task',
    description:
      'Immediately run one farm task on an instance without waiting for its schedule: main (harvest/plant/fertilize/expand/upgrade), friend (steal/help), grass, share, reward, gift, event, sell, land_scan, timed_harvest, restart.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['instanceId', 'task'],
      properties: {
        instanceId: { type: 'string' },
        task: {
          type: 'string',
          enum: ['main', 'friend', 'grass', 'share', 'reward', 'gift', 'event', 'sell', 'land_scan', 'timed_harvest', 'restart'],
        },
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        required: ['ok'],
        properties: { ok: { type: 'boolean' }, result: { type: 'object' }, error: { type: 'object' } },
      },
      render: (_args, value) => text(value.ok ? `已触发任务：${value.result.task}` : `触发失败：${value.error?.code} ${value.error?.message}`),
    },
    async execute(args) {
      const id = String((args && args.instanceId) || '')
      const task = String((args && args.task) || '')
      if (!id || !task) return { ok: false, error: { code: 'E_BAD_REQUEST', message: 'instanceId / task 必填' } }
      const r = await agent.runTask(id, task)
      return r.ok ? { ok: true, result: r.data } : { ok: false, error: r.error }
    },
  })

  // ── v2：把"看"与"点"交给模型，插件只做机械动作 ──────────────────
  safeRegister({
    name: 'farm_look',
    description:
      'Capture the current farm screen and save it as an image file, returning the file path. ' +
      'Then open that path with read_image so you can see the screen yourself and judge the plots. ' +
      'After any click, call this again to verify the screen actually changed.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['instanceId'],
      properties: {
        instanceId: { type: 'string', description: 'Instance id from farm_instances, e.g. "qq-64312".' },
      },
    },
    async execute(args) {
      const id = String((args && args.instanceId) || '')
      if (!id) return { ok: false, error: { code: 'E_BAD_REQUEST', message: 'instanceId 必填' } }
      const r = await agent.frame(id)
      if (!r.ok) return { ok: false, error: r.error }
      const payload = r.data || {}
      const b64 = payload.data || payload.base64 || payload.jpeg || null
      if (!b64) return { ok: false, error: { code: 'E_NO_FRAME', message: 'Agent 未返回画面数据' } }
      const mime = payload.mime || 'image/jpeg'
      const ext = mime.includes('png') ? 'png' : mime.includes('webp') ? 'webp' : 'jpg'
      const buf = Buffer.from(b64, 'base64')
      const dir = path.join(os.tmpdir(), 'dsh-qq-farm')
      fs.mkdirSync(dir, { recursive: true })
      const file = path.join(dir, 'look-' + Date.now() + '.' + ext)
      fs.writeFileSync(file, buf)
      log('farm_look: ' + file + ' (' + buf.length + ' bytes)')
      return { ok: true, path: file, bytes: buf.length, hint: 'open this path with read_image' }
    },
  })

  safeRegister({
    name: 'farm_click_plot',
    description:
      'Click the plot at row/col (0-based, row 0-5, col 0-3). The plugin converts it to screen ' +
      'coordinates using the latest detected lattice. Call farm_look afterwards to verify.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['instanceId', 'row', 'col'],
      properties: {
        instanceId: { type: 'string' },
        row: { type: 'number', description: '0-5, 0 is the top row of the field' },
        col: { type: 'number', description: '0-3, left to right' },
      },
    },
    async execute(args) {
      const id = String((args && args.instanceId) || '')
      if (!id) return { ok: false, error: { code: 'E_BAD_REQUEST', message: 'instanceId 必填' } }
      const r = await agent.clickPlot(id, Number(args.row), Number(args.col))
      return r.ok ? { ok: true, screen: r.data.screen } : { ok: false, error: r.error }
    },
  })

  safeRegister({
    name: 'farm_click',
    description:
      'Click at the given image pixel coordinates (capture is 824x1542; x 0-824, y 0-1542). ' +
      'The plugin converts to screen coordinates and refuses coordinates outside the game window.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['instanceId', 'x', 'y'],
      properties: {
        instanceId: { type: 'string' },
        x: { type: 'number', description: 'image pixel x' },
        y: { type: 'number', description: 'image pixel y' },
      },
    },
    async execute(args) {
      const id = String((args && args.instanceId) || '')
      if (!id) return { ok: false, error: { code: 'E_BAD_REQUEST', message: 'instanceId 必填' } }
      const r = await agent.click(id, Number(args.x), Number(args.y))
      return r.ok ? { ok: true, screen: r.data.screen } : { ok: false, error: r.error }
    },
  })

  safeRegister({
    name: 'farm_harvest_plot',
    description:
      'Harvest the plot at row/col until nothing is left (multi-round crops are handled inside). ' +
      'It clicks the plot to summon the hand tool, clicks the hand, and repeats while 可摘 remains. ' +
      'Returns how many rounds were harvested.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['instanceId', 'row', 'col'],
      properties: {
        instanceId: { type: 'string' },
        row: { type: 'number', description: '0-5' },
        col: { type: 'number', description: '0-3' },
      },
    },
    async execute(args) {
      const id = String((args && args.instanceId) || '')
      if (!id) return { ok: false, error: { code: 'E_BAD_REQUEST', message: 'instanceId 必填' } }
      const r = await agent.harvestPlot(id, Number(args.row), Number(args.col))
      return r.ok ? { ok: true, result: r.data.result } : { ok: false, error: r.error }
    },
  })

  return () => {
    for (const dispose of disposers) {
      try {
        dispose()
      } catch {
        // 忽略卸载失败
      }
    }
  }
}
