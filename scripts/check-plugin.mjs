#!/usr/bin/env node
/**
 * 插件侧集成测试：不启动 DSH，直接拿 routes.js 起一个 http server，
 * 验证鉴权、代理、SSE 聚合三条链路。
 *
 *   node scripts/check-plugin.mjs
 *
 * 覆盖的开关（每条都对应一个真实攻击面或故障面）：
 *   1. GET  → 200 且下发 HttpOnly cookie
 *   2. POST 无 cookie → 401
 *   3. POST 带 cookie → 200 且 Agent 状态真的变了
 *   4. 跨站标记（sec-fetch-site: cross-site）→ 403
 *   5. Origin 与 Host 不一致 → 403
 *   6. 未知路径 → 404
 *   7. SSE 首包带 agent 状态与 state 快照
 */

import http from 'node:http'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import { AgentClient } from '../lib/agent-client.js'
import { createRoutes } from '../lib/routes.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')

let failures = 0
const ok = (name, extra) => console.log(`  ✓ ${name}${extra ? ` — ${extra}` : ''}`)
const fail = (name, detail) => {
  failures += 1
  console.error(`  ✗ ${name}${detail ? ` — ${detail}` : ''}`)
}
const assert = (name, cond, detail) => (cond ? ok(name, detail) : fail(name, detail))

const agentPort = 5200 + Math.floor(Math.random() * 400)
const pluginPort = 5700 + Math.floor(Math.random() * 400)
const TOKEN = 'test-token-abc'

const agentProc = spawn(process.execPath, [path.join(root, 'agent/mock/index.js'), '--port', String(agentPort), '--instances', '2'], {
  stdio: ['ignore', 'pipe', 'pipe'],
})
let agentLog = ''
agentProc.stdout.on('data', (c) => (agentLog += String(c)))
agentProc.stderr.on('data', (c) => (agentLog += String(c)))

async function waitFor(url, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url)
      if (res.ok) return true
    } catch {
      /* 尚未监听 */
    }
    await new Promise((r) => setTimeout(r, 150))
  }
  return false
}

const agent = new AgentClient({ url: `http://127.0.0.1:${agentPort}`, log: (l) => console.log(`    [agent-client] ${l}`) })
const handler = createRoutes({
  agent,
  config: { ui: { defaultFps: 4 } },
  token: TOKEN,
  log: (l) => console.log(`    [plugin] ${l}`),
})
const server = http.createServer(handler)

try {
  const up = await waitFor(`http://127.0.0.1:${agentPort}/v1/health`)
  assert('Mock Agent 就绪', up, up ? undefined : agentLog.slice(0, 200))
  await new Promise((resolve) => server.listen(pluginPort, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${pluginPort}/plugins/dsh-qq-farm`

  // 1. GET + cookie
  const listRes = await fetch(`${base}/api/instances`)
  const listBody = await listRes.json()
  const setCookie = listRes.headers.get('set-cookie') || ''
  assert('GET /api/instances → 200', listRes.status === 200 && listBody.ok === true)
  assert('下发 HttpOnly cookie', /qqfarm=[^;]+/.test(setCookie) && /HttpOnly/i.test(setCookie) && /SameSite=Strict/.test(setCookie), setCookie.slice(0, 80))
  const cookie = setCookie.split(';')[0]
  const id = listBody.instances[0].id

  // 2. POST 无 cookie → 401
  const noCookie = await fetch(`${base}/api/instances/${id}/control`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ action: 'pause' }),
  })
  const noCookieBody = await noCookie.json()
  assert('POST 无 cookie → 401', noCookie.status === 401 && noCookieBody.error.code === 'E_AUTH', `${noCookie.status}`)

  // 3. POST 带 cookie → 200，且 Agent 状态真的变了
  const withCookie = await fetch(`${base}/api/instances/${id}/control`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', cookie },
    body: JSON.stringify({ action: 'pause' }),
  })
  const withCookieBody = await withCookie.json()
  assert('POST 带 cookie → 200', withCookie.status === 200 && withCookieBody.result.state === 'paused', JSON.stringify(withCookieBody.result || withCookieBody.error))

  const afterState = await (await fetch(`${base}/api/instances/${id}/state`, { headers: { cookie } })).json()
  assert('状态确实传递到 Agent', afterState.state.summary.state === 'paused', afterState.state.summary.state)

  // 4. 跨站 → 403
  const crossSite = await fetch(`${base}/api/instances/${id}/control`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', cookie, 'sec-fetch-site': 'cross-site' },
    body: JSON.stringify({ action: 'resume' }),
  })
  assert('跨站请求 → 403', crossSite.status === 403, `${crossSite.status}`)

  // 5. Origin 与 Host 不一致 → 403
  const badOrigin = await fetch(`${base}/api/instances`, {
    headers: { origin: 'http://evil.example.com' },
  })
  assert('异常 Origin → 403', badOrigin.status === 403, `${badOrigin.status}`)

  // 6. 未知路径 → 404
  const notFound = await fetch(`${base}/api/nope`)
  assert('未知路径 → 404', notFound.status === 404, `${notFound.status}`)

  // 7. SSE 聚合
  const sse = await new Promise((resolve) => {
    const controller = new AbortController()
    const timer = setTimeout(() => {
      controller.abort()
      resolve({ ok: false, reason: 'timeout' })
    }, 6000)
    fetch(`${base}/stream`, { signal: controller.signal })
      .then(async (res) => {
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buf = ''
        let sawAgent = false
        let sawState = false
        let sawFrame = false
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buf += decoder.decode(value, { stream: true })
          if (buf.includes('"t":"agent"')) sawAgent = true
          if (buf.includes('"t":"state"')) sawState = true
          if (buf.includes('"t":"frame"')) sawFrame = true
          if (sawAgent && sawState && sawFrame) break
        }
        clearTimeout(timer)
        controller.abort()
        resolve({ ok: sawAgent && sawState && sawFrame, sawAgent, sawState, sawFrame })
      })
      .catch((error) => {
        clearTimeout(timer)
        resolve({ ok: false, reason: String(error && error.message) })
      })
  })
  assert('SSE 含 agent 状态', sse.sawAgent === true, JSON.stringify(sse))
  assert('SSE 含 state 快照', sse.sawState === true)
  assert('SSE 含 frame 推送', sse.sawFrame === true)
} catch (error) {
  fail('集成测试异常', String((error && error.stack) || error).slice(0, 500))
} finally {
  agent.stop()
  server.close()
  agentProc.kill('SIGTERM')
}

console.log('')
if (failures === 0) {
  console.log('插件侧集成测试全部通过 ✅')
  process.exit(0)
} else {
  console.error(`${failures} 项失败 ❌`)
  process.exit(1)
}
