/**
 * Farm Agent（Mock 后端）—— M0 用。
 *
 * 目的：让整套协议、面板、控制闭环在**任意操作系统**上可运行、可演示、可测试，
 * 不依赖 Windows / QQ / 微信。它实现与真实 Agent 完全相同的 FAP v1 接口，
 * 只是"画面"是合成的 SVG、"任务"是假的定时器。
 *
 * 启动：
 *   node agent/mock/index.js --port 4571 --instances 2
 */

import http from 'node:http'
import { URL } from 'node:url'
import fap from '../../shared/fap.cjs'

// ── 参数 ──────────────────────────────────────────────────────────────
const argv = process.argv.slice(2)
function arg(name, fallback) {
  const i = argv.indexOf(`--${name}`)
  if (i >= 0 && argv[i + 1] !== undefined) return argv[i + 1]
  return fallback
}
const PORT = Number(arg('port', process.env.QQFARM_AGENT_PORT || 4571))
const INSTANCE_COUNT = Math.max(1, Number(arg('instances', 2)))
const TOKEN = arg('token', process.env.QQFARM_AGENT_TOKEN || '')
const AGENT_VERSION = '0.2.0-mock'

// ── 假状态机 ──────────────────────────────────────────────────────────
/** @type {Map<string, any>} */
const instances = new Map()
const idempotency = new Map() // key -> { at: number, result: object }
const IDEMPOTENCY_TTL = 30_000

function makeInstance(index) {
  const platform = index % 2 === 0 ? 'qq' : 'wechat'
  const id = `${platform}${index + 1}`
  const plots = fap.makePlots()
  // 造点初始差异：一半有作物，倒计时随实例错开
  for (const [i, plot] of plots.entries()) {
    if (i % 3 === 0) {
      plot.level = i % 6 === 0 ? 'red' : 'normal'
      plot.countdownSec = 30 + ((i * 7 + index * 11) % 90)
      plot.syncAt = new Date().toISOString()
    } else if (i % 3 === 1) {
      plot.needPlanting = true
    }
  }
  return {
    id,
    name: platform === 'qq' ? `QQ-号${index + 1}` : `微信-号${index + 1}`,
    platform,
    state: index === 0 ? 'running' : 'paused',
    currentTask: index === 0 ? 'main' : null,
    nextRun: index === 0 ? new Date(Date.now() + 18_000).toISOString() : null,
    lastError: null,
    fps: 4,
    rev: 1,
    profile: {
      level: 12 + index * 3,
      gold: 1200 + index * 880,
      coupon: 5 + index,
      exp: 340 + index * 90,
      syncedAt: new Date().toISOString(),
    },
    plots,
    tasks: Object.fromEntries(
      fap.TASKS.map((name) => [
        name,
        { enabled: name === 'main' || name === 'friend', trigger: 'interval', intervalSec: 60, nextRun: null },
      ]),
    ),
    stats: { harvest: 0, steal: 0, help: 0, sell: 0, totalActions: 0 },
    logs: [],
  }
}

for (let i = 0; i < INSTANCE_COUNT; i += 1) {
  const inst = makeInstance(i)
  instances.set(inst.id, inst)
}

const startedAt = Date.now()
const seq = { value: 0 }
const logRing = [] // 全局日志环形缓冲（重连补齐用）

function pushLog(instanceId, entry) {
  const line = {
    ts: new Date().toISOString(),
    level: entry.level || 'info',
    task: entry.task || null,
    msg: entry.msg || '',
    action: entry.action || null,
    conf: entry.conf ?? null,
  }
  logRing.push({ instance: instanceId, ...line })
  if (logRing.length > 5000) logRing.shift()
  broadcast({ t: 'log', instance: instanceId, entries: [line] })
}

function nowIso() {
  return new Date().toISOString()
}

/** 每秒推进一次假世界。 */
function tick() {
  for (const inst of instances.values()) {
    if (inst.state !== 'running') continue
    inst.rev += 1
    let harvested = 0
    for (const plot of inst.plots) {
      if (typeof plot.countdownSec === 'number' && plot.countdownSec > 0) {
        // 加速 30 倍，方便肉眼看到"成熟 → 收获 → 待播种"的循环
        plot.countdownSec = Math.max(0, plot.countdownSec - 30)
        plot.syncAt = nowIso()
        if (plot.countdownSec === 0) {
          plot.countdownSec = null
          plot.needPlanting = true
          harvested += 1
        }
      }
    }
    if (harvested > 0) {
      inst.stats.harvest += harvested
      inst.stats.totalActions += harvested
      pushLog(inst.id, { task: inst.currentTask, msg: `收获 ${harvested} 格`, action: 'btn_harvest', conf: 0.93 })
    }
    // 任务轮转
    if (Math.random() < 0.08) {
      const names = fap.TASKS.filter((n) => inst.tasks[n]?.enabled)
      inst.currentTask = names[Math.floor(Math.random() * names.length)] || 'main'
      inst.nextRun = new Date(Date.now() + 30_000).toISOString()
      pushLog(inst.id, { task: inst.currentTask, msg: '任务开始' })
      pushLog(inst.id, { task: inst.currentTask, msg: '任务完成' })
    }
    broadcast({ t: 'state', instance: inst.id, rev: inst.rev, sync: snapshot(inst) })
  }
}

function snapshot(inst) {
  return {
    rev: inst.rev,
    summary: {
      id: inst.id,
      name: inst.name,
      platform: inst.platform,
      state: inst.state,
      currentTask: inst.currentTask,
      nextRun: inst.nextRun,
      lastError: inst.lastError,
      fps: inst.fps,
    },
    profile: inst.profile,
    plots: inst.plots,
    tasks: inst.tasks,
    stats: inst.stats,
  }
}

// ── 合成画面（SVG，无任何图像依赖）────────────────────────────────────
const LEVEL_FILL = {
  unbuilt: '#242a31',
  normal: '#2f5d34',
  red: '#7d3434',
  black: '#232329',
  gold: '#7a6a2c',
  amethyst: '#4f3479',
}

function fmt(sec) {
  if (typeof sec !== 'number') return ''
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function renderSvg(inst) {
  const W = 480
  const H = 760
  const cols = 6
  const rows = 4
  const cw = 68
  const ch = 68
  const gap = 6
  const gridW = cols * cw + (cols - 1) * gap
  const x0 = Math.round((W - gridW) / 2)
  const y0 = 150

  const parts = []
  parts.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" font-family="-apple-system,BlinkMacSystemFont,PingFang SC,Microsoft YaHei,sans-serif">`)
  parts.push(`<rect width="${W}" height="${H}" fill="#14181d"/>`)
  // 顶部状态条
  parts.push(`<rect x="0" y="0" width="${W}" height="64" fill="#1c2229"/>`)
  parts.push(`<text x="20" y="28" fill="#e6edf3" font-size="16" font-weight="600">${escapeXml(inst.name)}</text>`)
  parts.push(`<text x="20" y="48" fill="#8b98a5" font-size="12">${fap.PLATFORM_LABEL[inst.platform]} · ${inst.state} · Lv.${inst.profile.level}</text>`)
  parts.push(`<text x="${W - 20}" y="28" text-anchor="end" fill="#f0b429" font-size="14">🪙 ${inst.profile.gold}</text>`)
  parts.push(`<text x="${W - 20}" y="48" text-anchor="end" fill="#8b98a5" font-size="12">exp ${inst.profile.exp}</text>`)
  // 地块
  for (const [i, plot] of inst.plots.entries()) {
    const col = i % cols
    const row = Math.floor(i / cols)
    const x = x0 + col * (cw + gap)
    const y = y0 + row * (ch + gap)
    const fill = LEVEL_FILL[plot.level] || LEVEL_FILL.normal
    parts.push(`<rect x="${x}" y="${y}" width="${cw}" height="${ch}" rx="8" fill="${fill}" stroke="#000" stroke-opacity="0.35"/>`)
    if (plot.countdownSec !== null) {
      parts.push(`<text x="${x + cw / 2}" y="${y + ch / 2 + 4}" text-anchor="middle" font-size="13" fill="#d7e3ee">${fmt(plot.countdownSec)}</text>`)
    } else if (plot.needPlanting) {
      parts.push(`<circle cx="${x + cw / 2}" cy="${y + ch / 2}" r="7" fill="none" stroke="#7bd88f" stroke-width="2"/>`)
      parts.push(`<text x="${x + cw / 2}" y="${y + ch - 10}" text-anchor="middle" font-size="10" fill="#7bd88f">待播种</text>`)
    }
    parts.push(`<text x="${x + 6}" y="${y + 14}" font-size="9" fill="#000" fill-opacity="0.45">${plot.id}</text>`)
  }
  // 底部
  const yBar = y0 + rows * (ch + gap) + 24
  parts.push(`<rect x="20" y="${yBar}" width="${W - 40}" height="1" fill="#2a323b"/>`)
  parts.push(`<text x="20" y="${yBar + 26}" fill="#8b98a5" font-size="12">任务 <tspan fill="#7bd88f">${inst.currentTask || '—'}</tspan></text>`)
  parts.push(`<text x="20" y="${yBar + 48}" fill="#8b98a5" font-size="12">收获 ${inst.stats.harvest} · 偷菜 ${inst.stats.steal} · 帮忙 ${inst.stats.help}</text>`)
  parts.push(`<text x="20" y="${yBar + 70}" fill="#4a5561" font-size="11">mock frame #${seq.value} · ${nowIso().slice(11, 19)}</text>`)
  parts.push('</svg>')
  return parts.join('')
}

function escapeXml(s) {
  return String(s).replace(/[<>&"']/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;' }[c]))
}

// ── SSE ──────────────────────────────────────────────────────────────
/** @type {Set<any>} */
const clients = new Set()

function broadcast(payload) {
  const frame = fap.sse(payload)
  for (const c of clients) {
    if (payload.instance && c.filter && !c.filter.has(payload.instance)) continue
    if (payload.t === 'frame' && !c.wantFrames) continue
    try {
      c.res.write(frame)
    } catch {
      clients.delete(c)
    }
  }
}

setInterval(() => {
  if (clients.size === 0) return
  seq.value += 1
  for (const inst of instances.values()) {
    const svg = renderSvg(inst)
    broadcast({
      t: 'frame',
      instance: inst.id,
      seq: seq.value,
      mime: 'image/svg+xml',
      data: Buffer.from(svg, 'utf8').toString('base64'),
    })
  }
  // 心跳，防代理超时
  if (seq.value % 4 === 0) broadcast({ t: 'ping', ts: nowIso() })
}, 250)

setInterval(tick, 1000)

// ── HTTP 路由 ────────────────────────────────────────────────────────
function json(res, status, body) {
  const data = JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'access-control-allow-origin': '*',
  })
  res.end(data)
}

function fail(res, status, code, message) {
  json(res, status, { ok: false, error: { code, message: message || fap.ERROR_TEXT[code] || code } })
}

function readBody(req) {
  return new Promise((resolve) => {
    let raw = ''
    req.on('data', (c) => {
      raw += c
      if (raw.length > 1_000_000) req.destroy()
    })
    req.on('end', () => {
      if (!raw) return resolve({})
      try {
        resolve(JSON.parse(raw))
      } catch {
        resolve(null)
      }
    })
  })
}

function authorized(req) {
  if (!TOKEN) return true
  return req.headers.authorization === `Bearer ${TOKEN}`
}

const server_ = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || '127.0.0.1'}`)
  const pathname = url.pathname

  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'access-control-allow-origin': '*',
      'access-control-allow-headers': 'content-type, authorization, idempotency-key',
      'access-control-allow-methods': 'GET, POST, OPTIONS',
    })
    return res.end()
  }

  if (!authorized(req)) return fail(res, 401, 'E_AUTH')

  // GET /v1/health
  if (req.method === 'GET' && pathname === '/v1/health') {
    return json(res, 200, {
      ok: true,
      agentId: 'mock',
      protocol: fap.PROTOCOL,
      agentVersion: AGENT_VERSION,
      platforms: fap.PLATFORMS,
      instanceCount: instances.size,
      uptimeSec: Math.round((Date.now() - startedAt) / 1000),
    })
  }

  // GET /v1/instances
  if (req.method === 'GET' && pathname === '/v1/instances') {
    return json(
      res,
      200,
      [...instances.values()].map((i) => snapshot(i).summary),
    )
  }

  // GET /v1/stream
  if (req.method === 'GET' && pathname === '/v1/stream') {
    const filterIds = fap.parseInstanceFilter(url.searchParams)
    const fps = Number(url.searchParams.get('fps') || 4)
    const since = url.searchParams.get('since')
    res.writeHead(200, {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-store',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
    })
    const client = {
      res,
      filter: filterIds ? new Set(filterIds) : null,
      wantFrames: fps > 0,
    }
    clients.add(client)
    res.write(
      fap.sse({
        t: 'hello',
        protocol: fap.PROTOCOL,
        agent: 'mock',
        agentVersion: AGENT_VERSION,
        rev: 0,
        instances: [...instances.keys()],
      }),
    )
    // 首帧全量
    for (const inst of instances.values()) {
      if (client.filter && !client.filter.has(inst.id)) continue
      res.write(fap.sse({ t: 'state', instance: inst.id, rev: inst.rev, sync: snapshot(inst) }))
    }
    // 日志补齐
    const backlog = since ? logRing.filter((l) => l.ts > since) : logRing.slice(-200)
    for (const line of backlog) {
      if (client.filter && !client.filter.has(line.instance)) continue
      res.write(fap.sse({ t: 'log', instance: line.instance, entries: [line] }))
    }
    req.on('close', () => {
      clients.delete(client)
    })
    return undefined
  }

  // /v1/instances/:id/...
  const m = /^\/v1\/instances\/([^/]+)(?:\/(.+))?$/.exec(pathname)
  if (m) {
    const inst = instances.get(decodeURIComponent(m[1]))
    if (!inst) return fail(res, 404, 'E_UNSUPPORTED', `未知实例 ${m[1]}`)
    const sub = m[2] || ''

    if (req.method === 'GET' && sub === 'state') return json(res, 200, snapshot(inst))

    if (req.method === 'GET' && sub === 'frame') {
      const svg = renderSvg(inst)
      res.writeHead(200, { 'content-type': 'image/svg+xml; charset=utf-8', 'cache-control': 'no-store' })
      return res.end(svg)
    }

    if (req.method === 'POST' && sub === 'control') {
      const body = await readBody(req)
      if (!body || typeof body.action !== 'string') return fail(res, 400, 'E_BAD_REQUEST', '缺少 action')
      const key = body.idempotencyKey || req.headers['idempotency-key']
      if (key) {
        const hit = idempotency.get(key)
        if (hit && Date.now() - hit.at < IDEMPOTENCY_TTL) return json(res, 200, hit.result)
      }
      if (!fap.CONTROL_ACTIONS.includes(body.action)) return fail(res, 400, 'E_UNSUPPORTED', `未知动作 ${body.action}`)
      if (body.action === 'run_once') {
        // run_once 不改变状态，只触发一次任务
        pushLog(inst.id, { task: inst.currentTask || 'main', msg: '立即执行（mock）' })
        const result = { ok: true, action: body.action, state: inst.state, rev: inst.rev }
        if (key) idempotency.set(key, { at: Date.now(), result })
        broadcast({ t: 'event', instance: inst.id, kind: 'task_start', detail: { task: inst.currentTask || 'main' } })
        return json(res, 200, result)
      }
      const next = fap.transition(inst.state, body.action)
      if (!next) {
        return fail(res, 409, 'E_UNSUPPORTED', `状态 ${inst.state} 不接受动作 ${body.action}`)
      }
      inst.state = next
      inst.rev += 1
      if (next === 'running') {
        inst.currentTask = inst.currentTask || 'main'
        inst.nextRun = new Date(Date.now() + 30_000).toISOString()
      }
      if (next === 'stopped' || next === 'paused') {
        inst.currentTask = next === 'paused' ? inst.currentTask : null
        inst.nextRun = null
      }
      pushLog(inst.id, { msg: `控制命令 ${body.action} → ${next}` })
      broadcast({ t: 'event', instance: inst.id, kind: body.action === 'repair' ? 'recover' : 'control', detail: { action: body.action, state: next } })
      broadcast({ t: 'state', instance: inst.id, rev: inst.rev, sync: snapshot(inst) })
      const result = { ok: true, action: body.action, state: next, rev: inst.rev }
      if (key) idempotency.set(key, { at: Date.now(), result })
      // starting → running（模拟窗口拉起）
      if (next === 'starting') {
        setTimeout(() => {
          if (inst.state !== 'starting') return
          inst.state = 'running'
          inst.currentTask = 'main'
          inst.rev += 1
          pushLog(inst.id, { msg: '窗口已就绪，进入运行态' })
          broadcast({ t: 'state', instance: inst.id, rev: inst.rev, sync: snapshot(inst) })
        }, 900)
      }
      return json(res, 200, result)
    }

    if (req.method === 'POST' && sub === 'task') {
      const body = await readBody(req)
      if (!body || typeof body.task !== 'string') return fail(res, 400, 'E_BAD_REQUEST', '缺少 task')
      if (!fap.TASKS.includes(body.task)) return fail(res, 400, 'E_UNSUPPORTED', `未知任务 ${body.task}`)
      if (inst.state !== 'running') return fail(res, 409, 'E_UNSUPPORTED', `实例未在运行态（${inst.state}）`)
      if (inst.tasks[body.task] && inst.tasks[body.task].enabled === false) return fail(res, 409, 'E_TASK_DISABLED')
      inst.currentTask = body.task
      inst.rev += 1
      pushLog(inst.id, { task: body.task, msg: '立即执行任务' })
      broadcast({ t: 'event', instance: inst.id, kind: 'task_start', detail: { task: body.task } })
      broadcast({ t: 'state', instance: inst.id, rev: inst.rev, sync: snapshot(inst) })
      setTimeout(() => {
        inst.rev += 1
        pushLog(inst.id, { task: body.task, msg: '任务完成（mock）' })
        broadcast({ t: 'event', instance: inst.id, kind: 'task_end', detail: { task: body.task, ok: true } })
        broadcast({ t: 'state', instance: inst.id, rev: inst.rev, sync: snapshot(inst) })
      }, 1200)
      return json(res, 200, { ok: true, task: body.task })
    }

    return fail(res, 404, 'E_UNSUPPORTED', `未知路径 ${pathname}`)
  }

  return fail(res, 404, 'E_UNSUPPORTED', `未知路径 ${pathname}`)
})

server_.listen(PORT, '127.0.0.1', () => {
  const ids = [...instances.keys()].join(',')
  console.log(`[mock-agent] FAP v${fap.PROTOCOL} listening on http://127.0.0.1:${PORT}`)
  console.log(`[mock-agent] instances: ${ids}${TOKEN ? ' (token required)' : ''}`)
})
