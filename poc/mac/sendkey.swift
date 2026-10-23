import Foundation
import CoreGraphics
// 用法: sendkey <pid> <keycode> [--sky] [--system]
let a = CommandLine.arguments
guard a.count >= 3, let pid = Int32(a[1]), let code = UInt16(a[2]) else { print("usage: sendkey <pid> <keycode> [--sky]"); exit(2) }
typealias PostToPidFn = @convention(c) (pid_t, CGEvent) -> Void
var skyFn: PostToPidFn? = nil
if a.contains("--sky"), let h = dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", RTLD_NOW) {
    for name in ["SLEventPostToPid", "SLSEventPostToPid"] {
        if let sym = dlsym(h, name) { skyFn = unsafeBitCast(sym, to: PostToPidFn.self); print("sky symbol: \(name)"); break }
    }
}
func send(_ e: CGEvent) {
    if a.contains("--system") { e.post(tap: .cghidEventTap) }
    else if let fn = skyFn { fn(pid, e) }
    else { e.postToPid(pid) }
}
guard let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
      let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else { print("event fail"); exit(1) }
send(down); usleep(30_000); send(up)
print("sent keycode \(code) → pid \(pid) mode=\(a.contains("--system") ? "system" : (skyFn != nil ? "sky" : "cg"))")
