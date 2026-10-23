/**
 * 浏览器侧 HTTP 面：`/plugins/dsh-qq-farm/*`。
 *
 * 这一层是"控制面代理"——它不实现任何农场逻辑，只做：
 *   1. 鉴权（同源 + HttpOnly Cookie token）
 *   2. 把浏览器请求翻译成 FAP 请求
 *   3. 把 Agent 的 SSE 聚合成一条浏览器 SSE（含状态缓存，供新开标签页立即出图）
 *
 * 为什么鉴权必须自己做：DSH 的 loopback 信任栅栏只护 `/api`，
 * `/plugins/**` 是插件自己的路由。面板能点"开始"，等于能控制鼠标键盘，
 * 所以这里不允许出现"默认放行"的分支。
 */

import fap from '../shared/fap.cjs'

const COOKIE_NAME = 'qqfarm'

/** @param {import('node:http').IncomingMessage} req */
function parseCookies(req) {
  const raw = req.headers.cookie
  if (!raw) return {}
  const out = {}
  for (const part of raw.split(';')) {
    const idx = part.indexOf('=')
    if (idx < 0) continue
    out[part.slice(0, idx).trim()] = decodeURIComponent(part.slice(idx + 1).trim())
  }
  return out
}

/**
 * 同源校验。GET 允许无 Origin（浏览器同源导航类请求不带），但拒绝跨站标记；
 * 写操作必须带 Origin 且与 Host 一致。
 * @returns {'ok'|'cross-site'|'bad-origin'}
 */
function sameOrigin(req) {
  const site = String(req.headers['sec-fetch-site'] || '')
  if (site === 'cross-site') return 'cross-site'
  if (site === 'same-origin' || site === 'none') return 'ok'
  const origin = req.headers.origin
  if (!origin) return 'ok'
  try {
    const host = new URL(origin).host
    return host === req.headers.host ? 'ok' : 'bad-origin'
  } catch {
    return 'bad-origin'
  }
}

function sendJson(res, status, body, extraHeaders) {
  const data = JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    ...(extraHeaders || {}),
  })
  res.end(data)
}

function sendError(res, status, code, message) {
  sendJson(res, status, { ok: false, error: { code, message: message || fap.ERROR_TEXT[code] || code } })
}

/**
 * @param {{
 *   agent: import('./agent-client.js').AgentClient,
 *   config: any,
 *   token: string,
 *   log: (line: string) => void,
 *   initialFps?: number,
 * }} deps
 */
export function createRoutes(deps) {
  const { agent, config, token, log } = deps
  /** @type {Map<string, any>} 实例状态缓存，新 SSE 连接秒出图 */
  const stateCache = new Map()
  /** @type {Set<import('node:http').ServerResponse>} */
  const writers = new Set()
  let agentFps = Number(config?.ui?.defaultFps || 4)

  agent.onEvent((payload) => {
    if (payload && payload.t === 'state' && payload.instance) {
      if (payload.sync) stateCache.set(payload.instance, payload.sync)
    }
    if (payload && payload.t === 'hello') {
      agentFps = Number(config?.ui?.defaultFps || 4)
    }
    const frame = fap.sse(payload)
    for (const res of writers) {
      try {
        res.write(frame)
      } catch {
        writers.delete(res)
      }
    }
  })

  agent.onStatus((status) => {
    const frame = fap.sse({
      t: 'agent',
      status: status.status,
      info: status.info || null,
      error: status.error || null,
      ts: new Date().toISOString(),
    })
    for (const res of writers) {
      try {
        res.write(frame)
      } catch {
        writers.delete(res)
      }
    }
  })

  function ensureAgentStream() {
    if (writers.size > 0 && agent.streamAbort) return
    agent.start({ instances: null, fps: agentFps })
  }

  /**
   * @param {import('node:http').IncomingMessage} req
   * @param {import('node:http').ServerResponse} res
   * @param {URL} url
   */
  async function handleStream(req, res, url) {
    const fps = Number(url.searchParams.get('fps') || agentFps)
    res.writeHead(200, {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-store',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
    })
    res.write(`retry: 2000\n\n`)
    // 先给缓存快照，避免"空白面板等首帧"
    res.write(
      fap.sse({
        t: 'agent',
        status: agent.status.status,
        info: agent.status.info || agent.lastHealth || null,
        error: agent.status.error || null,
        ts: new Date().toISOString(),
      }),
    )
    for (const [id, state] of stateCache) {
      res.write(fap.sse({ t: 'state', instance: id, rev: state.rev, sync: state }))
    }
    writers.add(res)
    ensureAgentStream()
    const ping = setInterval(() => {
      try {
        res.write(fap.sse({ t: 'ping', ts: new Date().toISOString() }))
      } catch {
        clearInterval(ping)
      }
    }, 15_000)
    if (typeof ping.unref === 'function') ping.unref()
    req.on('close', () => {
      clearInterval(ping)
      writers.delete(res)
    })
  }

  /**
   * @param {import('node:http').IncomingMessage} req
   * @param {import('node:http').ServerResponse} res
   */
  return async function handler(req, res) {
    let url
    try {
      url = new URL(req.url, `http://${req.headers.host || '127.0.0.1'}`)
    } catch {
      return sendError(res, 400, 'E_BAD_REQUEST', 'URL 非法')
    }
    const path = url.pathname.slice(fap.BASE.length) || '/'
    const isWrite = req.method !== 'GET' && req.method !== 'HEAD'

    // ── 鉴权 ──
    const originState = sameOrigin(req)
    if (originState === 'cross-site') return sendError(res, 403, 'E_AUTH', '拒绝跨站请求')
    if (originState === 'bad-origin') return sendError(res, 403, 'E_AUTH', 'Origin 与 Host 不一致')

    const cookies = parseCookies(req)
    const cookieOk = cookies[COOKIE_NAME] === token
    if (isWrite && !cookieOk) {
      return sendError(res, 401, 'E_AUTH', '缺少或错误的 qqfarm cookie，请先打开面板完成一次 GET')
    }
    const setCookie = cookieOk
      ? {}
      : {
          'set-cookie': `${COOKIE_NAME}=${token}; HttpOnly; SameSite=Strict; Path=${fap.BASE}; Max-Age=86400`,
        }

    // ── SSE ──
    if (req.method === 'GET' && path === '/stream') {
      return handleStream(req, res, url)
    }

    // ── 只读代理 ──
    if (req.method === 'GET' && path === '/api/health') {
      const r = await agent.health()
      const body = {
        ok: r.ok,
        plugin: { name: 'dsh-qq-farm', protocol: fap.PROTOCOL },
        agent: r.ok ? r.data : null,
        agentError: r.ok ? null : r.error,
        connection: agent.status.status,
        supervisor: config?.supervisorInfo || null,
      }
      return sendJson(res, r.ok ? 200 : 503, body, setCookie)
    }

    if (req.method === 'GET' && path === '/api/instances') {
      const r = await agent.listInstances()
      if (!r.ok) return sendJson(res, 503, { ok: false, error: r.error }, setCookie)
      return sendJson(res, 200, { ok: true, instances: r.data }, setCookie)
    }

    const stateMatch = /^\/api\/instances\/([^/]+)\/state$/.exec(path)
    if (req.method === 'GET' && stateMatch) {
      const r = await agent.instanceState(decodeURIComponent(stateMatch[1]))
      if (!r.ok) return sendJson(res, 503, { ok: false, error: r.error }, setCookie)
      return sendJson(res, 200, { ok: true, state: r.data }, setCookie)
    }

    const settingsMatch = /^\/api\/instances\/([^/]+)\/settings$/.exec(path)
    if (req.method === 'POST' && settingsMatch) {
      const body = await readJson(req)
      const r = await agent.settings(decodeURIComponent(settingsMatch[1]), body)
      return sendJson(res, r.ok ? 200 : r.status || 503, r.ok ? { ok: true, ...r.data } : { ok: false, error: r.error })
    }

    const harvestMatch = /^\/api\/instances\/([^/]+)\/harvest_plot$/.exec(path)
    if (req.method === 'POST' && harvestMatch) {
      const body = await readJson(req)
      const r = await agent.harvestPlot(decodeURIComponent(harvestMatch[1]), body.row, body.col)
      return sendJson(res, r.ok ? 200 : r.status || 503, r.ok ? { ok: true, ...r.data } : { ok: false, error: r.error })
    }

    const clickPlotMatch = /^\/api\/instances\/([^/]+)\/click_plot$/.exec(path)
    if (req.method === 'POST' && clickPlotMatch) {
      const body = await readJson(req)
      const r = await agent.clickPlot(decodeURIComponent(clickPlotMatch[1]), body.row, body.col)
      return sendJson(res, r.ok ? 200 : r.status || 503, r.ok ? { ok: true, ...r.data } : { ok: false, error: r.error })
    }

    const clickMatch = /^\/api\/instances\/([^/]+)\/click$/.exec(path)
    if (req.method === 'POST' && clickMatch) {
      const body = await readJson(req)
      const r = await agent.click(decodeURIComponent(clickMatch[1]), body.x, body.y)
      return sendJson(res, r.ok ? 200 : r.status || 503, r.ok ? { ok: true, ...r.data } : { ok: false, error: r.error })
    }

    const calibMatch = /^\/api\/instances\/([^/]+)\/calibrate$/.exec(path)
    if (req.method === 'GET' && calibMatch) {
      const r = await agent.calibrationState(decodeURIComponent(calibMatch[1]))
      if (!r.ok) return sendJson(res, 503, { ok: false, error: r.error }, setCookie)
      return sendJson(res, 200, { ok: true, ...r.data }, setCookie)
    }

    const tasksMatch = /^\/api\/instances\/([^/]+)\/tasks$/.exec(path)
    if (req.method === 'GET' && tasksMatch) {
      const r = await agent.tasks(decodeURIComponent(tasksMatch[1]))
      if (!r.ok) return sendJson(res, 503, { ok: false, error: r.error }, setCookie)
      return sendJson(res, 200, { ok: true, ...r.data }, setCookie)
    }

    const frameMatch = /^\/api\/instances\/([^/]+)\/frame$/.exec(path)
    if (req.method === 'GET' && frameMatch) {
      const r = await agent.frame(decodeURIComponent(frameMatch[1]))
      if (!r.ok) return sendJson(res, 503, { ok: false, error: r.error }, setCookie)
      res.writeHead(200, {
        'content-type': r.contentType,
        'cache-control': 'no-store',
        'content-length': r.buffer.length,
      })
      return res.end(r.buffer)
    }

    // ── 写操作（已通过 cookie 校验）──
    const controlMatch = /^\/api\/instances\/([^/]+)\/control$/.exec(path)
    if (req.method === 'POST' && controlMatch) {
      const body = await readJson(req)
      if (!body || typeof body.action !== 'string') return sendError(res, 400, 'E_BAD_REQUEST', '缺少 action')
      if (!fap.CONTROL_ACTIONS.includes(body.action)) return sendError(res, 400, 'E_UNSUPPORTED', `未知动作 ${body.action}`)
      const r = await agent.control(decodeURIComponent(controlMatch[1]), body.action, crypto.randomUUID())
      log(`control ${controlMatch[1]} ${body.action} -> ${r.ok ? 'ok' : r.error.code}`)
      return sendJson(res, r.ok ? 200 : r.status || 503, r.ok ? { ok: true, result: r.data } : { ok: false, error: r.error })
    }

    if (req.method === 'POST' && calibMatch) {
      const r = await agent.calibrate(decodeURIComponent(calibMatch[1]), 'start')
      log(`calibrate ${calibMatch[1]} -> ${r.ok ? 'started' : r.error.code}`)
      return sendJson(res, r.ok ? 200 : r.status || 503, r.ok ? { ok: true, ...r.data } : { ok: false, error: r.error })
    }

    if (req.method === 'POST' && tasksMatch) {
      const body = await readJson(req)
      if (!body || typeof body.name !== 'string') return sendError(res, 400, 'E_BAD_REQUEST', '缺少 name')
      const patch = {}
      if (typeof body.enabled === 'boolean') patch.enabled = body.enabled
      if (body.intervalSec !== undefined) patch.intervalSec = Number(body.intervalSec)
      const r = await agent.setTask(decodeURIComponent(tasksMatch[1]), body.name, patch)
      log(`task-config ${tasksMatch[1]} ${body.name} ${JSON.stringify(patch)} -> ${r.ok ? 'ok' : r.error.code}`)
      return sendJson(res, r.ok ? 200 : r.status || 503, r.ok ? { ok: true, ...r.data } : { ok: false, error: r.error })
    }

    const taskMatch = /^\/api\/instances\/([^/]+)\/task$/.exec(path)
    if (req.method === 'POST' && taskMatch) {
      const body = await readJson(req)
      if (!body || typeof body.task !== 'string') return sendError(res, 400, 'E_BAD_REQUEST', '缺少 task')
      const r = await agent.runTask(decodeURIComponent(taskMatch[1]), body.task)
      log(`task ${taskMatch[1]} ${body.task} -> ${r.ok ? 'ok' : r.error.code}`)
      return sendJson(res, r.ok ? 200 : r.status || 503, r.ok ? { ok: true, result: r.data } : { ok: false, error: r.error })
    }

    return sendError(res, 404, 'E_UNSUPPORTED', `未知路径 ${path}`)
  }
}

/** @param {import('node:http').IncomingMessage} req */
function readJson(req) {
  return new Promise((resolve) => {
    let raw = ''
    req.on('data', (chunk) => {
      raw += chunk
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
    req.on('error', () => resolve(null))
  })
}
