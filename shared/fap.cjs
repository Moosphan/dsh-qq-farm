'use strict'
/**
 * FAP（Farm Agent Protocol）v1 —— 唯一事实源。
 *
 * 宿主半（lib/*.js，ESM）、浏览器半（lib/client.js，手写 bundle）、
 * Mock Agent（agent/mock）与将来的真实 Agent 都必须与这里保持一致。
 *
 * 注意：浏览器半无法 import 本文件（它是预构建 bundle，不走打包），
 * 因此它在 lib/client.js 内联了一份极小的枚举副本（仅状态与错误码文案）。
 * 新增错误码时两处都要改 —— 这是 zero-build 的代价，已在 M0-任务清单 ADR-2 记录。
 */

/** 协议版本。不兼容变更时 +1，Agent 与插件启动时协商。 */
const PROTOCOL = 1

/** 浏览器侧路由前缀（插件自己的路由，不在 DSH /api 信任栅栏内）。 */
const BASE = '/plugins/dsh-qq-farm'

const PLATFORMS = ['qq', 'wechat']
const PLATFORM_LABEL = { qq: 'QQ', wechat: '微信' }

/** 实例状态机（见 docs/M0-任务清单.md §3.7）。 */
const STATES = ['idle', 'starting', 'running', 'paused', 'error', 'stopped']

/** 控制动作白名单。 */
const CONTROL_ACTIONS = ['start', 'pause', 'resume', 'stop', 'run_once', 'repair', 'restart']

/** 稳定错误码（面板与 LLM 都依赖它，禁止随意改名）。 */
const ERROR_TEXT = {
  E_NO_AGENT: '连不上 Agent',
  E_AUTH: '鉴权失败',
  E_WINDOW_LOST: '窗口已关闭或句柄失效',
  E_WINDOW_SIZE: '窗口尺寸/DPI 不符',
  E_CAPTURE_BLACK: '连续截图无效',
  E_PAGE_UNKNOWN: '页面识别失败',
  E_STUCK: '长时间无有效动作',
  E_TEMPLATE_MISS: '模板缺失或版本不匹配',
  E_TASK_DISABLED: '任务已被禁用',
  E_UNSUPPORTED: '方法或字段不支持',
  E_BAD_REQUEST: '请求参数非法',
}

/** 业务任务名（与参考实现 tasks/ 对齐）。 */
const TASKS = [
  'main',
  'friend',
  'grass',
  'share',
  'reward',
  'gift',
  'event',
  'sell',
  'land_scan',
  'timed_harvest',
  'restart',
]

/** 地块等级（与参考实现 config.land.plots[].level 对齐）。 */
const PLOT_LEVELS = ['unbuilt', 'normal', 'red', 'black', 'gold', 'amethyst']

/**
 * 状态迁移表：state -> action -> next state（null 表示非法）。
 * 非法迁移一律返回 null，由调用方回 409 + E_UNSUPPORTED，禁止静默忽略。
 */
const TRANSITIONS = {
  idle: { start: 'starting', stop: 'stopped' },
  starting: { stop: 'stopped' },
  running: { pause: 'paused', stop: 'stopped', run_once: 'running', repair: 'running', restart: 'starting' },
  paused: { resume: 'running', stop: 'stopped' },
  error: { start: 'starting', stop: 'stopped', repair: 'running' },
  stopped: { start: 'starting' },
}

/**
 * @param {string} state 当前状态
 * @param {string} action 控制动作
 * @returns {string|null} 迁移后的状态；非法返回 null
 */
function transition(state, action) {
  const row = TRANSITIONS[state]
  if (!row) return null
  return Object.prototype.hasOwnProperty.call(row, action) ? row[action] : null
}

/** 生成 6 列 × 4 行的 24 格地块骨架（列优先编号 1-1..6-4）。 */
function makePlots() {
  const plots = []
  for (let col = 1; col <= 6; col += 1) {
    for (let row = 1; row <= 4; row += 1) {
      plots.push({
        id: `${col}-${row}`,
        level: 'normal',
        countdownSec: null,
        syncAt: null,
        needPlanting: false,
        needUpgrade: false,
      })
    }
  }
  return plots
}

/** 按 `/a/b/c` 路径写入对象（RFC6902 子集的 replace/add 落点）。 */
function setPath(target, path, value) {
  const parts = String(path).split('/').filter(Boolean)
  if (parts.length === 0) return
  let cursor = target
  for (let i = 0; i < parts.length - 1; i += 1) {
    const key = parts[i]
    if (cursor[key] === undefined || cursor[key] === null || typeof cursor[key] !== 'object') {
      cursor[key] = {}
    }
    cursor = cursor[key]
  }
  cursor[parts[parts.length - 1]] = value
}

/**
 * 应用 RFC6902 子集（replace/add/remove）。
 * @param {object} target 目标状态对象（就地修改）
 * @param {Array<{op:string,path:string,value?:unknown}>} patch
 */
function applyPatch(target, patch) {
  if (!Array.isArray(patch)) return target
  for (const op of patch) {
    if (!op || typeof op.path !== 'string') continue
    if (op.op === 'replace' || op.op === 'add') setPath(target, op.path, op.value)
    else if (op.op === 'remove') setPath(target, op.path, null)
  }
  return target
}

/**
 * 构造一条 SSE 帧。
 * @param {object} payload 任意可 JSON 化的对象，必须含 `t` 字段
 * @returns {string}
 */
function sse(payload) {
  return `data: ${JSON.stringify(payload)}\n\n`
}

/** 从 URL 查询串解析 `instances=a,b`。 */
function parseInstanceFilter(searchParams) {
  const raw = searchParams.get('instances')
  if (!raw) return null
  const ids = raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  return ids.length > 0 ? ids : null
}

module.exports = {
  PROTOCOL,
  BASE,
  PLATFORMS,
  PLATFORM_LABEL,
  STATES,
  CONTROL_ACTIONS,
  ERROR_TEXT,
  TASKS,
  PLOT_LEVELS,
  TRANSITIONS,
  transition,
  makePlots,
  setPath,
  applyPatch,
  sse,
  parseInstanceFilter,
}
