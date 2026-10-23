#!/usr/bin/env node
/**
 * macOS 后端 × 插件路由 的端到端联调。
 *
 *   node scripts/check-mac.mjs
 *
 * 启动真实的 agent/mac（--dry-run，只识别不点击），再用 lib/routes.js 起插件侧
 * HTTP 面，验证面板实际依赖的整条链路：鉴权 → 代理 → SSE（含真实截图帧）。
 *
 * 需要：macOS + 已编译 agent/mac/bin/macd + 游戏窗口打开。
 * 没有游戏窗口时仍会通过（Agent 会返回 0 实例），但会标记为"窗口未就绪"。
 */

import http from 'node:http'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import { AgentClient } from '../lib/agent-client.js'
import { createRoutes } from '../lib/routes.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
const TOKEN = 'mac-e2e-token'

let failures = 0
const ok = (n, e) => console.log(`  ✓ ${n}${e ? ` — ${e}` : ''}`)
const fail = (n, d) => {
  failures += 1
  console.error(`  ✗ ${n}${d ? ` — ${d}` : ''}`)
}
const assert = (n, c, d) => (c ? ok(n, d) : fail(n, d))

const agentPort = 4600 + Math.floor(Math.random() * 300)
const pluginPort = 4900 + Math.floor(Math.random() * 300)

const agentProc = spawn('python3', [path.join(root, 'agent/mac/macagent.py'), '--port', String(agentPort), '--dry-run'], {
  stdio: ['ignore', 'pipe', 'pipe'],
})
let agentLog = ''
agentProc.stdout.on('data', (c) => (agentLog += String(c)))
agentProc.stderr.on('data', (c) => (agentLog += String(c)))

async function waitFor(url, timeoutMs = 15000) {
  const end = Date.now() + timeoutMs
  while (Date.now() < end) {
    try {
      const r = await fetch(url)
      if (r.ok) return true
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 200))
  }
  return false
}

const agent = new AgentClient({ url: `http://127.0.0.1:${agentPort}`, log: (l) => console.log(`    [client] ${l}`) })
const handler = createRoutes({ agent, config: { ui: { defaultFps: 2 } }, token: TOKEN, log: (l) => console.log(`    [plugin] ${l}`) })
const server = http.createServer(handler)

try {
  const up = await waitFor(`http://127.0.0.1:${agentPort}/v1/health`)
  assert('mac Agent 启动', up, up ? undefined : agentLog.slice(-400))
  await new Promise((r) => server.listen(pluginPort, '127.0.0.1', r))
  const base = `http://127.0.0.1:${pluginPort}/plugins/dsh-qq-farm`

  const healthRes = await fetch(`${base}/api/health`)
  const health = await healthRes.json()
  assert('插件 /api/health 200', healthRes.status === 200 || healthRes.status === 503)
  assert('上报 agentId=mac', health.agent && health.agent.agentId === 'mac', JSON.stringify(health.agent && health.agent.agentId))
  assert('识别到游戏窗口', health.agent && health.agent.window !== null,
    health.agent && health.agent.window ? `wid=${health.agent.window.wid} page=${health.agent.window.page}` : '窗口未就绪')
  assert('dryRun 生效', health.agent && health.agent.dryRun === true)

  const listRes = await fetch(`${base}/api/instances`)
  const cookie = (listRes.headers.get('set-cookie') || '').split(';')[0]
  const list = await listRes.json()
  assert('/api/instances 200', listRes.status === 200 && list.ok)
  assert('有实例', Array.isArray(list.instances) && list.instances.length >= 1, `${list.instances && list.instances.length}`)

  if (list.instances && list.instances.length > 0) {
    const id = list.instances[0].id
    const st = await (await fetch(`${base}/api/instances/${encodeURIComponent(id)}/state`)).json()
    assert('state 含 profile', st.state && typeof st.state.profile.level === 'number', `Lv${st.state.profile.level}`)
    assert('state 含 24 格地块', st.state.plots.length === 24, `${st.state.plots.length}`)

    // 真实截图（macd capture → JPEG）
    const frameRes = await fetch(`${base}/api/instances/${encodeURIComponent(id)}/frame`)
    const buf = Buffer.from(await frameRes.arrayBuffer())
    const isJpeg = buf[0] === 0xff && buf[1] === 0xd8
    assert('frame 是 JPEG', isJpeg && buf.length > 1000, `${buf.length} bytes, ${frameRes.headers.get('content-type')}`)

    // 写操作鉴权
    const noCookie = await fetch(`${base}/api/instances/${encodeURIComponent(id)}/control`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ action: 'run_once' }),
    })
    assert('POST 无 cookie → 401', noCookie.status === 401)

    const withCookie = await fetch(`${base}/api/instances/${encodeURIComponent(id)}/control`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', cookie },
      body: JSON.stringify({ action: 'run_once' }),
    })
    const wc = await withCookie.json()
    assert('POST 带 cookie → 200（dry-run 不点击）', withCookie.status === 200, JSON.stringify(wc).slice(0, 160))

    // SSE：应看到 agent 状态 + state + 真实帧
    const sse = await new Promise((resolve) => {
      const ctl = new AbortController()
      const timer = setTimeout(() => {
        ctl.abort()
        resolve({ ok: false })
      }, 12000)
      fetch(`${base}/stream?fps=2`, { signal: ctl.signal })
        .then(async (res) => {
          const reader = res.body.getReader()
          const dec = new TextDecoder()
          let buf2 = ''
          let sawState = false
          let sawFrame = false
          while (true) {
            const { done, value } = await reader.read()
            if (done) break
            buf2 += dec.decode(value, { stream: true })
            if (buf2.includes('"t":"state"')) sawState = true
            if (buf2.includes('"t":"frame"')) sawFrame = true
            if (sawState && sawFrame) break
          }
          clearTimeout(timer)
          ctl.abort()
          resolve({ ok: true, sawState, sawFrame })
        })
        .catch(() => {
          clearTimeout(timer)
          resolve({ ok: false })
        })
    })
    assert('SSE 推送 state', sse.sawState === true)
    assert('SSE 推送真实截图帧', sse.sawFrame === true)
  }
} catch (error) {
  fail('联调异常', String((error && error.stack) || error).slice(0, 600))
} finally {
  agent.stop()
  server.close()
  agentProc.kill('SIGTERM')
}

console.log('')
if (failures === 0) {
  console.log('macOS 端到端联调通过 ✅')
  process.exit(0)
} else {
  console.error(`${failures} 项失败 ❌`)
  if (agentLog) console.error('--- agent log ---\n' + agentLog.slice(-1200))
  process.exit(1)
}
