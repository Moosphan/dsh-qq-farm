import Foundation
import CoreGraphics
// 用法: drag <pid> <x1> <y1> <x2> <y2> [--system]
let a = CommandLine.arguments
guard a.count >= 6, let pid = Int32(a[1]),
      let x1 = Double(a[2]), let y1 = Double(a[3]),
      let x2 = Double(a[4]), let y2 = Double(a[5]) else {
    print("usage: drag <pid> <x1> <y1> <x2> <y2> [--system]"); exit(2)
}
let sys = a.contains("--system")
func post(_ t: CGEventType, _ p: CGPoint) {
    guard let e = CGEvent(mouseEventSource: nil, mouseType: t, mouseCursorPosition: p, mouseButton: .left) else { return }
    e.setIntegerValueField(.mouseEventClickState, value: 1)
    if sys { e.post(tap: .cghidEventTap) } else { e.postToPid(pid) }
}
let steps = 12
let p1 = CGPoint(x: x1, y: y1), p2 = CGPoint(x: x2, y: y2)
if sys { CGWarpMouseCursorPosition(p1); CGAssociateMouseAndMouseCursorPosition(boolean_t(1)); usleep(30_000) }
post(.leftMouseDown, p1)
for i in 1...steps {
    let t = Double(i) / Double(steps)
    let p = CGPoint(x: p1.x + (p2.x - p1.x) * t, y: p1.y + (p2.y - p1.y) * t)
    post(.leftMouseDragged, p)
    usleep(18_000)
}
post(.leftMouseUp, p2)
print("dragged (\(x1),\(y1)) → (\(x2),\(y2)) mode=\(sys ? "system" : "pid")")
