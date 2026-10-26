#!/usr/bin/env python3
"""地块自动识别（OpenCV）—— 供 macagent 调用，不需要人工标定、不点击。

用法: cvhelper.py <capture.png>
输出: {"ok":true,"cells":[{row,col,cx,cy,kind,rgb,green,cgreen,covered,group}...],
       "fit":{x0,y0,u,v,method,score,inliers,inside,outside,points}}

定位坐标**不依赖土地颜色**（土地升级会换土色：黑土/红土/沙土…）：
  1. 草地是场景底色 → 「草地之外的最大连通块」= 整块田地（field_region）
  2. 强边缘把画面切成块 → 填充率≈0.5、长宽比≈2 的菱形块 = 格子内部
     （cell_components，多阈值取并集，不调阈值）
  3. 候选原点 = 检出格心 − i·u − j·v，把 4×6 方块在画面上挪一遍，按
       (内点最多, 24 个格心在田地区域内 + 外侧 20 个探针在区域外, 离田地先验最近)
     选出一块。晶格是周期的，整格平移后格心依然在格子上；靠「外侧探针」和
     「田地先验位置」把等价解排除。
  4. 等轴测约束下最小二乘精修：u=(2a,a)、v=(-2a,a)，只拟合 (x0,y0,a)
     —— a 就是「田地被切成 4×6 时的格宽」，由检出格子的间距反算。
  5. 逐格统计三态（这一步才看颜色/结构）：
        - 棕土 + 中心有苗  → 已种 1x1（crop）
        - 棕土 + 中心无苗  → 空耕地（empty）
        - 整格绿 + 大植株  → 四格作物（bigcrop）
        - 整格均匀浅绿     → 未开垦（grass）
     非棕色土（升级皮肤）时改看结构：均匀浅绿=未开垦、有绿色/花叶=作物、否则空地。
  6. 2×2 的四格作物合并成一块（后三格标 covered）
  7. 行按屏幕 y 排序（游戏上方对应面板上方）
  8. 拟合结果缓存到 ~/.dsh-farm/lattice-<W>x<H>.json；几何证据不足时退回旧的
     土壤颜色拟合（search/plateau）作兜底。
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

COLS, ROWS = 4, 6            # 田地 4 列 × 6 行
BASE_W = 824.0               # 基准截图宽度（412 逻辑宽 @2x）
UNIT_U = np.array([56.0, 28.0])     # 实测基向量（截图坐标）：向右下
UNIT_V = np.array([-56.0, 28.0])    # 向左下
# 单格菱形 112 宽 × 56 高 ⇒ 格心间距就是上面的 |u|/|v|
CACHE_DIR = Path.home() / ".dsh-farm"

# 田地「大致位置」先验（归一化到截图宽高）。田地在一局游戏里是固定的，
# 只用来在「晶格等价解」之间挑一个（见 _prior_origin），不参与精确定位。
# 实测 8 张 824×1542 截图：x0=381.8~385.6, y0=836.2~836.7
PRIOR_ORIGIN = (0.4642, 0.5424)

# 格心采样窗口：25×13 落在菱形内部，且几乎覆盖格心土坑
PATCH_W, PATCH_H = 25, 13


def scale_of(w):
    """相对基准 824 宽的缩放（截图可能是 1x/2x/其它）。"""
    return max(0.25, min(3.0, float(w) / BASE_W))


def load(path):
    img = cv2.imread(path)
    if img is None:
        from PIL import Image
        img = cv2.cvtColor(np.array(Image.open(path).convert("RGB")), cv2.COLOR_RGB2BGR)
    return img


def _band(bgr):
    """田地只可能出现在屏幕纵向的这个区间里（排除天空/UI/底部按钮栏）。"""
    h = bgr.shape[0]
    return int(h * 0.45), int(h * 0.78)


def soil_mask(bgr):
    """翻耕后的棕色土壤。"""
    b, g, r = bgr[:, :, 0].astype(int), bgr[:, :, 1].astype(int), bgr[:, :, 2].astype(int)
    m = ((r > 110) & (r < 205) & (g > 70) & (g < 165) & (b > 30) & (b < 125)
         & (r - g > 24) & (g - b > 6)).astype(np.uint8)
    y0, y1 = _band(bgr)
    m[:y0, :] = 0
    m[y1:, :] = 0
    return m


def pit_mask(bgr):
    """格心的深色小土坑（种子窝）+ 地块之间的深色缝隙。"""
    b, g, r = bgr[:, :, 0].astype(int), bgr[:, :, 1].astype(int), bgr[:, :, 2].astype(int)
    m = ((r > 45) & (r < 150) & (g > 20) & (g < 120) & (b > 5) & (b < 105)
         & (r - g > 10) & (b < r - 15)).astype(np.uint8)
    y0, y1 = _band(bgr)
    m[:y0, :] = 0
    m[y1:, :] = 0
    return m


def tilled_mask(bgr):
    """「像耕地」的像素：棕土 + 格心土坑。草地、建筑、天空都不算。"""
    return ((soil_mask(bgr) | pit_mask(bgr)) > 0).astype(np.uint8)


def cell_score_map(mask, sc):
    """每像素 = 该像素当格心时「窗口内耕地占比」。"""
    kx = max(3, int(round(PATCH_W * sc))) | 1
    ky = max(3, int(round(PATCH_H * sc))) | 1
    return cv2.boxFilter(mask.astype(np.float32), -1, (kx, ky), normalize=True)


# ---------- 与土地颜色无关的几何证据 ----------
# 土地升级后土块会换成黑土/红土/沙土等颜色，所以格子的**坐标**不依赖土壤色：
#   1) 草地是场景底色（不随土地皮肤变）→ 「草地之外的整块」就是田地
#   2) 格子内部被边线围成菱形块 → 用形状(填充率/长宽比)认格子，与颜色无关


def lawn_mask(bgr):
    """草地（场景底色）。只依赖「草是绿的」这一条，不依赖土地/土壤颜色。"""
    b, g, r = bgr[:, :, 0].astype(int), bgr[:, :, 1].astype(int), bgr[:, :, 2].astype(int)
    return ((g > r + 4) & (g > b + 40)).astype(np.uint8)


def field_region(bgr):
    """整块田地（草地之外的最大连通块，填掉内部空洞）。

    田地的外沿（床沿/围边）与草地颜色不同，所以「非草地」的大块就是田地；
    未开垦的绿格子被床沿包在里面 → 填洞后一并算进田地。不依赖土壤颜色。
    """
    H, W = bgr.shape[:2]
    sc = scale_of(W)
    nl = (1 - lawn_mask(bgr)).astype(np.uint8)
    y0, y1 = _band(bgr)
    nl[:y0, :] = 0
    nl[y1:, :] = 0
    nl = cv2.morphologyEx(nl, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(nl, 8)
    if n <= 1:
        return None
    big = max(range(1, n), key=lambda i: stats[i][4])
    cells = stats[big][4] / (3136.0 * sc * sc)   # 折算成「几格」
    # 4×6 田地 + 床沿/装饰 ≈ 20~60 格；整屏木地板/店铺内景会 ≫ 这个量级
    if not (10.0 <= cells <= 90.0):
        return None
    m = (lab == big).astype(np.uint8)
    ff = m.copy()
    cv2.floodFill(ff, np.zeros((m.shape[0] + 2, m.shape[1] + 2), np.uint8), (0, 0), 1)
    return ((m | (1 - ff)) > 0).astype(np.uint8)


def gradient_map(bgr):
    g = cv2.GaussianBlur(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    return np.hypot(gx, gy)


def cell_components(bgr, pcts=(52, 58, 64, 70, 76, 82)):
    """颜色无关：强边缘把画面切块，取「菱形大小/长宽比/填充率」的块 = 格子内部。

    菱形（等轴测土块）在 bbox 里只占约一半面积 → 用 面积/(w*h) ≈ 0.5 认它，
    矩形图标、石块阴影都会被排除。多个阈值取并集（去重），阈值本身不用调。
    """
    H, W = bgr.shape[:2]
    sc = scale_of(W)
    G = gradient_map(bgr)
    y0, y1 = _band(bgr)
    blobs = []
    for pct in pcts:
        thr = max(16.0, float(np.percentile(G[y0:y1], pct)))
        strong = (G > thr).astype(np.uint8)
        strong[:y0, :] = 0
        strong[y1:, :] = 0
        strong = cv2.dilate(strong, np.ones((3, 3), np.uint8))
        n, lab, stats, cent = cv2.connectedComponentsWithStats(1 - strong, 8)
        for i in range(1, n):
            x, y, w, h, a = stats[i]
            if not (38 * sc <= w <= 125 * sc and 22 * sc <= h <= 66 * sc):
                continue
            fill = a / float(w * h)                 # 菱形 ≈ 0.5；矩形 ≈ 1
            if not (0.40 <= fill <= 0.60):
                continue
            if not (1.5 <= w / float(h) <= 3.0):
                continue
            if not (0.25 * 3136 * sc * sc <= a <= 1.15 * 3136 * sc * sc):
                continue
            blobs.append((float(cent[i][0]), float(cent[i][1])))
    # 去重（相邻 12px 内算同一个）
    out = []
    for p in blobs:
        if not any(abs(p[0] - q[0]) < 12 * sc and abs(p[1] - q[1]) < 12 * sc for q in out):
            out.append(p)
    return out


def block_support(region, o, u, v):
    """一块 4×6 田地跟「整块田地掩膜」的吻合程度。

    返回 (cells_inside, probes_outside)：
      cells_inside  — 24 个格心有几个落在田地区域里
      probes_outside— 紧贴这块田外侧那一圈格子（20 个）有几个在田地区域外
    整格平移后 cells_inside 可能还很高，但 probes_outside 会掉下来（挪出去的那
    一圈正好压在田地上），所以两者一起能把「哪一块是田地」定下来。
    """
    H, W = region.shape
    inside = 0
    for j in range(ROWS):
        for i in range(COLS):
            x = int(round(o[0] + u[0] * i + v[0] * j))
            y = int(round(o[1] + u[1] * i + v[1] * j))
            inside += int(0 <= x < W and 0 <= y < H and region[y, x])
    probes = ([(i, -1) for i in range(COLS)] + [(i, ROWS) for i in range(COLS)]
              + [(-1, j) for j in range(ROWS)] + [(COLS, j) for j in range(ROWS)])
    outside = 0
    for (i, j) in probes:
        x = int(round(o[0] + u[0] * i + v[0] * j))
        y = int(round(o[1] + u[1] * i + v[1] * j))
        if 0 <= x < W and 0 <= y < H and not region[y, x]:
            outside += 1
    return inside, outside


def _prior_origin(W, H, sc):
    """田地大致位置（先验）：有缓存用缓存，否则用内置归一化值。"""
    cached = load_cache(W, H)
    if cached:
        return np.array([cached["x0"], cached["y0"]], float)
    return np.array([PRIOR_ORIGIN[0] * W, PRIOR_ORIGIN[1] * H], float)


def find_plot_centers(bgr):
    """已翻耕地块的菱形中心（即晶格上的真实格心）。"""
    H, W = bgr.shape[:2]
    sc = scale_of(W)
    m = cv2.erode(soil_mask(bgr), np.ones((3, 3), np.uint8))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    a_lo, a_hi = 700 * sc * sc, 9000 * sc * sc
    w_lo, w_hi = 60 * sc, 115 * sc
    h_lo, h_hi = 30 * sc, 62 * sc
    pts = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if a_lo <= area <= a_hi and w_lo <= w <= w_hi and h_lo <= h <= h_hi:
            pts.append((float(cent[i][0]), float(cent[i][1])))
    return pts


def _basis(sc):
    """默认基向量（按分辨率缩放），保证 u 向右下、v 向左下。"""
    u, v = UNIT_U * sc, UNIT_V * sc
    return u, v


def lattice_offsets(u, v):
    """24 个格心相对原点的偏移，下标 = j*COLS + i（行优先）。"""
    offs = np.empty((COLS * ROWS, 2))
    for j in range(ROWS):
        for i in range(COLS):
            offs[j * COLS + i] = (u[0] * i + v[0] * j, u[1] * i + v[1] * j)
    return offs


def origin_score(F, offs, x0, y0):
    """24 个格心的「耕地占比」之和（0..24）。"""
    H, W = F.shape
    xs = x0 + offs[:, 0]
    ys = y0 + offs[:, 1]
    if xs.min() < 0 or xs.max() > W - 1 or ys.min() < 0 or ys.max() > H - 1:
        return 0.0
    return float(F[np.round(ys).astype(int), np.round(xs).astype(int)].sum())


def search_origin(F, offs, sc, step=None):
    """暴力搜索 (x0,y0)：让 24 个格心尽量落在耕地上。

    返回 (best, S, x0s, y0s)，S[y][x] 为该原点的得分（0..24）。
    搜索空间很小（~400×770 个位置，每次 24 次查表），numpy 向量化即可。
    """
    H, W = F.shape
    step = step or max(1, int(round(2 * sc)))
    x0s = np.arange(0, W, step)
    y0s = np.arange(0, H, step)
    # 列索引只依赖 offs[:,0]，预计算成 (nx,24)
    cols = np.round(x0s[:, None] + offs[:, 0][None, :]).astype(int)
    cols = np.clip(cols, 0, W - 1)
    S = np.zeros((len(y0s), len(x0s)), np.float32)
    for k, y0 in enumerate(y0s):
        ys = np.round(y0 + offs[:, 1]).astype(int)
        if ys.min() < 0 or ys.max() >= H:
            continue
        S[k] = F[ys[:, None], cols.T].sum(axis=0)
    iy, ix = np.unravel_index(int(np.argmax(S)), S.shape)
    best = (float(S[iy, ix]), float(x0s[ix]), float(y0s[iy]))
    return best, S, x0s, y0s


def _lattice_ij(p, o, inv_basis, tol):
    ij = inv_basis @ (np.asarray(p, float) - np.asarray(o, float))
    ri, rj = int(round(ij[0])), int(round(ij[1]))
    if abs(ij[0] - ri) <= tol and abs(ij[1] - rj) <= tol:
        return ri, rj, float(np.hypot(ij[0] - ri, ij[1] - rj))
    return None


def _inliers(pts, u, v, o, tol=0.3):
    """哪些检出点能作为本晶格的格心（含序号范围约束）。"""
    inv_basis = np.linalg.inv(np.array([[u[0], v[0]], [u[1], v[1]]]))
    out = []
    for p in pts:
        r = _lattice_ij(p, o, inv_basis, tol)
        if r is None:
            continue
        i, j, _err = r
        if 0 <= i < COLS and 0 <= j < ROWS:
            out.append((p, i, j))
    return out


def _refine(pts_ij, u, v, sc):
    """等轴测约束下最小二乘精修：u=(2a,a)、v=(-2a,a)，只拟合 (x0,y0,a)。

    格子的方向由画面投影定死（2:1 等轴测），自由度只剩「一格多粗」；把原来
    6 个参数的拟合压成 3 个，免得噪声把 u/v 拟歪（一行错位会一路放大）。
    `a` 由检出格子的间距反算 —— 也就是「量出田地被切成几块」的那个格宽。
    """
    u0, v0 = _basis(sc)
    a0 = abs(float(u0[1]))
    best = None
    for a in np.arange(0.90 * a0, 1.10 * a0 + 1e-9, max(0.02, 0.01 * a0)):
        uu = np.array([2.0 * a, a])
        vv = np.array([-2.0 * a, a])
        rs = np.array([np.asarray(p, float) - i * uu - j * vv for (p, i, j) in pts_ij])
        o = rs.mean(axis=0)
        rms = float(np.sqrt(((rs - o) ** 2).sum(axis=1).mean()))
        if best is None or rms < best[0]:
            best = (rms, o, a)
    rms, o, a = best
    if not np.all(np.isfinite(o)) or rms > 5.0 * sc:
        # 拟合不收敛（检出点太脏）→ 用默认基向量，只调原点
        uu, vv = u0, v0
        rs = np.array([np.asarray(p, float) - i * uu - j * vv for (p, i, j) in pts_ij])
        o = rs.mean(axis=0)
        return o, uu, vv, bool(np.all(np.isfinite(o)))
    return o, np.array([2.0 * a, a]), np.array([-2.0 * a, a]), True


def outside_frac(bgr, o, u, v, ring=(8, 12, 16)):
    """田地平行四边形「外侧一圈」的耕地占比。

    真正的田地四周是草地（≈0）；奶茶小铺的柜台/建筑地板整片都是木色，
    拟合会锁上去，但外侧一圈仍然全是棕色（≈0.6）→ 用这一条把假田地挡掉。
    采样点不够（贴边/出界）时返回 None，表示无法判断。
    """
    H, W = bgr.shape[:2]
    sc = scale_of(W)
    G = cv2.blur(tilled_mask(bgr).astype(np.float32), (9, 9))
    o = np.asarray(o, float)
    A = o - u / 2.0 - v / 2.0
    corners = [A, A + COLS * u, A + COLS * u + ROWS * v, A + ROWS * v]
    vals = []
    for k in range(4):
        p0, p1 = corners[k], corners[(k + 1) % 4]
        edge = p1 - p0
        d = edge / (np.linalg.norm(edge) + 1e-6)
        nrm = np.array([-d[1], d[0]])
        if np.dot(nrm, (p0 + p1) / 2.0 - o) < 0:
            nrm = -nrm                              # 朝外
        for t in np.linspace(0.15, 0.85, 18):
            for off in ring:
                p = p0 + edge * t + nrm * (off * sc)
                x, y = int(round(p[0])), int(round(p[1]))
                if 0 <= x < W and 0 <= y < H:
                    vals.append(float(G[y, x]))
    return float(np.mean(vals)) if len(vals) >= 12 else None


def fit_field(bgr):
    """定位田地晶格。返回 dict(x0,y0,u,v,method,score,inliers,points)；判不了返回 None。

    先几何、后颜色：
      1) 几何证据（与土地颜色无关）：草地之外那一整块 = 田地；菱形边线切出的
         块 = 格子。用「24 个格心在田地区域内 + 外侧一圈 20 个探针在区域外」
         排除晶格的整格平移等价解，再按等轴测约束把 (x0,y0,格宽a) 精修到 ~1px。
      2) 几何证据不够时，退回旧的土壤颜色拟合（沿用交接文档的搜索解法）。
    """
    H, W = bgr.shape[:2]
    sc = scale_of(W)
    u, v = _basis(sc)

    region = field_region(bgr)
    pts_shape = cell_components(bgr)
    pts_color = find_plot_centers(bgr)
    # 形状证据优先，颜色证据补充（同一格心去重）
    pts = list(pts_shape)
    for p in pts_color:
        if not any(abs(p[0] - q[0]) < 12 * sc and abs(p[1] - q[1]) < 12 * sc for q in pts):
            pts.append(p)

    prior = _prior_origin(W, H, sc)
    offs = lattice_offsets(u, v)
    best = None
    if len(pts) >= 4:
        # 候选原点 = 检出格心 − i·u − j·v：等于把 4×6 方块在整个画面上挪一遍
        cands, seen = [], set()
        for p in pts:
            for j in range(ROWS):
                for i in range(COLS):
                    o = np.array(p, float) - i * u - j * v
                    key = (int(round(o[0] / 2)), int(round(o[1] / 2)))
                    if key not in seen:
                        seen.add(key)
                        cands.append(o)
        for o_c in cands:
            if not _fits(o_c, offs, W, H):
                continue
            inl = _inliers(pts, u, v, o_c)
            if len(inl) < 4:
                continue
            sup = block_support(region, o_c, u, v) if region is not None else (0, 0)
            # 排序键：内点多 > 与整块田地吻合 > 靠近田地先验位置
            key = (len(inl), sup[0] + sup[1], -np.hypot(*(o_c - prior)))
            if best is None or key > best[0]:
                best = (key, o_c, inl, sup)

    method, o, npts, support = None, None, 0, (0, 0)
    if best is not None:
        (_n, _sup, _d), o_c, inl, sup = best
        o, u, v, ok = _refine(inl, u, v, sc)
        if ok:
            offs = lattice_offsets(u, v)
            inv_basis = np.linalg.inv(np.array([[u[0], v[0]], [u[1], v[1]]]))
            keep = []
            for p in pts:
                r = _lattice_ij(p, o, inv_basis, 0.3)
                if r and 0 <= r[0] < COLS and 0 <= r[1] < ROWS:
                    keep.append((p, r[0], r[1]))
            if 4 <= len(keep) < len(pts):
                o2, u2, v2, ok2 = _refine(keep, u, v, sc)
                if ok2:
                    o, u, v = o2, u2, v2
                    offs = lattice_offsets(u, v)
            method = "shape" if len(pts_shape) >= 4 else "blobs"
            npts = max(len(keep), len(inl))
            support = block_support(region, o, u, v) if region is not None else (0, 0)

    if method is None:
        # —— 退化路径：先看缓存，再退回土壤颜色 ——
        cached = load_cache(W, H)
        if cached:
            cu, cv_ = np.array(cached["u"], float), np.array(cached["v"], float)
            o_c = np.array([cached["x0"], cached["y0"]], float)
            coffs = lattice_offsets(cu, cv_)
            if _fits(o_c, coffs, W, H):
                F = cell_score_map(tilled_mask(bgr), sc)
                if origin_score(F, coffs, o_c[0], o_c[1]) > 0:
                    u, v, offs, o = cu, cv_, coffs, o_c
                    method = "cache"
        if method is None:
            F = cell_score_map(tilled_mask(bgr), sc)
            F = F * (region if region is not None else 1)     # 只在田地区域里搜
            (S, x0, y0), Smat, x0s, y0s = search_origin(F, offs, sc)
            if S <= 0:
                return None
            x0, y0 = _plateau_center(Smat, x0s, y0s, S)
            o = np.array([x0, y0], float)
            method = "search"
        npts = 0

    F = cell_score_map(tilled_mask(bgr), sc)
    # 相机/窗口变化会让原点漂移：拟合结果离缓存原点太远时，若缓存原点在本帧依然站得住，
    # 就用缓存（实测该窗口 shape 拟合会偏 17~40px，整整错开一格，导致点错地块）
    cached0 = load_cache(W, H)
    if cached0 and method != "cache":
        o_c = np.array([cached0["x0"], cached0["y0"]], float)
        if float(np.hypot(*(o - o_c))) > 40:
            cu0, cv0 = np.array(cached0["u"], float), np.array(cached0["v"], float)
            coffs0 = lattice_offsets(cu0, cv0)
            if _fits(o_c, coffs0, W, H) and origin_score(F, coffs0, o_c[0], o_c[1]) > 0:
                o, u, v, offs, method = o_c, cu0, cv0, coffs0, "cache"
                sc_note = "相机漂移，改用缓存原点"
    score = origin_score(F, offs, o[0], o[1])
    hits = int((cell_scores(F, offs, o) >= 0.5).sum())
    if not _fits(o, offs, W, H):
        if method == "cache":
            drop_cache(W, H)
        return None
    if method != "cache" and npts < 4 and score < 0.30 * COLS * ROWS:
        return None
    if method == "shape" and support[0] + support[1] < 26:
        # 24 格心在内 + 20 探针在外 = 44 项。实测真田地 ≥35，整格平移会掉到 ≤33
        return None
    ring = outside_frac(bgr, o, u, v)
    if ring is not None and ring > 0.35:
        # 外侧一圈也是"耕地" → 这不是田地（建筑地板 / 小铺柜台）
        if method == "cache":
            drop_cache(W, H)
        return None
    if npts >= 4 and (method != "cache"):
        save_cache(W, H, o, u, v, score)
    return {"x0": round(float(o[0]), 2), "y0": round(float(o[1]), 2),
            "u": [round(float(u[0]), 2), round(float(u[1]), 2)],
            "v": [round(float(v[0]), 2), round(float(v[1]), 2)],
            "method": method, "score": round(score, 2), "hits": hits,
            "inside": support[0], "outside": support[1],
            "ring": None if ring is None else round(ring, 2),
            "inliers": npts, "points": len(pts)}


def _fits(o, offs, W, H):
    """24 个格心是否都落在画面内。"""
    x = o[0] + offs[:, 0]
    y = o[1] + offs[:, 1]
    return bool(x.min() >= 0 and x.max() <= W - 1 and y.min() >= 0 and y.max() <= H - 1)

def _plateau_center(Smat, x0s, y0s, best_score):
    """网格搜索的最优点常在平台上，用「≥98% 最优」位置的逐轴中位数收敛。"""
    yy, xx = np.nonzero(Smat >= 0.98 * best_score)
    if len(xx) == 0:
        return float(x0s[0]), float(y0s[0])
    return float(np.median(x0s[xx])), float(np.median(y0s[yy]))


def cell_scores(F, offs, o):
    """每格「耕地占比」（行优先，长度 24）；出界的格记 0，不夹到边缘。"""
    H, W = F.shape
    xs = np.round(o[0] + offs[:, 0]).astype(int)
    ys = np.round(o[1] + offs[:, 1]).astype(int)
    ok = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)
    out = np.zeros(len(xs), np.float32)
    out[ok] = F[ys[ok], xs[ok]]
    return out


# ---------- 晶格缓存（Agent 重启不丢） ----------


def _cache_path(W, H):
    return CACHE_DIR / ("lattice-%dx%d.json" % (int(W), int(H)))


def load_cache(W, H):
    try:
        return json.loads(_cache_path(W, H).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_cache(W, H, o, u, v, score):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(W, H).write_text(json.dumps(
            {"x0": float(o[0]), "y0": float(o[1]),
             "u": [float(u[0]), float(u[1])], "v": [float(v[0]), float(v[1])],
             "score": round(float(score), 2), "img": [int(W), int(H)]},
            ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def drop_cache(W, H):
    try:
        _cache_path(W, H).unlink()
    except OSError:
        pass



# ---------- 模板图标扫描（macd find 只返回一个，这里返回全部） ----------


def find_icons(bgr, tpl, thr=0.60, x_range=None, y_range=None, scales=(0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25)):
    """多尺度匹配模板，返回去重后的 [(cx, cy, score)]，按 score 降序。

    用途：好友列表里的「小人铲土」图标 = 该好友需要帮忙，一次找全所有行。
    """
    hits = {}
    for sc in scales:
        t = cv2.resize(tpl, None, fx=sc, fy=sc, interpolation=cv2.INTER_LINEAR)
        if t.shape[0] > bgr.shape[0] or t.shape[1] > bgr.shape[1]:
            continue
        r = cv2.matchTemplate(bgr, t, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(r >= thr)
        for y, x in zip(ys, xs):
            cx = int(x + t.shape[1] / 2)
            cy = int(y + t.shape[0] / 2)
            if x_range and not (x_range[0] <= cx <= x_range[1]):
                continue
            if y_range and not (y_range[0] <= cy <= y_range[1]):
                continue
            key = (cx // 10, cy // 10)
            v = (cx, cy, float(r[y, x]))
            if key not in hits or v[2] > hits[key][2]:
                hits[key] = v
    out = []
    for (cx, cy, sc) in sorted(hits.values(), key=lambda v: -v[2]):
        if any(abs(cx - a) < 16 and abs(cy - b) < 16 for (a, b, _) in out):
            continue
        out.append((cx, cy, round(sc, 2)))
    return out


# ---------- 模板注册表（具名匹配）----------
# agent/mac/templates/manifest.json:
#   { "btn.oneclick_farm": {"file": "btn_oneclick_farm.png", "thr": 0.82, "scales": [0.9,1.0,1.1]} }
# 用名字找元素，不靠坐标猜；新增元素只要裁一张图 + 写一行 manifest。


def manifest_path():
    return Path(__file__).parent / "templates" / "manifest.json"


def load_manifest():
    try:
        return json.loads(manifest_path().read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_manifest(data):
    manifest_path().parent.mkdir(parents=True, exist_ok=True)
    manifest_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def template_of(name):
    """按名字取 (路径, 阈值, 尺度列表)。"""
    man = load_manifest()
    ent = man.get(name)
    if not ent:
        return None
    p = Path(__file__).parent / "templates" / ent.get("file", "")
    return (p, float(ent.get("thr", 0.80)), tuple(ent.get("scales") or (0.9, 1.0, 1.1)))


def find_named(bgr, name, x_range=None, y_range=None):
    """按名字找模板，返回 [(cx, cy, score, w, h)]（多尺度 + NMS）。"""
    got = template_of(name)
    if not got:
        return None
    path, thr, scales = got
    tpl = cv2.imread(str(path))
    if tpl is None:
        return None
    hits = {}
    for sc in scales:
        t = cv2.resize(tpl, None, fx=sc, fy=sc, interpolation=cv2.INTER_LINEAR)
        if t.shape[0] > bgr.shape[0] or t.shape[1] > bgr.shape[1]:
            continue
        r = cv2.matchTemplate(bgr, t, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(r >= thr)
        for y, x in zip(ys, xs):
            cx = int(x + t.shape[1] / 2)
            cy = int(y + t.shape[0] / 2)
            if x_range and not (x_range[0] <= cx <= x_range[1]):
                continue
            if y_range and not (y_range[0] <= cy <= y_range[1]):
                continue
            key = (cx // 8, cy // 8)
            v = (cx, cy, float(r[y, x]), t.shape[1], t.shape[0])
            if key not in hits or v[2] > hits[key][2]:
                hits[key] = v
    out = []
    for (cx, cy, sc, tw, th) in sorted(hits.values(), key=lambda v: -v[2]):
        if any(abs(cx - a) < tw * 0.5 and abs(cy - b) < th * 0.5 for (a, b, _, _, _) in out):
            continue
        out.append((cx, cy, round(sc, 3), tw, th))
    return out


def cell_features(bgr, cx, cy, half=None):
    H, W = bgr.shape[:2]
    if half is None:
        # 只看格心：菱形中心一定是纯地面材质，避开格间边框与阴影
        half = max(5, int(12 * scale_of(W)))
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
    r, g, green, cgreen, std = f["r"], f["g"], f["green"], f["cgreen"], f["std"]
    if (r - g) > 15:                       # 整格棕土
        return "crop" if cgreen > 0.06 else "empty"
    if (g - r) > 15:                       # 整格偏绿
        if green > 0.55 and std > 24:
            return "bigcrop"
        return "grass"
    # |R-G| 很小：黑土/灰土/沙土等升级皮肤（土色不再是棕色）→ 只看结构
    if green > 0.5 and std < 26:           # 均匀浅绿 = 未开垦
        return "grass"
    if green > 0.06:                       # 格心有绿色植株
        return "crop"
    if std > 30:                           # 格内结构复杂（花果/藤蔓）
        return "crop"
    return "empty"


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


def analyze(bgr):
    """截图 → 24 格结果（dict）。失败时抛 LookupError。"""
    H, W = bgr.shape[:2]
    fit = fit_field(bgr)
    if fit is None:
        raise LookupError("找不到田地晶格")

    x0, y0 = fit["x0"], fit["y0"]
    u = np.array(fit["u"], float)
    v = np.array(fit["v"], float)

    cells = []
    for j in range(ROWS):
        for i in range(COLS):
            x = int(round(x0 + i * u[0] + j * v[0]))
            y = int(round(y0 + i * u[1] + j * v[1]))
            if not (0 <= x < W and 0 <= y < H):
                continue
            f = cell_features(bgr, x, y)
            if f is None:
                continue
            cells.append({"row": int(j), "col": int(i), "cx": x, "cy": y,
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

    merge_bigcrops(cells, COLS, ROWS)
    return {"ok": True, "img": [W, H], "fit": fit, "points": fit["points"], "cells": cells}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "missing image"}))
        return 1
    if sys.argv[1] == "--list":
        print(json.dumps({"ok": True, "names": sorted(load_manifest().keys())}, ensure_ascii=False))
        return 0
    if sys.argv[1] == "--find":
        # 用法: cvhelper.py --find <png> <name> [--x lo,hi] [--y lo,hi]
        args = sys.argv[2:]
        if len(args) < 2:
            print(json.dumps({"ok": False, "error": "usage: --find <png> <name>"}))
            return 1
        bgr = load(args[0])
        name = args[1]
        xr = yr = None
        for i, a in enumerate(args):
            if a == "--x" and i + 1 < len(args):
                lo, hi = args[i + 1].split(",")
                xr = (int(lo), int(hi))
            elif a == "--y" and i + 1 < len(args):
                lo, hi = args[i + 1].split(",")
                yr = (int(lo), int(hi))
        if template_of(name) is None:
            print(json.dumps({"ok": False, "error": "manifest 里没有 %s" % name,
                              "names": sorted(load_manifest().keys())}, ensure_ascii=False))
            return 2
        hits = find_named(bgr, name, x_range=xr, y_range=yr)
        print(json.dumps({"ok": True, "name": name,
                          "matches": [{"cx": c, "cy": y, "score": sc, "w": w, "h": h}
                                      for (c, y, sc, w, h) in (hits or [])]},
                         ensure_ascii=False))
        return 0
    if sys.argv[1] == "--icons":
        # 用法: cvhelper.py --icons <png> [--tpl 模板] [--thr 0.6] [--x 350,430]
        args = sys.argv[2:]
        if not args:
            print(json.dumps({"ok": False, "error": "missing image"}))
            return 1
        bgr = load(args[0])
        tpl_path = str(Path(__file__).parent / "templates" / "help.png")
        thr, xr = 0.60, None
        for i, a in enumerate(args):
            if a == "--tpl" and i + 1 < len(args):
                tpl_path = args[i + 1]
            elif a == "--thr" and i + 1 < len(args):
                thr = float(args[i + 1])
            elif a == "--x" and i + 1 < len(args):
                lo, hi = args[i + 1].split(",")
                xr = (int(lo), int(hi))
        tpl = cv2.imread(tpl_path)
        if tpl is None:
            print(json.dumps({"ok": False, "error": "读不到模板 %s" % tpl_path}, ensure_ascii=False))
            return 1
        hits = find_icons(bgr, tpl, thr=thr, x_range=xr)
        print(json.dumps({"ok": True, "icons": [{"cx": c, "cy": y, "score": s} for (c, y, s) in hits]},
                         ensure_ascii=False))
        return 0
    bgr = load(sys.argv[1])
    try:
        data = analyze(bgr)
    except LookupError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2
    print(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
