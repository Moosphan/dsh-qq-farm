/**
 * 同机 Agent 进程托管（mode: managed）。
 *
 * 与 agent-client 分工：client 只管协议，supervisor 只管进程生命周期。
 * 两者都不做"业务判断"，崩溃分类与降级策略在 index.js 组装。
 */

import { spawn } from 'node:child_process'

const RESTART_MIN_MS = 1000
const RESTART_MAX_MS = 30_000
/** 进程存活超过这个时间就认为"启动成功过"，重置退避。 */
const HEALTHY_UPTIME_MS = 30_000

export class Supervisor {
  /**
   * @param {{
   *   command: string[],
   *   cwd?: string,
   *   env?: Record<string, string>,
   *   log?: (line: string) => void,
   * }} options
   */
  constructor(options) {
    this.command = options.command
    this.cwd = options.cwd
    this.env = options.env || {}
    this.log = options.log || (() => {})
    /** @type {import('node:child_process').ChildProcess|null} */
    this.child = null
    this.stopping = false
    this.restartDelay = RESTART_MIN_MS
    /** @type {string[]} 最近 200 行输出，面板/工具可直接读 */
    this.tail = []
    this.startedAt = 0
    this.restarts = 0
    /** @type {NodeJS.Timeout|null} */
    this.timer = null
  }

  get pid() {
    return this.child && this.child.pid ? this.child.pid : null
  }

  get running() {
    return Boolean(this.child && this.child.exitCode === null)
  }

  start() {
    if (this.child) return
    this.stopping = false
    const [cmd, ...args] = this.command
    this.log(`supervisor: spawn ${this.command.join(' ')}`)
    const child = spawn(cmd, args, {
      cwd: this.cwd,
      env: { ...process.env, ...this.env },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    this.child = child
    this.startedAt = Date.now()

    const onLine = (stream) => (chunk) => {
      const text = String(chunk)
      for (const line of text.split('\n')) {
        const trimmed = line.trimEnd()
        if (!trimmed) continue
        this.tail.push(`${stream}: ${trimmed}`)
        if (this.tail.length > 200) this.tail.shift()
        this.log(`[agent/${stream}] ${trimmed}`)
      }
    }
    child.stdout.on('data', onLine('out'))
    child.stderr.on('data', onLine('err'))

    child.on('error', (error) => {
      this.log(`supervisor: spawn failed: ${String(error)}`)
      this.child = null
      this.scheduleRestart()
    })

    child.on('exit', (code, signal) => {
      const uptime = Date.now() - this.startedAt
      this.child = null
      this.log(`supervisor: agent exited code=${code} signal=${signal} uptime=${Math.round(uptime / 1000)}s`)
      if (this.stopping) return
      if (uptime > HEALTHY_UPTIME_MS) this.restartDelay = RESTART_MIN_MS
      this.scheduleRestart()
    })
  }

  scheduleRestart() {
    if (this.stopping || this.timer) return
    const delay = this.restartDelay
    this.restartDelay = Math.min(RESTART_MAX_MS, Math.round(this.restartDelay * 1.8))
    this.restarts += 1
    this.log(`supervisor: restart in ${delay}ms (restart #${this.restarts})`)
    this.timer = setTimeout(() => {
      this.timer = null
      if (!this.stopping) this.start()
    }, delay)
    if (typeof this.timer.unref === 'function') this.timer.unref()
  }

  stop() {
    this.stopping = true
    if (this.timer) {
      clearTimeout(this.timer)
      this.timer = null
    }
    if (!this.child) return
    const child = this.child
    this.child = null
    try {
      child.kill('SIGTERM')
    } catch {
      // 已经退出
    }
    const killer = setTimeout(() => {
      try {
        child.kill('SIGKILL')
      } catch {
        // 已经退出
      }
    }, 5000)
    if (typeof killer.unref === 'function') killer.unref()
  }
}
