import Foundation
import CoreGraphics
import ApplicationServices

// 用法: postclick <pid> <screenX> <screenY> [--sky] [--hover]
//   --sky   走 SkyLight 私有框架的 SLEventPostToPid（Chromium 渲染层"信任信封"）
//   --hover 只发 mouseMoved（最安全的探测）
//   --trust 仅打印辅助功能/屏幕录制权限状态

func axTrusted(_ prompt: Bool) -> Bool {
    let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: prompt] as CFDictionary
    return AXIsProcessTrustedWithOptions(opts)
}

let args = CommandLine.arguments
if args.contains("--trust") {
    print("AXIsProcessTrusted = \(axTrusted(false))")
    print("CGPreflightScreenCaptureAccess = \(CGPreflightScreenCaptureAccess())")
    exit(0)
}
guard args.count >= 4, let pid = Int32(args[1]), let sx = Double(args[2]), let sy = Double(args[3]) else {
    FileHandle.standardError.write("usage: postclick <pid> <x> <y> [--sky] [--hover]\n".data(using: .utf8)!)
    exit(2)
}
let useSystem = CommandLine.arguments.contains("--system")
let useWarp = CommandLine.arguments.contains("--warp")
let useSky = args.contains("--sky")
let hoverOnly = args.contains("--hover")

print("AX trusted: \(axTrusted(false))  screenCapture: \(CGPreflightScreenCaptureAccess())")

let pt = CGPoint(x: sx, y: sy)
if useWarp {
    CGWarpMouseCursorPosition(pt)
    CGAssociateMouseAndMouseCursorPosition(boolean_t(1))
    usleep(40_000)
    let p = CGEvent(source: nil)?.location ?? .zero
    print("cursor 现在在 (\(p.x),\(p.y))")
}

// SkyLight 私有符号
typealias PostToPidFn = @convention(c) (pid_t, CGEvent) -> Void
var skyFn: PostToPidFn? = nil
if useSky {
    let path = "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight"
    if let h = dlopen(path, RTLD_NOW) {
        for name in ["SLEventPostToPid", "SLSEventPostToPid", "_SLEventPostToPid"] {
            if let sym = dlsym(h, name) {
                skyFn = unsafeBitCast(sym, to: PostToPidFn.self)
                print("SkyLight: 找到符号 \(name) @ \(sym)")
                break
            }
        }
        if skyFn == nil { print("SkyLight: 未找到 SLEventPostToPid，尝试列出候选:") 
            // 粗扫：打印含 PostToPid 的符号不可行，这里只报告失败
        }
    } else {
        print("SkyLight: dlopen 失败")
    }
}

func post(_ type: CGEventType, _ e: CGEvent) {
    if useSystem {
        e.post(tap: .cghidEventTap)          // 系统级：会移动真实光标、抢焦点（仅用于坐标标定）
    } else if let fn = skyFn {
        fn(pid, e)
    } else {
        e.postToPid(pid)
    }
}

func makeEvent(_ type: CGEventType, _ button: CGMouseButton = .left) -> CGEvent? {
    let e = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: pt, mouseButton: button)
    // 打上真实鼠标事件常带的字段，尽量贴近硬件事件
    e?.setIntegerValueField(.mouseEventClickState, value: 1)
    e?.setIntegerValueField(.mouseEventButtonNumber, value: 0)
    e?.setIntegerValueField(.mouseEventSubtype, value: 3) // NSEventSubtypeTouch? 真实点击常见值
    return e
}

if hoverOnly {
    if let e = makeEvent(.mouseMoved) { post(.mouseMoved, e); print("sent mouseMoved → pid \(pid) @(\(sx),\(sy))") }
    exit(0)
}

guard let down = makeEvent(.leftMouseDown), let up = makeEvent(.leftMouseUp) else {
    print("无法构造 CGEvent"); exit(1)
}
post(.leftMouseDown, down)
usleep(30_000)
post(.leftMouseUp, up)
print("sent click → pid \(pid) @(\(sx),\(sy)) mode=\(useSystem ? "system" : (useSky ? "sky" : "cg"))")
