import Foundation
import Vision
import CoreGraphics
import ImageIO

// 用 Apple Vision 识别 PNG 中的文字，输出 文本 + 归一化包围盒（左上原点）
guard CommandLine.arguments.count >= 2 else {
    FileHandle.standardError.write("usage: ocr <png>\n".data(using: .utf8)!)
    exit(2)
}
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
      let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
    FileHandle.standardError.write("cannot read image\n".data(using: .utf8)!)
    exit(1)
}

let W = Double(img.width), H = Double(img.height)
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = false
req.recognitionLanguages = ["zh-Hans", "en-US"]
req.minimumTextHeight = 0.008

let t0 = Date()
let handler = VNImageRequestHandler(cgImage: img, options: [:])
do { try handler.perform([req]) } catch {
    FileHandle.standardError.write("vision failed: \(error)\n".data(using: .utf8)!)
    exit(1)
}
let dt = Date().timeIntervalSince(t0) * 1000

var rows: [(String, CGRect, Float)] = []
for obs in (req.results ?? []) {
    guard let cand = obs.topCandidates(1).first else { continue }
    let bb = obs.boundingBox   // 左下原点，归一化
    let rect = CGRect(x: bb.minX, y: 1 - bb.maxY, width: bb.width, height: bb.height)
    rows.append((cand.string, rect, cand.confidence))
}
rows.sort { $0.1.minY < $1.1.minY }

print(String(format: "# image %.0fx%.0f  vision %.1fms  blocks %d", W, H, dt, rows.count))
for (text, r, conf) in rows {
    let px = Int(r.minX * W), py = Int(r.minY * H), pw = Int(r.width * W), ph = Int(r.height * H)
    print(String(format: "  [%4d,%4d %4dx%-4d conf %.2f] %@", px, py, pw, ph, conf, text))
}
