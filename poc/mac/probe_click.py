#!/usr/bin/env python3
"""Mac 后台点击送达探针。

流程：找窗口 → 抓帧 → OCR 定位目标文字 → 换算屏幕坐标 → postToPid 点击 → 再抓帧 → 对比

用法:
  python3 probe_click.py --label 仓库 [--sky] [--hover] [--dry]
"""
import argparse, re, subprocess, sys, time, json
from pathlib import Path

HERE = Path(__file__).parent
WINLIST = str(HERE / "winlist")
OCR = str(HERE / "ocr")
CLICK = str(HERE / "postclick")
TITLE_HINT = "QQ经典农场"


def find_window():
    out = subprocess.run([WINLIST], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if TITLE_HINT in line:
            m = re.search(r"wid=(\d+) pid=(\d+) layer=\d+ (\d+)x(\d+) @\((-?\d+),(-?\d+)\)", line)
            if m:
                wid, pid, w, h, x, y = (int(v) for v in m.groups())
                return {"wid": wid, "pid": pid, "w": w, "h": h, "x": x, "y": y}
    raise RuntimeError("找不到 QQ经典农场 窗口，先确认小程序已打开")


def capture(win, name):
    path = HERE / name
    subprocess.run(["screencapture", "-x", "-o", "-l", str(win["wid"]), str(path)], check=True)
    return path


def ocr(path):
    out = subprocess.run([OCR, str(path)], capture_output=True, text=True, check=True).stdout
    rows = []
    for line in out.splitlines()[1:]:
        m = re.match(r"\s*\[\s*(-?\d+),\s*(-?\d+)\s+(\d+)x(\d+)\s+conf ([\d.]+)\]\s?(.*)$", line)
        if m:
            px, py, pw, ph, conf, text = m.groups()
            rows.append({"x": int(px), "y": int(py), "w": int(pw), "h": int(ph), "conf": float(conf), "text": text})
    latency = re.search(r"vision ([\d.]+)ms", out)
    return rows, (float(latency.group(1)) if latency else None)


def image_to_screen(win, px, py):
    """抓帧是 @2x，窗口矩形是逻辑点 → screen = origin + img/(2)"""
    scale = 1.0  # 先假设 1x，下面用实测比例校正
    return win["x"] + px * scale / 2.0, win["y"] + py * scale / 2.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, help="要点击的文字")
    ap.add_argument("--sky", action="store_true")
    ap.add_argument("--system", action="store_true")
    ap.add_argument("--hover", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    win = find_window()
    print(f"窗口: wid={win['wid']} pid={win['pid']} {win['w']}x{win['h']} @({win['x']},{win['y']})")

    before = capture(win, "probe_before.png")
    rows_before, ms_before = ocr(before)
    print(f"OCR: {len(rows_before)} 块, {ms_before:.0f}ms")

    target = None
    for r in rows_before:
        if args.label in r["text"]:
            target = r
            break
    if not target:
        print(f"✗ 画面里没找到「{args.label}」")
        print("  当前可见文字:", " | ".join(r["text"] for r in rows_before[:12]))
        return 1

    cx = target["x"] + target["w"] / 2
    cy = target["y"] + target["h"] / 2
    sx, sy = image_to_screen(win, cx, cy)
    print(f"目标「{target['text']}」图像坐标 ({cx:.0f},{cy:.0f}) → 屏幕 ({sx:.0f},{sy:.0f})")

    if args.dry:
        print("--dry：不发送")
        return 0

    cmd = [CLICK, str(win["pid"]), str(round(sx)), str(round(sy))]
    if args.sky:
        cmd.append("--sky")
    if args.system:
        cmd.append("--system")
    if args.hover:
        cmd.append("--hover")
    print("执行:", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    print(res.stdout.strip() or res.stderr.strip())

    time.sleep(1.0)
    after = capture(win, "probe_after.png")
    rows_after, ms_after = ocr(after)
    texts_before = {r["text"] for r in rows_before}
    texts_after = {r["text"] for r in rows_after}
    gained = texts_after - texts_before
    lost = texts_before - texts_after
    print(f"\n点击后 OCR: {len(rows_after)} 块")
    print(f"新增文字: {sorted(gained)[:10] if gained else '(无)'}")
    print(f"消失文字: {sorted(lost)[:10] if lost else '(无)'}")
    verdict = "✅ 事件送达（画面有变化）" if (gained or lost) else "⚠️ 无可见变化（可能被丢弃，也可能该点本身无反应）"
    print(f"\n结论: {verdict}")
    return 0 if (gained or lost) else 2


if __name__ == "__main__":
    sys.exit(main())
