#!/usr/bin/env python3
"""Farm Agent · macOS 后端（FAP v1 实现）。

与 agent/mock 实现同一份协议，面板与插件零改动即可切换。

设计要点（对应 docs/技术方案.md 的稳定性约束）：
  1. **显式状态机**：每次循环先判定"当前是什么页面"，再决定动作；判不出来就只报错不动手。
  2. **动作前二次确认**：点击前要求文本精确匹配 + 置信度阈值 + 目标点落在窗口内。
  3. **动作后验证**：点击后重新抓帧对比，没变化就记一次失败（不无限点）。
  4. **只点白名单文字**：任何不在白名单里的目标一律不点（防止误触"出售"这类破坏性操作）。
  5. **可 dry-run**：--dry-run 只识别不点击，用于回归测试。

依赖：macd 二进制（同目录 bin/macd，由 build.sh 编译）+ Pillow。
"""

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

HERE = Path(__file__).resolve().parent
MACD = HERE / "bin" / "macd"
TEMPLATES = HERE / "templates"
ICON_CLOSE = TEMPLATES / "close_x.png"

# 地块网格在截图中的相对位置（比例，适配窗口缩放）。
# 4 列 × 6 行纵向排布；bbox 由「格内颜色最一致」自动校准得到（真机 824x1542 截图）：
#   x[270..700] y[740..1195]，每格约 108x76 px
PLOT_GRID = {"x0": 0.3277, "y0": 0.4799, "w": 0.5218, "h": 0.2951, "cols": 4, "rows": 6}

# 地块三态判定（用 G-R 差值区分，比单一色相阈值稳）：
#   作物     : G - R > 15   （作物是黄绿/绿色，绿通道明显更高）
#   空耕地   : R - G > 15   （翻耕后的土壤是棕色，红通道高）
#   未开垦地 : 其余（草地背景，R≈G，整体偏亮）
def _cell_kind(rgb):
    r, g, b = rgb
    if g - r > 15:
        return "crop"
    if r - g > 15:
        return "empty"
    return "grass"


def _group_big_crops(cells, cols, rows, color_tol=20):
    """标记占 2x2 的四格作物：四个相邻非空地颜色接近就视为一块四格作物。"""
    grid = {(c["row"], c["col"]): c for c in cells}
    marked = set()
    groups = []
    for r in range(rows - 1):
        for c in range(cols - 1):
            quad = [(r, c), (r + 1, c), (r, c + 1), (r + 1, c + 1)]
            cs = [grid.get(q) for q in quad]
            if any(x is None for x in cs) or any(q in marked for q in quad):
                continue
            if any(x["kind"] == "empty" for x in cs):
                continue
            mean = [sum(x["rgb"][i] for x in cs) / 4.0 for i in range(3)]
            dev = max(abs(x["rgb"][i] - mean[i]) for x in cs for i in range(3))
            if dev <= color_tol:
                for q in quad:
                    marked.add(q)
                groups.append({"cells": quad, "rgb": [int(v) for v in mean]})
    for q in marked:
        grid[q]["bigCrop"] = True
    return groups
AGENT_VERSION = "0.1.0-mac"
PROTOCOL = 1

# ── 协议常量（与 shared/fap.cjs 保持一致）────────────────────────────
TASK_NAMES = ["main", "friend", "grass", "share", "reward", "gift", "event",
              "sell", "land_scan", "timed_harvest", "restart"]

# 任务顺序（越靠前越先执行），与参考项目 executor.task_order 一致
TASK_ORDER = ["land_scan", "timed_harvest", "main", "friend", "grass",
              "sell", "reward", "gift", "event", "share", "restart"]

# 默认任务配置。当前 macOS 后端真正实现的只有 main（收获/播种/关弹窗），
# 其余在这里登记为"可配置但未实现"，面板上会标注，避免误以为在跑。
DEFAULT_TASKS = {
    "main":          {"enabled": True,  "intervalSec": 120, "label": "主流程（收获/播种/关弹窗）"},
    "friend":        {"enabled": False, "intervalSec": 1800, "label": "好友（偷菜/帮忙）"},
    "grass":         {"enabled": False, "intervalSec": 21600, "label": "种草"},
    "sell":          {"enabled": False, "intervalSec": 3600, "label": "仓库出售"},
    "reward":        {"enabled": False, "intervalSec": 21600, "label": "任务奖励"},
    "gift":          {"enabled": False, "intervalSec": 86400, "label": "礼包/邮件/月卡"},
    "event":         {"enabled": False, "intervalSec": 86400, "label": "活动"},
    "share":         {"enabled": False, "intervalSec": 86400, "label": "分享奖励"},
    "land_scan":     {"enabled": True,  "intervalSec": 1800, "label": "地块巡查（点格读作物/倒计时）"},
    "timed_harvest": {"enabled": False, "intervalSec": 86400, "label": "定时收获"},
    "restart":       {"enabled": False, "intervalSec": 14400, "label": "定时重启窗口"},
}
# 已经真正实现的任务（其余会返回 E_UNSUPPORTED）
IMPLEMENTED_TASKS = {"main", "land_scan"}
CONTROL_ACTIONS = ["start", "pause", "resume", "stop", "run_once", "repair", "restart"]

# ── 页面与可点击白名单 ───────────────────────────────────────────────
# 只允许点这些文字，其余一律不点（破坏性按钮如"出售/批量出售"永不进入白名单）
CLICKABLE = {
    "一键务农": "harvest_all",
    "一键收获": "harvest_all",
    "直接领取": "claim",
    "确定": "confirm",
    "我知道了": "confirm",
    "关闭": "close",
}


# 弹窗字典：命中关键词就按对应动作处理。顺序即优先级。
# 只允许「领取/直接领取/确定」这类非破坏性按钮，分享/购买一律不点。
POPUP_RULES = [
    {"keys": ["直接领取"],            "label": "直接领取", "kind": "click"},
    {"keys": ["点击空白处", "点击任意处"], "label": None,   "kind": "blank"},
    {"keys": ["我知道了", "知道了"],    "label": "我知道了", "kind": "click"},
    {"keys": ["确定", "确认"],         "label": "确定",   "kind": "click"},
    {"keys": ["领取奖励", "恭喜获得"],  "label": None,   "kind": "blank"},
]

# 页面判定用的关键文本
PAGE_SIGNATURES = [
    ("popup_notice", ["游戏更新公告", "更新公告", "点击空白处"]),
    ("popup_generic", ["领取奖励", "恭喜获得", "点击任意处", "我知道了",
                       "直接领取", "分享后可以获得", "化肥礼包"]),
    ("warehouse", ["批量出售", "批量锁定", "格子上限"]),
    ("shop", ["确认购买", "购买", "售价"]),
    ("friend", ["好友农场", "偷菜", "帮忙"]),
    ("main", ["仓库", "商店", "图鉴", "一键务农", "一键收获", "好友求助", "等级提升", "可扩建"]),
]

# 主界面上「可点且安全」的动作按钮（按优先级）
HARVEST_LABELS = ["一键收获"]
PLANT_LABELS = ["一键务农"]


def _image_size(path):
    """读 PNG 尺寸（8 字节头里的 IHDR），不依赖 Pillow。"""
    try:
        with open(path, "rb") as f:
            head = f.read(33)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return 0, 0
        w = int.from_bytes(head[16:20], "big")
        h = int.from_bytes(head[20:24], "big")
        return w, h
    except OSError:
        return 0, 0


CALIB_DIR = Path.home() / ".dsh-farm"


def _calib_path(pid):
    return CALIB_DIR / f"calib-{pid}.json"


def load_calibration(pid):
    p = _calib_path(pid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_calibration(pid, data):
    try:
        CALIB_DIR.mkdir(parents=True, exist_ok=True)
        _calib_path(pid).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        print(json.dumps({"level": "warn", "msg": f"标定保存失败: {e}"}), flush=True)


def lattice_from_corners(corners, cols=4, rows=6):
    """四角 → 网格中心列表（双线性插值）。

    corners 顺序：左上、右上、右下、左下（屏幕坐标）。
    返回 [{col,row,x,y}]，x/y 为屏幕点。
    """
    tl, tr, br, bl = corners
    out = []
    for r in range(rows):
        v = (r + 0.5) / rows
        left = (tl[0] + (bl[0] - tl[0]) * v, tl[1] + (bl[1] - tl[1]) * v)
        right = (tr[0] + (br[0] - tr[0]) * v, tr[1] + (br[1] - tr[1]) * v)
        for c in range(cols):
            u = (c + 0.5) / cols
            x = left[0] + (right[0] - left[0]) * u
            y = left[1] + (right[1] - left[1]) * u
            out.append({"col": c, "row": r, "x": x, "y": y})
    return out


HAND_TPL = Path(__file__).resolve().parent / "templates" / "hand.png"
STATE_DIR = Path.home() / ".dsh-farm"


def _task_state_path(instance_id):
    return STATE_DIR / f"tasks-{instance_id}.json"


def load_task_state(instance_id):
    """读该实例的任务配置（首次运行用 DEFAULT_TASKS，之后持久化到 ~/.dsh-farm）。"""
    state = {name: {"enabled": cfg["enabled"], "intervalSec": cfg["intervalSec"],
                    "nextRun": None, "lastRun": None, "lastResult": None}
             for name, cfg in DEFAULT_TASKS.items()}
    path = _task_state_path(instance_id)
    if path.exists():
        try:
            saved = json.loads(path.read_text("utf-8"))
            for name, cfg in saved.items():
                if name in state and isinstance(cfg, dict):
                    for k in ("enabled", "intervalSec", "nextRun", "lastRun", "lastResult"):
                        if k in cfg:
                            state[name][k] = cfg[k]
        except (OSError, json.JSONDecodeError) as e:
            print(json.dumps({"level": "warn", "msg": f"任务配置读取失败({e})，用默认值"}))
    return state


def save_task_state(instance_id, tasks):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _task_state_path(instance_id).write_text(
            json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        print(json.dumps({"level": "warn", "msg": f"任务配置保存失败({e})"}))


def log(level, msg, **extra):
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "level": level, "msg": msg}
    line.update(extra)
    print(json.dumps(line, ensure_ascii=False), flush=True)


# ── 原语：macd 调用 ──────────────────────────────────────────────────
class Mac:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self._capture_lock = threading.Lock()

    def _run(self, *args, timeout=30):
        try:
            out = subprocess.run([str(MACD), *[str(a) for a in args]],
                                 capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, "timeout"
        if out.returncode != 0:
            return None, (out.stderr or out.stdout).strip()[:400]
        return out.stdout, None

    def windows(self):
        out, err = self._run("winlist")
        if out is None:
            return []
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def find_game_window(self, title_contains="农场"):
        """找游戏窗口。

        主路径：标题含「农场」且 owner 是 QQ/微信容器。
        兜底：没有屏幕录制权限时 macOS 会把窗口标题隐去（title 为空），
        此时按 owner（QQ小程序 / 微信 / MiniProgram）加尺寸筛选。
        """
        wins = self.windows()
        for w in wins:
            owner = w.get("owner", "")
            title = w.get("title", "")
            if title_contains in title and ("QQ" in owner or "微信" in owner or "WeChat" in owner):
                platform = "wechat" if ("微信" in owner or "WeChat" in owner) else "qq"
                return {**w, "platform": platform, "matched_by": "title"}
        # 兜底：按 owner + 尺寸（小程序容器窗口通常是竖屏比例）
        cands = []
        for w in wins:
            owner = w.get("owner", "")
            if not ("小程序" in owner or "MiniProgram" in owner or "微信" in owner or "WeChat" in owner):
                continue
            if w.get("w", 0) >= 300 and w.get("h", 0) >= 500:
                cands.append(w)
        if cands:
            w = cands[0]
            platform = "wechat" if ("微信" in w.get("owner", "") or "WeChat" in w.get("owner", "")) else "qq"
            return {**w, "platform": platform, "matched_by": "owner"}
        return None

    def capture(self, wid, path):
        with self._capture_lock:
            out, err = self._run("capture", wid, path)
        if out is None:
            return None, err
        try:
            meta = json.loads(out.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return None, "capture 输出无法解析"
        return meta, None

    def ocr(self, path, roi=None):
        args = ["ocr", path]
        if roi:
            args += ["--roi", ",".join(str(int(v)) for v in roi)]
        out, err = self._run(*args, timeout=60)
        if out is None:
            return [], err
        blocks = []
        for line in out.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") == "text":
                blocks.append(row)
        return blocks, None

    def click(self, pid, sx, sy, mode="ghost", wid=None, repeat=2, gap=0.25):
        """点击。repeat>1 用于绕过 macOS 的 acceptsFirstMouse=false：
        第一次点击只激活窗口，第二次才真正落到控件上。"""
        if self.dry_run:
            return {"ok": True, "mode": "dry-run", "from": [sx, sy], "to": [sx, sy], "repeat": repeat}
        result = {"ok": False, "error": "未执行"}
        for i in range(max(1, repeat)):
            if mode == "bg" and wid:
                # SkyLight 真后台（Chromium 系有效；QQ/微信小程序容器实测无效）
                args = ["bgclick", pid, wid, int(round(sx)), int(round(sy))]
            elif mode == "ghost":
                # 幽灵点击：瞬移点击后把光标还原（小程序容器实测有效）
                args = ["click", pid, int(round(sx)), int(round(sy)), "--system", "--ghost"]
            else:
                args = ["click", pid, int(round(sx)), int(round(sy)), "--system", "--warp"]
            out, err = self._run(*args)
            if out is None:
                return {"ok": False, "error": err}
            try:
                result = json.loads(out.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                result = {"ok": False, "error": "click 输出无法解析"}
            if i + 1 < repeat:
                time.sleep(gap)
        result["repeat"] = repeat
        return result

    def find_icon(self, image_path, template_path, threshold=0.8):
        """模板匹配（macd 内置的多尺度 NCC）。返回 {cx, cy, score, w, h} 或 None。

        为什么不用 cv2：受管 Agent 跑在 /usr/bin/python3 上，没有 numpy/opencv；
        匹配能力做进 macd(Swift)，Agent 侧保持零第三方依赖。
        """
        if not Path(template_path).exists():
            return None
        out, _ = self._run("find", image_path, template_path, "--threshold", str(threshold), timeout=30)
        if not out:
            return None
        try:
            data = json.loads(out.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return None
        if not data.get("found"):
            return None
        m = data["matches"][0]
        return {"cx": m["cx"], "cy": m["cy"], "score": m["score"], "w": m["w"], "h": m["h"]}

    def read_grid(self, image_path, grid):
        """按网格采样颜色，判断每格是否为空地（纯图像分析，不点击）。"""
        img_w, img_h = _image_size(image_path)
        if img_w <= 0:
            return None
        out, _ = self._run("grid", image_path,
                           "--x0", int(img_w * grid["x0"]), "--y0", int(img_h * grid["y0"]),
                           "--w", int(img_w * grid["w"]), "--h", int(img_h * grid["h"]),
                           "--cols", grid["cols"], "--rows", grid["rows"], timeout=20)
        if not out:
            return None
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return None

    def idle_seconds(self):
        out, _ = self._run("idle")
        try:
            return float(json.loads(out.strip().splitlines()[-1])["idleSeconds"])
        except (json.JSONDecodeError, IndexError, KeyError, TypeError, AttributeError):
            return 999.0

    def frontmost(self):
        out, _ = self._run("frontmost")
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError, AttributeError):
            return None

    def activate(self, pid):
        """把 App 置前。幽灵点击前必须做：否则 macOS 的第一次点击只激活窗口，不点控件。"""
        out, _ = self._run("activate", pid)
        return out is not None

    def hittest(self, sx, sy):
        out, _ = self._run("hittest", sx, sy)
        if not out:
            return None
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return None


# ── 单实例状态与工作线程 ─────────────────────────────────────────────
class Instance:
    def __init__(self, win):
        self.wid = win["wid"]
        self.pid = win["pid"]
        self.platform = win["platform"]
        self.name = f"{'QQ' if self.platform == 'qq' else '微信'}·{win.get('title', '农场')}"
        self.id = f"{self.platform}-{self.wid}"
        self.state = "idle"
        self.current_task = None
        self.next_run = None
        self.last_error = None
        self.rev = 1
        self.page = "unknown"
        self.profile = {"level": 0, "gold": 0, "dou": 0, "coupon": 0, "exp": 0, "syncedAt": None}
        # 4 列 × 6 行，行优先顺序（面板按 4 列布局正好对齐）
        self.plots = [{"id": f"{c + 1}-{r + 1}", "level": "normal", "countdownSec": None,
                       "syncAt": None, "needPlanting": False, "needUpgrade": False,
                       "bigCrop": False, "cropColor": None}
                      for r in range(6) for c in range(4)]
        self.stats = {"harvest": 0, "plant": 0, "steal": 0, "help": 0, "sell": 0, "totalActions": 0}
        self.last_frame = None          # 缩略图字节（JPEG/PNG）
        self.frame_mime = "image/jpeg"  # 实际编码
        self.last_frame_at = 0.0
        self.ocr_blocks = []
        self.img_size = (0, 0)
        self.frame_hash = None
        self.frame_changed = False
        self.window_rect = (win["x"], win["y"], win["w"], win["h"])
        self.tmp_png = HERE / f".cache-{self.wid}.png"
        self.tmp_jpg = HERE / f".cache-{self.wid}.jpg"
        self.lock = threading.Lock()
        self.worker = None
        self.stop_flag = threading.Event()
        self.tasks = load_task_state(self.id)

    def summary(self):
        return {
            "id": self.id, "name": self.name, "platform": self.platform,
            "state": self.state, "currentTask": self.current_task,
            "nextRun": self.next_run, "lastError": self.last_error, "fps": 2,
        }

    def full_state(self):
        return {
            "rev": self.rev,
            "summary": self.summary(),
            "profile": self.profile,
            "plots": self.plots,
            "tasks": {name: {"enabled": cfg["enabled"], "intervalSec": cfg["intervalSec"],
                             "nextRun": cfg["nextRun"], "lastRun": cfg["lastRun"],
                             "lastResult": cfg["lastResult"],
                             "label": DEFAULT_TASKS[name]["label"],
                             "implemented": name in IMPLEMENTED_TASKS}
                      for name, cfg in self.tasks.items()},
            "taskOrder": TASK_ORDER,
            "plotsUpdatedAt": getattr(self, "plots_updated_at", None),
            "frameHash": getattr(self, "frame_hash", None),
            "frameChanged": getattr(self, "frame_changed", False),
            "idleRounds": getattr(self, "idle_rounds", 0),
            "stats": self.stats,
            "page": self.page,
        }


class Agent:
    def __init__(self, mac, fps=2.0, interval=2.5, allow_plant=True,
                 guard_idle=2.0, allow_focus_steal=False):
        self.allow_plant = allow_plant
        self.idle_rounds = 0
        # 用户优先：只有用户空闲 ≥ guard_idle 秒、且游戏已在前台时才操作，
        # 避免抢占鼠标点击 / 抢焦点。
        self.guard_idle = guard_idle
        self.allow_focus_steal = allow_focus_steal
        self.mac = mac
        self.fps = fps
        self.interval = interval
        self.instances = {}
        self.clients = set()
        self.lock = threading.Lock()
        self.log_ring = []
        self.started_at = time.time()
        self.frame_seq = 0

    # -- 事件广播 --
    def broadcast(self, payload):
        data = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
        with self.lock:
            dead = []
            for q in self.clients:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self.clients.discard(q)

    def push_log(self, inst, msg, level="info", **extra):
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "level": level,
                 "task": inst.current_task if inst else None, "msg": msg}
        entry.update({k: v for k, v in extra.items() if v is not None})
        print(json.dumps({"instance": inst.id if inst else None, **entry}, ensure_ascii=False), flush=True)
        self.log_ring.append({"instance": inst.id if inst else None, **entry})
        if len(self.log_ring) > 5000:
            self.log_ring = self.log_ring[-4000:]
        self.broadcast({"t": "log", "instance": inst.id if inst else None, "entries": [entry]})

    def push_state(self, inst):
        self.broadcast({"t": "state", "instance": inst.id, "rev": inst.rev, "sync": inst.full_state()})

    def fail(self, inst, code, msg):
        inst.last_error = {"code": code, "message": msg, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.push_log(inst, f"{code} {msg}", level="warn")
        self.push_state(inst)

    def user_busy_reason(self, inst):
        """返回 None 表示可以操作；否则返回占用的原因。

        判据只有一条：**用户是否空闲**。
        游戏窗口是否在前台不作为拦截条件 —— 用户空闲时把游戏调到前台是无害的，
        而"必须前台"会导致用户在别的窗口工作时机器人永远不干活。
        """
        if self.allow_focus_steal:
            return None
        idle = self.mac.idle_seconds()
        if idle < self.guard_idle:
            return f"用户正在使用电脑（空闲 {idle:.1f}s < {self.guard_idle:.0f}s）"
        return None

    # -- 地块标定 --
    def start_calibration(self, inst, count=4, timeout=90.0):
        """进入标定模式：记录用户在游戏窗口里连续点击的 4 个角点。

        做法：50ms 轮询光标位置，检测"移动后静止"当作一次点击，去抖后入队。
        用户只需在游戏里依次点 4 个角地块即可。
        """
        if getattr(self, "_calib_thread", None) and self._calib_thread.is_alive():
            return False
        self._calib_points = []
        self._calib_deadline = time.time() + timeout

        def worker():
            last = None
            still_since = None
            taken = 0
            while time.time() < self._calib_deadline and taken < count:
                out, _ = self.mac._run("cursorpos")
                try:
                    cur = json.loads(out.strip().splitlines()[-1])
                except (json.JSONDecodeError, IndexError, AttributeError):
                    time.sleep(0.05)
                    continue
                pos = (cur["x"], cur["y"])
                if last is None or pos != last:
                    still_since = time.time()
                    last = pos
                else:
                    if still_since and time.time() - still_since > 0.35:
                        x0, y0, w, h = inst.window_rect
                        if x0 <= pos[0] <= x0 + w and y0 <= pos[1] <= y0 + h:
                            if not self._calib_points or (
                                    abs(pos[0] - self._calib_points[-1][0]) + abs(pos[1] - self._calib_points[-1][1]) > 30):
                                self._calib_points.append(pos)
                                taken += 1
                                self.push_log(inst, f"标定点 {taken}/{count}: ({pos[0]},{pos[1]})")
                        still_since = None
                time.sleep(0.05)
            if len(self._calib_points) == count:
                pts = list(self._calib_points)
                cells = lattice_from_corners(pts, cols=4, rows=6)
                save_calibration(inst.pid, {"corners": pts,
                                            "cells": cells,
                                            "cols": 4, "rows": 6,
                                            "window": list(inst.window_rect),
                                            "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                self.push_log(inst, f"标定完成：4 角 {pts}，已生成 24 格中心")
                inst.plots_updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            else:
                self.push_log(inst, f"标定未完成（只记录到 {len(self._calib_points)} 个点）", level="warn")

        self._calib_thread = threading.Thread(target=worker, daemon=True)
        self._calib_thread.start()
        return True

    # -- 帧泵 --
    def frame_pump(self, fps=1.0):
        """面板打开时持续推帧。不做 OCR，纯截图 + 缩略图，成本很低。"""
        interval = 1.0 / max(fps, 0.2)
        while True:
            try:
                if self.clients:
                    inst = self.refresh_windows()
                    if inst is not None:
                        self.grab(inst, want_ocr=False)
                        self.frame_seq += 0
            except Exception as e:  # noqa: BLE001
                print(json.dumps({"level": "warn", "msg": f"帧泵异常: {e}"}), flush=True)
            time.sleep(interval)

    # -- 窗口发现 --
    def refresh_windows(self):
        """重新发现窗口；窗口重建后 wid/pid 会变，必须每次核对。"""
        win = self.mac.find_game_window()
        if win is None:
            return None
        iid = f"{win['platform']}-{win['wid']}"
        inst = self.instances.get(iid)
        if inst is None:
            inst = Instance(win)
            self.instances[iid] = inst
            how = win.get("matched_by", "title")
            note = "（标题被系统隐去，按 owner 匹配；授权屏幕录制后会更稳）" if how == "owner" else ""
            self.push_log(inst, f"发现游戏窗口 wid={win['wid']} pid={win['pid']} {win['w']}x{win['h']}{note}")
        else:
            inst.pid = win["pid"]
            inst.window_rect = (win["x"], win["y"], win["w"], win["h"])
        return inst

    # -- 抓帧 --
    def grab(self, inst, want_ocr=True):
        """抓一帧：PNG 全图用于 OCR，缩小 JPEG 用于推给面板。

        刻意不依赖 Pillow —— 桌面版 DSH 的 PATH 只有 /usr/bin，
        那里的 python3 没装任何第三方包。转码用 macOS 自带的 sips。
        """
        meta, err = self.mac.capture(inst.wid, str(inst.tmp_png))
        if meta is None:
            self.fail(inst, "E_WINDOW_LOST", f"截图失败：{err}")
            return False
        inst.img_size = (meta["px_w"], meta["px_h"])

        # 缩略图：sips 转 JPEG（失败就退回 PNG 原图，宁可大也别断流）
        jpg_ok = subprocess.run(
            ["/usr/bin/sips", "-s", "format", "jpeg", "-s", "formatOptions", "70",
             "-Z", "480", str(inst.tmp_png), "--out", str(inst.tmp_jpg)],
            capture_output=True,
        ).returncode == 0
        src = inst.tmp_jpg if jpg_ok else inst.tmp_png
        try:
            inst.last_frame = src.read_bytes()
        except OSError as e:
            self.fail(inst, "E_CAPTURE_BLACK", f"读图失败：{e}")
            return False
        inst.last_frame_at = time.time()
        inst.frame_mime = "image/jpeg" if jpg_ok else "image/png"
        # 画面指纹：用于"画面没变就不叫醒模型"（零 token 的变化检测）
        out, _ = self.mac._run("hash", str(inst.tmp_png))
        try:
            h = json.loads(out.strip().splitlines()[-1]).get("hash")
        except (json.JSONDecodeError, IndexError, AttributeError):
            h = None
        if h:
            inst.frame_changed = (inst.frame_hash is not None and h != inst.frame_hash)
            inst.frame_hash = h
        self.frame_seq += 1
        import base64
        self.broadcast({"t": "frame", "instance": inst.id, "seq": self.frame_seq,
                        "mime": inst.frame_mime,
                        "data": base64.b64encode(inst.last_frame).decode()})
        if want_ocr:
            blocks, err = self.mac.ocr(str(inst.tmp_png))
            if err:
                self.fail(inst, "E_CAPTURE_BLACK", f"OCR 失败：{err}")
                return False
            inst.ocr_blocks = blocks
            self.parse_profile(inst, blocks)
            inst.page = self.classify(inst)
            # 注意：这里刻意不再自动更新地块。
            # 地块状态只在 land_scan（30 分钟一次）或用户手动触发时刷新，
            # 否则每轮 OCR 的细微差异会让面板每 2 秒闪烁一次。
        return True

    # -- 从 OCR 结果里读数值 --
    def parse_profile(self, inst, blocks):
        import re
        texts = blocks
        # 等级：标题下方第一个纯数字块（2-3 位），且在窗口上半部
        for b in texts:
            if 180 < b["y"] < 260 and re.fullmatch(r"\d{2,3}", b["text"].strip()):
                try:
                    inst.profile["level"] = int(b["text"].strip())
                except ValueError:
                    pass
                break
        # 金币：右上角"万/亿"结尾的数字
        # 头部：金币是「xx万/亿」，正下方那个纯数字是「点券」
        right = [b for b in texts if b["x"] > inst.img_size[0] * 0.6 and b["y"] < 300]
        for b in right:
            t = b["text"].strip()
            if ("万" in t or "亿" in t) and not inst.profile.get("gold"):
                inst.profile["gold"] = self.parse_cn_number(t)
            elif t.replace(" ", "").isdigit() and 200 < b["y"] < 300:
                inst.profile["coupon"] = int(t.replace(" ", ""))
        # 「货币总览」弹窗：标签 + 右侧数值 配对（金豆豆/点券/钻石）
        labeled = {"金豆豆": "dou", "点券": "coupon", "钻石": "diamond", "金币": "gold"}
        for b in texts:
            label = b["text"].strip()
            if label not in labeled:
                continue
            key = labeled[label]
            want_y = b["y"] + b["h"] / 2
            best = None
            for other in texts:
                if other is b:
                    continue
                oy = other["y"] + other["h"] / 2
                if abs(oy - want_y) > 24 or other["x"] <= b["x"]:
                    continue
                digits = other["text"].replace(",", "").replace(" ", "").strip()
                if digits.isdigit():
                    if best is None or other["x"] < best["x"]:
                        best = other
            if best is not None:
                inst.profile[key] = int(best["text"].replace(",", "").replace(" ", "").strip())
        inst.profile["syncedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    @staticmethod
    def parse_cn_number(text):
        import re
        m = re.search(r"([\d.]+)\s*(万|亿)?", text.replace(" ", ""))
        if not m:
            return 0
        try:
            v = float(m.group(1))
        except ValueError:
            return 0
        unit = m.group(2)
        if unit == "万":
            v *= 1e4
        elif unit == "亿":
            v *= 1e8
        return int(v)

    # -- 地块状态（颜色分析）--
    def update_plots(self, inst):
        """地块状态读取。

        优先级：
          1. 已做四角标定 → 按标定出的 24 个格子中心采样（准确，推荐）
          2. 未标定 → 用默认 4x6 矩形网格近似
        分类用 G-R / R-G 差值三态：作物 / 空耕地 / 未开垦。
        """
        cells = self._cv_cells(inst) or self._sample_cells(inst)
        if not cells:
            return
        _group_big_crops(cells, PLOT_GRID["cols"], PLOT_GRID["rows"])
        by_pos = {(c["row"], c["col"]): c for c in cells}
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        for r in range(PLOT_GRID["rows"]):
            for col in range(PLOT_GRID["cols"]):
                idx = r * PLOT_GRID["cols"] + col
                if idx >= len(inst.plots):
                    break
                cell = by_pos.get((r, col))
                if cell is None:
                    continue
                plot = inst.plots[idx]
                kind = cell["kind"]
                plot["kind"] = kind
                plot["needPlanting"] = kind == "empty"
                plot["level"] = "unbuilt" if kind in ("empty", "grass") else "normal"
                plot["bigCrop"] = bool(cell["bigCrop"])
                plot["cropColor"] = cell["rgb"]
                plot["crop"] = plot.get("crop") if kind == "crop" else None
                plot["syncAt"] = now
                if kind != "crop":
                    plot["countdownSec"] = None
        inst.plots_updated_at = now

    # -- 逐格点击读弹窗（参考项目 land_scan 的做法）--
    _RE_NAME_CD = re.compile(r"([^\s:：]{1,8})\s*[：:]\s*(\d{1,2}:\d{2}:\d{2})")
    _RE_CD = re.compile(r"(\d{1,2}:\d{2}:\d{2})\s*后成熟")
    _RE_CD_ONLY = re.compile(r"(\d{1,2}:\d{2}:\d{2})")

    def _parse_plot_popup(self, blocks):
        """从地块弹窗文本里解析：作物名 / 成熟倒计时 / 是否空地。"""
        texts = [b["text"] for b in blocks]
        joined = " ".join(texts)
        crop, cd = None, None
        for t in texts:
            m = self._RE_NAME_CD.search(t)
            if m:
                crop, cd = m.group(1), m.group(2)
                break
        if cd is None:
            m = self._RE_CD.search(joined)
            if m:
                cd = m.group(1)
        empty = any(k in joined for k in ("播种", "种植", "空地", "可开垦"))
        unopened = any(k in joined for k in ("开垦", "扩建"))
        has_farm_info = any(k in joined for k in ("一键施肥", "后成熟", "小时"))
        return {"crop": crop, "countdown": cd,
                "empty": empty or (not has_farm_info and cd is None),
                "unopened": unopened}

    def scan_land(self, inst):
        """逐格点击读取地块状态（参考项目 land_scan）。

        24 格 × (点击 + 截图 + OCR) ≈ 40~60 秒，因此按 30 分钟为默认间隔。
        """
        # 必须先抓一帧最新画面：cvhelper 读的是 inst.tmp_png
        self.mac.activate(inst.pid)
        if not self.grab(inst):
            self.fail(inst, "E_CAPTURE_BLACK", "巡查前抓帧失败")
            return False, "抓帧失败"
        # 用户优先闸门：用户在用电脑 / 游戏不在前台时，整个巡查直接跳过
        busy = self.user_busy_reason(inst)
        if busy:
            self.push_log(inst, f"巡查暂缓：{busy}")
            return False, f"暂缓（{busy}）"
        # 页面守卫：不在农场主页时绝不点击（否则会在商店/活动页乱点）
        if self.classify(inst) != "main":
            self.fail(inst, "E_PAGE_UNKNOWN",
                      f"当前页面「{self.classify(inst)}」不是农场主页，跳过巡查（不会乱点）")
            return False, "页面不是农场主页"
        base_cells = self._cv_cells(inst)
        if not base_cells:
            self.fail(inst, "E_PAGE_UNKNOWN", "田地定位失败（cvhelper 未返回格子）")
            return False, "田地定位失败"
        pw, ph = inst.img_size
        x0, y0, w, h = inst.window_rect
        found = 0
        for cell in base_cells:
            if inst.stop_flag.is_set():
                return False, "已停止"
            ix, iy = cell["cx"], cell["cy"]
            sx = x0 + ix * w / pw
            sy = y0 + iy * h / ph
            if not self._in_window(inst, sx, sy):
                continue
            if self.user_busy_reason(inst):
                self.push_log(inst, "巡查中断：用户开始使用电脑，立即让出")
                return False, "用户接管，巡查中断"
            self.mac.activate(inst.pid)
            self.mac.click(inst.pid, sx, sy, mode="ghost", wid=inst.wid, repeat=1)
            # 轮询等弹窗出现（最多 ~2.4s），比固定 sleep 稳
            info = {"crop": None, "countdown": None, "empty": True, "unopened": False}
            for _ in range(6):
                time.sleep(0.4)
                if not self.grab(inst):
                    break
                info = self._parse_plot_popup(inst.ocr_blocks)
                if info["crop"] or info["countdown"] or info["empty"]:
                    break
            idx = cell["row"] * 4 + cell["col"]
            if idx < len(inst.plots):
                plot = inst.plots[idx]
                plot["kind"] = "grass" if info["unopened"] else ("empty" if info["empty"] else "crop")
                plot["needPlanting"] = info["empty"] and not info["unopened"]
                plot["crop"] = info["crop"]
                plot["countdownSec"] = self._cd_seconds(info["countdown"])
                plot["syncAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                plot["cropColor"] = cell.get("rgb")
            found += 1
            # 关掉弹窗，避免遮挡下一格
            xbtn = next((b for b in inst.ocr_blocks if b["text"].strip() in ("X", "×", "✕")), None)
            if xbtn is not None:
                xs = x0 + (xbtn["x"] + xbtn["w"] / 2) * w / pw
                ys = y0 + (xbtn["y"] + xbtn["h"] / 2) * h / ph
                if self._in_window(inst, xs, ys):
                    self.mac.click(inst.pid, xs, ys, mode="ghost", wid=inst.wid, repeat=1)
                    time.sleep(0.4)
        inst.plots_updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.push_state(inst)
        return True, f"地块巡查完成：{found} 格"

    @staticmethod
    def _cd_seconds(text):
        if not text:
            return None
        try:
            h, m, sec = [int(x) for x in text.split(":")]
            return h * 3600 + m * 60 + sec
        except (ValueError, AttributeError):
            return None

    def harvest_plot(self, inst, row, col, max_rounds=6):
        """逐轮收获一块地（点作物唤出「手」→ 点手收一轮 → 复查「可摘」）。

        已实测：手每收一轮就消失，必须重新点作物唤出；2×2 等多轮作物需循环。
        """
        cells = getattr(inst, "last_cells", None) or self._cv_cells(inst) or []
        target = next((c for c in cells if c["row"] == row and c["col"] == col), None)
        if target is None:
            # 还没识别过：先抓一帧跑一次识别
            if not self.grab(inst):
                return False, "抓帧失败"
            cells = self._cv_cells(inst) or []
            target = next((c for c in cells if c["row"] == row and c["col"] == col), None)
        if target is None or not target.get("cx"):
            return False, f"没有 ({row},{col}) 的坐标，请先让插件识别一次"
        pw, ph = inst.img_size
        x0, y0, w, h = inst.window_rect
        rounds = 0
        for _ in range(max_rounds):
            if self.user_busy_reason(inst):
                return True, f"用户接管，已收 {rounds} 轮"
            # 1) 点作物唤出「手」
            csx = x0 + target["cx"] * w / pw
            csy = y0 + target["cy"] * h / ph
            self.mac.activate(inst.pid)
            self.mac.click(inst.pid, csx, csy, mode="ghost", wid=inst.wid, repeat=1)
            time.sleep(0.9)
            if not self.grab(inst):
                break
            # 2) 找「手」
            out, _ = self.mac._run("find", str(inst.tmp_png), str(HAND_TPL),
                                   "--threshold", "0.80", timeout=20)
            try:
                hit = json.loads(out.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError, AttributeError):
                hit = {}
            if not hit.get("found") or not hit.get("matches"):
                break                      # 没有手 = 没得收
            m = hit["matches"][0]
            hsx = x0 + m["cx"] * w / pw
            hsy = y0 + m["cy"] * h / ph
            # 3) 点手收一轮
            self.mac.click(inst.pid, hsx, hsy, mode="ghost", wid=inst.wid, repeat=1)
            rounds += 1
            time.sleep(1.5)
            if not self.grab(inst):
                break
            if not any("可摘" in b["text"] for b in inst.ocr_blocks):
                break                      # 收完了
        self.push_log(inst, f"收获地块 ({row},{col})：{rounds} 轮")
        return True, f"收获 {rounds} 轮"

    def _cv_cells(self, inst):
        """用 cvhelper.py（OpenCV）自动识别田地和每块地状态。

        这是首选方式：不需要人工标定，也不需要点击地块。
        需要系统里有带 cv2/numpy 的 Python；找不到就返回 None 回退。
        """
        helper = HERE / "cvhelper.py"
        if not helper.exists():
            return None
        py = None
        for cand in (os.environ.get("QQFARM_CV_PYTHON"), "/usr/local/bin/python3", "/opt/homebrew/bin/python3"):
            if cand and Path(cand).exists():
                py = cand
                break
        if py is None:
            self.push_log(inst, "cvhelper: 找不到带 cv2 的 Python（尝试 QQFARM_CV_PYTHON / /usr/local/bin/python3）", level="warn")
            return None
        try:
            r = subprocess.run([py, str(helper), str(inst.tmp_png)],
                               capture_output=True, text=True, timeout=25)
            if r.returncode != 0 or not r.stdout.strip():
                self.push_log(inst, f"cvhelper 失败 rc={r.returncode} err={(r.stderr or '')[-160:]}", level="warn")
                return None
            data = json.loads(r.stdout.strip().splitlines()[-1])
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
            self.push_log(inst, f"cvhelper 异常: {e}", level="warn")
            return None
        if not data.get("ok") or not data.get("cells"):
            self.push_log(inst, f"cvhelper 无结果: {str(data)[:120]}", level="warn")
            return None
        self.push_log(inst, f"cvhelper 定位到 {len(data['cells'])} 格（特征点 {data.get('points')}）")
        out = []
        for c in data["cells"]:
            rgb = c["rgb"]
            out.append({"row": c["row"], "col": c["col"],
                        "cx": c.get("cx"), "cy": c.get("cy"),
                        "rgb": rgb, "kind": _cell_kind(rgb),
                        "bigCrop": False, "group": c.get("group"),
                        "covered": c.get("covered", False)})
        inst.plots_updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        inst.last_cells = out
        return out

    def _sample_cells(self, inst):
        calib = load_calibration(inst.pid)
        pw, ph = inst.img_size
        x0, y0, w, h = inst.window_rect
        if not pw or not ph or not w or not h:
            return None
        if calib and calib.get("cells"):
            cols, rows = calib.get("cols", 4), calib.get("rows", 6)
            pts, meta = [], []
            for cell in calib["cells"]:
                ix = int((cell["x"] - x0) * pw / w)
                iy = int((cell["y"] - y0) * ph / h)
                pts.append(f"{ix},{iy}")
                meta.append((cell["row"], cell["col"], cell["x"], cell["y"]))
            out, _ = self.mac._run("sample", str(inst.tmp_png), "--pts", ";".join(pts), "--r", "18", timeout=20)
            if out:
                try:
                    data = json.loads(out.strip().splitlines()[-1])
                except (json.JSONDecodeError, IndexError):
                    data = None
                if data and data.get("samples"):
                    res = []
                    for (m, smp) in zip(meta, data["samples"]):
                        rgb = [smp["r"], smp["g"], smp["b"]]
                        res.append({"row": m[0], "col": m[1], "rgb": rgb,
                                    "kind": _cell_kind(rgb), "bigCrop": False})
                    return res
        data = self.mac.read_grid(str(inst.tmp_png), PLOT_GRID)
        if not data or not data.get("cells"):
            return None
        return [{"row": c["row"], "col": c["col"],
                 "rgb": [int(c.get("r", 0)), int(c.get("g", 0)), int(c.get("b", 0))],
                 "kind": _cell_kind([int(c.get("r", 0)), int(c.get("g", 0)), int(c.get("b", 0))]),
                 "bigCrop": False} for c in data["cells"]]

    # -- 页面判定 --
    def classify(self, inst):
        texts = [b["text"] for b in inst.ocr_blocks]
        joined = " ".join(texts)
        for page, keys in PAGE_SIGNATURES:
            hits = sum(1 for k in keys if k in joined)
            if page == "main" and hits >= 1:
                return page
            if hits >= 2:
                return page
        return "unknown"

    # -- 坐标换算：图像像素 → 屏幕逻辑点 --
    def to_screen(self, inst, ix, iy):
        x0, y0, w, h = inst.window_rect
        px_w, px_h = inst.img_size
        if px_w <= 0 or px_h <= 0:
            return None
        sx = x0 + ix * (w / px_w)
        sy = y0 + iy * (h / px_h)
        return sx, sy

    def find_block(self, inst, text, min_conf=0.5):
        best = None
        for b in inst.ocr_blocks:
            if text in b["text"] and b["conf"] >= min_conf:
                if best is None or b["conf"] > best["conf"]:
                    best = b
        return best

    # -- 一步动作：返回 (是否做了动作, 说明) --
    def step(self, inst, task_name):
        # 窗口可能在两步之间被移动/缩放（激活、用户拖动），坐标必须基于最新矩形
        self.refresh_windows()
        if not self.grab(inst):
            return False, "抓帧失败"

        page = self.classify(inst)
        inst.page = page
        now = time.strftime("%H:%M:%S")
        busy = self.user_busy_reason(inst)
        if busy:
            if getattr(inst, "idle_rounds", 0) % 10 == 0:
                self.push_log(inst, f"[{now}] 暂缓：{busy}", level="debug")
            inst.idle_rounds = getattr(inst, "idle_rounds", 0) + 1
            return False, f"暂缓（{busy}）"
        # 已知页面白名单之外一律不动作
        if page not in ("main", "popup_notice", "popup_generic", "warehouse", "shop", "friend", "unknown"):
            self.fail(inst, "E_PAGE_UNKNOWN", f"页面「{page}」不在白名单，停手")
            return False, "页面不在白名单"

        # ① 弹窗优先：按弹窗字典走（优先点「直接领取/确定」这类安全按钮，其次点空白）
        if page in ("popup_notice", "popup_generic"):
            texts = " ".join(b["text"] for b in inst.ocr_blocks)
            for rule in POPUP_RULES:
                if not any(k in texts for k in rule["keys"]):
                    continue
                if rule["kind"] == "click" and rule["label"]:
                    block = self.find_block(inst, rule["label"], min_conf=0.45)
                    if block:
                        ok, why = self._click_block(inst, block, rule["label"], now)
                        self.push_log(inst, f"[{now}] 弹窗（{rule['keys'][0]}）→ {why}")
                        return ok, why
                closed, why = self._close_popup(inst)
                self.push_log(inst, f"[{now}] 弹窗（{rule['keys'][0]}）→ 点空白：{why}")
                return closed, why
            closed, why = self._close_popup(inst)
            self.push_log(inst, f"[{now}] 未识别弹窗 → 点空白：{why}")
            return closed, why

        # ①.5 非主页：先用 × 图标关掉（OCR 看不见纯图标，必须靠模板匹配）
        if page in ("warehouse", "shop", "friend", "unknown"):
            hit = self.mac.find_icon(str(inst.tmp_png), str(ICON_CLOSE), threshold=0.80)
            if hit:
                sx, sy = self.to_screen(inst, hit["cx"], hit["cy"])
                if self._in_window(inst, sx, sy):
                    self.mac.activate(inst.pid)
                    self.refresh_windows()
                    sx, sy = self.to_screen(inst, hit["cx"], hit["cy"])
                    for _ in range(2):
                        self.mac.click(inst.pid, sx, sy, mode="ghost", wid=inst.wid, repeat=1)
                        time.sleep(0.35)
                    self.push_log(inst, f"[{now}] 页面「{page}」→ 模板匹配命中关闭按钮"
                                        f"({hit['cx']},{hit['cy']} score={hit['score']:.2f})，已点击",
                                  action="close_icon", conf=hit["score"])
                    time.sleep(1.4)
                    ok = self.grab(inst)
                    new_page = self.classify(inst) if ok else "unknown"
                    inst.page = new_page
                    if new_page == "main":
                        inst.stats["totalActions"] += 1
                        self.push_state(inst)
                        return True, "已从子页面返回主页"
                    self.push_log(inst, f"[{now}] 点击 × 后页面仍为「{new_page}」")
                    return False, f"关闭失败（页面仍为 {new_page}）"
            self.push_log(inst, f"[{now}] 页面「{page}」未匹配到关闭按钮，尝试点空白处")

        # ② 主页：收获 → 播种
        if page == "main":
            block = self.find_block(inst, HARVEST_LABELS[0])
            action, label = (block, HARVEST_LABELS[0]) if block else (None, None)
            if action is None:
                plan = self.find_block(inst, PLANT_LABELS[0])
                if plan is not None:
                    if self.allow_plant:
                        action, label = plan, PLANT_LABELS[0]
                    else:
                        self.push_log(inst, f"[{now}] 有「{PLANT_LABELS[0]}」但未开启播种权限（--allow-plant），跳过")
                        return False, "空闲：有播种入口但未授权"
            if action is None:
                # 正常状态：农场已收完或种子耗尽，属"无事可做"，不是错误
                inst.idle_rounds = getattr(inst, "idle_rounds", 0) + 1
                if inst.idle_rounds == 1 or inst.idle_rounds % 10 == 0:
                    self.push_log(inst, f"[{now}] 空闲第 {inst.idle_rounds} 轮：主页无可执行动作"
                                        f"（无作物可收 / 无播种入口）")
                return False, "空闲：无可执行动作"
            inst.idle_rounds = 0
            return self._click_block(inst, action, label, now)

        # ③ 其他页面：本后端尚未实现，报错但不乱点
        self.fail(inst, "E_PAGE_UNKNOWN", f"当前页面「{page}」不在支持范围（不会乱点）")
        return False, f"页面不支持：{page}"

    def _close_popup(self, inst):
        """点空白处关弹窗。空白点由 OCR 反证：附近没有文字块。"""
        w, h = inst.img_size
        candidates = [(30, int(h * 0.45)), (30, int(h * 0.6)), (int(w * 0.94), int(h * 0.45)),
                      (int(w * 0.06), int(h * 0.75))]
        target = None
        for cx, cy in candidates:
            busy = any(abs(b["x"] + b["w"] / 2 - cx) < 70 and abs(b["y"] + b["h"] / 2 - cy) < 70
                       for b in inst.ocr_blocks)
            if not busy:
                target = (cx, cy)
                break
        if target is None:
            self.fail(inst, "E_PAGE_UNKNOWN", "弹窗存在但找不到安全空白点")
            return False, "无安全空白点"
        self.mac.activate(inst.pid)
        self.refresh_windows()
        sx, sy = self.to_screen(inst, *target)
        if not self._in_window(inst, sx, sy):
            return False, "空白点越界"
        self.mac.click(inst.pid, sx, sy, mode="ghost", wid=inst.wid)
        time.sleep(1.2)
        ok = self.grab(inst)
        new_page = self.classify(inst) if ok else "unknown"
        inst.page = new_page
        if new_page not in ("popup_notice", "popup_generic"):
            inst.stats["totalActions"] += 1
            self.push_state(inst)
            return True, f"弹窗已关闭（→ {new_page}）"
        self.fail(inst, "E_PAGE_UNKNOWN", "点击空白后弹窗仍在")
        return False, "弹窗未关闭"

    def _click_block(self, inst, block, label, now):
        ix = block["x"] + block["w"] / 2
        iy = block["y"] + block["h"] / 2
        # 先激活（否则第一次点击被吞），再重取窗口矩形（激活可能改变位置/尺寸）
        self.mac.activate(inst.pid)
        self.refresh_windows()
        sx, sy = self.to_screen(inst, ix, iy)
        if not self._in_window(inst, sx, sy):
            self.fail(inst, "E_BAD_REQUEST", f"点击点越界 ({sx:.0f},{sy:.0f})")
            return False, "点击点越界"
        before = self.signature(inst)
        self.mac.click(inst.pid, sx, sy, mode="ghost", wid=inst.wid)
        self.push_log(inst, f"[{now}] 点击「{label}」({sx:.0f},{sy:.0f}) conf={block['conf']:.2f}",
                      action=label, conf=round(block["conf"], 2))
        time.sleep(1.6)
        ok = self.grab(inst)
        after = self.signature(inst) if ok else before
        inst.page = self.classify(inst) if ok else inst.page
        if after != before:
            key = "harvest" if label in HARVEST_LABELS else "plant"
            inst.stats[key] = inst.stats.get(key, 0) + 1
            inst.stats["totalActions"] += 1
            self.push_state(inst)
            return True, f"「{label}」已执行（画面变化，页面 → {inst.page}）"
        self.fail(inst, "E_STUCK", f"点击「{label}」后画面无变化")
        return False, f"「{label}」点击无效"

    def _in_window(self, inst, sx, sy):
        x0, y0, w, h = inst.window_rect
        return x0 <= sx <= x0 + w and y0 <= sy <= y0 + h

    def signature(self, inst):
        return tuple(sorted(b["text"] for b in inst.ocr_blocks))

    # -- 工作线程（调度器）--
    def run_worker(self, inst, task_name="main", once=False):
        """按 TASK_ORDER 执行到期任务。

        - once=True：把所有启用的任务各跑一遍后结束（页面的「立即执行」）
        - once=False：常驻调度，空闲时每秒检查一次
        """
        rounds = 0
        while not inst.stop_flag.is_set():
            if inst.state in ("paused", "stopped", "error"):
                break

            now = time.time()
            due = [n for n in TASK_ORDER
                   if inst.tasks[n]["enabled"] and (inst.tasks[n]["nextRun"] is None or inst.tasks[n]["nextRun"] <= now)]
            if once:
                # 单次执行：优先跑指定的任务名，否则跑所有启用任务
                due = [task_name] if task_name in inst.tasks else [n for n in TASK_ORDER if inst.tasks[n]["enabled"]]

            if not due:
                inst.current_task = None
                nxt = [t["nextRun"] for t in inst.tasks.values() if t["enabled"] and t["nextRun"]]
                inst.next_run = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(min(nxt))) if nxt else None
                self.push_state(inst)
                if once:
                    break
                inst.stop_flag.wait(1.0)
                continue

            name = due[0]
            rounds += 1
            inst.current_task = name
            inst.rev += 1
            self.broadcast({"t": "event", "instance": inst.id, "kind": "task_start", "detail": {"task": name}})
            self.push_state(inst)
            started = time.time()
            try:
                if name == "land_scan":
                    if inst.frame_hash and inst.frame_changed is False:
                        self.push_log(inst, "巡查跳过：画面与上次一致（变化检测）", level="debug")
                        did, why = False, "画面无变化，跳过"
                    else:
                        did, why = self.scan_land(inst)
                elif name in IMPLEMENTED_TASKS:
                    did, why = self.step(inst, name)
                else:
                    did, why = False, f"任务「{name}」尚未实现（macOS 后端当前仅 main 可用）"
            except Exception as e:  # noqa: BLE001
                did, why = False, f"任务异常：{e}"

            cfg = inst.tasks[name]
            cfg["lastRun"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started))
            cfg["lastResult"] = why
            cfg["nextRun"] = time.time() + max(5, int(cfg["intervalSec"]))
            inst.next_run = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(cfg["nextRun"]))
            save_task_state(inst.id, inst.tasks)
            self.push_log(inst, f"[{time.strftime('%H:%M:%S')}] 任务 {name}：{why}",
                          task=name)
            self.broadcast({"t": "event", "instance": inst.id, "kind": "task_end",
                            "detail": {"task": name, "ok": did, "note": why}})
            self.push_state(inst)
            if once:
                # 单次模式：跑完这一轮就退出（之前这里是 continue，导致每秒无限重跑）
                cfg["nextRun"] = None
                break

        if once and inst.state == "running":
            inst.state = "idle"
            inst.rev += 1
            inst.current_task = None
            self.push_state(inst)


# ── HTTP + SSE ──────────────────────────────────────────────────────
AGENT: Agent = None  # type: ignore


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):  # 静音默认访问日志
        return

    def _json(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return None

    def do_GET(self):  # noqa: N802
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)

        if p == "/v1/health":
            inst = AGENT.refresh_windows()
            return self._json(200, {
                "ok": True, "agentId": "mac", "protocol": PROTOCOL, "agentVersion": AGENT_VERSION,
                "platforms": ["qq", "wechat"],
                "instanceCount": len(AGENT.instances),
                "dryRun": AGENT.mac.dry_run,
                "window": None if inst is None else {
                    "wid": inst.wid, "pid": inst.pid, "page": inst.page,
                    "img": list(inst.img_size),
                },
                "uptimeSec": int(time.time() - AGENT.started_at),
            })

        if p == "/v1/instances":
            inst = AGENT.refresh_windows()
            return self._json(200, [i.summary() for i in AGENT.instances.values()])

        m = None
        import re
        m = re.match(r"^/v1/instances/([^/]+)/state$", p)
        if m:
            inst = AGENT.instances.get(m.group(1))
            if inst is None:
                return self._json(404, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": "未知实例"}})
            if not inst.ocr_blocks:
                AGENT.grab(inst)
            AGENT.push_state(inst)
            return self._json(200, inst.full_state())

        m = re.match(r"^/v1/instances/([^/]+)/calibrate$", p)
        if m:
            inst = AGENT.instances.get(m.group(1)) or AGENT.refresh_windows()
            if inst is None:
                return self._json(404, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": "未找到实例"}})
            pts = getattr(AGENT, "_calib_points", [])
            return self._json(200, {"ok": True, "points": pts, "done": len(pts) >= 4,
                                    "saved": load_calibration(inst.pid),
                                    "recording": bool(getattr(AGENT, "_calib_thread", None)
                                                      and AGENT._calib_thread.is_alive())})

        m = re.match(r"^/v1/instances/([^/]+)/tasks$", p)
        if m:
            inst = AGENT.instances.get(m.group(1)) or AGENT.refresh_windows()
            if inst is None:
                return self._json(404, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": "未找到实例"}})
            return self._json(200, {"ok": True, "order": TASK_ORDER,
                                    "implemented": sorted(IMPLEMENTED_TASKS),
                                    "tasks": inst.full_state()["tasks"]})

        m = re.match(r"^/v1/instances/([^/]+)/frame$", p)
        if m:
            inst = AGENT.instances.get(m.group(1))
            if inst is None:
                return self._json(404, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": "未知实例"}})
            if inst.last_frame is None or time.time() - inst.last_frame_at > 3:
                AGENT.grab(inst, want_ocr=False)
            data = inst.last_frame or b""
            self.send_response(200)
            self.send_header("content-type", getattr(inst, "frame_mime", "image/jpeg"))
            self.send_header("content-length", str(len(data)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return None

        if p == "/v1/stream":
            fps = float((q.get("fps") or ["2"])[0])
            self.send_response(200)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("connection", "keep-alive")
            self.end_headers()
            q_ = queue.Queue(maxsize=64)
            with AGENT.lock:
                AGENT.clients.add(q_)
            try:
                self.wfile.write(f"data: {json.dumps({'t':'hello','protocol':PROTOCOL,'agent':'mac','agentVersion':AGENT_VERSION,'instances':list(AGENT.instances.keys())}, ensure_ascii=False)}\n\n".encode())
                for inst in AGENT.instances.values():
                    self.wfile.write(f"data: {json.dumps({'t':'state','instance':inst.id,'rev':inst.rev,'sync':inst.full_state()}, ensure_ascii=False)}\n\n".encode())
                last_ping = time.time()
                while True:
                    try:
                        chunk = q_.get(timeout=1.0)
                        self.wfile.write(chunk)
                    except queue.Empty:
                        if time.time() - last_ping > 12:
                            self.wfile.write(f"data: {{\"t\":\"ping\",\"ts\":\"{time.strftime('%H:%M:%S')}\"}}\n\n".encode())
                            last_ping = time.time()
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with AGENT.lock:
                    AGENT.clients.discard(q_)
            return None

        return self._json(404, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": f"未知路径 {p}"}})

    def do_POST(self):  # noqa: N802
        import re
        p = self.path.split("?")[0]
        body = self._body()
        if body is None:
            return self._json(400, {"ok": False, "error": {"code": "E_BAD_REQUEST", "message": "JSON 非法"}})

        m = re.match(r"^/v1/instances/([^/]+)/control$", p)
        if m:
            inst = AGENT.refresh_windows() or AGENT.instances.get(m.group(1))
            if inst is None:
                return self._json(404, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": "未找到游戏窗口"}})
            action = body.get("action")
            if action not in CONTROL_ACTIONS:
                return self._json(400, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": f"未知动作 {action}"}})
            if action in ("start", "resume", "run_once"):
                if inst.state == "running" and inst.worker and inst.worker.is_alive():
                    return self._json(200, {"ok": True, "action": action, "state": inst.state, "rev": inst.rev, "note": "已在运行"})
                inst.stop_flag.clear()
                once = action == "run_once"
                inst.state = "running" if not once else inst.state
                inst.current_task = "main"
                inst.rev += 1
                inst.worker = threading.Thread(target=AGENT.run_worker, args=(inst, "main", once), daemon=True)
                inst.worker.start()
                AGENT.push_log(inst, f"控制：{'执行一轮' if once else action}")
                AGENT.push_state(inst)
                return self._json(200, {"ok": True, "action": action, "state": inst.state, "rev": inst.rev})
            if action in ("pause", "stop"):
                inst.stop_flag.set()
                inst.state = "paused" if action == "pause" else "stopped"
                inst.rev += 1
                AGENT.push_log(inst, f"控制：{action}")
                AGENT.push_state(inst)
                return self._json(200, {"ok": True, "action": action, "state": inst.state, "rev": inst.rev})
            if action in ("repair", "restart"):
                AGENT.instances.clear()
                inst2 = AGENT.refresh_windows()
                if inst2 is None:
                    return self._json(409, {"ok": False, "error": {"code": "E_WINDOW_LOST", "message": "未找到游戏窗口"}})
                inst2.state = "idle"
                AGENT.push_log(inst2, "已重新绑定窗口（rebind）")
                AGENT.push_state(inst2)
                return self._json(200, {"ok": True, "action": action, "state": inst2.state, "rev": inst2.rev})
            return self._json(400, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": action}})

        m = re.match(r"^/v1/instances/([^/]+)/calibrate$", p)
        if m:
            inst = AGENT.instances.get(m.group(1)) or AGENT.refresh_windows()
            if inst is None:
                return self._json(404, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": "未找到实例"}})
            started = AGENT.start_calibration(inst, count=4)
            return self._json(200 if started else 409,
                              {"ok": started, "action": "start", "count": 4,
                               "hint": "现在请在游戏里依次点击 4 个角的地块（左上→右上→右下→左下）"})

        m = re.match(r"^/v1/instances/([^/]+)/harvest_plot$", p)
        if m:
            inst = AGENT.instances.get(m.group(1)) or AGENT.refresh_windows()
            if inst is None:
                return self._json(404, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": "未找到实例"}})
            try:
                row, col = int(body.get("row")), int(body.get("col"))
            except (TypeError, ValueError):
                return self._json(400, {"ok": False, "error": {"code": "E_BAD_REQUEST", "message": "row/col 必填"}})
            ok, why = AGENT.harvest_plot(inst, row, col)
            return self._json(200 if ok else 409, {"ok": ok, "result": why,
                                                   "error": None if ok else {"code": "E_HARVEST", "message": why}})

        # ── v2：机械点击原语（插件不判断，只把模型给的坐标落到屏幕上）──
        m = re.match(r"^/v1/instances/([^/]+)/click$", p)
        if m:
            inst = AGENT.instances.get(m.group(1)) or AGENT.refresh_windows()
            if inst is None:
                return self._json(404, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": "未找到实例"}})
            try:
                ix = float(body.get("x")); iy = float(body.get("y"))
            except (TypeError, ValueError):
                return self._json(400, {"ok": False, "error": {"code": "E_BAD_REQUEST", "message": "x/y 必填"}})
            pw, ph = inst.img_size
            x0, y0, w, h = inst.window_rect
            if not pw or not ph:
                return self._json(503, {"ok": False, "error": {"code": "E_NO_FRAME", "message": "还没有画面"}})
            sx, sy = x0 + ix * w / pw, y0 + iy * h / ph
            if not (x0 <= sx <= x0 + w and y0 <= sy <= y0 + h):
                return self._json(400, {"ok": False, "error": {"code": "E_BAD_COORD", "message": "坐标不在游戏窗口内"}})
            AGENT.mac.activate(inst.pid)
            AGENT.mac.click(inst.pid, sx, sy, mode="ghost", wid=inst.wid, repeat=1)
            AGENT.push_log(inst, f"点击 图像({ix:.0f},{iy:.0f}) → 屏幕({sx:.0f},{sy:.0f})")
            return self._json(200, {"ok": True, "screen": [round(sx, 1), round(sy, 1)]})

        m = re.match(r"^/v1/instances/([^/]+)/click_plot$", p)
        if m:
            inst = AGENT.instances.get(m.group(1)) or AGENT.refresh_windows()
            if inst is None:
                return self._json(404, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": "未找到实例"}})
            cells = getattr(inst, "last_cells", None) or AGENT._cv_cells(inst) or []
            target = None
            try:
                row, col = int(body.get("row")), int(body.get("col"))
            except (TypeError, ValueError):
                return self._json(400, {"ok": False, "error": {"code": "E_BAD_REQUEST", "message": "row/col 必填"}})
            for c in cells:
                if c.get("row") == row and c.get("col") == col:
                    target = c
                    break
            if target is None or not target.get("cx"):
                return self._json(404, {"ok": False, "error": {
                    "code": "E_NO_CELL", "message": f"没有 ({row},{col}) 的坐标，请先 farm_look 让插件识别一次"}})
            pw, ph = inst.img_size
            x0, y0, w, h = inst.window_rect
            sx, sy = x0 + target["cx"] * w / pw, y0 + target["cy"] * h / ph
            AGENT.mac.activate(inst.pid)
            AGENT.mac.click(inst.pid, sx, sy, mode="ghost", wid=inst.wid, repeat=1)
            AGENT.push_log(inst, f"点击地块 ({row},{col}) → 屏幕({sx:.0f},{sy:.0f})")
            return self._json(200, {"ok": True, "screen": [round(sx, 1), round(sy, 1)]})

        m = re.match(r"^/v1/instances/([^/]+)/tasks$", p)
        if m:
            inst = AGENT.instances.get(m.group(1)) or AGENT.refresh_windows()
            if inst is None:
                return self._json(404, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": "未找到实例"}})
            name = body.get("name")
            if name not in DEFAULT_TASKS:
                return self._json(400, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": f"未知任务 {name}"}})
            cfg = inst.tasks[name]
            if "enabled" in body:
                cfg["enabled"] = bool(body["enabled"])
                cfg["nextRun"] = None if cfg["enabled"] else cfg["nextRun"]
            if "intervalSec" in body:
                try:
                    cfg["intervalSec"] = max(5, int(body["intervalSec"]))
                except (TypeError, ValueError):
                    return self._json(400, {"ok": False, "error": {"code": "E_BAD_REQUEST", "message": "intervalSec 非法"}})
            save_task_state(inst.id, inst.tasks)
            inst.rev += 1
            AGENT.push_log(inst, f"任务配置更新：{name} enabled={cfg['enabled']} interval={cfg['intervalSec']}s")
            AGENT.push_state(inst)
            return self._json(200, {"ok": True, "task": name, "config": {
                "enabled": cfg["enabled"], "intervalSec": cfg["intervalSec"]}})

        m = re.match(r"^/v1/instances/([^/]+)/task$", p)
        if m:
            inst = AGENT.instances.get(m.group(1)) or AGENT.refresh_windows()
            task = body.get("task")
            if task not in TASK_NAMES:
                return self._json(400, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": f"未知任务 {task}"}})
            if task not in IMPLEMENTED_TASKS:
                return self._json(409, {"ok": False, "error": {
                    "code": "E_UNSUPPORTED", "message": f"任务 {task} 尚未实现（已实现：{sorted(IMPLEMENTED_TASKS)}）"}})
            inst.stop_flag.clear()
            inst.current_task = task
            inst.rev += 1
            AGENT.broadcast({"t": "event", "instance": inst.id, "kind": "task_start", "detail": {"task": task}})
            threading.Thread(target=AGENT.run_worker, args=(inst, task, True), daemon=True).start()
            return self._json(200, {"ok": True, "task": task})

        return self._json(404, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": f"未知路径 {p}"}})


def main():
    ap = argparse.ArgumentParser(description="Farm Agent · macOS 后端")
    ap.add_argument("--port", type=int, default=4571)
    ap.add_argument("--fps", type=float, default=2.0)
    ap.add_argument("--interval", type=float, default=2.5, help="循环间隔秒")
    ap.add_argument("--dry-run", action="store_true", help="只识别不点击")
    ap.add_argument("--guard-idle", type=float, default=2.0,
                    help="用户空闲多少秒后才允许操作（默认 2s，避免抢鼠标）")
    ap.add_argument("--allow-focus-steal", action="store_true",
                    help="允许把游戏窗口拉到前台（会打断用户，默认关闭）")
    ap.add_argument("--no-plant", action="store_true", help="只收获，不点「一键务农」")
    ap.add_argument("--allow-plant", action="store_true", help="（默认开启）允许点击「一键务农」")
    ap.add_argument("--token", default="", help="非空则要求 Authorization: Bearer <token>")
    args = ap.parse_args()

    if not MACD.exists():
        log("error", f"缺少 macd 二进制：{MACD}，请先运行 agent/mac/build.sh")
        sys.exit(2)
    if subprocess.run([str(MACD), "winlist"], capture_output=True).returncode != 0:
        log("error", "macd 无法执行")
        sys.exit(2)

    global AGENT
    AGENT = Agent(Mac(dry_run=args.dry_run), fps=args.fps, interval=args.interval,
                  allow_plant=not args.no_plant,
                  guard_idle=args.guard_idle, allow_focus_steal=args.allow_focus_steal)
    inst = AGENT.refresh_windows()
    if inst is None:
        log("warn", "未发现游戏窗口（请确认 QQ/微信 小程序已打开农场），仍会启动服务并等待")
    else:
        AGENT.grab(inst)
        log("info", f"已绑定窗口 wid={inst.wid} pid={inst.pid} 页面={AGENT.classify(inst)}")

    threading.Thread(target=AGENT.frame_pump, args=(args.fps or 1.0,), daemon=True).start()

    # 端口自动避让：旧实例（例如 DSH 重启后遗留的孤儿进程）占着默认端口时，
    # 自动顺延到下一个可用端口，并把实际端口写进 ~/.dsh-farm/agent-port.json，
    # 插件读这个文件即可跟随，不需要人工杀进程。
    srv = None
    bound = None
    for candidate in range(args.port, args.port + 10):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            bound = candidate
            break
        except OSError:
            log("warn", f"端口 {candidate} 被占用，尝试 {candidate + 1}")
    if srv is None:
        log("error", "4571~4580 都被占用，放弃启动")
        sys.exit(3)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "agent-port.json").write_text(json.dumps({
            "port": bound, "pid": os.getpid(),
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
    except OSError as e:
        log("warn", f"端口文件写入失败: {e}")
    log("info", f"FAP v{PROTOCOL} listening on http://127.0.0.1:{bound}"
                + (" [DRY-RUN]" if args.dry_run else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("info", "bye")


if __name__ == "__main__":
    main()
