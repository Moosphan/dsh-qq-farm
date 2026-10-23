/**
 * FAP 客户端（插件 → Agent）。
 *
 * 职责：HTTP 命令 + SSE 订阅 + 自动重连 + 状态广播。
 * 刻意不做缓存：缓存属于 routes 层（面板需要的是"最后一次已知状态"）。
 */

import fap from '../shared/fap.cjs'

const RECONNECT_MIN_MS = 800
const RECONNECT_MAX_MS = 10_000

/**
 * @typedef {{ status: 'online'|'offline', info?: object, error?: object }} AgentStatus
 */

export class AgentClient {
  /**
   * @param {{ url: string, token?: string, log?: (line: string) => void }} options
   */
  constructor(options) {
    this.baseUrl = String(options.url || 'http://127.0.0.1:4571').replace(/\/+$/, '')
    /** 端口可能因避让而变化：失败时调用它重新解析一次 */
    this.resolveUrl = options.resolveUrl || null
    this.token = options.token || ''
    this.log = options.log || (() => {})
    /** @type {AgentStatus} */
    this.status = { status: 'offline' }
    /** @type {Set<(s: AgentStatus) => void>} */
    this.statusListeners = new Set()
    /** @type {AbortController|null} */
    this.streamAbort = null
    this.reconnectDelay = RECONNECT_MIN_MS
    /** @type {Set<(payload: any) => void>} */
    this.eventListeners = new Set()
    this.closed = false
    /** 最近一次成功探活的信息，面板用来显示 agent 版本 */
    this.lastHealth = null
  }

  headers(extra) {
    const h = { ...(extra || {}) }
    if (this.token) h.authorization = `Bearer ${this.token}`
    return h
  }

  /** @private */
  async request(path, init = {}, timeoutMs = 5000, retried = false) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    try {
      const res = await fetch(`${this.baseUrl}${path}`, {
        ...init,
        signal: controller.signal,
        headers: this.headers({ 'content-type': 'application/json', ...(init.headers || {}) }),
      })
      const text = await res.text()
      let body = null
      try {
        body = text ? JSON.parse(text) : null
      } catch {
        body = { ok: false, error: { code: 'E_UNSUPPORTED', message: text.slice(0, 200) } }
      }
      if (!res.ok) {
        const err = body && body.error ? body.error : { code: 'E_UNSUPPORTED', message: `HTTP ${res.status}` }
        return { ok: false, status: res.status, error: err }
      }
      return { ok: true, status: res.status, data: body }
    } catch (error) {
      // 连不上：可能是 Agent 因端口避让换了端口，按端口文件重解析后重试一次
      if (this.resolveUrl) {
        try {
          const next = this.resolveUrl()
          if (next && next !== this.baseUrl) {
            this.log(`agent-client: 端口变化 ${this.baseUrl} → ${next}，重试`)
            this.baseUrl = next
            return await this.request(path, init, timeoutMs, true)
          }
        } catch {
          /* 忽略重解析失败 */
        }
      }
      return { ok: false, status: 0, error: { code: 'E_NO_AGENT', message: `连不上 Agent（${this.baseUrl}）：${String(error && error.message)}` } }
    } finally {
      clearTimeout(timer)
    }
  }

  async health() {
    const r = await this.request('/v1/health')
    if (r.ok) this.lastHealth = r.data
    return r
  }

  listInstances() {
    return this.request('/v1/instances')
  }

  instanceState(id) {
    return this.request(`/v1/instances/${encodeURIComponent(id)}/state`)
  }

  control(id, action, idempotencyKey) {
    return this.request(`/v1/instances/${encodeURIComponent(id)}/control`, {
      method: 'POST',
      body: JSON.stringify({ action, idempotencyKey }),
    })
  }

  click(id, x, y) {
    return this.request(`/v1/instances/${encodeURIComponent(id)}/click`, {
      method: 'POST',
      body: JSON.stringify({ x, y }),
    })
  }

  harvestPlot(id, row, col) {
    return this.request(`/v1/instances/${encodeURIComponent(id)}/harvest_plot`, {
      method: 'POST',
      body: JSON.stringify({ row, col }),
    }, 60000)
  }

  clickPlot(id, row, col) {
    return this.request(`/v1/instances/${encodeURIComponent(id)}/click_plot`, {
      method: 'POST',
      body: JSON.stringify({ row, col }),
    })
  }

  settings(id, patch) {
    return this.request(`/v1/instances/${encodeURIComponent(id)}/settings`, {
      method: 'POST',
      body: JSON.stringify(patch || {}),
    })
  }

  calibrate(id, action) {
    return this.request(`/v1/instances/${encodeURIComponent(id)}/calibrate`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
  }

  calibrationState(id) {
    return this.request(`/v1/instances/${encodeURIComponent(id)}/calibrate`)
  }

  tasks(id) {
    return this.request(`/v1/instances/${encodeURIComponent(id)}/tasks`)
  }

  setTask(id, name, patch) {
    return this.request(`/v1/instances/${encodeURIComponent(id)}/tasks`, {
      method: 'POST',
      body: JSON.stringify({ name, ...patch }),
    })
  }

  runTask(id, task) {
    return this.request(`/v1/instances/${encodeURIComponent(id)}/task`, {
      method: 'POST',
      body: JSON.stringify({ task, idempotencyKey: crypto.randomUUID() }),
    })
  }

  /** 取单帧原始字节（用于"截图"按钮）。 */
  async frame(id) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 5000)
    try {
      const res = await fetch(`${this.baseUrl}/v1/instances/${encodeURIComponent(id)}/frame`, {
        signal: controller.signal,
        headers: this.headers({}),
      })
      if (!res.ok) return { ok: false, error: { code: 'E_WINDOW_LOST', message: `HTTP ${res.status}` } }
      const buffer = Buffer.from(await res.arrayBuffer())
      return { ok: true, contentType: res.headers.get('content-type') || 'application/octet-stream', buffer }
    } catch (error) {
      return { ok: false, error: { code: 'E_NO_AGENT', message: String(error && error.message) } }
    } finally {
      clearTimeout(timer)
    }
  }

  /** @param {(s: AgentStatus) => void} fn */
  onStatus(fn) {
    this.statusListeners.add(fn)
    return () => this.statusListeners.delete(fn)
  }

  /** @private */
  setStatus(next) {
    this.status = next
    for (const fn of this.statusListeners) {
      try {
        fn(next)
      } catch (error) {
        this.log(`agent-client: status listener failed: ${String(error)}`)
      }
    }
  }

  /** @param {(payload: any) => void} fn */
  onEvent(fn) {
    this.eventListeners.add(fn)
    return () => this.eventListeners.delete(fn)
  }

  /** @private */
  emit(payload) {
    for (const fn of this.eventListeners) {
      try {
        fn(payload)
      } catch (error) {
        this.log(`agent-client: event listener failed: ${String(error)}`)
      }
    }
  }

  /**
   * 开始订阅。内部自带重连；重复调用是幂等的。
   * @param {{ instances?: string[]|null, fps?: number, since?: string|null }} options
   */
  start(options = {}) {
    if (this.closed) return
    if (this.streamAbort) return
    const params = new URLSearchParams()
    if (options.instances && options.instances.length > 0) params.set('instances', options.instances.join(','))
    params.set('fps', String(options.fps ?? 4))
    if (options.since) params.set('since', options.since)

    const controller = new AbortController()
    this.streamAbort = controller
    this.consume(`/v1/stream?${params.toString()}`, controller).catch((error) => {
      this.log(`agent-client: stream loop ended: ${String(error && error.message)}`)
    })
  }

  /** @private 单次连接的消费循环，返回后由 scheduleReconnect 决定重试。 */
  async consume(path, controller) {
    try {
      const res = await fetch(`${this.baseUrl}${path}`, {
        signal: controller.signal,
        headers: this.headers({ accept: 'text/event-stream' }),
      })
      if (!res.ok || !res.body) throw new Error(`stream HTTP ${res.status}`)
      this.reconnectDelay = RECONNECT_MIN_MS
      const health = await this.health()
      this.setStatus({ status: 'online', info: health.ok ? health.data : null })

      const decoder = new TextDecoder()
      let buffer = ''
      for await (const chunk of res.body) {
        buffer += decoder.decode(chunk, { stream: true })
        let idx = buffer.indexOf('\n\n')
        while (idx >= 0) {
          const block = buffer.slice(0, idx)
          buffer = buffer.slice(idx + 2)
          idx = buffer.indexOf('\n\n')
          const dataLines = block
            .split('\n')
            .filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).trim())
          if (dataLines.length === 0) continue
          try {
            this.emit(JSON.parse(dataLines.join('\n')))
          } catch {
            // 半包/脏帧直接丢弃，不影响后续
          }
        }
      }
      throw new Error('stream closed by agent')
    } catch (error) {
      if (this.closed || controller.signal.aborted) {
        this.streamAbort = null
        return
      }
      this.setStatus({
        status: 'offline',
        error: { code: 'E_NO_AGENT', message: String(error && error.message) },
      })
      this.streamAbort = null
      this.scheduleReconnect()
    }
  }

  /** @private */
  scheduleReconnect() {
    if (this.closed) return
    const delay = this.reconnectDelay
    this.reconnectDelay = Math.min(RECONNECT_MAX_MS, Math.round(this.reconnectDelay * 1.8))
    setTimeout(() => {
      if (this.closed || this.streamAbort) return
      this.start({ instances: null, fps: 4 })
    }, delay)
  }

  stop() {
    this.closed = true
    if (this.streamAbort) {
      this.streamAbort.abort()
      this.streamAbort = null
    }
    this.setStatus({ status: 'offline' })
  }
}
