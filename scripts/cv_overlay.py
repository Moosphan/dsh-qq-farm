#!/usr/bin/env python3
"""把 cvhelper 拟合出的 24 个格心/菱形画到截图上，肉眼验收晶格定位。

用法:
  python3 scripts/cv_overlay.py <截图> [-o 输出.png] [--json]

默认输出 <截图>.overlay.png；同时把 4×6 三态表打到终端。

验收标准（交接文档）：24 个点全部落在棕色菱形内，且行列关系与画面一致
（(0,0) 在田地最上，(0,5) 最左，(3,0) 最右，(3,5) 最下）。
标签用 (i, j) = (列, 行)，与交接文档里的公式 x = x0 + 56*(i-j) 一致。
"""
import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent" / "mac"))
import cvhelper as cvh  # noqa: E402

KIND_STYLE = {
    "empty":   ((0, 165, 255), "空地 empty"),
    "crop":    ((80, 200, 80), "已种 crop"),
    "bigcrop": ((200, 90, 220), "四格 bigcrop"),
    "grass":   ((150, 150, 150), "未开垦 grass"),
}


def draw(img, data):
    W, H = data["img"]
    fit = data["fit"]
    o = np.array([fit["x0"], fit["y0"]], float)
    u = np.array(fit["u"], float)
    v = np.array(fit["v"], float)
    out = img.copy()
    overlay = img.copy()

    cells = data["cells"]
    for c in cells:
        col = KIND_STYLE.get(c["kind"], KIND_STYLE["grass"])[0]
        cxy = np.array([c["cx"], c["cy"]], float)
        quad = np.array([cxy + u / 2 + v / 2, cxy + u / 2 - v / 2,
                         cxy - u / 2 - v / 2, cxy - u / 2 + v / 2], np.int32)
        cv2.fillPoly(overlay, [quad], col)
    out = cv2.addWeighted(overlay, 0.22, out, 0.78, 0)

    for c in cells:
        col = KIND_STYLE.get(c["kind"], (KIND_STYLE["grass"]))[0]
        cxy = np.array([c["cx"], c["cy"]], float)
        quad = np.array([cxy + u / 2 + v / 2, cxy + u / 2 - v / 2,
                         cxy - u / 2 - v / 2, cxy - u / 2 + v / 2], np.int32)
        cv2.polylines(out, [quad], True, col, 2, cv2.LINE_AA)
        x, y = int(round(c["cx"])), int(round(c["cy"]))
        cv2.drawMarker(out, (x, y), (255, 255, 255), cv2.MARKER_CROSS, 22, 1, cv2.LINE_AA)
        cv2.circle(out, (x, y), 5, (0, 0, 255), -1, cv2.LINE_AA)
        lab = "%d,%d" % (c["col"], c["row"])
        cv2.putText(out, lab, (x - 17, y + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(out, lab, (x - 17, y + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (255, 255, 255), 1, cv2.LINE_AA)

    # 田地平行四边形
    corners = [o - u / 2 - v / 2, o - u / 2 - v / 2 + 4 * u,
               o - u / 2 - v / 2 + 4 * u + 6 * v, o - u / 2 - v / 2 + 6 * v]
    cv2.polylines(out, [np.array(corners, np.int32)], True, (255, 255, 0), 2, cv2.LINE_AA)

    return out, corners


def draw_info(img, fit):
    """在成品图上写拟合参数（画在裁剪后的图上，免得被裁掉）。"""
    u, v = fit["u"], fit["v"]
    lines = [
        "fit %s  x0=%.1f y0=%.1f  u=(%.1f,%.1f) v=(%.1f,%.1f)"
        % (fit["method"], fit["x0"], fit["y0"], u[0], u[1], v[0], v[1]),
        "score=%.1f/24  hits=%d  inliers=%d  points=%d  in24=%s out20=%s"
        % (fit["score"], fit["hits"], fit["inliers"], fit["points"],
           fit.get("inside"), fit.get("outside")),
        "label = (i,j) = (col,row)",
    ]
    for k, t in enumerate(lines):
        cv2.putText(img, t, (14, 26 + k * 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(img, t, (14, 26 + k * 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (255, 255, 255), 1, cv2.LINE_AA)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("-o", "--out")
    ap.add_argument("--json", action="store_true", help="打印 cvhelper 原始 JSON")
    args = ap.parse_args()

    img = cvh.load(args.image)
    if img is None:
        print("读不到图片: %s" % args.image)
        return 1
    try:
        data = cvh.analyze(img)
    except LookupError as e:
        print("拟合失败: %s" % e)
        return 2
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))

    out, corners = draw(img, data)
    H, W = out.shape[:2]
    sc = cvh.scale_of(W)
    pad = int(round(50 * sc))
    bx0 = max(0, int(min(p[0] for p in corners)) - pad)
    bx1 = min(W, int(max(p[0] for p in corners)) + pad)
    by0 = max(0, int(min(p[1] for p in corners)) - pad - 70)
    by1 = min(H, int(max(p[1] for p in corners)) + pad)
    crop = out[by0:by1, bx0:bx1]
    zoom = 2 if crop.shape[1] < 900 else 1
    if zoom > 1:
        crop = cv2.resize(crop, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_LANCZOS4)
    crop = draw_info(crop, data["fit"])

    outp = Path(args.out) if args.out else Path(args.image).with_suffix(".overlay.png")
    cv2.imwrite(str(outp), crop)

    print("图: %s  (%dx%d)" % (Path(args.image).name, W, H))
    fi = data["fit"]
    print("拟合: %s  x0=%.1f y0=%.1f  u=(%.1f,%.1f) v=(%.1f,%.1f)  格心在内 %s/24 外侧探针在外 %s/20"
          % (fi["method"], fi["x0"], fi["y0"], fi["u"][0], fi["u"][1],
             fi["v"][0], fi["v"][1], fi.get("inside"), fi.get("outside")))
    print("三态分类（行优先，row=0 在最上）:")
    cells = {(c["row"], c["col"]): c for c in data["cells"]}
    for r in range(cvh.ROWS):
        print("  row %d: " % r + "  ".join(
            "%s" % (cells.get((r, c), {}).get("kind", "-"))[:5].ljust(5) for c in range(cvh.COLS)))
    print("叠加图: %s" % outp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
