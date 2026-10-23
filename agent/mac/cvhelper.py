#!/usr/bin/env python3
"""地块自动识别（OpenCV）—— 供 macagent 调用，不需要人工标定、不点击。

用法: cvhelper.py <capture.png>
输出: {"ok":true,"cells":[{row,col,cx,cy,kind,rgb,green,cgreen,covered,group}...]}

原理：
  1. 土壤掩膜 → 腐蚀 → 连通域 → 已翻耕地块的菱形中心
  2. 两两位移众数 → 等轴测晶格基向量（实测 u=(56,28) v=(-56,28)）
  3. 反解整数格坐标 → 补全 4×6
  4. 逐格统计：整格偏棕 / 整格偏绿 / 中心小苗（cgreen）
       - 棕土 + 中心有苗  → 已种 1x1（crop）
       - 棕土 + 中心无苗  → 空耕地（empty）
       - 整格绿 + 大植株  → 四格作物（bigcrop）
       - 整格均匀浅绿     → 未开垦（grass）
  5. 2×2 的四格作物合并成一块（后三格标 covered）
  6. 行按屏幕 y 排序（游戏上方对应面板上方）
"""
import json
import sys
from collections import Counter

import cv2
import numpy as np


def load(path):
    img = cv2.imread(path)
    if img is None:
        from PIL import Image
        img = cv2.cvtColor(np.array(Image.open(path).convert("RGB")), cv2.COLOR_RGB2BGR)
    return img


def soil_mask(bgr):
    b, g, r = bgr[:, :, 0].astype(int), bgr[:, :, 1].astype(int), bgr[:, :, 2].astype(int)
    m = ((r > 110) & (r < 205) & (g > 70) & (g < 165) & (b > 30) & (b < 125)
         & (r - g > 24) & (g - b > 6)).astype(np.uint8)
    h = bgr.shape[0]
    m[: int(h * 0.45), :] = 0
    m[int(h * 0.78):, :] = 0
    return m


def find_plot_centers(bgr):
    H, W = bgr.shape[:2]
    sc = max(0.25, min(3.0, W / 824.0))          # 相对基准 824x1542 的缩放
    m = cv2.erode(soil_mask(bgr), np.ones((3, 3), np.uint8))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    a_lo, a_hi = 700 * sc * sc, 9000 * sc * sc
    w_lo, w_hi = 60 * sc, 115 * sc
    h_lo, h_hi = 30 * sc, 62 * sc
    pts = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if a_lo <= area <= a_hi and w_lo <= w <= w_hi and h_lo <= h <= h_hi:
            pts.append(cent[i])
    return pts


def fit_lattice(pts):
    if len(pts) < 4:
        return None
    diffs = []
    for i in range(len(pts)):
        for j in range(len(pts)):
            if i == j:
                continue
            d = pts[j] - pts[i]
            if 40 < np.linalg.norm(d) < 95:
                diffs.append((round(d[0] / 4) * 4, round(d[1] / 4) * 4))
    if not diffs:
        return None
    picked = []
    for (dx, dy), _cnt in Counter(diffs).most_common(24):
        v = np.array([dx, dy], float)
        n = np.linalg.norm(v)
        if not (50 < n < 80):
            continue
        if any(abs(np.dot(v / n, q / np.linalg.norm(q))) > 0.75 for q in picked):
            continue
        picked.append(v)
        if len(picked) == 2:
            break
    return (picked[0], picked[1]) if len(picked) == 2 else None


def cell_features(bgr, cx, cy, half=None):
    H, W = bgr.shape[:2]
    if half is None:
        half = max(8, int(26 * max(0.25, min(3.0, W / 824.0))))
    x0, x1 = max(0, cx - half), min(W, cx + half)
    y0, y1 = max(0, cy - int(half * 0.6)), min(H, cy + int(half * 0.6))
    patch = bgr[y0:y1, x0:x1].reshape(-1, 3).astype(int)
    if patch.size == 0:
        return None
    b, g, r = patch.mean(0)
    green = float(((patch[:, 1] - patch[:, 0] > 12) & (patch[:, 1] - patch[:, 2] > 12)).mean())
    std = float(patch.std(0).mean())
    cx0, cx1 = max(0, cx - 7), min(W, cx + 7)
    cy0, cy1 = max(0, cy - 5), min(H, cy + 5)
    center = bgr[cy0:cy1, cx0:cx1].reshape(-1, 3).astype(int)
    cgreen = float((center[:, 1] - center[:, 2] > 8).mean()) if center.size else 0.0
    return {"r": int(r), "g": int(g), "b": int(b),
            "green": round(green, 3), "cgreen": round(cgreen, 3), "std": round(std, 1)}


def classify(f):
    r, g, green, cgreen = f["r"], f["g"], f["green"], f["cgreen"]
    if (r - g) > 15:                       # 整格棕土
        return "crop" if cgreen > 0.06 else "empty"
    if (g - r) > 15:                       # 整格偏绿
        if green > 0.55 and f["std"] > 24:
            return "bigcrop"
        return "grass"
    return "crop" if cgreen > 0.10 else "grass"


def merge_bigcrops(cells, cols, rows):
    grid = {(c["row"], c["col"]): c for c in cells}
    for r in range(rows - 1):
        for c in range(cols - 1):
            quad = [(r, c), (r, c + 1), (r + 1, c), (r + 1, c + 1)]
            cs = [grid.get(q) for q in quad]
            if any(x is None for x in cs):
                continue
            if any(x.get("group") for x in cs):
                continue
            kinds = [x["kind"] for x in cs]
            if kinds.count("bigcrop") >= 2 and all(k in ("bigcrop", "crop") for k in kinds):
                gid = "%d-%d" % (r, c)
                for q in quad:
                    grid[q]["group"] = gid
                    grid[q]["kind"] = "bigcrop"
                for q in quad[1:]:
                    grid[q]["covered"] = True


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "missing image"}))
        return 1
    bgr = load(sys.argv[1])
    H, W = bgr.shape[:2]
    pts = find_plot_centers(bgr)
    lat = fit_lattice(pts)
    if not pts or lat is None:
        print(json.dumps({"ok": False, "error": "找不到地块", "points": len(pts)}))
        return 2
    u, v = lat
    step = 62.6 * max(0.25, min(3.0, W / 824.0))
    u = u / np.linalg.norm(u) * step
    v = v / np.linalg.norm(v) * step
    o = np.array(pts[0], float)
    Minv = np.linalg.inv(np.array([[u[0], v[0]], [u[1], v[1]]]))
    idx = [tuple(np.round(Minv @ (np.array(p) - o)).astype(int)) for p in pts]
    imin = min(i for i, j in idx)
    jmin = min(j for i, j in idx)

    cells = []
    for j in range(jmin, jmin + 6):
        for i in range(imin, imin + 4):
            p = o + i * u + j * v
            x, y = int(round(p[0])), int(round(p[1]))
            if not (0 <= x < W and 0 <= y < H):
                continue
            f = cell_features(bgr, x, y)
            if f is None:
                continue
            cells.append({"row": int(j - jmin), "col": int(i - imin), "cx": x, "cy": y,
                          "kind": classify(f), "rgb": [f["r"], f["g"], f["b"]],
                          "green": f["green"], "cgreen": f["cgreen"], "std": f["std"]})

    # 行按屏幕 y 排序：游戏里靠上的排在面板上方
    rows = sorted({c["row"] for c in cells},
                  key=lambda r: float(np.mean([c["cy"] for c in cells if c["row"] == r])))
    rowmap = {r: k for k, r in enumerate(rows)}
    for c in cells:
        c["row"] = rowmap[c["row"]]
    cols = sorted({c["col"] for c in cells},
                  key=lambda cc: float(np.mean([c["cx"] for c in cells if c["col"] == cc])))
    colmap = {cc: k for k, cc in enumerate(cols)}
    for c in cells:
        c["col"] = colmap[c["col"]]

    merge_bigcrops(cells, 4, 6)
    print(json.dumps({"ok": True, "u": u.round(1).tolist(), "v": v.round(1).tolist(),
                      "points": len(pts), "img": [W, H], "cells": cells}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
