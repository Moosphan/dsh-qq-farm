import Foundation
import CoreGraphics
import ApplicationServices

// 用法: keycmd <pid> <keyname> [--cmd|--shift|--opt|--ctrl]
// 用于给目标进程发送组合键（先尝试 SkyLight 通道，再退回 postToPid，最后系统级）
let args = CommandLine.arguments
guard args.count >= 3, let pid = Int32(args[1]) else { print("usage: keycmd <pid> <key> [--cmd]"); exit(2) }
let keyName = args[2]
let codes: [String: CGKeyCode] = [
    "r": 15, "w": 13, "q": 12, "escape": 53, "return": 36, "tab": 48,
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "j": 38, "k": 40, "l": 37,
    "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "n": 45, "m": 46,
    "space": 49, "f5": 96, "f12": 111,
]
guard let code = codes[keyName.lowercased()] else { print("未知按键: \(keyName)"); exit(2) }
var flags: CGEventFlags = []
if args.contains("--cmd") { flags.insert(.maskCommand) }
if args.contains("--shift") { flags.insert(.maskShift) }
if args.contains("--opt") { flags.insert(.maskAlternate) }
if args.contains("--ctrl") { flags.insert(.maskControl) }

typealias PostToPidFn = @convention(c) (pid_t, CGEvent) -> Void
var skyFn: PostToPidFn? = nil
if let h = dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", RTLD_NOW),
   let sym = dlsym(h, "SLEventPostToPid") {
    skyFn = unsafeBitCast(sym, to: PostToPidFn.self)
}
func send(_ e: CGEvent) {
    if args.contains("--system") { e.post(tap: .cghidEventTap) }
    else if let fn = skyFn { e.setIntegerValueField(.keyboardEventAutorepeat, value: 0); fn(pid, e) }
    else { e.postToPid(pid) }
}
// 某些容器要求 AppKit-active 才收键盘：先把窗口设为 key（不置前）
let useActivate = !args.contains("--no-activate")
if useActivate,
   let asPath = dlopen("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices", RTLD_NOW),
   let gps = dlsym(asPath, "GetProcessForPID") {
    var psn = [UInt8](repeating: 0, count: 8)
    let fn = unsafeBitCast(gps, to: (@convention(c) (pid_t, UnsafeMutableRawPointer) -> Int32).self)
    if fn(pid, &psn) == 0,
       let h = dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", RTLD_NOW),
       let rec = dlsym(h, "SLPSPostEventRecordTo") {
        let post = unsafeBitCast(rec, to: (@convention(c) (UnsafeRawPointer, UnsafeRawPointer) -> Int32).self)
        var buf = [UInt8](repeating: 0, count: 0xF8)
        buf[0x04] = 0xF8; buf[0x08] = 0x0D; buf[0x8A] = 0x01
        psn.withUnsafeBytes { p in buf.withUnsafeBytes { b in _ = post(p.baseAddress!, b.baseAddress!) } }
        usleep(40_000)
    }
}
guard let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
      let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else { exit(1) }
down.flags = flags; up.flags = flags
send(down); usleep(25_000); send(up)
print("{\"ok\":true,\"key\":\"\(keyName)\",\"flags\":\(flags.rawValue)}")
