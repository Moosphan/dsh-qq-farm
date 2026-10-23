import Foundation
import CoreGraphics

// 列出所有可见窗口：windowID / pid / owner / title / bounds
let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
let pats = ["qq", "wechat", "微信", "农", "小程序"]
for w in list {
    let owner = (w[kCGWindowOwnerName as String] as? String) ?? "?"
    let name = (w[kCGWindowName as String] as? String) ?? ""
    let pid = (w[kCGWindowOwnerPID as String] as? Int) ?? -1
    let wid = (w[kCGWindowNumber as String] as? Int) ?? -1
    let layer = (w[kCGWindowLayer as String] as? Int) ?? -1
    let b = w[kCGWindowBounds as String] as? [String: Any] ?? [:]
    let bw = (b["Width"] as? Double) ?? 0
    let bh = (b["Height"] as? Double) ?? 0
    let bx = (b["X"] as? Double) ?? 0
    let by = (b["Y"] as? Double) ?? 0
    let hay = (owner + " " + name).lowercased()
    let hit = pats.contains { hay.contains($0) } || owner.contains("QQ") || owner.contains("WeChat")
    if hit && layer == 0 {
        print(String(format: "wid=%d pid=%d layer=%d %.0fx%.0f @(%.0f,%.0f) owner=%@ title=%@",
                     wid, pid, layer, bw, bh, bx, by, owner, name.isEmpty ? "-" : name))
    }
}
