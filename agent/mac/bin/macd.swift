// macd — macOS 自动化原语（单二进制，供 FAP Agent 调用）
//
// 用法:
//   macd winlist                          列出窗口（JSON lines）
//   macd capture <wid> <out.png>          截取窗口（CGWindowListCreateImage）
//   macd ocr <png> [--roi x,y,w,h]        Vision OCR（JSON lines）
//   macd click <pid> <x> <y> [--warp] [--system] [--sky]
//   macd drag <pid> <x1> <y1> <x2> <y2> [--system]
//   macd hittest <x> <y>                  该屏幕点最上层窗口
//
// 坐标约定：全部为「屏幕逻辑点」（与 CGEvent / CGWindowBounds 同一坐标系）。
// 截图输出为物理像素（Retina 下是逻辑点的 2 倍），调用方负责换算。

import Foundation
import CoreGraphics
import ApplicationServices
import ImageIO
import UniformTypeIdentifiers
import Vision

// ── 工具 ─────────────────────────────────────────────────────────────
func die(_ msg: String, code: Int32 = 2) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(code)
}

func jsonLine(_ dict: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys]),
       let s = String(data: data, encoding: .utf8) {
        print(s)
    }
}

func windowInfo(_ w: [String: Any]) -> [String: Any] {
    let b = w[kCGWindowBounds as String] as? [String: Any] ?? [:]
    return [
        "wid": (w[kCGWindowNumber as String] as? Int) ?? -1,
        "pid": (w[kCGWindowOwnerPID as String] as? Int) ?? -1,
        "owner": (w[kCGWindowOwnerName as String] as? String) ?? "",
        "title": (w[kCGWindowName as String] as? String) ?? "",
        "layer": (w[kCGWindowLayer as String] as? Int) ?? -1,
        "x": (b["X"] as? Double) ?? 0,
        "y": (b["Y"] as? Double) ?? 0,
        "w": (b["Width"] as? Double) ?? 0,
        "h": (b["Height"] as? Double) ?? 0,
    ]
}

func allWindows(onScreenOnly: Bool = true) -> [[String: Any]] {
    let opts: CGWindowListOption = onScreenOnly ? [.optionOnScreenOnly, .excludeDesktopElements] : [.excludeDesktopElements]
    return (CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]]) ?? []
}


// ── 模板匹配（归一化互相关，多尺度）─────────────────────────────────
// 纯 Swift 实现，不依赖 numpy/opencv：受管 Agent 跑在 /usr/bin/python3 上，
// 没有任何第三方包，所以匹配能力必须落在 macd 里。

func grayImage(_ path: String, maxW: Int = 0) -> (w: Int, h: Int, px: [UInt8])? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { return nil }
    var w = img.width, h = img.height
    if maxW > 0 && w > maxW {
        let s = Double(maxW) / Double(w)
        w = maxW; h = max(1, Int(Double(h) * s))
    }
    var buf = [UInt8](repeating: 0, count: w * h)
    guard let cs = CGColorSpace(name: CGColorSpace.linearGray) else { return nil }
    let ok = buf.withUnsafeMutableBytes { raw -> Bool in
        guard let ctx = CGContext(data: raw.baseAddress, width: w, height: h, bitsPerComponent: 8,
                                  bytesPerRow: w, space: cs,
                                  bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return false }
        ctx.interpolationQuality = .high
        ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
        return true
    }
    return ok ? (w, h, buf) : nil
}

/// 归一化互相关搜索。返回最佳 (x, y, score)，坐标为图像像素。
func nccSearch(_ img: (w: Int, h: Int, px: [UInt8]), _ tpl: (w: Int, h: Int, px: [UInt8]))
    -> (x: Int, y: Int, score: Double) {
    let tw = tpl.w, th = tpl.h
    guard tw > 1, th > 1, img.w > tw, img.h > th else { return (0, 0, -1) }
    // 模板去均值 + 归一化
    var tSum = 0.0
    for v in tpl.px { tSum += Double(v) }
    let tMean = tSum / Double(tw * th)
    var tZero = [Double](repeating: 0, count: tw * th)
    var tNorm = 0.0
    for i in 0..<(tw * th) {
        let d = Double(tpl.px[i]) - tMean
        tZero[i] = d
        tNorm += d * d
    }
    tNorm = sqrt(tNorm)
    if tNorm < 1e-6 { return (0, 0, -1) }

    // 图像积分图（和、平方和）
    let W = img.w, H = img.h
    var iSum = [Double](repeating: 0, count: (W + 1) * (H + 1))
    var iSq  = [Double](repeating: 0, count: (W + 1) * (H + 1))
    for y in 0..<H {
        var rowSum = 0.0, rowSq = 0.0
        for x in 0..<W {
            let v = Double(img.px[y * W + x])
            rowSum += v; rowSq += v * v
            iSum[(y + 1) * (W + 1) + (x + 1)] = iSum[y * (W + 1) + (x + 1)] + rowSum
            iSq[(y + 1) * (W + 1) + (x + 1)]  = iSq[y * (W + 1) + (x + 1)] + rowSq
        }
    }
    func rect(_ ii: [Double], _ x0: Int, _ y0: Int, _ x1: Int, _ y1: Int) -> Double {
        let a = ii[y0 * (W + 1) + x0], b = ii[y0 * (W + 1) + x1]
        let c = ii[y1 * (W + 1) + x0], d = ii[y1 * (W + 1) + x1]
        return d - b - c + a
    }

    var best = (x: 0, y: 0, score: -1.0)
    let area = Double(tw * th)
    for y in 0...(H - th) {
        for x in 0...(W - tw) {
            let s = rect(iSum, x, y, x + tw, y + th)
            let sq = rect(iSq, x, y, x + tw, y + th)
            var iNorm2 = sq - (s * s) / area
            if iNorm2 <= 1e-6 { continue }
            // 互相关
            var cross = 0.0
            for ty in 0..<th {
                let iRow = (y + ty) * W + x
                let tRow = ty * tw
                for tx in 0..<tw { cross += Double(img.px[iRow + tx]) * tZero[tRow + tx] }
            }
            if cross == 0 { continue }
            iNorm2 = iNorm2.squareRoot()
            let score = cross / (iNorm2 * tNorm)
            if score > best.score { best = (x, y, score) }
        }
    }
    return best
}

// ── 命令 ─────────────────────────────────────────────────────────────
let args = Array(CommandLine.arguments.dropFirst())
guard let cmd = args.first else {
    die("""
usage: macd <command> ...
  winlist | capture <wid> <out.png> | ocr <png> [--roi x,y,w,h]
  click <pid> <x> <y> [--warp] [--system] [--sky]
  drag <pid> <x1> <y1> <x2> <y2> [--system] | hittest <x> <y>
""")
}

switch cmd {
case "winlist":
    let listAll = args.contains("--all")
    let allSpaces = args.contains("--all-spaces")
    let pats = ["qq", "微信", "wechat", "农场", "小程序", "mini"]
    for w in allWindows(onScreenOnly: !allSpaces) where (w[kCGWindowLayer as String] as? Int) == 0 {
        let info = windowInfo(w)
        if listAll { jsonLine(info); continue }
        let hay = ((info["owner"] as? String ?? "") + " " + (info["title"] as? String ?? "")).lowercased()
        if pats.contains(where: { hay.contains($0) }) { jsonLine(info) }
    }

case "capture":
    guard args.count >= 3, let wid = Int(args[1]) else { die("capture <wid> <out.png>") }
    let out = args[2]
    guard let img = CGWindowListCreateImage(.null, .optionIncludingWindow, CGWindowID(wid),
                                            [.boundsIgnoreFraming, .bestResolution]) else {
        die("CGWindowListCreateImage 返回 nil（可能窗口已关闭，或缺少屏幕录制权限）", code: 3)
    }
    let url = URL(fileURLWithPath: out)
    guard let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil) else {
        die("无法创建输出文件 \(out)")
    }
    CGImageDestinationAddImage(dest, img, nil)
    if !CGImageDestinationFinalize(dest) { die("写 PNG 失败") }
    jsonLine(["ok": true, "path": out, "px_w": img.width, "px_h": img.height])

case "ocr":
    guard args.count >= 2 else { die("ocr <png> [--roi x,y,w,h]") }
    let path = args[1]
    var roi: CGRect? = nil
    if let i = args.firstIndex(of: "--roi"), i + 1 < args.count {
        let parts = args[i + 1].split(separator: ",").compactMap { Double($0) }
        if parts.count == 4 { roi = CGRect(x: parts[0], y: parts[1], width: parts[2], height: parts[3]) }
    }
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let full = CGImageSourceCreateImageAtIndex(src, 0, nil) else { die("读图失败: \(path)") }
    var img = full
    var originX = 0.0, originY = 0.0
    if let r = roi {
        // ROI 以整图像素为单位，左下原点（Vision 坐标系）
        guard let cropped = full.cropping(to: r) else { die("ROI 越界") }
        img = cropped
        originX = r.minX
        originY = r.minY
    }
    let W = Double(img.width), H = Double(img.height)
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    req.minimumTextHeight = 0.006
    let t0 = Date()
    do {
        try VNImageRequestHandler(cgImage: img, options: [:]).perform([req])
    } catch {
        die("Vision 失败: \(error)")
    }
    let ms = Date().timeIntervalSince(t0) * 1000
    jsonLine(["type": "meta", "img_w": Int(W), "img_h": Int(H), "ms": Int(ms),
              "roi": roi == nil ? NSNull() : ["x": originX, "y": originY]])
    for obs in (req.results ?? []) {
        guard let cand = obs.topCandidates(1).first else { continue }
        let bb = obs.boundingBox                       // 左下原点、归一化
        let x = (bb.minX * W + originX)
        let y = ((1 - bb.maxY) * H + originY)           // → 左上原点、像素
        jsonLine(["type": "text", "text": cand.string, "conf": (cand.confidence * 100).rounded() / 100,
                  "x": Int(x), "y": Int(y), "w": Int(bb.width * W), "h": Int(bb.height * H)])
    }

case "click", "drag":
    let isDrag = (cmd == "drag")
    let need = isDrag ? 6 : 4
    guard args.count >= need else { die("\(cmd) 参数不足") }
    guard let pid = Int32(args[1]) else { die("pid 非法") }
    let useSystem = args.contains("--system")
    let useWarp = args.contains("--warp")
    let useSky = args.contains("--sky")

    var skyFn: (@convention(c) (pid_t, CGEvent) -> Void)? = nil
    if useSky, let h = dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", RTLD_NOW) {
        for name in ["SLEventPostToPid", "SLSEventPostToPid"] {
            if let sym = dlsym(h, name) {
                skyFn = unsafeBitCast(sym, to: (@convention(c) (pid_t, CGEvent) -> Void).self)
                break
            }
        }
    }
    func deliver(_ e: CGEvent) {
        if useSystem { e.post(tap: .cghidEventTap) }
        else if let fn = skyFn { fn(pid, e) }
        else { e.postToPid(pid) }
    }

    var p1: CGPoint, p2: CGPoint
    if isDrag {
        p1 = CGPoint(x: Double(args[2])!, y: Double(args[3])!)
        p2 = CGPoint(x: Double(args[4])!, y: Double(args[5])!)
    } else {
        p1 = CGPoint(x: Double(args[2])!, y: Double(args[3])!)
        p2 = p1
    }
    let useGhost = args.contains("--ghost")
    var savedCursor = CGPoint.zero
    if useGhost {
        // 幽灵点击：记下用户光标 → 瞬移到目标 → 点击 → 移回原位
        savedCursor = CGEvent(source: nil)?.location ?? .zero
        CGWarpMouseCursorPosition(p1)
        CGAssociateMouseAndMouseCursorPosition(boolean_t(1))
        usleep(40_000)
    } else if useWarp || useSystem {
        CGWarpMouseCursorPosition(p1)
        CGAssociateMouseAndMouseCursorPosition(boolean_t(1))
        usleep(30_000)
    }
    func mouse(_ t: CGEventType, _ p: CGPoint) -> CGEvent? {
        let e = CGEvent(mouseEventSource: nil, mouseType: t, mouseCursorPosition: p, mouseButton: .left)
        e?.setIntegerValueField(.mouseEventClickState, value: 1)
        e?.setIntegerValueField(.mouseEventButtonNumber, value: 0)
        e?.setIntegerValueField(.mouseEventSubtype, value: 3)
        return e
    }
    guard let down = mouse(.leftMouseDown, p1) else { die("事件构造失败") }
    deliver(down)
    if isDrag {
        let steps = 12
        for i in 1...steps {
            let t = Double(i) / Double(steps)
            let p = CGPoint(x: p1.x + (p2.x - p1.x) * t, y: p1.y + (p2.y - p1.y) * t)
            if let d = mouse(.leftMouseDragged, p) { deliver(d) }
            usleep(18_000)
        }
    } else {
        usleep(30_000)
    }
    if let up = mouse(.leftMouseUp, p2) { deliver(up) }
    if useGhost {
        usleep(60_000)
        CGWarpMouseCursorPosition(savedCursor)
        CGAssociateMouseAndMouseCursorPosition(boolean_t(1))
    }
    jsonLine(["ok": true,
              "mode": useGhost ? "ghost" : (useSystem ? "system" : (skyFn != nil ? "sky" : "pid")),
              "from": [p1.x, p1.y], "to": [p2.x, p2.y],
              "cursor_restored": useGhost ? [savedCursor.x, savedCursor.y] : NSNull()])

case "find":
    // macd find <image.png> <template.png> [--threshold 0.75] [--coarse 4]
    // 多尺度模板匹配（窗口尺寸可能变化，所以按尺度搜索）。输出 JSON。
    guard args.count >= 3 else { die("find <image> <template> [--threshold 0.75]") }
    let imgPath = args[1], tplPath = args[2]
    var threshold = 0.75
    if let i = args.firstIndex(of: "--threshold"), i + 1 < args.count, let v = Double(args[i + 1]) { threshold = v }
    var coarse = 4
    if let i = args.firstIndex(of: "--coarse"), i + 1 < args.count, let v = Int(args[i + 1]) { coarse = max(1, v) }

    guard let imgFull = grayImage(imgPath, maxW: 0) else { die("读图失败: \(imgPath)") }
    guard let tplFull = grayImage(tplPath, maxW: 0) else { die("读模板失败: \(tplPath)") }
    var matches: [[String: Any]] = []
    // 尺度范围覆盖「窗口被缩小/放大」的情形
    for scale in [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.4, 1.6] {
        let tw = Int(Double(tplFull.w) * scale), th = Int(Double(tplFull.h) * scale)
        if tw < 8 || th < 8 || tw >= imgFull.w || th >= imgFull.h { continue }
        // 粗搜：整体降采样
        let sw = max(16, imgFull.w / coarse), sh = max(16, imgFull.h / coarse)
        guard let smallImg = grayImage(imgPath, maxW: sw), let smallTpl = grayImage(tplPath, maxW: tw / coarse) else { continue }
        let r = nccSearch(smallImg, smallTpl)
        if r.score < 0.45 { continue }
        // 精搜：以粗结果为中心，在 full-res 上用该尺度的模板复核
        let cx = r.x * coarse, cy = r.y * coarse
        let tScale = Int(round(Double(tplFull.w) * scale))
        guard let scaled = grayImage(tplPath, maxW: tScale) else { continue }
        // 在精搜区域做一次局部匹配：裁剪图像窗口再跑一次 ncc
        let padX = max(tw, 24), padY = max(th, 24)
        let x0 = max(0, cx - padX), y0 = max(0, cy - padY)
        let x1 = min(imgFull.w, cx + tw + padX), y1 = min(imgFull.h, cy + th + padY)
        var sub = [UInt8](repeating: 0, count: (x1 - x0) * (y1 - y0))
        for yy in y0..<y1 {
            for xx in x0..<x1 { sub[(yy - y0) * (x1 - x0) + (xx - x0)] = imgFull.px[yy * imgFull.w + xx] }
        }
        let subImg = (w: x1 - x0, h: y1 - y0, px: sub)
        let fine = nccSearch(subImg, scaled)
        let score = fine.score
        if score >= threshold {
            matches.append([
                "score": (score * 1000).rounded() / 1000,
                "scale": scale,
                "x": x0 + fine.x, "y": y0 + fine.y,
                "w": scaled.w, "h": scaled.h,
                "cx": x0 + fine.x + scaled.w / 2, "cy": y0 + fine.y + scaled.h / 2,
            ])
        }
    }
    matches.sort { ($0["score"] as! Double) > ($1["score"] as! Double) }
    jsonLine(["ok": true, "found": !matches.isEmpty, "count": matches.count,
              "image": [imgFull.w, imgFull.h], "matches": Array(matches.prefix(3))])

case "hash":
    // macd hash <image> → 8x8 灰度均值哈希（用于判断画面有无变化，零 token）
    guard args.count >= 2 else { die("hash <image>") }
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: args[1]) as CFURL, nil),
          let cg = CGImageSourceCreateImageAtIndex(src, 0, nil) else { die("读图失败") }
    let side = 8
    var rgba = [UInt8](repeating: 0, count: side * side * 4)
    _ = rgba.withUnsafeMutableBytes { raw -> Bool in
        guard let ctx = CGContext(data: raw.baseAddress, width: side, height: side, bitsPerComponent: 8,
                                  bytesPerRow: side * 4, space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
        ctx.interpolationQuality = .low
        ctx.draw(cg, in: CGRect(x: 0, y: 0, width: side, height: side))
        return true
    }
    var grays: [Double] = []
    for i in stride(from: 0, to: rgba.count, by: 4) {
        let r = Double(rgba[i]), g = Double(rgba[i+1]), b = Double(rgba[i+2])
        grays.append((r * 0.299 + g * 0.587 + b * 0.114))
    }
    let avg = grays.reduce(0, +) / Double(grays.count)
    var hashStr = ""
    for v in grays { hashStr += v > avg ? "1" : "0" }
    jsonLine(["ok": true, "hash": hashStr, "cells": side * side])

case "sample":
    // macd sample <image> --pts "x1,y1;x2,y2;..." [--r 10]
    // 在指定像素点周围取平均色，用于按标定格子读地块状态。
    guard args.count >= 2 else { die("sample <image> --pts \"x,y;x,y\"") }
    var ptsArg = ""
    if let i = args.firstIndex(of: "--pts"), i + 1 < args.count { ptsArg = args[i + 1] }
    var radius = 10
    if let i = args.firstIndex(of: "--r"), i + 1 < args.count, let v = Int(args[i + 1]) { radius = v }
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: args[1]) as CFURL, nil),
          let cg = CGImageSourceCreateImageAtIndex(src, 0, nil) else { die("读图失败") }
    let W = cg.width, H = cg.height
    var rgba = [UInt8](repeating: 0, count: W * H * 4)
    _ = rgba.withUnsafeMutableBytes { raw -> Bool in
        guard let ctx = CGContext(data: raw.baseAddress, width: W, height: H, bitsPerComponent: 8,
                                  bytesPerRow: W * 4, space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
        ctx.draw(cg, in: CGRect(x: 0, y: 0, width: W, height: H))
        return true
    }
    var out: [[String: Any]] = []
    for pair in ptsArg.split(separator: ";") {
        let parts = pair.split(separator: ",").compactMap { Int($0.trimmingCharacters(in: .whitespaces)) }
        guard parts.count == 2 else { continue }
        let (px, py) = (parts[0], parts[1])
        var sr = 0.0, sg = 0.0, sb = 0.0, n = 0.0
        for y in max(0, py - radius)...min(H - 1, py + radius) {
            for x in max(0, px - radius)...min(W - 1, px + radius) {
                let i = (y * W + x) * 4
                sr += Double(rgba[i]); sg += Double(rgba[i + 1]); sb += Double(rgba[i + 2]); n += 1
            }
        }
        if n > 0 {
            out.append(["x": px, "y": py, "r": Int(sr / n), "g": Int(sg / n), "b": Int(sb / n)])
        }
    }
    jsonLine(["ok": true, "img": [W, H], "samples": out])

case "idle":
    // macd idle → 距用户最后一次鼠标/键盘操作过了多少秒
    let types: [CGEventType] = [.leftMouseDown, .rightMouseDown, .mouseMoved, .leftMouseDragged,
                                .scrollWheel, .keyDown]
    var minSec = 1e9
    for t in types {
        let sec = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: t)
        if sec < minSec { minSec = sec }
    }
    jsonLine(["ok": true, "idleSeconds": (minSec * 10).rounded() / 10])

case "frontmost":
    // macd frontmost → 当前最前面的窗口属于谁
    for w in allWindows() where (w[kCGWindowLayer as String] as? Int) == 0 {
        let i = windowInfo(w)
        jsonLine(["ok": true, "pid": i["pid"]!, "owner": i["owner"]!, "title": i["title"]!])
        exit(0)
    }
    jsonLine(["ok": false])

case "perms":
    // macd perms [--prompt]：自检/触发 屏幕录制 + 辅助功能 授权
    let prompt = args.contains("--prompt")
    let axOpts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: prompt] as CFDictionary
    let ax = AXIsProcessTrustedWithOptions(axOpts)
    let sr = CGPreflightScreenCaptureAccess()
    if prompt && !sr { _ = CGRequestScreenCaptureAccess() }
    jsonLine(["ok": ax && sr, "accessibility": ax, "screenRecording": sr,
              "hint": ax && sr ? "权限齐全" :
                "缺权限：系统设置 → 隐私与安全性 → 给运行 DSH 的 App 勾选「屏幕录制」和「辅助功能」，然后**重启该 App**"])

case "grid":
    // macd grid <image> --x0 N --y0 N --w N --h N --cols 6 --rows 4 [--pad 0.18]
    // 按网格采样每格颜色，判断空地/作物（纯图像分析，不需要点击）。
    guard args.count >= 2 else { die("grid <image> --x0 .. --y0 .. --w .. --h .. --cols 6 --rows 4") }
    func num(_ key: String, _ def: Double) -> Double {
        if let i = args.firstIndex(of: "--\(key)"), i + 1 < args.count, let v = Double(args[i + 1]) { return v }
        return def
    }
    let gx0 = num("x0", 0), gy0 = num("y0", 0), gw = num("w", 0), gh = num("h", 0)
    let cols = Int(num("cols", 6)), rows = Int(num("rows", 4))
    let padRatio = num("pad", 0.18)
    guard let img = grayImage(args[1], maxW: 0), gw > 0, gh > 0, cols > 0, rows > 0 else { die("参数非法") }
    // 需要彩色，重新读一次 RGB
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: args[1]) as CFURL, nil),
          let cg = CGImageSourceCreateImageAtIndex(src, 0, nil) else { die("读图失败") }
    let W = cg.width, H = cg.height
    var rgba = [UInt8](repeating: 0, count: W * H * 4)
    let cs = CGColorSpaceCreateDeviceRGB()
    _ = rgba.withUnsafeMutableBytes { raw -> Bool in
        guard let ctx = CGContext(data: raw.baseAddress, width: W, height: H, bitsPerComponent: 8,
                                  bytesPerRow: W * 4, space: cs,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
        ctx.draw(cg, in: CGRect(x: 0, y: 0, width: W, height: H))
        return true
    }
    func pixel(_ x: Int, _ y: Int) -> (Double, Double, Double) {
        let i = (y * W + x) * 4
        return (Double(rgba[i]), Double(rgba[i + 1]), Double(rgba[i + 2]))
    }
    let cw = gw / Double(cols), ch = gh / Double(rows)
    var cells: [[String: Any]] = []
    for r in 0..<rows {
        for c in 0..<cols {
            let x0i = Int(gx0 + cw * Double(c)), y0i = Int(gy0 + ch * Double(r))
            let x1i = Int(gx0 + cw * Double(c + 1)), y1i = Int(gy0 + ch * Double(r + 1))
            let pdx = Int(Double(x1i - x0i) * padRatio), pdy = Int(Double(y1i - y0i) * padRatio)
            var sumR = 0.0, sumG = 0.0, sumB = 0.0, n = 0.0, greenN = 0.0
            for y in max(0, y0i + pdy)..<min(H, max(y0i + pdy + 1, y1i - pdy)) {
                for x in max(0, x0i + pdx)..<min(W, max(x0i + pdx + 1, x1i - pdx)) {
                    let (pr, pg, pb) = pixel(x, y)
                    sumR += pr; sumG += pg; sumB += pb; n += 1
                    if pg > pr + 12 && pg > pb + 12 { greenN += 1 }
                }
            }
            guard n > 0 else { continue }
            let mr = sumR / n, mg = sumG / n, mb = sumB / n
            let green = greenN / n
            // 空地：棕褐色土壤，绿色极少
            let empty = green < 0.06 && mr > mg && mg > mb && mr > 90
            cells.append(["row": r, "col": c,
                          "r": Int(mr), "g": Int(mg), "b": Int(mb),
                          "green": (green * 1000).rounded() / 1000,
                          "empty": empty])
        }
    }
    jsonLine(["ok": true, "cols": cols, "rows": rows, "img": [W, H],
              "grid": ["x0": gx0, "y0": gy0, "w": gw, "h": gh], "cells": cells])

case "activate":
    // 把目标 App 置前（AX frontmost）。幽灵点击前必须先做，否则第一次点击只激活窗口不触发控件。
    guard args.count >= 2, let pid = Int32(args[1]) else { die("activate <pid>") }
    let app = AXUIElementCreateApplication(pid)
    var r = AXUIElementSetAttributeValue(app, kAXFrontmostAttribute as CFString, kCFBooleanTrue)
    usleep(120_000)
    jsonLine(["ok": r == .success, "pid": Int(pid), "axResult": Int(r.rawValue)])

case "cursorpos":
    if let e = CGEvent(source: nil) {
        let p = e.location
        jsonLine(["x": Int(p.x), "y": Int(p.y)])
    } else {
        jsonLine(["x": -1, "y": -1])
    }

case "bgclick":
    // ── 真后台点击（Chromium 兼容配方）────────────────────────────────
    // 移植自 cua-driver (MIT) 的 click_at_xy_chromium 路由计划：
    //   macd bgclick <pid> <wid> <screenX> <screenY> [--count N] [--no-primer]
    //
    // 五步缺一不可：
    //   0) activate_without_raise：给目标窗口发 focus 记录（不置前、不抢键）
    //   1) mouseMoved@目标        (f0=2)
    //   2) primer down/up@(-1,-1) (f0=1/2) —— 推进 Chromium 的 user-activation 门禁
    //   3) down/up@目标           (f0=3, clickState 1..N)
    // 每个事件都打：f40=pid（Chromium 合成事件过滤）、f51/f91/f92=CGWindowID（窗口路由）、
    // f58=同一 gesture id、CGEventSetWindowLocation(屏幕坐标)；事件源必须是 HIDSystemState。
    guard args.count >= 5, let pid = Int32(args[1]), let wid = UInt32(args[2]),
          let sx = Double(args[3]), let sy = Double(args[4]) else {
        die("bgclick <pid> <wid> <screenX> <screenY> [--count N] [--no-primer]")
    }
    var clickCount = 1
    if let i = args.firstIndex(of: "--count"), i + 1 < args.count, let n = Int(args[i + 1]) {
        clickCount = max(1, min(2, n))
    }
    let withPrimer = !args.contains("--no-primer")

    let skyPath = "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight"
    guard let sky = dlopen(skyPath, RTLD_NOW) else { die("SkyLight dlopen 失败") }
    guard let postSym = dlsym(sky, "SLEventPostToPid") else { die("缺少 SLEventPostToPid") }
    let postToPid = unsafeBitCast(postSym, to: (@convention(c) (pid_t, CGEvent) -> Void).self)
    guard let setFieldSym = dlsym(sky, "SLEventSetIntegerValueField") else { die("缺少 SLEventSetIntegerValueField") }
    let setField = unsafeBitCast(setFieldSym, to: (@convention(c) (CGEvent, UInt32, Int64) -> Void).self)
    let postRecordSym = dlsym(sky, "SLPSPostEventRecordTo")
    let postRecord = postRecordSym.map { unsafeBitCast($0, to: (@convention(c) (UnsafeRawPointer, UnsafeRawPointer) -> Int32).self) }

    guard let cg = dlopen("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics", RTLD_NOW),
          let setLocSym = dlsym(cg, "CGEventSetWindowLocation") else { die("缺少 CGEventSetWindowLocation") }
    let setWindowLocation = unsafeBitCast(setLocSym, to: (@convention(c) (CGEvent, CGPoint) -> Void).self)

    // ── 0) 激活不置前：0xF8 字节 focus 记录 ──
    var activated = false
    if let post = postRecord {
        var psn = [UInt8](repeating: 0, count: 8)
        var gotPSN = false
        // 优先 GetProcessForPID（简单可靠）
        if let asPath = dlopen("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices", RTLD_NOW),
           let sym = dlsym(asPath, "GetProcessForPID") {
            let fn = unsafeBitCast(sym, to: (@convention(c) (pid_t, UnsafeMutableRawPointer) -> Int32).self)
            gotPSN = fn(pid, &psn) == 0
        }
        if gotPSN {
            var buf = [UInt8](repeating: 0, count: 0xF8)
            buf[0x04] = 0xF8
            buf[0x08] = 0x0D
            buf[0x3C] = UInt8(wid & 0xFF)
            buf[0x3D] = UInt8((wid >> 8) & 0xFF)
            buf[0x3E] = UInt8((wid >> 16) & 0xFF)
            buf[0x3F] = UInt8((wid >> 24) & 0xFF)
            func sendRecord(_ bytes: inout [UInt8]) {
                psn.withUnsafeBytes { psnPtr in
                    bytes.withUnsafeBytes { bufPtr in
                        _ = post(psnPtr.baseAddress!, bufPtr.baseAddress!)
                    }
                }
            }
            // ① focus 记录
            var focusBuf = buf
            focusBuf[0x8A] = 0x01
            sendRecord(&focusBuf)
            usleep(120_000)
            // ② make-key 记录对（cua: event_kind 0x01/0x02）——我原先漏了这步
            for kind in [UInt8(0x01), UInt8(0x02)] {
                var keyBuf = buf
                keyBuf[0x08] = kind
                sendRecord(&keyBuf)
                usleep(60_000)
            }
            activated = true
        }
    }
    // AppKit 需要一点时间更新 key-window 路由
    usleep(50_000)

    guard let source = CGEventSource(stateID: .hidSystemState) else { die("CGEventSource 创建失败") }
    let groupId = Int64(Date().timeIntervalSince1970.truncatingRemainder(dividingBy: 2000) * 1_000_000)
    var posted = 0

    func makeEvent(_ type: CGEventType, _ p: CGPoint, phase: Int64, clickState: Int64) -> CGEvent? {
        guard let e = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: p, mouseButton: .left) else { return nil }
        setField(e, 0, phase)          // kCGMouseEventNumber = 手势阶段
        setField(e, 1, clickState)     // clickState
        setField(e, 3, 0)              // 左键
        setField(e, 7, 3)              // NSEventSubtypeTouch
        setField(e, 40, Int64(pid))    // ★ Chromium 合成事件过滤
        setField(e, 51, Int64(wid))    // windowNumber
        setField(e, 91, Int64(wid))    // WindowUnderMousePointer
        setField(e, 92, Int64(wid))    // WindowUnderMousePointerThatCanHandleThisEvent
        setField(e, 58, groupId)       // 同一手势
        setWindowLocation(e, p)        // 屏幕坐标戳
        // 公共字段也补一份，便于窗口系统对齐
        e.setIntegerValueField(.mouseEventClickState, value: clickState)
        e.setIntegerValueField(.mouseEventSubtype, value: 3)
        return e
    }

    // cua-driver 的做法是「belt+suspenders」：SkyLight 私有路径 + 公共 CGEventPostToPid 都发一遍。
    // 我们之前只发了 SkyLight，实测对本容器无效；补上公共 API 再试。
    func postBoth(_ pid: pid_t, _ e: CGEvent) {
        postToPid(pid, e)          // SkyLight 私有路径（Chromium/Catalyst）
        e.postToPid(pid)           // 公共 API（AppKit 目标；Swift 里是实例方法）
    }

    let target = CGPoint(x: sx, y: sy)
    let off = CGPoint(x: -1, y: -1)

    // 1) mouseMoved@目标
    if let e = makeEvent(.mouseMoved, target, phase: 2, clickState: 0) {
        postBoth(pid, e); posted += 1; usleep(15_000)
    }
    // 2) primer：屏幕外 down/up
    if withPrimer {
        if let d = makeEvent(.leftMouseDown, off, phase: 1, clickState: 1) { postBoth(pid, d); usleep(1_000) }
        if let u = makeEvent(.leftMouseUp, off, phase: 2, clickState: 1) { postBoth(pid, u); posted += 1; usleep(100_000) }
    }
    // 3) 目标 down/up
    for i in 1...clickCount {
        let st = Int64(i)
        if let d = makeEvent(.leftMouseDown, target, phase: 3, clickState: st) {
            postBoth(pid, d); usleep(1_000)
        }
        if let u = makeEvent(.leftMouseUp, target, phase: 3, clickState: st) {
            postBoth(pid, u); posted += 1
            if i < clickCount { usleep(80_000) }
        }
    }
    jsonLine(["ok": true, "mode": "sky-bg", "activated": activated, "primer": withPrimer,
              "count": clickCount, "events": posted, "target": [sx, sy], "wid": wid])

case "hittest":
    guard args.count >= 3, let x = Double(args[1]), let y = Double(args[2]) else { die("hittest <x> <y>") }
    for w in allWindows() where (w[kCGWindowLayer as String] as? Int) == 0 {
        let i = windowInfo(w)
        let wx = i["x"] as! Double, wy = i["y"] as! Double
        let ww = i["w"] as! Double, wh = i["h"] as! Double
        if x >= wx, x <= wx + ww, y >= wy, y <= wy + wh { jsonLine(i); exit(0) }
    }
    jsonLine(["wid": -1, "owner": "", "title": ""])

default:
    die("未知命令: \(cmd)")
}
