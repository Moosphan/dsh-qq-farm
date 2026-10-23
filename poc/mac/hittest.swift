import Foundation
import CoreGraphics
// 用法: hittest <x> <y>  → 打印该屏幕坐标处（front-to-back 第一个）命中的窗口
let a = CommandLine.arguments
guard a.count >= 3, let x = Double(a[1]), let y = Double(a[2]) else { print("usage: hittest <x> <y>"); exit(2) }
let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
for w in list {
    let b = w[kCGWindowBounds as String] as? [String: Any] ?? [:]
    let bx = (b["X"] as? Double) ?? 0, by = (b["Y"] as? Double) ?? 0
    let bw = (b["Width"] as? Double) ?? 0, bh = (b["Height"] as? Double) ?? 0
    let layer = (w[kCGWindowLayer as String] as? Int) ?? -1
    if x >= bx, x <= bx + bw, y >= by, y <= by + bh, layer == 0 {
        let owner = (w[kCGWindowOwnerName as String] as? String) ?? "?"
        let name = (w[kCGWindowName as String] as? String) ?? ""
        let wid = (w[kCGWindowNumber as String] as? Int) ?? -1
        let pid = (w[kCGWindowOwnerPID as String] as? Int) ?? -1
        print("命中: wid=\(wid) pid=\(pid) owner=\(owner) title=\(name.isEmpty ? "-" : name)")
        exit(0)
    }
}
print("未命中任何窗口（可能点在桌面/其他 space）")
