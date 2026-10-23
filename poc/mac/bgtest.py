#!/usr/bin/env python3
"""在可控 Chrome 页面上验证「真后台点击」是否送达。

判定方式：测试页把点击计数写进 document.title → 直接在窗口标题里读结果，
不需要截图或 OCR。
"""
import json, subprocess, sys, time, os, signal

MACD = "../../agent/mac/bin/macd"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PAGE = "file://" + os.path.abspath("clicktest.html")
PROFILE = "/tmp/qqfarm-chrome-test"

def run(*a):
    r = subprocess.run([MACD, *[str(x) for x in a]], capture_output=True, text=True)
    return r.stdout.strip()

def title_of(owner_kw="Chrome"):
    for line in run("winlist").splitlines():
        w = json.loads(line)
        if owner_kw.lower() in (w["owner"] + w["title"]).lower() and w["title"].startswith("CLICKS:"):
            return w
    return None

print("启动 Chrome（独立 user-data-dir，不碰你的配置）…")
proc = subprocess.Popen([CHROME, f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check",
                         "--new-window", "--window-position=120,120", "--window-size=600,700", PAGE],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

win = None
for _ in range(40):
    time.sleep(0.5)
    win = title_of()
    if win:
        break
if not win:
    print("✗ 没找到测试页窗口"); proc.terminate(); sys.exit(1)

print(f"窗口: wid={win['wid']} pid={win['pid']} {win['w']:.0f}x{win['h']:.0f} @({win['x']:.0f},{win['y']:.0f}) 标题={win['title']}")

W, H = win["w"], win["h"]
sx = win["x"] + W / 2
sy = win["y"] + H / 2 + 61          # 按钮中心（按 flex 布局估算）
local_content = (W / 2, H / 2 + 61 - 28)   # 内容区相对坐标
local_frame = (W / 2, H / 2 + 61)          # 含标题栏相对坐标

def cursor():
    return json.loads(run("cursorpos"))

def try_click(label, args):
    before = title_of()["title"]
    c0 = cursor()
    out = run(*args)
    time.sleep(0.9)
    after = title_of()["title"]
    c1 = cursor()
    moved = (c0 != c1)
    ok = before != after
    print(f"  {label:<28} 标题 {before} → {after}  送达={'✅' if ok else '❌'}  光标移动={'是' if moved else '否'}")
    return ok

print("\nA. 公开 API: CGEvent.postToPid")
try_click("postToPid", ("click", win["pid"], sx, sy))

print("B. SkyLight + 窗口本地坐标(内容区) + primer")
try_click("bgclick content-local", ("bgclick", win["pid"], sx, sy, *local_content))

print("C. SkyLight + 窗口本地坐标(含标题栏) + primer")
try_click("bgclick frame-local", ("bgclick", win["pid"], sx, sy, *local_frame))

print("D. SkyLight 无 primer（内容区坐标）")
try_click("bgclick --no-primer", ("bgclick", win["pid"], sx, sy, *local_content, "--no-primer"))

print("\n清理")
proc.terminate()
try: proc.wait(timeout=5)
except subprocess.TimeoutExpired: proc.kill()
