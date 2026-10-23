#!/usr/bin/env node
/**
 * 自检：语法检查 + FAP 契约测试 + Mock Agent 端到端冒烟。
 *
 *   node scripts/check.mjs
 *
 * 这个脚本是 M0 的"质量闸门"：它不验证农场功能（那是 M1），
 * 只验证"协议没写漏、Mock 能跑通、插件能过语法"。
 */

import { spawn, execFileSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
const require = createRequire(import.meta.url)
const fap = require(path.join(root, 'shared', 'fap.cjs'))

let failures = 0
function ok(name, extra) {
  console.log(`  ✓ ${name}${extra ? ` — ${extra}` : ''}`)
}
function fail(name, detail) {
  failures += 1
  console.error(`  ✗ ${name}${detail ? ` — ${detail}` : ''}`)
}
function assert(name, cond, detail) {
  if (cond) ok(name, detail)
  else fail(name, detail)
}

// ── 1. 语法 ───────────────────────────────────────────────────────────
console.log('\n[1/3] 语法检查')
const jsFiles = ['lib/index.js', 'lib/routes.js', 'lib/agent-client.js', 'lib/supervisor.js', 'lib/tools.js', 'lib/client.js', 'agent/mock/index.js']
for (const rel of jsFiles) {
  try {
    execFileSync(process.execPath, ['--check', path.join(root, rel)], { stdio: 'pipe' })
    ok(rel)
  } catch (error) {
    fail(rel, String(error.stderr || error.message).slice(0, 300))
  }
}
try {
  execFileSync(process.execPath, ['--check', path.join(root, 'shared/fap.cjs')], { stdio: 'pipe' })
  ok('shared/fap.cjs')
} catch (error) {
  fail('shared/fap.cjs', String(error.stderr || error.message).slice(0, 300))
}

// ── 2. FAP 契约 ──────────────────────────────────────────────────────
console.log('\n[2/3] FAP 契约')
assert('PROTOCOL 为 1', fap.PROTOCOL === 1)
assert('BASE 前缀正确', fap.BASE === '/plugins/dsh-qq-farm')
assert('地块 24 格', fap.makePlots().length === 24, `${fap.makePlots().length}`)
assert('非法迁移返回 null', fap.transition('idle', 'pause') === null)
assert('合法迁移 idle→start→starting', fap.transition('idle', 'start') === 'starting')
assert('running→pause→paused', fap.transition('running', 'pause') === 'paused')
assert(
  '所有状态都在 STATES 内',
  Object.values(fap.TRANSITIONS).every((row) => Object.values(row).every((s) => fap.STATES.includes(s))),
)
const target = { summary: { currentTask: 'main' } }
fap.applyPatch(target, [{ op: 'replace', path: '/summary/currentTask', value: 'friend' }])
assert('RFC6902 子集 replace', target.summary.currentTask === 'friend')
assert('SSE 帧格式', fap.sse({ t: 'ping' }) === 'data: {"t":"ping"}\n\n')
assert('错误码文案完整', Object.keys(fap.ERROR_TEXT).every((k) => typeof fap.ERROR_TEXT[k] === 'string'))

// ── 3. Mock Agent 端到端 ──────────────────────────────────────────────
console.log('\n[3/3] Mock Agent 端到端')
const port = 5000 + Math.floor(Math.random() * 900)
const baseUrl = `http://127.0.0.1:${port}`
const child = spawn(process.execPath, [path.join(root, 'agent/mock/index.js'), '--port', String(port), '--instances', '2'], {
  stdio: ['ignore', 'pipe', 'pipe'],
})
let agentLog = ''
child.stdout.on('data', (c) => {
  agentLog += String(c)
})
child.stderr.on('data', (c) => {
  agentLog += String(c)
})

async function waitForHealth(timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${baseUrl}/v1/health`)
      if (res.ok) return true
    } catch {
      /* 尚未监听 */
    }
    await new Promise((r) => setTimeout(r, 150))
  }
  return false
}

try {
  const healthy = await waitForHealth()
  assert('Agent 启动并响应 /v1/health', healthy, healthy ? undefined : agentLog.slice(0, 300))

  if (healthy) {
    const health = await (await fetch(`${baseUrl}/v1/health`)).json()
    assert('health.protocol === 1', health.protocol === 1)
    assert('health 报告 2 个实例', health.instanceCount === 2, `${health.instanceCount}`)

    const instances = await (await fetch(`${baseUrl}/v1/instances`)).json()
    assert('/v1/instances 返回数组', Array.isArray(instances) && instances.length === 2)
    const id = instances[0].id

    const state = await (await fetch(`${baseUrl}/v1/instances/${id}/state`)).json()
    assert('state.plots 为 24 格', Array.isArray(state.plots) && state.plots.length === 24)
    assert('state 带 rev', typeof state.rev === 'number')

    const frameRes = await fetch(`${baseUrl}/v1/instances/${id}/frame`)
    const frameType = frameRes.headers.get('content-type') || ''
    const frameBody = await frameRes.text()
    assert('单帧为 SVG', frameType.includes('svg') && frameBody.startsWith('<svg'), frameType)
    assert('帧非空', frameBody.length > 500, `${frameBody.length} bytes`)

    // 暂停 → 继续
    const pause = await (await fetch(`${baseUrl}/v1/instances/${id}/control`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ action: 'pause', idempotencyKey: 'k1' }),
    })).json()
    assert('pause 生效', pause.ok === true && pause.state === 'paused', JSON.stringify(pause))

    // 幂等：同 key 重复请求应返回同一结果而不是再执行一次
    const pauseAgain = await (await fetch(`${baseUrl}/v1/instances/${id}/control`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ action: 'pause', idempotencyKey: 'k1' }),
    })).json()
    assert('idempotencyKey 去重', pauseAgain.state === 'paused' && pauseAgain.rev === pause.rev)

    const resume = await (await fetch(`${baseUrl}/v1/instances/${id}/control`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ action: 'resume' }),
    })).json()
    assert('resume 生效', resume.ok === true && resume.state === 'running')

    // 非法迁移必须 409：此时实例已是 running，再发 resume 或 pause 之外的非法动作
    const bad = await fetch(`${baseUrl}/v1/instances/${id}/control`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ action: 'resume' }),
    })
    const badBody = await bad.json()
    assert('非法迁移返回 409', bad.status === 409 && badBody.error && badBody.error.code === 'E_UNSUPPORTED', `${bad.status}`, JSON.stringify(badBody))

    const task = await (await fetch(`${baseUrl}/v1/instances/${id}/task`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ task: 'friend' }),
    })).json()
    assert('立即执行任务', task.ok === true && task.task === 'friend')

    // SSE：连上后应收到 hello + state
    const sseOk = await new Promise((resolve) => {
      const controller = new AbortController()
      const timer = setTimeout(() => {
        controller.abort()
        resolve(false)
      }, 4000)
      fetch(`${baseUrl}/v1/stream?fps=0`, { signal: controller.signal })
        .then(async (res) => {
          const reader = res.body.getReader()
          const decoder = new TextDecoder()
          let buf = ''
          let sawHello = false
          let sawState = false
          while (true) {
            const { done, value } = await reader.read()
            if (done) break
            buf += decoder.decode(value, { stream: true })
            if (buf.includes('"t":"hello"')) sawHello = true
            if (buf.includes('"t":"state"')) sawState = true
            if (sawHello && sawState) break
          }
          clearTimeout(timer)
          controller.abort()
          resolve(sawHello && sawState)
        })
        .catch(() => {
          clearTimeout(timer)
          resolve(false)
        })
    })
    assert('SSE 首包 hello + state', sseOk)
  }
} catch (error) {
  fail('端到端冒烟异常', String(error && error.stack || error).slice(0, 400))
} finally {
  child.kill('SIGTERM')
}

// ── 汇总 ─────────────────────────────────────────────────────────────
console.log('')
if (failures === 0) {
  console.log('全部通过 ✅')
  process.exit(0)
} else {
  console.error(`${failures} 项失败 ❌`)
  process.exit(1)
}
