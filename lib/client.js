/**
 * dsh-qq-farm 浏览器半（预构建 bundle，手写 React.createElement —— 见 M0-任务清单 ADR-2）。
 *
 * 约束：本文件是"自包含 bundle"，只能 require('react')，不得 import 任何 DSH 内部包，
 * 也不得 import 本包的 shared/fap.cjs（浏览器侧模块解析不支持相对路径）。
 * 因此此处内联了一份极小的枚举副本；新增错误码时需与 shared/fap.cjs 同步。
 */
window.__ModuleLoader__.load({
  id: 'dsh-qq-farm',
  factory: function (require) {
    var module = { exports: {} }
    var exports = module.exports

    var React = require('react')
    var h = React.createElement
    var useState = React.useState
    var useEffect = React.useEffect
    var useSyncExternalStore = React.useSyncExternalStore
    var useRef = React.useRef

    var BASE = '/plugins/dsh-qq-farm'

    var STATE_LABEL = {
      idle: '空闲',
      starting: '启动中',
      running: '运行中',
      paused: '已暂停',
      error: '异常',
      stopped: '已停止',
    }
    var STATE_COLOR = {
      idle: 'var(--dsw-alias-label-tertiary)',
      starting: 'var(--dsw-alias-state-warn-primary)',
      running: 'var(--dsw-alias-state-success-primary)',
      paused: 'var(--dsw-alias-label-secondary)',
      error: 'var(--dsw-alias-state-error-primary)',
      stopped: 'var(--dsw-alias-label-tertiary)',
    }
    var ERROR_TEXT = {
      E_NO_AGENT: '连不上 Agent',
      E_AUTH: '鉴权失败',
      E_WINDOW_LOST: '窗口已关闭',
      E_WINDOW_SIZE: '窗口尺寸不符',
      E_CAPTURE_BLACK: '截图无效',
      E_PAGE_UNKNOWN: '页面识别失败',
      E_STUCK: '长时间无动作',
      E_TEMPLATE_MISS: '模板缺失',
      E_TASK_DISABLED: '任务已禁用',
      E_UNSUPPORTED: '不支持该操作',
      E_BAD_REQUEST: '请求非法',
    }
    var PLOT_COLOR = {
      // 空地＝游戏里的土壤棕
      unbuilt: 'linear-gradient(180deg,#b0855c,#8b5f3a)',
      normal: 'color-mix(in srgb, var(--dsw-alias-state-success-primary) 32%, var(--dsw-alias-bg-layer-1))',
      red: 'color-mix(in srgb, var(--dsw-alias-state-error-primary) 42%, var(--dsw-alias-bg-layer-1))',
      black: 'color-mix(in srgb, var(--dsw-alias-label-primary) 22%, var(--dsw-alias-bg-layer-1))',
      gold: 'color-mix(in srgb, var(--dsw-alias-state-warn-primary) 46%, var(--dsw-alias-bg-layer-1))',
      amethyst: 'color-mix(in srgb, var(--dsw-alias-brand-primary) 44%, var(--dsw-alias-bg-layer-1))',
    }

    // ── 样式（factory 内副作用，随模块物化注入一次）──────────────────
    var STYLE_ID = 'dsh-qq-farm-style'
    if (typeof document !== 'undefined' && !document.getElementById(STYLE_ID)) {
      var style = document.createElement('style')
      style.id = STYLE_ID
      style.dataset.plugin = 'dsh-qq-farm'
      style.textContent = [
        // ── QQ 农场风格：木质边框 + 奶油底 + 金绿按钮 ──
        '.qqf-root{position:fixed;right:18px;bottom:18px;z-index:60;pointer-events:auto;',
        'font:13px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}',
        '.qqf-pill{display:inline-flex;align-items:center;gap:8px;height:38px;padding:0 16px;border-radius:19px;',
        'cursor:pointer;font-weight:700;color:#fff8e7;',
        'background:linear-gradient(180deg,#a9744f,#6b4423);border:2px solid #c99a63;',
        'box-shadow:0 6px 18px rgba(60,35,10,.38),inset 0 1px 0 rgba(255,255,255,.28)}',
        '.qqf-pill:hover{filter:brightness(1.08)}',
        '.qqf-dot{width:9px;height:9px;border-radius:50%;flex:none;box-shadow:0 0 0 2px rgba(255,255,255,.4)}',
        // 固定高度：切 tab 不变形
        '.qqf-panel{width:900px;height:648px;max-height:min(92vh,648px);display:flex;flex-direction:column;',
        'overflow:hidden;color:#4a3520;border:3px solid #8b5a2b;border-radius:16px;',
        'background:linear-gradient(180deg,#fdf6e3,#f6e6c4);',
        'box-shadow:0 18px 44px rgba(60,35,10,.42),inset 0 0 0 2px rgba(255,255,255,.55)}',
        '.qqf-head{display:flex;align-items:center;gap:8px;padding:10px 12px;flex:none;color:#fff8e7;cursor:move;user-select:none;',
        'background:linear-gradient(180deg,#a9744f,#8b5a2b);border-bottom:2px solid #6b4423}',
        '.qqf-title{font-weight:700;flex:1;font-size:14px;text-shadow:0 1px 0 rgba(0,0,0,.3)}',
        '.qqf-x{cursor:pointer;background:rgba(0,0,0,.18);border:1px solid rgba(255,255,255,.35);color:#fff8e7;',
        'font-size:13px;line-height:18px;width:22px;height:22px;border-radius:7px;padding:0}',
        '.qqf-x:hover{background:rgba(0,0,0,.3)}',
        '.qqf-tabs{display:flex;gap:6px;padding:8px 10px;flex:none;background:#f2e0b8;border-bottom:1px solid #d9b98b}',
        '.qqf-tab{border:2px solid #d9b98b;background:#fffbee;color:#8a6a45;border-radius:10px;',
        'padding:4px 12px;font-size:12px;font-weight:700;cursor:pointer}',
        '.qqf-tab[data-on=true]{background:linear-gradient(180deg,#ffd976,#f5b942);border-color:#d99a2b;',
        'color:#5a3a12;box-shadow:inset 0 1px 0 rgba(255,255,255,.7)}',
        '.qqf-main{flex:1;min-height:0;display:flex;gap:10px;padding:10px}',
        '.qqf-left{width:430px;flex:none;display:flex;flex-direction:column;gap:8px;min-height:0}',
        '.qqf-right{flex:1;min-width:0;display:flex;flex-direction:column;min-height:0;',
        'background:#fffbee;border:2px solid #e6d2ad;border-radius:12px;overflow:hidden}',
        '.qqf-body{flex:1;min-height:0;overflow:auto;padding:10px;display:flex;flex-direction:column;gap:8px}',
        '.qqf-sec{display:flex;flex-direction:column;gap:6px;flex:none}',
        '.qqf-sec-t{font-size:11px;font-weight:700;color:#8a6a45;letter-spacing:.02em}',
        '.qqf-inst{display:flex;align-items:center;gap:8px;padding:7px 9px;border-radius:10px;cursor:pointer;',
        'background:#fffdf6;border:2px solid #e6d2ad}',
        '.qqf-inst[data-on=true]{border-color:#5fae3a;background:#f3fbe9}',
        '.qqf-inst-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;',
        'color:#4a3520;font-weight:600}',
        '.qqf-chip{font-size:11px;padding:1px 7px;border-radius:7px;background:#efe0bd;color:#7a5c3a;border:1px solid #e0cda6}',
        '.qqf-frame{width:100%;max-height:392px;object-fit:contain;border-radius:10px;background:#2b1c0e;',
        'border:3px solid #8b5a2b;box-shadow:inset 0 0 14px rgba(0,0,0,.55)}',
        '.qqf-empty{display:flex;align-items:center;justify-content:center;height:120px;border-radius:10px;',
        'background:#fffbee;border:2px dashed #d9b98b;color:#a08560;font-size:12px;text-align:center;padding:0 16px}',
        '.qqf-row{display:flex;gap:6px;flex-wrap:wrap;flex:none}',
        '.qqf-btn{flex:1;min-width:62px;height:32px;border-radius:9px;cursor:pointer;font-size:12px;font-weight:700;',
        'color:#4a2f0c;background:linear-gradient(180deg,#ffe08a,#f5b942);border:2px solid #d99a2b;',
        'box-shadow:inset 0 -2px 0 rgba(120,70,0,.18),inset 0 1px 0 rgba(255,255,255,.7)}',
        '.qqf-btn:hover:not(:disabled){filter:brightness(1.07)}',
        '.qqf-btn:active:not(:disabled){transform:translateY(1px)}',
        '.qqf-btn:disabled{opacity:.45;cursor:not-allowed;filter:grayscale(.35)}',
        '.qqf-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;padding:8px;border-radius:12px;',
        'background:#e6d0a6;border:2px solid #c9a976;box-shadow:inset 0 2px 6px rgba(90,60,20,.18)}',
        '.qqf-plot{position:relative;aspect-ratio:1.35;border-radius:7px;display:flex;align-items:center;',
        'justify-content:center;font-size:10px;font-weight:700;color:#fff;overflow:hidden;',
        'text-shadow:0 1px 2px rgba(0,0,0,.6);box-shadow:inset 0 0 0 2px rgba(255,255,255,.22)}',
        '.qqf-plot[data-crop=big]{box-shadow:inset 0 0 0 2px #ffd166,0 0 0 2px rgba(255,209,102,.5)}',
        '.qqf-plot-name{font-size:10px;line-height:1.1;padding:1px 2px;border-radius:4px;',
        'background:rgba(0,0,0,.28);white-space:nowrap;max-width:96%;overflow:hidden}',
        '.qqf-plot-need{position:absolute;right:3px;top:2px;font-size:9px;color:#ffe9a8;',
        'background:rgba(120,70,0,.55);border-radius:3px;padding:0 3px}',
        '.qqf-logs{font:11px/1.65 ui-monospace,Menlo,Consolas,monospace;color:#5a4526;background:#fffbee;',
        'border-radius:10px;padding:8px;overflow:auto;white-space:pre-wrap;border:2px solid #e6d2ad;',
        'max-height:100%;min-height:80px}',
        '.qqf-logline{display:flex;gap:6px}',
        '.qqf-logline[data-lv=warn]{color:#b06a00}',
        '.qqf-logline[data-lv=error]{color:#c0392b}',
        '.qqf-logline[data-lv=debug]{color:#9a8a70}',
        '.qqf-stats{display:flex;gap:10px;font-size:11px;color:#7a5c3a;flex-wrap:wrap}',
        '.qqf-err{font-size:11px;color:#c0392b;font-weight:700}',
        '.qqf-task{display:flex;align-items:center;gap:6px;padding:6px 8px;border-radius:10px;',
        'background:#fffbee;border:2px solid #e6d2ad}',
        '.qqf-task[data-on=true]{border-color:#9ed07a;background:#f4fbe9}',
        '.qqf-task-label{flex:1;min-width:0;font-size:12px;font-weight:600;color:#4a3520;',
        'overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
        '.qqf-task-meta{font-size:10px;color:#9a7a52;white-space:nowrap}',
        '.qqf-switch{width:36px;height:20px;border-radius:10px;border:2px solid #c9a976;background:#e8d3ab;',
        'position:relative;cursor:pointer;flex:none;padding:0}',
        '.qqf-switch[data-on=true]{background:#5fae3a;border-color:#4a8a2c}',
        '.qqf-knob{position:absolute;top:1px;left:1px;width:14px;height:14px;border-radius:50%;background:#fff;',
        'box-shadow:0 1px 2px rgba(0,0,0,.35);transition:transform .12s}',
        '.qqf-switch[data-on=true] .qqf-knob{transform:translateX(16px)}',
        '.qqf-input{width:52px;height:24px;border-radius:7px;border:2px solid #e6d2ad;background:#fffbee;',
        'color:#4a3520;font-size:11px;padding:0 4px}',
        '.qqf-mini{height:24px;padding:0 8px;border-radius:7px;cursor:pointer;font-size:11px;font-weight:700;',
        'background:#f6ead0;color:#6b4a24;border:2px solid #e0cda6}',
        '.qqf-mini:hover:not(:disabled){background:#fffdf6}',
        '.qqf-mini:disabled{opacity:.45;cursor:not-allowed}',
        '.qqf-lv{font-size:10px;padding:2px 7px;border-radius:7px;border:2px solid #e0cda6;',
        'background:#fffbee;color:#9a7a52;cursor:pointer;font-weight:700}',
        '.qqf-lv[data-on=true]{background:#ffd166;border-color:#d99a2b;color:#5a3a12}',
      ].join('')
      document.head.appendChild(style)
    }

    // ── 状态容器（单一可变对象 + 不可变快照，供 useSyncExternalStore）───
    var store = {
      snapshot: {
        agent: { status: 'offline', info: null, error: null },
        instances: [],
        states: {},
        frames: {},
        logs: [],
        selected: null,
      },
      listeners: new Set(),
      lastFrames: {},
      frameSubs: new Set(),
      source: null,
      started: false,
    }

    function publish(next) {
      store.snapshot = next
      var snapshot = next
      store.listeners.forEach(function (fn) {
        try {
          fn(snapshot)
        } catch (e) {
          /* 订阅者异常不得影响其他订阅者 */
        }
      })
    }

    function patch(part) {
      publish(Object.assign({}, store.snapshot, part))
    }

    function upsertInstance(summary) {
      var list = store.snapshot.instances.slice()
      var idx = -1
      for (var i = 0; i < list.length; i += 1) if (list[i].id === summary.id) idx = i
      if (idx >= 0) list[idx] = summary
      else list.push(summary)
      var selected = store.snapshot.selected
      if (!selected && list.length > 0) selected = list[0].id
      patch({ instances: list, selected: selected })
    }

    function pushLog(entry) {
      var logs = store.snapshot.logs.concat([entry])
      if (logs.length > 200) logs = logs.slice(logs.length - 200)
      patch({ logs: logs })
    }

    function handleEvent(msg) {
      if (!msg || typeof msg.t !== 'string') return
      if (msg.t === 'agent') {
        patch({ agent: { status: msg.status || 'offline', info: msg.info || null, error: msg.error || null } })
        return
      }
      if (msg.t === 'hello') {
        patch({ agent: Object.assign({}, store.snapshot.agent, { status: 'online' }) })
        return
      }
      if (msg.t === 'state' && msg.instance) {
        if (msg.sync) {
          var states = Object.assign({}, store.snapshot.states)
          states[msg.instance] = msg.sync
          var next = { states: states }
          var frames = store.snapshot.frames
          publish(Object.assign({}, store.snapshot, next, { frames: frames }))
          upsertInstance(msg.sync.summary)
        }
        return
      }
      if (msg.t === 'frame' && msg.instance && msg.data) {
        // 帧不进入 React 快照：直接写 <img>.src，避免每帧重渲染整个面板（日志闪烁的根因）
        var url = 'data:' + (msg.mime || 'image/jpeg') + ';base64,' + msg.data
        store.lastFrames[msg.instance] = url
        store.frameSubs.forEach(function (fn) {
          try { fn(msg.instance, url) } catch (e) { /* 忽略 */ }
        })
        return
      }
      if (msg.t === 'log' && Array.isArray(msg.entries)) {
        msg.entries.forEach(function (e) {
          pushLog({
            instance: msg.instance,
            ts: e.ts,
            level: e.level,
            task: e.task,
            msg: e.msg,
            action: e.action,
            conf: e.conf,
          })
        })
        return
      }
      if (msg.t === 'event' && msg.instance) {
        pushLog({
          instance: msg.instance,
          ts: new Date().toISOString(),
          level: msg.kind === 'error' || msg.kind === 'stuck' ? 'warn' : 'info',
          task: msg.detail && msg.detail.task,
          msg: '系统事件 ' + msg.kind,
        })
        // 事件可能改变状态，主动拉一次
        refreshInstance(msg.instance)
      }
    }

    function refreshInstance(id) {
      fetch(BASE + '/api/instances/' + encodeURIComponent(id) + '/state', { credentials: 'same-origin' })
        .then(function (r) {
          return r.ok ? r.json() : null
        })
        .then(function (body) {
          if (body && body.ok && body.state) {
            var states = Object.assign({}, store.snapshot.states)
            states[id] = body.state
            patch({ states: states })
            upsertInstance(body.state.summary)
          }
        })
        .catch(function () {
          /* 离线时忽略 */
        })
    }

    function connect() {
      if (store.source) return
      // 先拉一次实例列表，让面板在没有 SSE 首帧时也不空白
      fetch(BASE + '/api/instances', { credentials: 'same-origin' })
        .then(function (r) {
          return r.ok ? r.json() : null
        })
        .then(function (body) {
          if (body && body.ok && Array.isArray(body.instances)) {
            var list = body.instances.slice()
            patch({
              instances: list,
              selected: store.snapshot.selected || (list[0] ? list[0].id : null),
            })
          }
        })
        .catch(function () {
          /* 离线 */
        })

      if (typeof EventSource !== 'function') {
        // 非浏览器环境（SSR 快照、Electron 外壳）：只保留一次性拉取，不建立长连接
        return
      }
      var es = new EventSource(BASE + '/stream')
      store.source = es
      es.onmessage = function (ev) {
        try {
          handleEvent(JSON.parse(ev.data))
        } catch (e) {
          /* 脏帧丢弃 */
        }
      }
      es.onopen = function () {
        patch({ agent: Object.assign({}, store.snapshot.agent, { status: 'online' }) })
      }
      es.onerror = function () {
        patch({ agent: Object.assign({}, store.snapshot.agent, { status: 'offline' }) })
        // EventSource 自带重连；此处不主动 close，避免进入"关闭后不再重连"的状态
      }
    }

    function disconnect() {
      if (store.source) {
        store.source.close()
        store.source = null
      }
    }

    function post(path, body) {
      return fetch(BASE + path, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body || {}),
      }).then(function (r) {
        return r
          .json()
          .catch(function () {
            return { ok: false, error: { code: 'E_UNSUPPORTED', message: 'HTTP ' + r.status } }
          })
          .then(function (data) {
            if (!data.ok && data.error) {
              pushLog({
                instance: null,
                ts: new Date().toISOString(),
                level: 'warn',
                msg: '操作失败 ' + data.error.code + ' ' + (ERROR_TEXT[data.error.code] || data.error.message || ''),
              })
            }
            return data
          })
      })
    }

    // ── 组件 ──────────────────────────────────────────────────────
    function fmtCountdown(plot, now) {
      if (typeof plot.countdownSec !== 'number') return ''
      var base = plot.syncAt ? Date.parse(plot.syncAt) : now
      var elapsed = Math.max(0, (now - base) / 1000)
      var remain = Math.max(0, Math.round(plot.countdownSec - elapsed))
      var m = Math.floor(remain / 60)
      var s = remain % 60
      return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0')
    }

    function fmtClock(ts) {
      if (!ts) return '--:--:--'
      var d = new Date(ts)
      if (Number.isNaN(d.getTime())) return '--:--:--'
      return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0') + ':' + String(d.getSeconds()).padStart(2, '0')
    }

    function Dot(props) {
      return h('span', { className: 'qqf-dot', style: { background: props.color || 'var(--dsw-alias-label-tertiary)' } })
    }

    function fmtMoney(n) {
      n = Number(n) || 0
      if (n >= 1e8) return (n / 1e8).toFixed(2).replace(/\.?0+$/, '') + '亿'
      if (n >= 1e4) return (n / 1e4).toFixed(1).replace(/\.0$/, '') + '万'
      return String(Math.round(n))
    }

    function kindLabel(kind) {
      if (!kind) return '—'
      return kind === 'empty' ? '空' : kind === 'grass' ? '荒地' : '作物'
    }

    function PlotGrid(props) {
      var plots = props.plots || []
      var now = props.now
      var cells = plots.map(function (plot) {
        var txt = fmtCountdown(plot, now)
        var kind = plot.kind || null    // 未识别就保持未知，不默认成作物
        var bg
        if (kind === 'empty') bg = PLOT_COLOR.unbuilt
        else if (kind === 'grass') bg = PLOT_COLOR.grass
        else if (!kind) bg = 'repeating-linear-gradient(45deg,#e8d3ab,#e8d3ab 6px,#dcc39a 6px,#dcc39a 12px)'
        else if (plot.cropColor) bg = 'rgb(' + plot.cropColor.join(',') + ')'
        else bg = 'linear-gradient(180deg,#7cbf4a,#4e8f2c)'
        return h(
          'div',
          {
            key: plot.id,
            className: 'qqf-plot',
            'data-crop': plot.bigCrop ? 'big' : 'normal',
            title: plot.id + ' · ' + kindLabel(kind) + (plot.crop ? '（' + plot.crop + '）' : '') +
              (plot.bigCrop ? ' · 四格作物' : '') + (plot.needUpgrade ? ' · 可升级' : '') +
              (plot.cropColor ? ' · rgb(' + plot.cropColor.join(',') + ')' : ''),
            style: { background: bg },
          },
          h('span', { className: 'qqf-plot-name' }, kindLabel(kind)),
          plot.bigCrop ? h('span', { className: 'qqf-plot-need' }, '四格') : null,
        )
      })
      if (cells.length === 0) {
        return h('div', { className: 'qqf-empty' }, '暂无地块数据')
      }
      var groups = {}
      plots.forEach(function (p) { if (p && p.group) groups[p.group] = 1 })
      var bigGroupCount = Object.keys(groups).length
      var emptyCount = plots.filter(function (p) { return p.kind === 'empty' }).length
      var grassCount = plots.filter(function (p) { return p.kind === 'grass' }).length
      var unknownCount = plots.filter(function (p) { return !p.kind }).length
      var bigCount = plots.filter(function (p) { return p.bigCrop }).length
      return h(
        'div',
        { className: 'qqf-sec' },
        h(
          'div',
          { className: 'qqf-stats', style: { marginBottom: '4px' } },
          h('span', null, '空耕地 ' + emptyCount),
          h('span', null, '未开垦 ' + grassCount),
          h('span', null, '作物 ' + (plots.length - emptyCount - grassCount - unknownCount)),
          unknownCount > 0 ? h('span', null, '未识别 ' + unknownCount) : null,
          bigGroupCount > 0 ? h('span', null, '四格作物 ' + bigGroupCount + ' 块') : null,
        ),
        h('div', { className: 'qqf-grid' }, cells),
      )
    }

    function InstanceRow(props) {
      var item = props.item
      var on = props.selected === item.id
      return h(
        'div',
        {
          className: 'qqf-inst',
          'data-on': on ? 'true' : 'false',
          onClick: function () {
            props.onSelect(item.id)
          },
        },
        h(Dot, { color: STATE_COLOR[item.state] || STATE_COLOR.idle }),
        h('span', { className: 'qqf-inst-name' }, item.name || item.id),
        item.platform === 'wechat' ? h('span', { className: 'qqf-chip' }, '微信') : h('span', { className: 'qqf-chip' }, 'QQ'),
        h('span', { className: 'qqf-chip' }, STATE_LABEL[item.state] || item.state),
        item.currentTask ? h('span', { className: 'qqf-chip' }, item.currentTask) : null,
      )
    }

    function TaskRow(props) {
      var t = props.task
      var name = props.name
      var iv = useState(String(t.intervalSec))
      var interval = iv[0]
      var setInterval_ = iv[1]
      var disabled = !t.implemented
      return h(
        'div',
        { className: 'qqf-task', 'data-on': t.enabled ? 'true' : 'false' },
        h(
          'button',
          {
            className: 'qqf-switch',
            'data-on': t.enabled ? 'true' : 'false',
            title: t.enabled ? '已启用' : '已停用',
            onClick: function () {
              props.onToggle(name, !t.enabled)
            },
          },
          h('span', { className: 'qqf-knob' }),
        ),
        h('span', { className: 'qqf-task-label', title: t.label || name }, t.label || name),
        disabled
          ? h('span', { className: 'qqf-task-meta' }, '未实现')
          : h(
              'span',
              { className: 'qqf-task-meta' },
              t.nextRun ? '下次 ' + String(t.nextRun).slice(11, 16) : '待触发',
            ),
        h('input', {
          className: 'qqf-input',
          value: interval,
          title: '间隔（秒）',
          onChange: function (e) {
            setInterval_(e.target.value)
          },
        }),
        h(
          'button',
          {
            className: 'qqf-mini',
            disabled: disabled,
            onClick: function () {
              props.onInterval(name, Number(interval))
            },
          },
          '应用',
        ),
        h(
          'button',
          {
            className: 'qqf-mini',
            disabled: disabled,
            onClick: function () {
              props.onRun(name)
            },
          },
          '执行',
        ),
      )
    }

    function Dock(props) {
      var api = props
      var snap = useSyncExternalStore(api.subscribe, api.getSnapshot, api.getSnapshot)
      var openState = useState(props.initialOpen === true)
      var open = openState[0]
      var setOpen = openState[1]
      var tabState = useState('status')
      var tab = tabState[0]
      var setTab = tabState[1]
      var nowState = useState(Date.now())
      var now = nowState[0]
      var setNow = nowState[1]
      var posState = useState(null)   // 拖动后的位置 {x,y}；null = 默认右下角
      var pos = posState[0]
      var setPos = posState[1]
      var lvState = useState('all')
      var levelFilter = lvState[0]
      var setLevelFilter = lvState[1]

      useEffect(
        function () {
          if (!open) return undefined
          var timer = setInterval(function () {
            setNow(Date.now())
          }, 1000)
          return function () {
            clearInterval(timer)
          }
        },
        [open],
      )

      function startDrag(e) {
        if (e.button !== 0) return
        var panel = e.currentTarget.parentElement
        var rect = panel.getBoundingClientRect()
        var startX = e.clientX
        var startY = e.clientY
        var baseX = rect.left
        var baseY = rect.top
        function onMove(ev) {
          var nx = Math.max(4, Math.min(window.innerWidth - 120, baseX + ev.clientX - startX))
          var ny = Math.max(4, Math.min(window.innerHeight - 40, baseY + ev.clientY - startY))
          setPos({ x: nx, y: ny })
        }
        function onUp() {
          window.removeEventListener('mousemove', onMove)
          window.removeEventListener('mouseup', onUp)
        }
        window.addEventListener('mousemove', onMove)
        window.addEventListener('mouseup', onUp)
      }

      var instances = snap.instances || []
      var running = instances.filter(function (i) {
        return i.state === 'running'
      }).length
      var errors = instances.filter(function (i) {
        return i.state === 'error'
      }).length
      var online = snap.agent.status === 'online'
      var selectedId = snap.selected || (instances[0] ? instances[0].id : null)
      var selected = null
      for (var i = 0; i < instances.length; i += 1) if (instances[i].id === selectedId) selected = instances[i]
      var full = selectedId ? snap.states[selectedId] : null
      var imgRef = useRef(null)
      useEffect(
        function () {
          var off = api.subscribeFrames(function (id, url) {
            if (imgRef.current && id === selectedId) imgRef.current.src = url
          })
          return off
        },
        [selectedId],
      )
      var allLogs = snap.logs || []
      var logs = allLogs
        .filter(function (e) {
          return levelFilter === 'all' || (e.level || 'info') === levelFilter
        })
        .slice(-60)
        .reverse()

      if (!open) {
        return h(
          'div',
          { className: 'qqf-root' },
          h(
            'div',
            {
              className: 'qqf-pill',
              onClick: function () {
                setOpen(true)
              },
              title: online ? '农场助手（在线）' : '农场助手（Agent 离线）',
            },
            h(Dot, {
              color: !online
                ? 'var(--dsw-alias-state-error-primary)'
                : errors > 0
                  ? 'var(--dsw-alias-state-warn-primary)'
                  : 'var(--dsw-alias-state-success-primary)',
            }),
            h('span', null, '🌾 农场 ' + running + '/' + instances.length),
            errors > 0 ? h('span', { className: 'qqf-chip' }, '异常 ' + errors) : null,
          ),
        )
      }

      var controls = [
        { action: 'start', label: '开始' },
        { action: 'pause', label: '暂停' },
        { action: 'stop', label: '停止' },
        { action: 'run_once', label: '立即执行' },
        { action: 'repair', label: '修复' },
      ]

      // ── 左栏：实时画面 + 控制 ──
      var leftColumn = h(
        'div',
        { className: 'qqf-left' },
        h(
          'div',
          { className: 'qqf-sec' },
          h('span', { className: 'qqf-sec-t' }, '实时画面'),
          h('img', {
            ref: imgRef,
            className: 'qqf-frame',
            alt: selected ? selected.name : 'farm',
            src: (selectedId && api.lastFrame(selectedId)) || undefined,
            style: (selectedId && api.lastFrame(selectedId)) ? undefined : { display: 'none' },
          }),
          (selectedId && api.lastFrame(selectedId)) ? null
            : h('div', { className: 'qqf-empty', style: { height: '200px' } }, online ? '等待首帧…' : 'Agent 离线'),
        ),
        h(
          'div',
          { className: 'qqf-row' },
          controls.map(function (c) {
            return h(
              'button',
              {
                key: c.action,
                className: 'qqf-btn',
                disabled: !online || !selectedId,
                onClick: function () {
                  api.control(selectedId, c.action)
                },
              },
              c.label,
            )
          }),
        ),
        h(
          'div',
          { className: 'qqf-sec' },
          h('span', { className: 'qqf-sec-t' }, '实例'),
          instances.length === 0
            ? h('div', { className: 'qqf-empty' }, online ? '未托管实例' : '未连接 Agent')
            : instances.map(function (item) {
                return h(InstanceRow, { key: item.id, item: item, selected: selectedId, onSelect: api.select })
              }),
        ),
        full
          ? h(
              'div',
              { className: 'qqf-stats' },
              h('span', null, '页面 ' + (full.page || '—')),
              h('span', null, 'Lv.' + full.profile.level),
              h('span', null, '金币 ' + fmtMoney(full.profile.gold)),
              h('span', null, '点券 ' + fmtMoney(full.profile.coupon || 0)),
              h('span', null, '金豆豆 ' + (full.profile.dou ? fmtMoney(full.profile.dou) : '—')),
              h('span', null, '钻石 ' + (full.profile.diamond ? fmtMoney(full.profile.diamond) : '—')),
              h('span', null, '动作 ' + full.stats.totalActions),
            )
          : null,
        selected && selected.lastError
          ? h(
              'div',
              { className: 'qqf-err' },
              '错误 ' + selected.lastError.code + '：' + (ERROR_TEXT[selected.lastError.code] || selected.lastError.message || ''),
            )
          : null,
      )

      // ── 右栏：状态 / 任务 / 日志 ──
      var statusBody = h(
        'div',
        { className: 'qqf-body' },
        full
          ? h(
              'div',
              { className: 'qqf-stats' },
              h('span', null, '收获 ' + full.stats.harvest),
              h('span', null, '播种 ' + (full.stats.plant || 0)),
              h('span', null, '空闲轮 ' + (full.idleRounds || 0)),
              h('span', null, '地块更新 ' + (full.plotsUpdatedAt ? String(full.plotsUpdatedAt).slice(11, 19) : '—')),
            )
          : null,
        full ? h(PlotGrid, { plots: full.plots, now: now }) : h('div', { className: 'qqf-empty' }, '暂无状态'),
      )

      var taskList = full && full.tasks ? full.tasks : {}
      var order = (full && full.taskOrder) || Object.keys(taskList)
      var tasksBody = h(
        'div',
        { className: 'qqf-body' },
        h('div', { className: 'qqf-sec-t' }, '巡查模式'),
        h(
          'div',
          { className: 'qqf-row', style: { gap: '4px' } },
          [
            ['notify', '通知模型（推荐）'],
            ['script', '脚本执行'],
          ].map(function (pair) {
            var cur = (taskList.land_scan && taskList.land_scan.mode) || 'notify'
            return h(
              'button',
              {
                key: pair[0],
                className: 'qqf-lv',
                'data-on': cur === pair[0] ? 'true' : 'false',
                onClick: function () {
                  api.setTask(selectedId, 'land_scan', { mode: pair[0] })
                },
              },
              pair[1],
            )
          }),
        ),
        h('div', { className: 'qqf-sec-t' }, '用户空闲闸门（秒）'),
        h(
          'div',
          { className: 'qqf-row', style: { gap: '4px' } },
          [0, 2, 5, 15].map(function (sec) {
            var cur = (full && full.settings && full.settings.guardIdle) || 2
            return h(
              'button',
              {
                key: 'g' + sec,
                className: 'qqf-lv',
                'data-on': Math.abs(cur - sec) < 0.01 ? 'true' : 'false',
                onClick: function () {
                  api.settings(selectedId, { guardIdle: sec })
                },
              },
              sec === 0 ? '关闭' : sec + 's',
            )
          }),
        ),
        h('div', { className: 'qqf-sec-t' }, '变化检测'),
        h(
          'div',
          { className: 'qqf-row', style: { gap: '4px' } },
          [true, false].map(function (on) {
            var cur = full && full.settings ? full.settings.changeGate !== false : true
            return h(
              'button',
              {
                key: 'cg' + on,
                className: 'qqf-lv',
                'data-on': cur === on ? 'true' : 'false',
                onClick: function () {
                  api.settings(selectedId, { changeGate: on })
                },
              },
              on ? '开启（省 token）' : '关闭',
            )
          }),
        ),
        h('div', { className: 'qqf-sec-t' }, '开关 + 间隔秒 + 立即执行'),
        order
          .filter(function (n) {
            return taskList[n]
          })
          .map(function (n) {
            return h(TaskRow, {
              key: n,
              name: n,
              task: taskList[n],
              onToggle: function (name, enabled) {
                api.setTask(selectedId, name, { enabled: enabled })
              },
              onInterval: function (name, sec) {
                api.setTask(selectedId, name, { intervalSec: sec })
              },
              onRun: function (name) {
                api.runTask(selectedId, name)
              },
            })
          }),
      )

      var logsBody = h(
        'div',
        { className: 'qqf-body' },
        h(
          'div',
          { className: 'qqf-row', style: { gap: '4px' } },
          ['all', 'info', 'debug', 'warn', 'error'].map(function (lv) {
            return h(
              'button',
              {
                key: lv,
                className: 'qqf-lv',
                'data-on': levelFilter === lv ? 'true' : 'false',
                onClick: function () {
                  setLevelFilter(lv)
                },
              },
              lv,
            )
          }),
          h(
            'button',
            { className: 'qqf-mini', style: { marginLeft: 'auto' }, onClick: function () { api.clearLogs() } },
            '清空',
          ),
        ),
        logs.length === 0
          ? h('div', { className: 'qqf-empty' }, '暂无日志')
          : h(
              'div',
              { className: 'qqf-logs', style: { maxHeight: 'none' } },
              logs.map(function (entry, idx) {
                return h(
                  'div',
                  { key: 'log' + idx, className: 'qqf-logline', 'data-lv': entry.level || 'info' },
                  h('span', null, fmtClock(entry.ts)),
                  h('span', null, (entry.task ? '[' + entry.task + '] ' : '') + (entry.msg || '')),
                )
              }),
            ),
      )

      var rootStyle = pos ? { left: pos.x + 'px', top: pos.y + 'px', right: 'auto', bottom: 'auto' } : null
      return h(
        'div',
        { className: 'qqf-root', style: rootStyle || undefined },
        h(
          'div',
          { className: 'qqf-panel' },
          h(
            'div',
            { className: 'qqf-head', onMouseDown: startDrag, title: '按住这里可拖动面板' },
            h(Dot, { color: online ? 'var(--dsw-alias-state-success-primary)' : 'var(--dsw-alias-state-error-primary)' }),
            h('span', { className: 'qqf-title' }, '🌾 农场助手'),
            h('span', { className: 'qqf-chip' }, online ? 'agent ' + ((snap.agent.info && snap.agent.info.agentVersion) || 'ok') : '离线'),
            h('button', { className: 'qqf-x', title: '收起', onClick: function () { setOpen(false) } }, '✕'),
          ),
          h(
            'div',
            { className: 'qqf-main' },
            leftColumn,
            h(
              'div',
              { className: 'qqf-right' },
              h(
                'div',
                { className: 'qqf-tabs' },
                [
                  ['status', '状态'],
                  ['tasks', '任务'],
                  ['logs', '日志'],
                ].map(function (pair) {
                  return h(
                    'button',
                    {
                      key: pair[0],
                      className: 'qqf-tab',
                      'data-on': tab === pair[0] ? 'true' : 'false',
                      onClick: function () {
                        setTab(pair[0])
                      },
                    },
                    pair[1] + (pair[0] === 'logs' && allLogs.length ? ' ' + allLogs.length : ''),
                  )
                }),
              ),
              tab === 'status' ? statusBody : tab === 'tasks' ? tasksBody : logsBody,
            ),
          ),
        ),
      )
    }

    // ── 对外 api（同时作为 slot inject face）──────────────────────────
    var api = {
      subscribe: function (fn) {
        store.listeners.add(fn)
        return function () {
          store.listeners.delete(fn)
        }
      },
      getSnapshot: function () {
        return store.snapshot
      },
      subscribeFrames: function (fn) {
        store.frameSubs.add(fn)
        return function () { store.frameSubs.delete(fn) }
      },
      lastFrame: function (id) {
        return store.lastFrames[id] || null
      },
      select: function (id) {
        patch({ selected: id })
        refreshInstance(id)
      },
      control: function (id, action) {
        if (!id) return
        post('/api/instances/' + encodeURIComponent(id) + '/control', { action: action })
      },
      calibrate: function (id) {
        if (!id) return
        pushLog({ instance: id, ts: new Date().toISOString(), level: 'info',
                  msg: '已进入标定：请在游戏里依次点 4 个角的地块（左上→右上→右下→左下）' })
        post('/api/instances/' + encodeURIComponent(id) + '/calibrate', { action: 'start' }).then(function (r) {
          if (r && r.ok) {
            pushLog({ instance: id, ts: new Date().toISOString(), level: 'info', msg: '标定录制中…点完 4 个角后自动保存' })
          }
        })
      },
      setTask: function (id, name, patch) {
        if (!id) return
        post('/api/instances/' + encodeURIComponent(id) + '/tasks', Object.assign({ name: name }, patch)).then(function () {
          refreshInstance(id)
        })
      },
      clearLogs: function () {
        patch({ logs: [] })
      },
      runTask: function (id, task) {
        if (!id) return
        post('/api/instances/' + encodeURIComponent(id) + '/task', { task: task })
      },
      start: function () {
        if (store.started) return
        store.started = true
        connect()
      },
      stop: function () {
        store.started = false
        disconnect()
      },
    }

    function apply(ctx) {
      var slots = ctx.get('slots')
      api.start()
      // 卸载时断开 SSE（cordis: effect fn 立即执行，返回值是清理器）
      ctx.effect(function () {
        return function () {
          api.stop()
        }
      })
      // declaration-injection：等 shell.overlay 被 layout 声明后再注册
      ctx.effect(function () {
        return slots.inject('shell.overlay', function () {
          return slots.register(
            {
              name: 'shell.overlay',
              id: 'qq-farm',
              order: 40,
              label: function () {
                return '农场'
              },
              // inject 必须是工厂函数：DSH 会调用 entry.inject() 取注入面，
              // 传对象会抛 "TypeError: inject is not a function"（已实测）
              inject: function () {
                return api
              },
            },
            Dock,
          )
        })
      })
    }

    exports.inject = ['slots']
    exports.apply = apply
    return module.exports
  },
})
