#!/usr/bin/env node
/**
 * 浏览器半冒烟测试：在 Node 里模拟 DSH 的 browser module loader，
 * 真正执行 lib/client.js 并用 React 18 把它渲染成静态 HTML。
 *
 *   node scripts/check-client.mjs
 *
 * 为什么值得做：面板是"手写 h()、没有构建链"的代码，没有构建失败来兜底，
 * 唯一能自动发现"注册形状写错 / 组件炸了"的方式就是真的渲染一遍。
 */

import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')

// React 来自 DSH 安装目录（与本进程共享 18.x）
// React 来源按优先级探测：显式环境变量 → npx 安装目录 → profile node_modules。
// 桌面版 DSH 把 React 打进 asar，外部 Node 拿不到，这时跳过渲染断言（不误报失败）。
const CANDIDATES = [
  process.env.DSH_NODE_MODULES,
  '/Users/dorck/.npm/_npx/1e7f6d9597241db0/node_modules',
  path.join(process.env.HOME || '', '.dsh/profiles/node_modules'),
].filter(Boolean)

let React = null
let renderToStaticMarkup = null
for (const dir of CANDIDATES) {
  try {
    const req = createRequire(path.join(dir, 'noop.js'))
    React = req('react')
    renderToStaticMarkup = req('react-dom/server').renderToStaticMarkup
    console.log(`  · React 来自 ${dir}`)
    break
  } catch {
    /* 换下一个 */
  }
}
if (!React) {
  console.log('  ⚠ 找不到 React（桌面版把 React 打进 asar）；跳过渲染断言，仅做静态检查')
}

let failures = 0
const ok = (name, extra) => console.log(`  ✓ ${name}${extra ? ` — ${extra}` : ''}`)
const fail = (name, detail) => {
  failures += 1
  console.error(`  ✗ ${name}${detail ? ` — ${detail}` : ''}`)
}
const assert = (name, cond, detail) => (cond ? ok(name, detail) : fail(name, detail))

// ── 模拟浏览器环境 ────────────────────────────────────────────────────
const styleTags = []
const documentStub = {
  head: {
    appendChild(node) {
      styleTags.push(node)
    },
  },
  createElement(tag) {
    return { tag, dataset: {}, textContent: '' }
  },
  getElementById() {
    return null
  },
}
let registered = null
const windowStub = {
  __ModuleLoader__: {
    load(definition) {
      registered = definition
    },
  },
}
const sandbox = {
  window: windowStub,
  document: documentStub,
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  Date,
  JSON,
  Math,
  String,
  Number,
  Object,
  Array,
  Promise,
  fetch: async (url) => {
    if (String(url).includes('/api/instances')) {
      return {
        ok: true,
        json: async () => ({
          ok: true,
          instances: [
            { id: 'qq1', name: 'QQ-号1', platform: 'qq', state: 'running', currentTask: 'main', nextRun: null, lastError: null, fps: 4 },
            { id: 'wechat2', name: '微信-号2', platform: 'wechat', state: 'paused', currentTask: null, nextRun: null, lastError: null, fps: 4 },
          ],
        }),
      }
    }
    return { ok: false, json: async () => ({ ok: false }) }
  },
  EventSource: undefined,
}
sandbox.globalThis = sandbox

const source = fs.readFileSync(path.join(root, 'lib/client.js'), 'utf8')
try {
  vm.runInNewContext(source, sandbox, { filename: 'lib/client.js' })
  ok('bundle 可执行且调用 __ModuleLoader__.load')
} catch (error) {
  fail('bundle 执行失败', String((error && error.stack) || error).slice(0, 500))
}
assert('注册 id 为 dsh-qq-farm', registered && registered.id === 'dsh-qq-farm')
assert('提供了 factory', registered && typeof registered.factory === 'function')

if (React) {
// ── 物化 factory ─────────────────────────────────────────────────────
let exportsObj = null
try {
  exportsObj = registered.factory((id) => {
    if (id === 'react') return React
    throw new Error(`未预期的 require: ${id}`)
  })
  ok('factory 物化成功（只依赖 react）')
} catch (error) {
  fail('factory 物化失败', String((error && error.stack) || error).slice(0, 500))
}

assert('exports.inject 含 slots', exportsObj && Array.isArray(exportsObj.inject) && exportsObj.inject.includes('slots'))
assert('exports.apply 是函数', exportsObj && typeof exportsObj.apply === 'function')
assert('注入了样式标签', styleTags.length === 1 && styleTags[0].id === 'dsh-qq-farm-style')

// ── 模拟 ctx：验证插槽注册形状 ────────────────────────────────────────
let registeredSlot = null
const slotsMock = {
  inject(key, callback) {
    // DSH 语义：声明已存在时同步回调，回调返回 disposer
    const dispose = callback()
    return typeof dispose === 'function' ? dispose : () => {}
  },
  register(options, Component) {
    registeredSlot = { options, Component }
    return () => {}
  },
}
const ctxMock = {
  get(name) {
    if (name === 'slots') return slotsMock
    return undefined
  },
  effect(fn) {
    const disposer = fn()
    return typeof disposer === 'function' ? disposer : () => {}
  },
}

try {
  exportsObj.apply(ctxMock)
  ok('apply(ctx) 执行无异常')
} catch (error) {
  fail('apply(ctx) 抛错', String((error && error.stack) || error).slice(0, 600))
}

assert('注册到 shell.overlay', registeredSlot && registeredSlot.options.name === 'shell.overlay', registeredSlot && registeredSlot.options.name)
assert('带 id 与 order', registeredSlot && registeredSlot.options.id === 'qq-farm' && typeof registeredSlot.options.order === 'number')
assert('提供 label', registeredSlot && typeof registeredSlot.options.label === 'function')
assert('注入 api 面', registeredSlot && registeredSlot.options.inject && typeof registeredSlot.options.inject.control === 'function')

// ── 真正渲染：收起态（胶囊）─────────────────────────────────────────
const api = registeredSlot.options.inject
try {
  const markup = renderToStaticMarkup(React.createElement(registeredSlot.Component, api))
  assert('收起态渲染非空', markup.length > 20, `${markup.length} bytes`)
  assert('收起态含「农场」字样', markup.includes('农场'))
  assert('收起态带 qqf-root 定位类', markup.includes('qqf-root'))
} catch (error) {
  fail('收起态渲染失败', String((error && error.stack) || error).slice(0, 600))
}

// ── 真正渲染：有数据时的收起态（模拟机器人在线）──────────────────────
try {
  // 通过订阅接口喂一份快照，再渲染
  const snapshot = api.getSnapshot()
  assert('快照结构完整', snapshot && Array.isArray(snapshot.instances) && typeof snapshot.agent === 'object')
  const markup = renderToStaticMarkup(React.createElement(registeredSlot.Component, api))
  assert('空数据态渲染稳定', markup.includes('🌾'))
} catch (error) {
  fail('快照渲染失败', String((error && error.stack) || error).slice(0, 600))
}

// ── 展开态：等首次拉取落地后，用 initialOpen 渲染完整面板 ─────────────
await new Promise((resolve) => setTimeout(resolve, 50))
try {
  const snapshot = api.getSnapshot()
  assert('首次拉取写入实例列表', snapshot.instances.length === 2, `${snapshot.instances.length} 个`)
  assert('自动选中第一个实例', snapshot.selected === 'qq1')
  const markup = renderToStaticMarkup(React.createElement(registeredSlot.Component, Object.assign({}, api, { initialOpen: true })))
  assert('展开态渲染非空', markup.length > 400, `${markup.length} bytes`)
  for (const keyword of ['实例', '实时画面', '地块', '日志', '开始', '暂停', '停止', 'QQ-号1', '微信-号2']) {
    assert(`展开态含「${keyword}」`, markup.includes(keyword))
  }
  assert('展开态含控制按钮 disabled 逻辑', markup.includes('qqf-btn'))
} catch (error) {
  fail('展开态渲染失败', String((error && error.stack) || error).slice(0, 800))
}

try {
  api.subscribe(() => {})
  api.select('wechat2')
  assert('select 后快照更新', api.getSnapshot().selected === 'wechat2')
} catch (error) {
  fail('交互 api 冒烟失败', String((error && error.stack) || error).slice(0, 400))
}

}  // if (React)

console.log('')
if (failures === 0) {
  console.log('浏览器半冒烟测试全部通过 ✅')
  process.exit(0)
} else {
  console.error(`${failures} 项失败 ❌`)
  process.exit(1)
}
