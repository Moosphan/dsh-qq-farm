#!/usr/bin/env python3
"""从一张截图里裁模板，写进 agent/mac/templates/ 并登记到 manifest.json。

用法：
  # 按矩形裁（x,y,w,h 都是截图像素）
  python3 scripts/crop_template.py shot.png --rect 60,1130,80,44 --name btn.oneclick_farm

  # 顺手自检：在同一张图上按名字找一次，打印命中分数
  python3 scripts/crop_template.py shot.png --rect ... --name ... --verify

  # 裁完想再看看叠加图（红框=模板源区域，绿框=自检命中）
  python3 scripts/crop_template.py shot.png --rect ... --name ... --verify --preview out.png
"""
import argparse
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
TPL_DIR = ROOT / "agent" / "mac" / "templates"
MANIFEST = TPL_DIR / "manifest.json"


def safe_file(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", name).replace(".", "_") + ".png"


def load_manifest():
    try:
        return json.loads(MANIFEST.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--rect", required=True, help="x,y,w,h（截图像素）")
    ap.add_argument("--name", required=True, help="如 btn.oneclick_farm / seed.strawberry")
    ap.add_argument("--thr", type=float, default=0.80)
    ap.add_argument("--scales", default="0.9,1.0,1.1")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--preview")
    args = ap.parse_args()

    img = cv2.imread(args.image)
    if img is None:
        print(json.dumps({"ok": False, "error": "读不到图 %s" % args.image}))
        return 2
    x, y, w, h = (int(v) for v in args.rect.split(","))
    if w < 4 or h < 4:
        print(json.dumps({"ok": False, "error": "矩形太小"}))
        return 2
    if x < 0 or y < 0 or x + w > img.shape[1] or y + h > img.shape[0]:
        print(json.dumps({"ok": False, "error": "矩形超出图像范围"}))
        return 2

    tpl = img[y:y + h, x:x + w].copy()
    TPL_DIR.mkdir(parents=True, exist_ok=True)
    fname = safe_file(args.name)
    cv2.imwrite(str(TPL_DIR / fname), tpl)

    man = load_manifest()
    man[args.name] = {
        "file": fname,
        "thr": args.thr,
        "scales": [float(s) for s in args.scales.split(",")],
        "category": args.name.split(".")[0],
        "rect": [x, y, w, h],
        "source": Path(args.image).name,
    }
    MANIFEST.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")

    out = {"ok": True, "name": args.name, "file": str(TPL_DIR / fname),
           "size": [w, h], "manifest": str(MANIFEST)}

    if args.verify or args.preview:
        best = None
        for sc in [float(s) for s in args.scales.split(",")]:
            t = cv2.resize(tpl, None, fx=sc, fy=sc, interpolation=cv2.INTER_LINEAR)
            if t.shape[0] > img.shape[0] or t.shape[1] > img.shape[1]:
                continue
            r = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(r)
            if best is None or mx > best[0]:
                best = (float(mx), loc[0] + t.shape[1] // 2, loc[1] + t.shape[0] // 2, t.shape)
        if best:
            out["verify"] = {"score": round(best[0], 3), "cx": best[1], "cy": best[2],
                             "thr": args.thr, "pass": best[0] >= args.thr}
        if args.preview:
            vis = img.copy()
            cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 0, 255), 2)
            cv2.putText(vis, "template", (x, max(12, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
            if best:
                tw, th = best[3][1], best[3][0]
                cv2.rectangle(vis, (best[1] - tw // 2, best[2] - th // 2),
                              (best[1] + tw // 2, best[2] + th // 2), (0, 255, 0), 2)
                cv2.putText(vis, "match %.2f" % best[0], (best[1] - tw // 2, max(12, best[2] - th // 2 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 1)
            Path(args.preview).parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(args.preview, vis)
            out["preview"] = args.preview

    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("verify", {}).get("pass", True) else 1


if __name__ == "__main__":
    sys.exit(main())
