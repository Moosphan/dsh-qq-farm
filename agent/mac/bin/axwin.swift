import Foundation
import ApplicationServices

// 用法: axwin <pid> [--pos x,y] [--size w,h] [--info]
// 通过 Accessibility API 读写窗口几何，避免用鼠标拖拽。
let a = CommandLine.arguments
guard a.count >= 2, let pid = Int32(a[1]) else { print("usage: axwin <pid> [--pos x,y] [--size w,h] [--info]"); exit(2) }

let app = AXUIElementCreateApplication(pid)
var value: CFTypeRef?
guard AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &value) == .success,
      let windows = value as? [AXUIElement], let win = windows.first else {
    print("{\"ok\":false,\"error\":\"无法获取窗口（需要辅助功能权限）\"}")
    exit(3)
}

func argValue(_ flag: String) -> [Double]? {
    guard let i = a.firstIndex(of: flag), i + 1 < a.count else { return nil }
    return a[i + 1].split(separator: ",").compactMap { Double($0) }
}

if let p = argValue("--pos") , p.count == 2 {
    var pt = CGPoint(x: p[0], y: p[1])
    if let v = AXValueCreate(.cgPoint, &pt) {
        let e = AXUIElementSetAttributeValue(win, kAXPositionAttribute as CFString, v)
        if e != .success { print("pos 设置失败: \(e.rawValue)") }
    }
}
if let s = argValue("--size"), s.count == 2 {
    var sz = CGSize(width: s[0], height: s[1])
    if let v = AXValueCreate(.cgSize, &sz) {
        let e = AXUIElementSetAttributeValue(win, kAXSizeAttribute as CFString, v)
        if e != .success { print("size 设置失败: \(e.rawValue)") }
    }
}

var posRef: CFTypeRef?
var sizeRef: CFTypeRef?
var pt = CGPoint.zero
var sz = CGSize.zero
if AXUIElementCopyAttributeValue(win, kAXPositionAttribute as CFString, &posRef) == .success,
   let v = posRef {
    AXValueGetValue(v as! AXValue, .cgPoint, &pt)
}
if AXUIElementCopyAttributeValue(win, kAXSizeAttribute as CFString, &sizeRef) == .success,
   let v = sizeRef {
    AXValueGetValue(v as! AXValue, .cgSize, &sz)
}
print("{\"ok\":true,\"pos\":[\(Int(pt.x)),\(Int(pt.y))],\"size\":[\(Int(sz.width)),\(Int(sz.height))]}")
