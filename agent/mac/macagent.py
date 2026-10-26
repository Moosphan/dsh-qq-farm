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
import urllib.parse
from urllib.parse import urlparse, parse_qs
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
ICON_HELP = TEMPLATES / "help.png"      # 好友列表/头像条上的「小人铲土」= 需要帮忙
ICON_STEAL = TEMPLATES / "steal.png"    # 好友列表上的「手套+面罩」= 有可偷的作物

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
AGENT_VERSION = "0.2.0-mac"
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
IMPLEMENTED_TASKS = {"main", "land_scan", "friend", "reward", "grass", "timed_harvest", "event"}

# 好友/任务面板用到的标签（只有这些会被点，其余一律不点）
NAV_FRIEND = ["好友"]                    # 底部导航「好友」
LABEL_VISIT = "拜访"                     # 好友列表里的拜访按钮
LABEL_STEAL = "一键偷取"                 # 好友农场：一键偷菜
LABEL_HELP = "一键务农"                  # 好友农场：一键务农 = 帮忙（除草/杀虫），加经验
LABEL_HOME = "回家"                      # 好友农场：回家
LABEL_CLAIM = "领取"                     # 任务面板：领取奖励
HELP_LABELS = ["除草", "帮忙", "浇水", "一键帮忙"]   # 好友求助/帮忙
TASK_PANEL_SIGNS = ["成长任务", "每日任务", "一键种植"]
CONTROL_ACTIONS = ["start", "pause", "resume", "stop", "run_once", "repair", "restart"]

# ── 页面与可点击白名单 ───────────────────────────────────────────────
# 只允许点这些文字，其余一律不点（破坏性按钮如"出售/批量出售"永不进入白名单）
CLICKABLE = {
    "一键务农": "harvest_all",
    "一键施肥": "fertilize",
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
    ("task_panel", ["成长任务", "每日任务"]),
    ("svip", ["SVIP特权", "会员礼包", "开通会员", "专属商城"]),
    ("event_shop", ["兑换商店", "店铺等级", "营业额", "制作奶茶"]),
    # 主页先判：主页有「带田园犬回家」气泡，不能因为含「回家」就当成好友农场
    ("main", ["仓库", "商店", "图鉴", "一键务农", "一键收获", "好友求助", "等级提升", "可扩建"]),
    ("friend_farm", ["回家", "一键偷取"]),
    ("warehouse", ["批量出售", "批量锁定", "格子上限"]),
    ("shop", ["确认购买", "购买", "售价"]),
    ("friend", ["拜访", "邀请", "好友农场", "偷菜"]),
]
# 各页面判定需要的命中条数（默认 2）
PAGE_MIN_HITS = {"main": 3}

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
    """读该实例的任务配置（首次运行用 DEFAULT_TASKS，之后持久化到 ~/.dsh-farm）。

    窗口重建后 instance_id（平台-wid）会变 → 先找同名文件，再退回上一次的
    「模板」（tasks-last.json），这样用户开过的常驻任务不会因为窗口重开而丢。
    """
    state = {name: {"enabled": cfg["enabled"], "intervalSec": cfg["intervalSec"],
                    "nextRun": None, "lastRun": None, "lastResult": None}
             for name, cfg in DEFAULT_TASKS.items()}
    path = _task_state_path(instance_id)
    if not path.exists() and _task_last_path().exists():
        path = _task_last_path()
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


def _settings_path(platform):
    return STATE_DIR / ("settings-%s.json" % platform)


def load_settings(platform):
    try:
        data = json.loads(_settings_path(platform).read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(platform, settings, click_mode=None):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        blob = dict(settings)
        if click_mode:
            blob["clickMode"] = click_mode
        _settings_path(platform).write_text(json.dumps(blob, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    except OSError:
        pass


def _daily_path(platform):
    return STATE_DIR / ("stats-%s.json" % platform)


def load_daily_stats(platform):
    """读按天统计；文件坏了或没文件就开一个今天的空记录（面板才不会显示「今日（—）」）。"""
    today = time.strftime("%Y-%m-%d")
    empty = {"date": today, "harvest": 0, "plant": 0, "steal": 0, "help": 0,
             "sell": 0, "totalActions": 0}
    try:
        data = json.loads(_daily_path(platform).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"today": empty, "history": []}
    if not isinstance(data, dict):
        return {"today": empty, "history": []}
    if (data.get("today") or {}).get("date") != today:
        if data.get("today"):
            data.setdefault("history", []).append(data["today"])
        data["today"] = empty
    data.setdefault("history", [])
    return data


def save_daily_stats(platform, data):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _daily_path(platform).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    except OSError:
        pass


def _task_last_path():
    return STATE_DIR / "tasks-last.json"


def save_task_state(instance_id, tasks):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(tasks, ensure_ascii=False, indent=2)
        _task_state_path(instance_id).write_text(blob, encoding="utf-8")
        _task_last_path().write_text(blob, encoding="utf-8")
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

    def windows(self, all_spaces=False):
        out, err = self._run("winlist", *(["--all-spaces"] if all_spaces else []))
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

    def focus_game_app(self):
        """已禁用：主动 activate 会抢焦点 / 弹授权 / 卡死。仅保留方法名以免旧调用报错。"""
        return None

    def find_game_window(self, title_contains="农场", all_spaces=False):
        """找游戏窗口。

        主路径：标题含「农场」且 owner 是 QQ/微信容器。
        兜底：没有屏幕录制权限时 macOS 会把窗口标题隐去（title 为空），
        此时按 owner（QQ小程序 / 微信 / MiniProgram）加尺寸筛选。
        """
        wins = self.windows(all_spaces=all_spaces)
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
            # 竖屏比例：农场是 412×771；微信聊天窗 876×614 这类横窗不能当游戏
            if w.get("w", 0) >= 300 and w.get("h", 0) >= 500 and w.get("h", 0) > w.get("w", 0) * 1.3:
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

    def click(self, pid, sx, sy, mode="warp", wid=None, repeat=2, gap=0.25):
        """点击。repeat>1 用于绕过 macOS 的 acceptsFirstMouse=false：
        第一次点击只激活窗口，第二次才真正落到控件上。"""
        if self.dry_run:
            return {"ok": True, "mode": "dry-run", "from": [sx, sy], "to": [sx, sy], "repeat": repeat}
        result = {"ok": False, "error": "未执行"}
        if mode == "auto":
            mode = "warp"
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
        # 按天统计（持久化到 ~/.dsh-farm/stats-<platform>.json，重启/换窗口不丢）
        self.daily = load_daily_stats(win["platform"])
        self.last_frame = None          # 缩略图字节（JPEG/PNG）
        self.frame_mime = "image/jpeg"  # 实际编码
        self.last_frame_at = 0.0
        self.ocr_blocks = []
        self.img_size = (0, 0)
        self.frame_hash = None
        self.frame_changed = False
        # v2：调度器只"标记待判断"，不自动点击；由模型看图后决定动作
        self.pending_wake = None
        # 运行时可变设置（面板可改，无需重启）
        self.settings = {"guardIdle": 2.0, "changeGate": True, "buyPolicy": "auto"}
        # 设置持久化：重启/换窗口后不再回到默认（否则用户设的 guardIdle=0 会被悄悄改回 2）
        try:
            self.settings.update({k: v for k, v in load_settings(self.platform).items()
                                  if k in ("guardIdle", "changeGate", "buyPolicy", "fertilize")})
        except (TypeError, ValueError):
            pass
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
                             # nextRun 统一给 ISO 字符串（面板要显示 HH:MM），
                             # 再附一个 nextInSec 便于显示「x 分钟后」
                             "nextRun": (time.strftime("%Y-%m-%dT%H:%M:%S",
                                                       time.localtime(cfg["nextRun"]))
                                         if cfg["nextRun"] else None),
                             "nextInSec": (max(0, int(cfg["nextRun"] - time.time()))
                                           if cfg["nextRun"] else None),
                             "lastRun": cfg["lastRun"],
                             "lastResult": cfg["lastResult"],
                             "label": DEFAULT_TASKS[name]["label"],
                             "implemented": name in IMPLEMENTED_TASKS}
                      for name, cfg in self.tasks.items()},
            "taskOrder": TASK_ORDER,
            "plotsUpdatedAt": getattr(self, "plots_updated_at", None),
            "frameHash": getattr(self, "frame_hash", None),
            "frameChanged": getattr(self, "frame_changed", False),
            "pendingWake": getattr(self, "pending_wake", None),
            "settings": getattr(self, "settings", {}),
            "idleRounds": getattr(self, "idle_rounds", 0),
            "stats": self.stats,
            "event": getattr(self, "event", None),
            "lastCrop": getattr(self, "last_crop", None),
            "seeds": getattr(self, "seeds", None),
            "seedsRaw": getattr(self, "seeds_raw", None),
            "statsToday": (self.daily.get("today") or {}) if
                          (self.daily.get("today") or {}).get("date") == time.strftime("%Y-%m-%d")
                          else {"date": time.strftime("%Y-%m-%d"), "harvest": 0, "plant": 0,
                                "steal": 0, "help": 0, "sell": 0, "totalActions": 0},
            "statsHistory": (self.daily.get("history") or [])[-7:],
            "page": self.page,
        }


class Agent:
    # 点击模式：
    #   "auto"（默认）= 先 ghost（点完把光标还原，尽量不打扰用户），失败才升级到 warp
    #   "ghost" = 只用幽灵点击；"warp" = 只用真实光标（最可靠但会挪光标）
    click_mode = "auto"
    # auto 模式的起点：ghost（点完把光标还原）。
    # 实测 2026-10-26：`macd bgclick`（SkyLight 真后台）对本容器**无效**
    #   —— 三种参数（primer / --no-primer / --count 2）画面都无变化；
    #   而 `--ghost` 一次就生效且光标被还原。所以 bg 只作为显式选项保留，
    #   auto 链从 ghost 开始，失败才升级 warp。
    effective_click_mode = "ghost"

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

    def bump_daily_amount(self, inst, key, amount):
        """按天累计金额（income/expGain），不计入 totalActions。"""
        try:
            today = time.strftime("%Y-%m-%d")
            d = inst.daily
            if d.get("today", {}).get("date") != today:
                self.bump_daily(inst, "totalActions", 0)
                d = inst.daily
            cur = d.setdefault("today", {})
            cur[key] = int(cur.get(key, 0)) + int(amount)
            save_daily_stats(inst.platform, d)
        except (KeyError, TypeError, ValueError):
            pass

    def bump_daily(self, inst, key, n=1):
        """按天累计动作数（harvest/plant/steal/help/sell）。"""
        try:
            today = time.strftime("%Y-%m-%d")
            d = inst.daily
            if d.get("today", {}).get("date") != today:
                # 换天：把昨天推进 history 再重开
                if d.get("today"):
                    d.setdefault("history", []).append(d["today"])
                    d["history"] = d["history"][-30:]
                d["today"] = {"date": today, "harvest": 0, "plant": 0, "steal": 0,
                              "help": 0, "sell": 0, "totalActions": 0}
            if key in d["today"]:
                d["today"][key] = int(d["today"][key]) + int(n)
            d["today"]["totalActions"] = int(d["today"].get("totalActions", 0)) + int(n)
            save_daily_stats(inst.platform, d)
        except (KeyError, TypeError, ValueError):
            pass

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
        guard = float(inst.settings.get("guardIdle", self.guard_idle))
        idle = self.mac.idle_seconds()
        if idle < guard:
            # warp 点击会挪动真实光标，自己刚点的那几下会被算成"用户在活动"→ 自己把自己挡住。
            # 所以：如果空闲时间短是因为**我们刚点过**，就不算占用。
            since_own = time.time() - getattr(self, "last_action_at", 0)
            if since_own <= guard + 1.0:
                return None
            return f"用户正在使用电脑（空闲 {idle:.1f}s < {guard:.0f}s）"
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
        """重新发现窗口；窗口重建后 wid/pid 会变，必须每次核对。

        注意：这里**绝不**主动 activate 任何 App。之前为了找回被最小化的窗口去
        osascript 唤起应用，结果会弹系统授权、抢走焦点（还把微信拎到前台），
        而且 osascript 卡住时会把整个 HTTP 处理线程堵死（面板显示 E_NO_AGENT）。
        窗口没了就等它回来，由外部（用户）决定是否重开。
        """
        win = self.mac.find_game_window()
        if win is None:
            # 不在当前桌面？可能是窗口切到别的 Space / 被全屏应用挡在后面。
            # 这种状态下**不能点击**（点击会落到当前桌面），所以只提示、不建实例。
            elsewhere = self.mac.find_game_window(all_spaces=True)
            if elsewhere is not None:
                nowt = time.time()
                if nowt - getattr(self, "_offscreen_warn_at", 0) > 120:
                    self._offscreen_warn_at = nowt
                    self.push_log(None, "游戏窗口在其它桌面/全屏后面（当前桌面看不到），"
                                        "为避免点错，暂不操作；请把游戏窗口切到当前桌面", level="warn")
                return None
            # 窗口不在了：先把僵尸实例清掉（否则面板会一直显示"运行中"，
            # 而它的调度线程还在对已关闭的窗口瞎点）
            try:
                live = {w.get("wid") for w in self.mac.windows()}
                for iid, ins in list(self.instances.items()):
                    if ins.wid in live:
                        continue
                    ins.stop_flag.set()
                    ins.state = "stopped"
                    self.push_log(ins, f"窗口 {ins.wid} 已关闭 → 实例下线（任务已停）")
                    self.instances.pop(iid, None)
            except (AttributeError, TypeError):
                pass
            return None
        iid = f"{win['platform']}-{win['wid']}"
        inst = self.instances.get(iid)
        if inst is None:
            inst = Instance(win)
            self.instances[iid] = inst
            how = win.get("matched_by", "title")
            note = "（标题被系统隐去，按 owner 匹配；授权屏幕录制后会更稳）" if how == "owner" else ""
            self.push_log(inst, f"发现游戏窗口 wid={win['wid']} pid={win['pid']} {win['w']}x{win['h']}{note}")
            # 有启用的任务就自动开跑：agent 重启后不该静默停摆（否则面板显示「空闲、下次已过期」）
            if any(cfg.get("enabled") for cfg in inst.tasks.values()):
                inst.stop_flag.clear()
                inst.state = "running"
                inst.current_task = "main"
                inst.rev += 1
                inst.worker = threading.Thread(target=self.run_worker, args=(inst, "main", False),
                                               daemon=True)
                inst.worker.start()
                self.push_log(inst, "自动恢复调度：有启用任务，直接进入运行中")
        else:
            inst.pid = win["pid"]
            inst.window_rect = (win["x"], win["y"], win["w"], win["h"])
        # 清理已关闭窗口的僵尸实例：否则它的调度线程会一直「抓帧失败」刷屏，
        # 面板上还会多出一个永远红的实例。
        try:
            live = {w.get("wid") for w in self.mac.windows()}
            for iid, ins in list(self.instances.items()):
                if ins.wid in live:
                    continue
                ins.stop_flag.set()
                ins.state = "stopped"
                self.push_log(ins, f"窗口 {ins.wid} 已关闭 → 实例下线")
                self.instances.pop(iid, None)
        except (AttributeError, TypeError):
            pass
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
    # 收获结算：「总价值 1万」「经验值 +6781」
    _RE_TOTAL = re.compile(r"总价值\s*([0-9]+(?:\.[0-9]+)?)\s*(万)?")
    _RE_EXP = re.compile(r"经验(?:值)?\s*\+?\s*([0-9]+(?:\.[0-9]+)?)\s*(万)?")

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
            self.mac.click(inst.pid, sx, sy, mode=self.click_mode, wid=inst.wid, repeat=1)
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
                    self.mac.click(inst.pid, xs, ys, mode=self.click_mode, wid=inst.wid, repeat=1)
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

    def _seed_buttons(self, inst):
        """种子栏里的候选种子：种子栏中间那条带上的纯数字角标。

        必须先用「一键种植」这个标签确认种子栏真的开着 —— 否则商城/扩建弹窗里的
        数字（原价 80000、优惠价 56000…）会被误当成种子角标，点下去就买种子了。
        """
        texts = [str(b.get("text", "")).strip() for b in inst.ocr_blocks]
        if not any("一键种植" in t for t in texts):
            return []
        h = inst.img_size[1]
        lo, hi = h * 0.645, h * 0.735          # 角标那条窄带（实测 y≈1080/1542）
        nums = []
        for b in inst.ocr_blocks:
            t = str(b.get("text", "")).strip()
            if t.isdigit() and lo <= b["y"] <= hi and len(t) <= 3:
                nums.append((int(t), b))
        return sorted(nums, key=lambda z: z[1]["x"])

    def _bottom_button(self, inst, labels=("一键偷取", "一键务农", "一键收获", "一键施肥")):
        """按 OCR 找底部大按钮（图标在文字上方），返回 (label, ix, iy) 或 None。

        说明：底部这几个按钮共用同一个位置、图标相似，只能用**文字标签**区分；
        而且按钮的可点区域在文字**上方**（图标），所以要往上偏一点。
        """
        W, H = inst.img_size or (892, 1662)
        for lab in labels:
            b = self.find_block(inst, lab, min_conf=0.40)
            if b is None:
                continue
            ix = int(b["x"] + b["w"] / 2)
            iy = int(b["y"] + b["h"] / 2)
            return (lab, ix, max(0, iy - int(H * 0.024)))
        return None

    def _open_seed_bar(self, inst, cx, cy):
        """点一格，判断这一格到底是什么状态。

        返回 ("seeds", nums)  种子栏出现（=真的是空地）
             ("planted", text) 弹出的是倒计时/作物信息（=其实已经种上了）
             ("none", None)    什么都没有（多半是「一键种植」没开）
        """
        pw, ph = inst.img_size
        x0, y0, w, h = inst.window_rect
        tsx = x0 + cx * w / pw
        tsy = y0 + cy * h / ph
        self.mac.activate(inst.pid)
        self.refresh_windows()
        tsx = x0 + cx * w / pw
        tsy = y0 + cy * h / ph
        self.mac.click(inst.pid, tsx, tsy, mode=self.click_mode, wid=inst.wid, repeat=1)
        time.sleep(1.6)
        if not self.grab(inst):
            return "none", None
        nums = self._seed_buttons(inst)
        if nums:
            return "seeds", nums
        info = self._parse_plot_popup(inst.ocr_blocks)
        if info["crop"] or info["countdown"]:
            return "planted", str(info["crop"] or info["countdown"])
        return "none", None

    def _enable_one_click_plant(self, inst):
        """打开「一键种植」开关（确认弹窗文字读不到，靠位置点确认）。"""
        btn = next((b for b in inst.ocr_blocks if "一键种植" in str(b.get("text", ""))), None)
        if btn is None:
            return False
        pw, ph = inst.img_size
        x0, y0, w, h = inst.window_rect
        bsx = x0 + (btn["x"] + btn["w"] / 2) * w / pw
        bsy = y0 + (btn["y"] + btn["h"] / 2) * h / ph
        self.mac.click(inst.pid, bsx, bsy, mode=self.click_mode, wid=inst.wid, repeat=1)
        time.sleep(1.6)
        csx = x0 + 573 * w / pw
        csy = y0 + 880 * h / ph
        self.mac.click(inst.pid, csx, csy, mode=self.click_mode, wid=inst.wid, repeat=1)
        time.sleep(1.8)
        return True

    def plant(self, inst, seed="auto"):
        """原子操作：种植。

        流程（已实测的 UI 事实）：
          1. 找空地（cvhelper 分类为 empty）
          2. 点空地 → 底部弹出种子栏（每格带数量角标）
          3. 选一个种子：auto=数量最多（背包主力），event=数量最少（通常是新买的活动种），
             传数字（如 "18"）= 选数量角标正好是这个数的种子
          4. 选中后游戏会自动种满所有空地

        注意：**刚种下的种子和空地在画面上长得一样**（种子埋在土坑里），
        cvhelper 会把它们都判成 empty。所以点开这一格如果弹出的是倒计时/作物
        信息，说明这格已经种上了 → 换下一格再试，不要直接失败。
        """
        if self.user_busy_reason(inst):
            return False, "用户正在使用电脑，暂缓"
        if not self.grab(inst):
            return False, "抓帧失败"
        cells = self._cv_cells(inst)
        if not cells:
            # 全空地画面没有颜色差异，cvhelper 会失效 → 回退上次成功的晶格
            cells = getattr(inst, "last_cells", None) or []
            if not cells:
                return False, "田地识别失败（无历史晶格可供回退），未执行种植"
            self.push_log(inst, "cvhelper 失效，改用上次识别的晶格坐标", level="warn")
        empties = [c for c in cells if c.get("kind") == "empty" and c.get("cx")]
        if not empties:
            return True, f"识别到 {len(cells)} 格，确实没有空地"
        # 从田地中心向外试，最多 6 格（避开建筑/装饰，也避开"已经种了但看着像空地"的格子）
        cx0 = sum(c["cx"] for c in cells) / len(cells)
        cy0 = sum(c["cy"] for c in cells) / len(cells)
        empties.sort(key=lambda c: (c["cx"] - cx0) ** 2 + (c["cy"] - cy0) ** 2)
        nums, tried_planted = None, 0
        for target in empties[:6]:
            state, payload = self._open_seed_bar(inst, target["cx"], target["cy"])
            if state == "seeds":
                nums = payload
                break
            if state == "planted":
                tried_planted += 1
                self.push_log(inst, f"({target['row']},{target['col']}) 看着像空地，其实已种"
                                    f"（{payload}），换下一格", level="debug")
                continue
        if nums is None:
            # 种子栏没出现：可能「一键种植」没开 → 开一次再试最近的一格
            if not self._enable_one_click_plant(inst):
                if tried_planted:
                    return True, f"试了 {tried_planted} 格都是已种状态，没有真正可种的地"
                return False, "种子栏没出现，且找不到「一键种植」按钮"
            target = empties[min(tried_planted, len(empties) - 1)]
            state, payload = self._open_seed_bar(inst, target["cx"], target["cy"])
            if state != "seeds":
                if state == "planted":
                    return True, "已开启一键种植，但试到的格子都已种上（种子阶段肉眼与空地一致）"
                return False, "已开启一键种植，但种子栏仍未出现"
            nums = payload
        nums.sort(key=lambda x: x[0])
        pick = None
        seed_s = str(seed).strip()
        if seed_s.isdigit():                      # 明确指定数量角标（选那个种子）
            want = int(seed_s)
            pick = next((t for t in nums if t[0] == want), None)
            if pick is None:
                return False, f"种子栏里没有数量为 {want} 的种子（现有 {[c for c, _ in nums]}）"
        if pick is None:
            pick = nums[0] if seed_s == "event" else nums[-1]
        count, box = pick
        pw, ph = inst.img_size
        x0, y0, w, h = inst.window_rect
        bsx = x0 + (box["x"] + box["w"] / 2) * w / pw
        bsy = y0 + (box["y"] + box["h"] / 2) * h / ph
        self.mac.click(inst.pid, bsx, bsy, mode=self.click_mode, wid=inst.wid, repeat=1)
        time.sleep(2.5)
        self.push_log(inst, f"种植：选择种子 x{count}（策略 {seed}），已点选")
        return True, f"已点选种子 x{count}，等待自动种满"

    def _click(self, inst, sx, sy, wait=1.2, repeat=1, gap=0.0):
        """统一点击入口。

        auto 模式：先用 ghost（光标会还原，不打断用户），失败再升级到 warp；
        一旦 warp 成功就记住，后续直接用，避免每步都试探。
        """
        mode = self.click_mode
        if mode == "auto":
            mode = self.effective_click_mode
        if mode != "bg":
            self.mac.activate(inst.pid)     # bg 模式不需要激活，也就不抢焦点
        # 实测：ghost 单击常常被"第一次点击只激活窗口"吃掉（好友列表点「拜访」毫无反应），
        # 双击才稳；warp 单击即可，重复点可能触发别的行为。
        if mode == "ghost" and repeat < 2:
            repeat, gap = 2, max(gap, 0.12)
        self.mac.click(inst.pid, sx, sy, mode=mode, wid=inst.wid, repeat=repeat, gap=gap)
        return mode

    def _click_changed(self, inst, sx, sy, wait=1.2, repeat=1, gap=0.0):
        """点击并校验画面是否变；auto 模式按 bg → ghost → warp 逐级升级。"""
        # auto：ghost → warp（bg 对本容器无效，仅在显式 clickMode=bg 时使用）
        chain = (["ghost", "warp"] if self.effective_click_mode != "bg"
                 else ["bg", "ghost", "warp"]) if self.click_mode == "auto" else [self.click_mode]
        # 从"已认定有效"的那级开始试
        if self.click_mode == "auto" and self.effective_click_mode in chain:
            chain = chain[chain.index(self.effective_click_mode):]
        before = self.signature(inst)
        for mode in chain:
            self.mac.activate(inst.pid) if mode != "bg" else None
            self.mac.click(inst.pid, sx, sy, mode=mode, wid=inst.wid, repeat=repeat, gap=gap)
            time.sleep(wait)
            if self.grab(inst) and self.signature(inst) != before:
                if self.click_mode == "auto" and mode != self.effective_click_mode:
                    self.effective_click_mode = mode
                    self.push_log(inst, f"点击模式切换为 {mode}" +
                                  ("（真后台，不动光标）" if mode == "bg" else
                                   "（光标会还原）" if mode == "ghost" else "（会移动光标）"),
                                  level="info")
                return True, mode
        if self.click_mode == "auto":
            self.push_log(inst, "bg/ghost/warp 全部点不动（画面无变化）", level="warn")
        return False, chain[-1] if chain else "warp"

    def _click_xy(self, inst, ix, iy, wait=2.0, verify=True, activate=True):
        self.last_action_at = time.time()
        """按图像坐标点一下（先换算再激活再单点）。

        verify=False 时不再抓帧（抓一帧要截图+sips+hash+OCR ≈1.5s，盲点流程全靠它提速）。
        """
        sx, sy = self.to_screen(inst, ix, iy)
        if not self._in_window(inst, sx, sy):
            return False, "点击点越界"
        if activate:
            self._click(inst, sx, sy, wait=0, repeat=1)
        time.sleep(wait)
        if not verify:
            return True, "已点击(未校验)"
        ok = self.grab(inst)
        return ok, "已点击"

    def _friend_rows(self, inst):
        """从好友列表页解析 [(好友名, 拜访按钮)]，按 y 排序。"""
        visits = [b for b in inst.ocr_blocks if str(b["text"]).strip() == LABEL_VISIT]
        rows = []
        for v in visits:
            vy = v["y"] + v["h"] / 2
            name = ""
            for b in inst.ocr_blocks:
                t = str(b["text"]).strip()
                by = b["y"] + b["h"] / 2
                if abs(by - vy) < 26 and b["x"] + b["w"] < v["x"] - 40 \
                        and t and not t.isdigit() and "QQ" not in t and "经典农场" not in t:
                    if len(t) > len(name):
                        name = t
            rows.append((name, v))
        rows.sort(key=lambda r: r[1]["y"])
        return rows

    def _bottom_friends(self, inst):
        """好友农场底部的「好友头像条」：[(昵称, x, y)]（y 用头像中心）。

        在这条上点另一个头像就能直接切到那家农场，不用先回家。
        """
        out = []
        h = inst.img_size[1]
        for b in inst.ocr_blocks:
            t = str(b["text"]).strip()
            if not t or t.isdigit():
                continue
            if "QQ" in t or "经典农场" in t or "ID:" in t:
                continue
            if h * 0.88 < b["y"] < h * 0.96 and b["w"] < 200:
                out.append((t, int(b["x"] + b["w"] / 2), int(b["y"] - h * 0.028)))
        out.sort(key=lambda r: r[1])
        return out

    def _farm_header_name(self, inst):
        """好友农场页头部的好友昵称。"""
        best = ""
        for b in inst.ocr_blocks:
            t = str(b["text"]).strip()
            if not t or t.isdigit() or "QQ" in t or "经典农场" in t:
                continue
            if 150 < b["y"] < 260 and b["w"] < 260 and len(t) > len(best):
                best = t
        return best

    def _cv_python(self):
        """找带 cv2/numpy 的 Python（cvhelper 用）。"""
        for cand in (os.environ.get("QQFARM_CV_PYTHON"), "/usr/local/bin/python3",
                     "/opt/homebrew/bin/python3"):
            if cand and Path(cand).exists():
                return cand
        return None

    def _icon_matches(self, inst, tpl, threshold=0.62):
        """模板匹配（走 cvhelper --icons，能用 cv2 一次找全所有图标）。

        macd find 只返回分数最高的那一个图标（多尺度三连），好友列表里 5 个图标
        只能找到 1 个，所以这里改走 cvhelper。
        """
        py = self._cv_python()
        helper = HERE / "cvhelper.py"
        if py is None or not helper.exists():
            return []
        cmd = [py, str(helper), "--icons", str(inst.tmp_png), "--thr", str(threshold)]
        if tpl is not None and Path(tpl) != ICON_HELP:
            cmd += ["--tpl", str(tpl)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            data = json.loads(r.stdout.strip().splitlines()[-1])
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, IndexError):
            return []
        if not data.get("ok"):
            return []
        return [(float(i["cx"]), float(i["cy"]), float(i["score"])) for i in data.get("icons") or []]

    def _rows_needing_help(self, inst):
        """好友列表里「有铲土图标」的行：[(名字, 拜访按钮)]，按 y 排序。"""
        icons = self._icon_matches(inst, ICON_HELP)
        if not icons:
            return []
        out = []
        for (name, b) in self._friend_rows(inst):
            cy = b["y"] + b["h"] / 2
            for (x, y, _) in icons:
                if abs(y - cy) < 55 and 330 < x < 470:
                    out.append((name, b))
                    break
        return out

    # ── 仓库种子（C）──────────────────────────────────────────────────
    # 注意：**不能**把「种子」放进跳过词——每种子的名字都以「…种子」结尾（曾因此全部读不出来）
    _SEED_NAME_SKIP = ("单价", "成熟", "可收", "季产", "经验", "收入", "概率", "获得",
                       "出售", "批量", "锁定", "格子上限", "果实：", "道具")

    def _seed_name_from_panel(self, inst):
        """从仓库详情面板读种子名（实测在面板最上方，y≈0.71H 附近的中文块）。"""
        H = (inst.img_size or (892, 1662))[1]
        best = None
        for b in inst.ocr_blocks:
            t = str(b["text"]).strip()
            if not (3 <= len(t) <= 10):
                continue
            if any(k in t for k in self._SEED_NAME_SKIP):
                continue
            if 0.66 * H < b["y"] < 0.78 * H and b["x"] < 0.55 * (inst.img_size[0]):
                if best is None or b["y"] < best[0]:
                    best = (b["y"], t)
        return best[1] if best else None

    def read_seeds(self, inst):
        """读仓库「种子」页：逐槽点开 → 详情面板名称 + 槽位数量 → {名称: 数量}。

        实测布局（892×1662）：
          标签 果实/超变果实/种子/道具 在 y≈0.18H；卡片 5 列，数量在 y≈0.35H 一带；
          底部详情面板给出「名称 / 单价 / 成熟 / 可收 / 经验 / 收入」（含「出售」按钮＝红线，不点）。
        """
        self._ensure_main(inst)
        W, H = inst.img_size or (892, 1662)
        self._click_xy(inst, int(W * 0.060), int(H * 0.938), wait=1.6)     # 底部「仓库」
        if not self._wait_for(inst, ["格子上限", "果实"], timeout=4.0, step=0.35):
            self.push_log(inst, "仓库：打不开", level="warn")
            return 0, "打不开仓库"
        tab = self.find_block(inst, "种子", min_conf=0.40)
        if tab is not None:
            self._tap_text(inst, "种子", wait=1.3)
        if not self.grab(inst):
            return 0, "抓帧失败"
        # 槽位数量 = 网格里的纯数字（y 在 0.30~0.45H）
        slots = []
        for b in inst.ocr_blocks:
            t = str(b["text"]).strip()
            if t.isdigit() and 0.26 * H < b["y"] < 0.52 * H and b["w"] < 90:
                slots.append((int(b["x"] + b["w"] / 2), int(b["y"] - 55), int(t)))
        slots.sort(key=lambda s: (s[1] // 40, s[0]))
        seeds, seen = {}, set()
        for (cx, cy, cnt) in slots[:20]:
            self._click_xy(inst, cx, cy, wait=1.0)
            if not self.grab(inst):
                break
            name = self._seed_name_from_panel(inst)
            if not name or name in seen:
                continue
            seen.add(name)
            seeds[name] = seeds.get(name, 0) + cnt
            self.push_log(inst, f"仓库种子：{name} × {cnt}")
        inst.seeds = seeds
        self.push_log(inst, f"仓库种子：共 {len(seeds)} 种 → " +
                            "、".join(f"{k}×{v}" for k, v in list(seeds.items())[:10]))
        self._ensure_main(inst, tries=3)
        return (1, "已读取种子库存") if seeds else (0, "没解析出种子")

    # ── 具名模板匹配（templates/manifest.json）──────────────────────────
    def find_named(self, inst, name, x_range=None, y_range=None):
        """按 manifest 里的名字找模板（多尺度 + NMS），返回 [(cx, cy, score)]。"""
        py = self._cv_python()
        helper = HERE / "cvhelper.py"
        if py is None or not helper.exists():
            return []
        cmd = [py, str(helper), "--find", str(inst.tmp_png), name]
        if x_range:
            cmd += ["--x", "%d,%d" % (int(x_range[0]), int(x_range[1]))]
        if y_range:
            cmd += ["--y", "%d,%d" % (int(y_range[0]), int(y_range[1]))]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            data = json.loads(r.stdout.strip().splitlines()[-1])
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, IndexError):
            return []
        if not data.get("ok"):
            return []
        return [(float(m["cx"]), float(m["cy"]), float(m["score"])) for m in data.get("matches") or []]

    def click_named(self, inst, name, wait=1.2, verify=True, index=0):
        """按名字点模板命中处；未命中返回 (False, 原因)。"""
        hits = self.find_named(inst, name)
        if not hits:
            return False, "模板未命中 %s" % name
        hits.sort(key=lambda h: -h[2])
        if index >= len(hits):
            return False, "命中数不足 %s" % name
        cx, cy, sc = hits[index]
        ok = self._click_xy(inst, cx, cy, wait=wait, verify=verify)
        self.push_log(inst, f"模板点击「{name}」({cx:.0f},{cy:.0f}) score={sc:.2f}")
        return True, f"{name} score={sc:.2f}"

    # ── 页面纠偏：点错了自己退回来 ─────────────────────────────────────
    def _ensure_main(self, inst, tries=4, timeout=2.5):
        """确保回到农场主页；进错页（SVIP/仓库/活动/好友）就依次尝试退出。

        返回 (是否在主页, 说明)。退出手段按安全顺序：
          ① OCR 里的「关闭 / 返回 / 回家 / 我知道了」 ② 右上角 × 图标（模板匹配）
        红线：绝不点「开通会员 / 分享 / 购买 / 出售」这类按钮。
        """
        for i in range(tries):
            if not self.grab(inst):
                return False, "抓帧失败"
            page = inst.page
            if page == "main":
                return True, "已在主页"
            # ⓪ 活动页（左上角返回箭头）
            if page in ("event_shop", "svip"):
                if self._leave_event(inst) or (page == "svip" and False):
                    continue
            # ① 文字按钮
            for label in ("关闭", "返回", "回家", "我知道了", "取消"):
                if self.find_block(inst, label, min_conf=0.4):
                    self._tap_text(inst, label, wait=0.9, verify=False)
                    break
            else:
                # ② × 图标
                hit = self.mac.find_icon(str(inst.tmp_png), str(ICON_CLOSE), threshold=0.78)
                if hit:
                    self._click_xy(inst, hit["cx"], hit["cy"], wait=0.9, verify=False)
                elif self.click_named(inst, "btn.home", wait=1.0, verify=False)[0]:
                    self.grab(inst)
                    continue
                else:
                    # ③ 固定坐标兜底：好友农场/好友列表的「回家」在右下角（OCR 偶尔认不出）
                    w, h = inst.img_size or (824, 1542)
                    self._click_xy(inst, int(w * 0.867), int(h * 0.772), wait=1.0, verify=False)
                    self.grab(inst)
                    if inst.page == "main":
                        continue
                    # ④ 点空白处（弹窗通用关法）
                    self._close_popup(inst)
            self.grab(inst)
        ok = inst.page == "main"
        if not ok:
            self.push_log(inst, f"页面纠偏失败：当前仍是「{inst.page}」", level="warn")
        return ok, ("已在主页" if ok else f"仍是 {inst.page}")

    def _read_settlement(self, inst):
        """读刚弹出的收获结算（总价值/经验值），并记进当天统计。返回 (价值, 经验) 或 None。"""
        joined = " ".join(str(b["text"]) for b in inst.ocr_blocks)
        mt = self._RE_TOTAL.search(joined)
        me = self._RE_EXP.search(joined)
        if not mt and not me:
            return None
        total = 0.0
        if mt:
            total = float(mt.group(1)) * (10000 if mt.group(2) else 1)
        exp = 0.0
        if me:
            exp = float(me.group(1)) * (10000 if me.group(2) else 1)
        self.bump_daily_amount(inst, "income", int(total))
        self.bump_daily_amount(inst, "expGain", int(exp))
        self.push_log(inst, f"收获结算：总价值 {int(total)}、经验 +{int(exp)}")
        return int(total), int(exp)

    def _harvest_ripe_by_tap(self, inst):
        """没有「一键收获」时逐株收：点「可摘」下方的作物本体（点树冠无效）。"""
        done = 0
        for _ in range(4):
            badge = self.find_block(inst, "可摘", min_conf=0.40)
            if badge is None:
                break
            ix = badge["x"] + badge["w"] / 2 + 20
            iy = badge["y"] + badge["h"] / 2 + 85
            self._click_xy(inst, ix, iy, wait=1.5)
            if not self.grab(inst):
                break
            if self.find_block(inst, "可摘", min_conf=0.40) is None:
                done += 1
                self.bump_daily(inst, "harvest")
                self._read_settlement(inst)
                self.push_log(inst, f"逐株收获：第 {done} 株 ✓")
            else:
                break
        return done

    def _count_empty(self, inst):
        """用 cvhelper 数「空地」格数（含草/未识别都算需要处理）；失败返回 None。"""
        cells = self._cv_cells(inst)
        if cells is None:
            return None
        return sum(1 for c in cells if (c.get("kind") or "empty") in ("empty", "grass", "unknown", None))

    def _crop_name_at(self, inst, cx=None, cy=None):
        """点开一格读弹窗顶部的作物名（如「草莓」「狗尾草」）。"""
        W, H = inst.img_size or (822, 1542)
        self._click_xy(inst, int(cx if cx is not None else W * 0.487),
                       int(cy if cy is not None else H * 0.545), wait=1.2)
        if not self.grab(inst):
            return None
        # 弹窗结构（实测）：作物名在最上面，下面紧跟「经验 / 收入 / 种子 / 初熟」
        base = None
        for b in inst.ocr_blocks:
            if str(b["text"]).strip() == "经验":
                base = b["y"]
                break
        if base is None:
            return None
        best = None
        for b in inst.ocr_blocks:
            t = str(b["text"]).strip()
            if not (1 < len(t) <= 6) or t.isdigit():
                continue
            if any(k in t for k in ("经验", "收入", "种子", "发芽", "成熟", "初熟", "小时", "分钟")):
                continue
            if 5 < base - b["y"] < 70 and b["x"] < 300:
                if best is None or b["y"] < best[0]:
                    best = (b["y"], t)
        return best[1] if best else None

    def _open_seed_bar_any(self, inst):
        """挨个试格心，直到唤出种子栏（返回 True 表示「一键种植」出现了）。"""
        W, H = inst.img_size or (822, 1542)
        cells = self._cv_cells(inst) or []
        order = ([c for c in cells if (c.get("kind") or "") == "empty"]
                 + [c for c in cells if (c.get("kind") or "") != "empty"])[:12]
        probes = [(int(c["cx"]), int(c["cy"])) for c in order] or [
            (int(W * 0.487), int(H * 0.570)), (int(W * 0.42), int(H * 0.605)),
            (int(W * 0.55), int(H * 0.605))]
        for (x, y) in probes:
            self._click_xy(inst, x, y, wait=1.2)
            if not self.grab(inst):
                return False
            if self.find_block(inst, "一键种植", min_conf=0.40):
                return True
        return False

    def plant_seed(self, inst, name=None):
        """按名字种：模板命中种子卡 → 点 → 读弹窗作物名校验。

        没配 `seed.<name>` 模板时返回 False（调用方退回角标流程）。
        模板未命中时**绝不乱点**（这是上次把狗尾草当草莓的教训）。
        """
        name = str(name or inst.settings.get("seedName", "草莓"))
        tpl = "seed." + name
        if not self.find_named(inst, tpl):
            return False, f"没有模板 {tpl}（先去 C1 校准 + 裁模板）"
        if not self._open_seed_bar_any(inst):
            return False, "打不开种子栏"
        hits = self.find_named(inst, tpl)
        if not hits:
            # 可能要翻页：按 seedPage 翻页后再找
            W, H = inst.img_size or (822, 1542)
            for _ in range(max(0, int(inst.settings.get("seedPage", 2) or 2) - 1)):
                self._click_xy(inst, int(W * 0.92), int(H * 0.682), wait=0.9, verify=False)
                self.grab(inst)
            hits = self.find_named(inst, tpl)
        if not hits:
            return False, f"当前页没找到 {tpl}"
        cx, cy, sc = sorted(hits, key=lambda h: -h[2])[0]
        before = self._exp_now(inst)
        self._click_xy(inst, cx, cy, wait=1.7)
        if not self.grab(inst):
            return False, "抓帧失败"
        after = self._exp_now(inst)
        got = self._crop_name_at(inst)
        inst.last_crop = got
        if got and name not in got:
            self.push_log(inst, f"按模板种失败：实际是「{got}」而非「{name}」", level="warn")
            return False, f"作物不符（{got}）"
        if before is not None and after is not None and after <= before and not got:
            return False, "点了没经验增长、也读不到作物名"
        self.bump_daily(inst, "plant")
        self.push_log(inst, f"按模板种「{name}」成功（score={sc:.2f}，作物={got or '?'}）")
        return True, f"已种 {name}"

    def _replant(self, inst):
        """收完补种：点空地 → 种子栏 → 依次试种子卡，直到「点卡没经验增长」为止。

        实测（2026-10-24）：
          - 一键种植开启时，点任意种子卡会把**所有能种的空地**一次种满并加经验
          - 草莓（活动作物）在第 2 页第 1 张卡
          - **种子数量不足时会剩空地**：所以种完要换下一张卡继续，直到点卡不再有经验增长
            （注意：刚种下的幼苗看起来和空地一样，不能用 cvhelper 的 empty 判定来复查）
        """
        W, H = inst.img_size or (822, 1542)
        seed_page = int(inst.settings.get("seedPage", 1) or 1)
        # C3：库存已知时，先确认想要的种子有货；没货就降级到有货的任意种子
        want_name = str(inst.settings.get("seedName", "") or "")
        stock = getattr(inst, "seeds", None)
        if stock:
            if want_name not in stock:
                alt = max(stock.items(), key=lambda kv: kv[1])[0]
                self.push_log(inst, f"补种：库存里没「{want_name}」，改用最多的「{alt}」×{stock[alt]}",
                              level="warn")
                inst.settings["seedName"] = alt
            elif stock[want_name] <= 0:
                alt = max(stock.items(), key=lambda kv: kv[1])[0]
                self.push_log(inst, f"补种：「{want_name}」已用完，改用「{alt}」", level="warn")
                inst.settings["seedName"] = alt
        card_fx = (0.16, 0.32, 0.48, 0.63, 0.79)
        arrow = (int(W * 0.92), int(H * 0.682))

        # 点空地才会弹种子栏；点已种的格子只弹倒计时。所以用 cvhelper 的格心逐个试，
        # 先试被分类为 empty 的，再试其它（刚种的幼苗也会被分类成 empty，所以都试）。
        cells = self._cv_cells(inst) or []
        order = ([c for c in cells if (c.get("kind") or "") == "empty"]
                 + [c for c in cells if (c.get("kind") or "") != "empty"])[:12]

        def open_bar():
            if not order:
                for (fx, fy) in ((0.487, 0.570), (0.42, 0.605), (0.55, 0.605)):
                    self._click_xy(inst, int(W * fx), int(H * fy), wait=1.2)
                    if self.grab(inst) and self.find_block(inst, "一键种植", min_conf=0.4):
                        return True
                return False
            for c in order:
                self._click_xy(inst, int(c["cx"]), int(c["cy"]), wait=1.2)
                if not self.grab(inst):
                    return False
                if self.find_block(inst, "一键种植", min_conf=0.4):
                    return True
            return False

        def card_points():
            """用种子卡上数量角标的 OCR 位置定位卡（固定 x 会点歪）。"""
            pts = []
            for b in inst.ocr_blocks:
                t = str(b["text"]).strip()
                if t.isdigit() and 930 < b["y"] < 1160 and b["w"] < 90:
                    pts.append((int(b["x"] + b["w"] / 2), int(b["y"] + b["h"] / 2)))
            return sorted(pts)

        def tap_card(fx):
            before = self._exp_now(inst)
            pts = card_points()
            if pts:
                self._click_xy(inst, pts[0][0], pts[0][1], wait=1.6)
            else:
                self._click_xy(inst, int(W * fx), int(H * 0.672), wait=1.6)
            if not self.grab(inst):
                return -1
            after = self._exp_now(inst)
            if before is None or after is None:
                return 0
            return int(after - before)

        # A4b：配了种子模板就按名字种（可校验），否则走角标流程
        if self.find_named(inst, "seed." + want_name):
            ok, why = self.plant_seed(inst, want_name)
            if ok:
                return 1, why
            self.push_log(inst, f"按模板种失败（{why}），退回角标流程", level="warn")
        if not open_bar():
            return 0, "没弹出种子栏"
        for _ in range(max(0, seed_page - 1)):
            self._click_xy(inst, arrow[0], arrow[1], wait=0.8, verify=False)
            self.grab(inst)

        planted, misses = 0, 0
        for attempt in range(12):
            gained = tap_card(card_fx[attempt % len(card_fx)])
            if gained > 0:
                planted += 1
                misses = 0
                self.bump_daily(inst, "plant")
                self.push_log(inst, f"补种：第 {attempt + 1} 张卡 → 经验 +{gained}")
            else:
                misses += 1
                self.push_log(inst, f"补种：第 {attempt + 1} 张卡没反应（剩余空地可能已种满或该种子用完）",
                              level="debug")
                if misses >= 2:
                    break
            # 种子栏可能已关（种满即关），需要继续就重新打开
            if self.find_block(inst, "一键种植", min_conf=0.4) is None:
                if not open_bar():
                    break
            if (attempt + 1) % len(card_fx) == 0:
                self._click_xy(inst, arrow[0], arrow[1], wait=0.8, verify=False)
                self.grab(inst)
        if planted:
            # 校验种下去的是什么：弹窗顶部会显示作物名（曾把「狗尾草」当成草莓种满全田）
            want = str(inst.settings.get("seedName", "") or "")
            got = self._crop_name_at(inst)
            inst.last_crop = got
            if got and want and want not in got:
                self.push_log(inst, f"补种校验失败：实际种的是「{got}」，期望「{want}」"
                                    f"（seedPage/卡位置需要修正）", level="warn")
                return 1, f"已补种 {planted} 次，但作物是「{got}」而非「{want}」"
            if got:
                self.push_log(inst, f"补种校验通过：作物「{got}」")
            return 1, f"已补种 {planted} 次（作物 {got or '?'}）"
        return 0, "补种未生效"

    def _exp_now(self, inst):
        """从 HUD 读当前经验（形如 1.1万/4.7万 或 3432/4.7万）→ 数字。"""
        for b in inst.ocr_blocks:
            t = str(b["text"])
            if "/" in t and ("万" in t or t.replace("/", "").isdigit()):
                cur = t.split("/")[0]
                try:
                    if cur.endswith("万"):
                        return float(cur[:-1]) * 10000
                    return float(cur)
                except ValueError:
                    continue
        return None

    def _fertilize(self, inst):
        """施肥：点开一个已种地块 → 弹窗里的「一键施肥」→ 校验倒计时变短/经验增长。

        为什么要点地块：「一键施肥」不是主页常驻按钮，它在**地块弹窗**里
        （2026-10-23 实测位置 608,1155，但以 OCR 实际位置为准，避免漂移）。
        """
        cells = self._cv_cells(inst) or []
        # 优先挑已经种了的地块（cvhelper 分类 crop），没有就挨个试
        order = ([c for c in cells if (c.get("kind") or "") == "crop"]
                 + [c for c in cells if (c.get("kind") or "") != "crop"])[:8]
        if not order:
            return 0, "没有可点地块"
        for c in order:
            self._click_xy(inst, int(c["cx"]), int(c["cy"]), wait=1.2)
            if not self.grab(inst):
                return 0, "抓帧失败"
            btn = self.find_block(inst, "一键施肥", min_conf=0.40)
            if btn is None:
                continue
            before = self._popup_cd_seconds(inst)
            self._tap_text(inst, "一键施肥", wait=1.6)
            if not self.grab(inst):
                return 0, "抓帧失败"
            after = self._popup_cd_seconds(inst)
            gain = (before - after) if (before is not None and after is not None) else 0
            gone = self.find_block(inst, "一键施肥", min_conf=0.40) is None
            if gain >= 60 or gone:
                self.push_log(inst, f"施肥：倒计时 {before}→{after} 秒（缩短 {gain}s，按钮消失={gone}）")
                return 1, "已施肥"
            self.push_log(inst, f"施肥：点了但倒计时只 -{gain}s 且按钮还在，判为无效", level="warn")
            return 0, "施肥无效"
        return 0, "没找到「一键施肥」"

    def _popup_cd_seconds(self, inst):
        """读弹窗里的「xx:xx:xx 后成熟」→ 秒（与被用作静态解析的 `_cd_seconds(text)` 区分）。"""
        joined = " ".join(str(b["text"]) for b in inst.ocr_blocks)
        m = self._RE_CD.search(joined)
        if not m:
            return None
        h, mi, se = (int(x) for x in m.group(1).split(":"))
        return h * 3600 + mi * 60 + se

    # ── 活动页（只读）──────────────────────────────────────────────────
    def _leave_event(self, inst):
        """活动页返回：左上角返回箭头（实测 0.067W, 0.144H）。"""
        W, H = inst.img_size or (822, 1542)
        for (fx, fy) in ((0.067, 0.144), (0.05, 0.14)):
            self._click_xy(inst, int(W * fx), int(H * fy), wait=1.2, verify=False)
            if self._wait_for(inst, ["仓库", "商店"], timeout=3.0, step=0.3):
                return True
        return False

    def read_event(self, inst):
        """读当期活动（只读）：店铺等级 / 营业额 / 倒计时；读完退回主页。

        红线：不点「兑换商店」「制作奶茶」「店铺升级」这类会花资源/改变状态的按钮。
        """
        self._ensure_main(inst)
        if not self.grab(inst):
            return False, "抓帧失败"
        block = None
        for txt in ("奶茶热销中", "热销中", "奶茶"):
            block = self.find_block(inst, txt, min_conf=0.35)
            if block:
                break
        if block is None:
            return False, "主页没有活动入口"
        self._click_xy(inst, block["x"] + block["w"] / 2, block["y"] + block["h"] / 2, wait=2.2)
        self._wait_for(inst, ["店铺等级", "营业额"], timeout=5.0, step=0.4)
        if not self.grab(inst):
            return False, "抓帧失败"
        icons = inst.ocr_blocks

        def digits(t):
            d = re.sub(r"[^0-9]", "", str(t))
            return int(d) if d else None

        # 店铺等级：数字在「店铺等级」标签正上方
        lv = None
        for b in icons:
            if "店铺等级" in str(b["text"]):
                for c in icons:
                    t = str(c["text"]).strip()
                    if (t.isdigit() and abs(c["x"] - b["x"]) < 70
                            and 0 < b["y"] - c["y"] < 70):
                        lv = int(t)
                        break
        # 营业额：「营业额：` + 数字（可能被 OCR 混在一起）
        turnover = None
        for b in icons:
            t = str(b["text"])
            if "营业额" in t:
                turnover = digits(t)
                if turnover is None:
                    for c in icons:
                        if abs(c["y"] - b["y"]) < 26 and c["x"] > b["x"]:
                            v = digits(c["text"])
                            if v is not None:
                                turnover = v
                                break
                break
        # 倒计时：21天6时 / 明日04:10
        cd = None
        for b in icons:
            m = re.search(r"\d+\s*天\s*\d+\s*[时小]|\d{1,2}:\d{2}", str(b["text"]))
            if m:
                cd = m.group(0).strip()
                break
        info = {
            "name": "奶茶热销中" if any("奶茶" in str(b["text"]) for b in icons) else "活动",
            "shopLevel": lv, "turnover": turnover, "countdown": cd,
            "page": inst.page,
        }
        inst.event = info
        self.push_log(inst, f"活动页：{info['name']} 店铺{lv if lv is not None else '?'}级 "
                            f"营业额{turnover if turnover is not None else '?'} 倒计时 {cd or '?'}")
        left = self._leave_event(inst)
        if not left:
            self._ensure_main(inst, tries=3)
        return True, (f"{info['name']} 店铺 {lv if lv is not None else '?'} 级，"
                      f"营业额 {turnover if turnover is not None else '?'}，剩余 {cd or '?'}")

    def _handle_help_card(self, inst):
        """主页右下角「好友求助」卡片（小果 好友求助 🤚）：点进去偷/帮忙，再回家。

        卡片会自动消失，所以每次巡检都看一眼；点卡片本体（文字左侧），避开右上角的 ×。
        """
        block = self.find_block(inst, "好友求助", min_conf=0.40)
        if block is None:
            return 0, "no-card"
        name = ""
        for b in inst.ocr_blocks:
            t = str(b["text"]).strip()
            if b["y"] < block["y"] - 20 and b["x"] < block["x"] and t and not t.isdigit():
                name = t
        # 点卡片头像区（文字左侧 ~45px）；卡片右上角有 ×（约 +190,-35），绝不能点那
        ix, iy = block["x"] - 45, block["y"] + 4
        self.push_log(inst, f"好友求助卡片：{name or '?'} → 点进去处理 ({ix},{iy})")
        self._click_xy(inst, ix, iy, wait=1.2, verify=False)
        if not self._wait_for(inst, [LABEL_HOME, LABEL_STEAL], timeout=5.0, step=0.3):
            self.push_log(inst, "好友求助卡片：没进到好友农场，跳过", level="warn")
            return 0, "no-page"
        did = 0
        w, h = inst.img_size or (822, 1542)
        fx, fy = int(w * 0.517), int(h * 0.746)     # 图标中心（点文字中心无效）
        if self.find_block(inst, LABEL_STEAL):
            self._click_xy(inst, fx, fy, wait=1.6)
            if self.grab(inst) and not self.find_block(inst, LABEL_STEAL):
                did += 1
                self.bump_daily(inst, "steal")
                self.push_log(inst, f"好友求助「{name or '?'}」：偷菜成功")
        if self.find_block(inst, LABEL_HELP):
            self._click_xy(inst, fx, fy, wait=1.6)
            if self.grab(inst) and not self.find_block(inst, LABEL_HELP):
                did += 1
                self.bump_daily(inst, "help")
                self.push_log(inst, f"好友求助「{name or '?'}」：一键务农（帮忙）✓")
        self._ensure_main(inst, tries=2)
        return did, "ok"

    # ── 好友：偷菜 / 帮忙 ─────────────────────────────────────────────
    def _tap_text(self, inst, text, wait=1.6, min_conf=0.45, exact=True, verify=True):
        self.last_action_at = time.time()
        """点一个 OCR 文本块，返回 (ok, block, why)。

        exact=True 时要求文字完全相等（避免把「好友」点成「好友求助」）。
        """
        block = None
        for b in inst.ocr_blocks:
            t = str(b["text"]).strip()
            if b["conf"] < min_conf:
                continue
            if (t == text) if exact else (text in t):
                if block is None or b["conf"] > block["conf"]:
                    block = b
        if block is None:
            return False, None, f"没找到「{text}」"
        ix, iy = block["x"] + block["w"] / 2, block["y"] + block["h"] / 2
        sx, sy = self.to_screen(inst, ix, iy)      # 用截图那一刻的窗口矩形换算
        if not self._in_window(inst, sx, sy):
            return False, block, f"「{text}」点击点越界"
        before = self.signature(inst)
        # 与 /click 原语保持一致：activate 后立刻单点（refresh 后再点反而会丢）
        self._click(inst, sx, sy, wait=0, repeat=1)
        time.sleep(wait)
        ok = self.grab(inst) if verify else True
        if ok:
            inst.page = self.classify(inst)
        changed = ok and verify and self.signature(inst) != before
        self.push_log(inst, f"点击「{text}」({ix:.0f},{iy:.0f})→({sx:.0f},{sy:.0f}) "
                            f"{'画面变化' if changed else '无变化'}（页面 {inst.page}）", action=text)
        return changed, block, "已点击"

    def _wait_for(self, inst, texts, timeout=5.0, step=0.7):
        """等画面出现这些文字之一（好友农场/任务面板加载要一两秒）。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if any(any(t in str(b["text"]) for b in inst.ocr_blocks) for t in texts):
                return True
            time.sleep(step)
            if not self.grab(inst):
                return False
        return any(any(t in str(b["text"]) for b in inst.ocr_blocks) for t in texts)

    def _open_friend_list(self, inst):
        """回主页 → 点底部「好友」→ 等好友列表。"""
        if inst.page != "main":
            okm, whym = self._ensure_main(inst, tries=3)
            if not okm:
                self.push_log(inst, f"回不到主页（{whym}），放弃开好友列表", level="warn")
                return False
        for attempt in range(4):
            if any(LABEL_VISIT in str(b["text"]) for b in inst.ocr_blocks):
                return True
            self._tap_text(inst, NAV_FRIEND[0], wait=0.6)
            if self._wait_for(inst, [LABEL_VISIT], timeout=3.5, step=0.3):
                return True
        return False

    def visit_friends(self, inst, max_friends=12):
        """好友（脚本盲点版）：只在开跑和收尾各看一次屏，中间全程固定坐标点击。

        为什么快：抓一帧（截图+sips+hash+OCR）约 1.5s，原来每一步都抓一帧；
        现在一轮里只做 2 次识别（好友列表图标 + 收尾复检），其余全是
        「等 1.2s → 点固定坐标」，单个好友 ~2.7s。
        """
        t_start = time.time()
        okp, why = self._ensure_main(inst)
        if not okp:
            self.push_log(inst, f"好友任务：{why}", level="warn")
            return False, f"不在农场主页（{why}）"
        W, H = inst.img_size if inst.img_size else (824, 1542)
        sx = lambda fx: int(fx * W)           # 固定坐标按分辨率比例给
        sy = lambda fy: int(fy * H)
        # 实测：按钮的**图标**在文字右上方约 (+63,-28)；点文字中心游戏不响应
        POS_FARM = (sx(0.517), sy(0.746))     # 「一键务农/一键偷取」图标中心 (822,1542 → 425,1150)
        POS_HOME = (sx(0.894), sy(0.770))     # 「回家」图标中心 (735,1187)
        POS_FRIEND_NAV = (sx(0.856), sy(0.938))   # 底部导航「好友」
        strip_xs = [sx(0.18), sx(0.365), sx(0.55), sx(0.73), sx(0.90)]
        strip_y = sy(0.905)                   # 好友农场底部头像条

        if not self.grab(inst):
            return False, "抓帧失败"
        card_bonus = 0
        if inst.page == "main":
            card_bonus, _ = self._handle_help_card(inst)
        if not self._open_friend_list(inst):
            self.fail(inst, "E_PAGE_UNKNOWN", "打不开好友列表（没出现「拜访」）")
            return False, "打不开好友列表"
        time.sleep(0.6)                 # 列表还在滑入时扫图标会错位
        if not self.grab(inst):
            return False, "抓帧失败"
        icons = self._icon_matches(inst, ICON_HELP)
        thieves = self._icon_matches(inst, ICON_STEAL)
        rows = self._friend_rows(inst)
        if not rows:
            # OCR 解析不出「拜访」行 → 用模板命中兜底（模板定位比 OCR 稳）
            hits = sorted(self.find_named(inst, "btn.visit"), key=lambda h: h[1])[:6]
            rows = [("?", {"x": h[0] - 30, "y": h[1] - 12, "w": 60, "h": 24}) for h in hits]
            if hits:
                self.push_log(inst, f"好友列表：OCR 无行，用模板命中 {len(hits)} 个「拜访」")
        if icons or thieves:
            self.push_log(inst, f"图标：可帮 {len(icons)} 个 / 可偷 {len(thieves)} 个")
        targets = []
        for (name, b) in rows:
            cy = b["y"] + b["h"] / 2
            can_help = any(abs(y - cy) < 62 and 340 < x < 435 for (x, y, _) in icons)
            can_steal = any(abs(y - cy) < 62 and 360 < x < 450 for (x, y, _) in thieves)
            if can_help or can_steal:
                targets.append((name, b))
        if not targets:
            self.push_log(inst, f"好友列表：没有带铲土图标的好友（图标命中 {len(icons)} 个）")
            hit = self.mac.find_icon(str(inst.tmp_png), str(ICON_CLOSE), threshold=0.80)
            if hit:
                self._click_xy(inst, hit["cx"], hit["cy"], wait=0.8)
            return True, "没有需要帮忙的好友（已退出列表）"

        self.push_log(inst, f"好友：{len(targets)} 位带铲土图标 → " +
                            "、".join(n for n, _ in targets[:8]))
        helped, stole = card_bonus, 0
        self.mac.activate(inst.pid)          # 只激活一次，后面盲点不再重复激活
        # 从列表进第一个
        n0, b0 = targets[0]
        self._click_xy(inst, b0["x"] + b0["w"] / 2, b0["y"] + b0["h"] / 2,
                       wait=1.2, verify=False, activate=False)
        for k in range(min(max_friends, 12)):
            if inst.stop_flag.is_set() or self.user_busy_reason(inst):
                break
            # 进农场后先等页面加载（好友农场比主页慢，1.2s 常常不够 → 按钮还没出现）
            if not self._wait_for(inst, [LABEL_HOME, LABEL_STEAL, LABEL_HELP], timeout=5.0, step=0.3):
                self.push_log(inst, f"好友「{here if 'here' in dir() else '?'}」：农场没加载出来，跳过",
                              level="warn")
                self.grab(inst)
            self.grab(inst)
            here = self._farm_header_name(inst) or (targets[k][0] if k < len(targets) else "")
            has_steal = self.find_block(inst, LABEL_STEAL) is not None
            has_help = self.find_block(inst, LABEL_HELP) is not None
            btn = self._bottom_button(inst)
            self.push_log(inst, f"好友「{here}」：偷取={has_steal} 务农={has_help} 按钮={btn[0] if btn else '无'}",
                          level="debug")
            if btn is not None:
                self._click_xy(inst, btn[1], btn[2], wait=1.0, verify=False, activate=False)
            else:
                self._click_xy(inst, POS_FARM[0], POS_FARM[1], wait=1.0, verify=False, activate=False)
            ok = False
            if self.grab(inst):
                got_exp = any("经验" in str(b["text"]) for b in inst.ocr_blocks)
                # 只校验**刚点的那一个**按钮是否消失：好友家常常同时有「一键偷取」和「一键务农」，
                # 偷完只剩务农，用"任一按钮还在"判断会把成功误判成失败（实测踩过）
                clicked = btn[0] if btn is not None else None
                still = self.find_block(inst, clicked) if clicked else (
                    self.find_block(inst, LABEL_HELP) or self.find_block(inst, LABEL_STEAL))
                ok = got_exp or not still
            if ok:
                if has_steal:
                    stole += 1
                    self.push_log(inst, f"好友「{here}」：偷菜成功")
                elif has_help:
                    helped += 1
                    self.push_log(inst, f"好友「{here}」：一键务农（帮忙）✓")
            elif has_steal or has_help:
                self.push_log(inst, f"好友「{here}」：点了没反应（偷取={has_steal} 帮忙={has_help}）", level="debug")
            # 底部头像条：只点「带小铲土图标」的头像（图标在头像右上角，中心≈图标x-40）
            if k == 0:
                icons = self._icon_matches(inst, ICON_HELP, threshold=0.62)
                strip_av = []
                for (ix, iy, sc) in icons:
                    if 1290 < iy < 1440 and 120 < ix < 810:
                        strip_av.append((int(ix - 40), int(0.905 * H)))
                strip_av.sort()
                self.push_log(inst, f"头像条上带图标：{len(strip_av)} 个")
            if k >= len(strip_av):
                break
            self._click_xy(inst, strip_av[k][0], strip_av[k][1], wait=1.2, verify=False, activate=False)
        # 收尾：回家
        self._click_xy(inst, POS_HOME[0], POS_HOME[1], wait=0.8, verify=False, activate=False)
        self.grab(inst)
        if not any(k == "仓库" for b in inst.ocr_blocks for k in [str(b["text"]).strip()]):
            self._click_xy(inst, POS_HOME[0], POS_HOME[1], wait=0.8)
            self.grab(inst)
        # 收尾：确保回到主页（好友农场/列表都要退出来）
        self._ensure_main(inst, tries=3)
        inst.stats["steal"] = inst.stats.get("steal", 0) + stole
        inst.stats["help"] = inst.stats.get("help", 0) + helped
        self.push_log(inst, f"好友收尾：stole={stole} helped={helped} 即将记账")
        if stole:
            self.bump_daily(inst, "steal", stole)
        if helped:
            self.bump_daily(inst, "help", helped)
        self.push_log(inst, f"好友收尾：记账后 today.help={(inst.daily.get('today') or {}).get('help')}")
        inst.stats["totalActions"] += 1
        self.push_state(inst)
        return True, f"帮忙 {helped} 次 / {len(targets)} 位标记（耗时 {time.time() - t_start:.0f}s）"

    def help_friends(self, inst, max_friends=3):
        """帮好友铲地/帮忙：只点 OCR 里出现的「除草/帮忙/浇水」。"""
        if not self.grab(inst):
            return False, "抓帧失败"
        if not self._open_friend_list(inst):
            return False, "打不开好友列表"
        visits = sorted([b for b in inst.ocr_blocks if str(b["text"]).strip() == LABEL_VISIT],
                        key=lambda b: b["y"])
        helped = 0
        for idx in range(min(max_friends, len(visits))):
            if self.user_busy_reason(inst):
                break
            visits = sorted([b for b in inst.ocr_blocks if str(b["text"]).strip() == LABEL_VISIT],
                            key=lambda b: b["y"])
            if idx >= len(visits):
                break
            b = visits[idx]
            ok, why = self._click_xy(inst, b["x"] + b["w"] / 2, b["y"] + b["h"] / 2, wait=1.4)
            if not ok:
                continue
            self._wait_for(inst, [LABEL_HOME], timeout=6.0)
            hit = None
            for lab in [LABEL_HELP] + HELP_LABELS:
                if self.find_block(inst, lab, min_conf=0.45):
                    hit = lab
                    break
            if hit:
                ok, _, _ = self._tap_text(inst, hit, wait=2.0)
                helped += 1 if ok else 0
                self.push_log(inst, f"帮好友「{hit}」：{'成功' if ok else '无效'}")
            else:
                self.push_log(inst, "这家没有「除草/帮忙/浇水」可点", level="debug")
            self._tap_text(inst, LABEL_HOME, wait=1.4)
            self._wait_for(inst, ["仓库", "商店"], timeout=4.0)
            self._tap_text(inst, NAV_FRIEND[0], wait=1.8)
            self._wait_for(inst, [LABEL_VISIT], timeout=4.0)
        inst.stats["help"] = inst.stats.get("help", 0) + helped
        return True, f"帮忙 {helped} 次"

    # ── 只收不种（timed_harvest）──────────────────────────────────────
    def harvest_only(self, inst):
        """只点「一键收获」，不碰「一键务农」，不补种。"""
        okp, why = self._ensure_main(inst)
        if not okp:
            return False, f"不在农场主页（{why}）"
        if inst.page != "main":
            self._tap_text(inst, LABEL_HOME, wait=0.8, verify=False)
            self._wait_for(inst, ["仓库", "商店"], timeout=3.0, step=0.3)
        block = self.find_block(inst, HARVEST_LABELS[0], min_conf=0.40)
        if block is None:
            return True, "没有可收的作物（页面上无「一键收获」）"
        ok, _, why = self._tap_text(inst, HARVEST_LABELS[0], wait=1.8)
        if ok:
            inst.stats["harvest"] = inst.stats.get("harvest", 0) + 1
            self.bump_daily(inst, "harvest")
            inst.stats["totalActions"] += 1
            settle = self._read_settlement(inst)
            self.push_state(inst)
            if settle:
                return True, f"已一键收获（总价值 {settle[0]}、经验 +{settle[1]}）"
            return True, "已一键收获"
        return False, why

    # ── 左下角任务面板：领取奖励 ───────────────────────────────────────
    def claim_tasks(self, inst):
        """点左下角任务卡 → 把「领取」全点掉 → 关面板。"""
        self._ensure_main(inst)
        if not self.grab(inst):
            return False, "抓帧失败"
        def _opened():
            return any(k in str(b["text"]) for b in inst.ocr_blocks for k in ("成长任务", "每日任务"))

        if not _opened():
            w, h = inst.img_size
            spots = []
            card = self.find_block(inst, "完成", min_conf=0.4)
            if card is not None:
                spots.append((int(card["x"] + card["w"] / 2), int(card["y"] + card["h"] / 2)))
            spots.append((int(w * 0.146), int(h * 0.85)))   # 兜底：左下角任务卡
            for (ix, iy) in spots:
                self.mac.activate(inst.pid)
                time.sleep(0.35)
                self.refresh_windows()
                sx, sy = self.to_screen(inst, ix, iy)
                if not self._in_window(inst, sx, sy):
                    continue
                self.mac.click(inst.pid, sx, sy, mode=self.click_mode, wid=inst.wid, repeat=1)
                time.sleep(1.9)
                self.grab(inst)
                if _opened():
                    break
            if not _opened():
                self.fail(inst, "E_PAGE_UNKNOWN", "打不开任务面板")
                return False, "打不开任务面板"
        claimed = 0
        for _ in range(8):
            if self.user_busy_reason(inst):
                break
            # 先关掉「恭喜获得」这类弹窗（点空白处）
            if any("恭喜获得" in str(b["text"]) for b in inst.ocr_blocks):
                self._close_popup(inst)
                self.grab(inst)
                continue
            btns = [b for b in inst.ocr_blocks if str(b["text"]).strip() == LABEL_CLAIM]
            if not btns:
                break
            btns.sort(key=lambda b: b["y"])
            ok, _, why = self._tap_text(inst, LABEL_CLAIM, wait=1.9)
            if ok:
                claimed += 1
            else:
                break
        # 关面板
        if any(k in str(b["text"]) for b in inst.ocr_blocks for k in ("成长任务", "每日任务")):
            self._tap_text(inst, "关闭" if self.find_block(inst, "关闭", 0.4) else "任务", wait=1.2)
            hit = self.mac.find_icon(str(inst.tmp_png), str(ICON_CLOSE), threshold=0.80)
            if hit:
                sx, sy = self.to_screen(inst, hit["cx"], hit["cy"])
                self.mac.activate(inst.pid)
                self.mac.click(inst.pid, sx, sy, mode=self.click_mode, wid=inst.wid)
                time.sleep(1.4)
                self.grab(inst)
        self.push_log(inst, f"任务面板：领取 {claimed} 个奖励")
        return True, f"领取 {claimed} 个任务奖励"

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
            self.mac.click(inst.pid, csx, csy, mode=self.click_mode, wid=inst.wid, repeat=1)
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
            self.mac.click(inst.pid, hsx, hsy, mode=self.click_mode, wid=inst.wid, repeat=1)
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
        可通过设置 `useCv=false` 完全关闭（此时靠 OCR + 固定坐标，主流程不受影响）。
        """
        if not inst.settings.get("useCv", True):
            return None
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
        fit = data.get("fit") or {}
        self.push_log(inst, "cvhelper 定位到 %d 格（%s x0=%s y0=%s score=%s hits=%s 特征点 %s）" % (
            len(data["cells"]), fit.get("method"), fit.get("x0"), fit.get("y0"),
            fit.get("score"), fit.get("hits"), data.get("points")))
        out = []
        for c in data["cells"]:
            rgb = c["rgb"]
            # 优先用 cvhelper 的三态判定（它同时看结构/绿度，土地升级换土色也不失效）；
            # 老字段缺失时退回纯 RGB 判定
            kind = c.get("kind") if c.get("kind") in ("crop", "empty", "bigcrop", "grass") else _cell_kind(rgb)
            out.append({"row": c["row"], "col": c["col"],
                        "cx": c.get("cx"), "cy": c.get("cy"),
                        "rgb": rgb, "kind": kind,
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
            if hits >= PAGE_MIN_HITS.get(page, 2):
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
        if page not in ("main", "popup_notice", "popup_generic", "warehouse", "shop",
                        "friend", "friend_farm", "task_panel", "unknown",
                        "event_shop", "svip"):
            self.fail(inst, "E_PAGE_UNKNOWN", f"页面「{page}」不在白名单，停手")
            return False, "页面不在白名单"

        # ⓪ 进错页先自己退回来（SVIP/活动/仓库这类误入页面）
        if page not in ("main", "popup_notice", "popup_generic", "task_panel"):
            okm, whym = self._ensure_main(inst)
            if okm:
                page = inst.page
            else:
                self.push_log(inst, f"页面纠偏失败（{page} → {inst.page}）", level="warn")
                return False, whym

        # ⓪ 专项任务：各自有自己的流程，先于通用页面逻辑
        if page not in ("popup_notice", "popup_generic"):
            if task_name == "friend":
                return self.visit_friends(inst)
            if task_name == "grass":
                return self.help_friends(inst)
            if task_name == "reward":
                return self.claim_tasks(inst)
            if task_name == "timed_harvest":
                return self.harvest_only(inst)
            if task_name == "event":
                return self.read_event(inst)

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
                        self.mac.click(inst.pid, sx, sy, mode=self.click_mode, wid=inst.wid, repeat=1)
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
            # 底部按钮（收获/务农/施肥/偷取）图标高度相似，模板会误命中 → 只用 OCR 标签判断
            if action is None:
                plan = self.find_block(inst, PLANT_LABELS[0])
                if plan is not None:
                    if self.allow_plant:
                        action, label = plan, PLANT_LABELS[0]
                    else:
                        self.push_log(inst, f"[{now}] 有「{PLANT_LABELS[0]}」但未开启播种权限（--allow-plant），跳过")
                        return False, "空闲：有播种入口但未授权"
            if action is None and inst.settings.get("fertilize"):
                # 「一键施肥」在地块弹窗里，主页 OCR 扫不到 → 走点地块的流程
                fed, why = self._fertilize(inst)
                if fed:
                    inst.stats["totalActions"] += 1
                    self.push_state(inst)
                    return True, f"施肥完成（{why}）"
            if action is None:
                # 没有一键按钮，但田里有「可摘」→ 逐株点收
                ripe = self._harvest_ripe_by_tap(inst)
                if ripe:
                    inst.stats["totalActions"] += 1
                    self.push_state(inst)
                    return True, f"逐株收获 {ripe} 株"
            if action is None:
                planted, why = self._replant(inst)
                if planted:
                    inst.stats["totalActions"] += 1
                    self.push_state(inst)
                    return True, f"补种完成（{why}）"
            if action is None:
                # 兜底：OCR 没认出「一键务农/一键收获」，但按钮可能只是没识别到 →
                # 点固定坐标（图标中心）一次，画面有变化就算成功
                W, H = inst.img_size or (822, 1542)
                fx, fy = int(W * 0.517), int(H * 0.746)
                before_sig = self.signature(inst)
                self._click_xy(inst, fx, fy, wait=1.6)
                if self.grab(inst) and self.signature(inst) != before_sig:
                    inst.stats["totalActions"] += 1
                    self.bump_daily(inst, "plant")
                    self.push_state(inst)
                    self.push_log(inst, "兜底：点固定坐标的一键务农/收获（OCR 没认出按钮）")
                    return True, "兜底点击底部按钮（画面有变化）"
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
        if page == "unknown":
            # 切页动画/加载中会短暂认不出来：不算故障，下一轮再看
            self.push_log(inst, "页面未识别（可能在加载），本轮跳过", level="debug")
            return False, "页面未识别"
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
        self.mac.click(inst.pid, sx, sy, mode=self.click_mode, wid=inst.wid)
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
        self.mac.click(inst.pid, sx, sy, mode=self.click_mode, wid=inst.wid)
        self.push_log(inst, f"[{now}] 点击「{label}」({sx:.0f},{sy:.0f}) conf={block['conf']:.2f}",
                      action=label, conf=round(block["conf"], 2))
        time.sleep(1.6)
        ok = self.grab(inst)
        after = self.signature(inst) if ok else before
        inst.page = self.classify(inst) if ok else inst.page
        if after != before:
            key = "harvest" if label in HARVEST_LABELS else "plant"
            if key == "harvest":
                self._read_settlement(inst)
            inst.stats[key] = inst.stats.get(key, 0) + 1
            self.bump_daily(inst, key)
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
                    mode = str(inst.tasks.get("land_scan", {}).get("mode", "notify"))
                    gate_on = bool(inst.settings.get("changeGate", True))
                    if gate_on and inst.frame_hash and inst.frame_changed is False:
                        self.push_log(inst, "巡查跳过：画面与上次一致（变化检测）", level="debug")
                        did, why = False, "画面无变化，跳过"
                    elif mode == "script":
                        did, why = self.scan_land(inst)     # 旧行为：脚本逐格点击
                    else:
                        # 新行为：只标记"有待判断的变化"，把判断交给模型
                        inst.pending_wake = {"reason": "land_scan",
                                             "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                        self.push_log(inst, "巡查：画面有变化 → 已标记待模型判断（不自动点击）")
                        did, why = True, "已标记待判断"
                elif name in IMPLEMENTED_TASKS:
                    did, why = self.step(inst, name)
                else:
                    did, why = False, f"任务「{name}」尚未实现（macOS 后端当前仅 main 可用）"
            except Exception as e:  # noqa: BLE001
                did, why = False, f"任务异常：{e}"

            cfg = inst.tasks[name]
            cfg["lastRun"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started))
            cfg["lastResult"] = why
            if str(why).startswith("暂缓"):
                # 被「用户在用电脑」挡住时别等一整个周期，30 秒后重试（用户一走开就补上）
                cfg["nextRun"] = time.time() + 30
            else:
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

        # 一次性任务（面板「立即执行」/HTTP /task）**不改调度状态**。
        # 之前这里把 running 改成 idle，等于把正在跑的调度器悄悄停掉：
        # 面板显示「空闲」、下次时间停在过去、看起来"再也不执行了"。
        if once:
            inst.current_task = None
            inst.rev += 1
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
            st = inst.full_state()
            try:
                ring = getattr(AGENT, "log_ring", None) or []
                st["recentLogs"] = [e.get("msg") for e in ring
                                    if e.get("instance") in (inst.id, None)][-15:]
            except Exception:
                st["recentLogs"] = []
            return self._json(200, st)

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
                AGENT.push_log(None, f"控制：{body.get('action')} 未生效（游戏窗口已关闭，没有可控制的实例）",
                               level="warn")
                return self._json(409, {"ok": False, "error": {
                    "code": "E_NO_INSTANCE",
                    "message": "游戏窗口已关闭，没有运行中的实例（重新打开小程序后会自动接管）"}})
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

        m = re.match(r"^/v1/instances/([^/]+)/plant$", p)
        if m:
            inst = AGENT.instances.get(m.group(1)) or AGENT.refresh_windows()
            if inst is None:
                return self._json(404, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": "未找到实例"}})
            ok, why = AGENT.plant(inst, str(body.get("seed", "auto")))
            return self._json(200 if ok else 409, {"ok": ok, "result": why,
                                                   "error": None if ok else {"code": "E_PLANT", "message": why}})

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
            AGENT.mac.click(inst.pid, sx, sy, mode=AGENT.click_mode, wid=inst.wid, repeat=2, gap=0.12)
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
            AGENT.mac.click(inst.pid, sx, sy, mode=AGENT.click_mode, wid=inst.wid, repeat=2, gap=0.12)
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
            if "mode" in body:
                mode = str(body["mode"])
                if mode not in ("notify", "script"):
                    return self._json(400, {"ok": False, "error": {"code": "E_BAD_REQUEST", "message": "mode 只能是 notify / script"}})
                cfg["mode"] = mode
            save_task_state(inst.id, inst.tasks)
            inst.rev += 1
            AGENT.push_log(inst, f"任务配置更新：{name} enabled={cfg['enabled']} interval={cfg['intervalSec']}s")
            AGENT.push_state(inst)
            return self._json(200, {"ok": True, "task": name, "config": {
                "enabled": cfg["enabled"], "intervalSec": cfg["intervalSec"],
                "mode": cfg.get("mode", "notify")}})

        m = re.match(r"^/v1/instances/([^/]+)/settings$", p)
        if m:
            inst = AGENT.instances.get(m.group(1)) or AGENT.refresh_windows()
            if inst is None:
                return self._json(404, {"ok": False, "error": {"code": "E_UNSUPPORTED", "message": "未找到实例"}})
            if "guardIdle" in body:
                try:
                    inst.settings["guardIdle"] = max(0.0, min(600.0, float(body["guardIdle"])))
                except (TypeError, ValueError):
                    return self._json(400, {"ok": False, "error": {"code": "E_BAD_REQUEST", "message": "guardIdle 非法"}})
            if "changeGate" in body:
                inst.settings["changeGate"] = bool(body["changeGate"])
            if "clickMode" in body and body["clickMode"] in ("warp", "ghost"):
                AGENT.click_mode = body["clickMode"]
            if "useCv" in body:
                inst.settings["useCv"] = bool(body["useCv"])
            if "fertilize" in body:
                inst.settings["fertilize"] = bool(body["fertilize"])
            if "buyPolicy" in body:
                policy = str(body["buyPolicy"])
                if policy not in ("auto", "owned", "never"):
                    return self._json(400, {"ok": False, "error": {
                        "code": "E_BAD_REQUEST", "message": "buyPolicy 只能是 auto / owned / never"}})
                inst.settings["buyPolicy"] = policy
            save_settings(inst.platform, inst.settings, AGENT.click_mode)
            AGENT.push_log(inst, f"设置更新：guardIdle={inst.settings['guardIdle']}s "
                                 f"changeGate={inst.settings['changeGate']} "
                                 f"buyPolicy={inst.settings['buyPolicy']}")
            AGENT.push_state(inst)
            return self._json(200, {"ok": True, "settings": inst.settings})

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
