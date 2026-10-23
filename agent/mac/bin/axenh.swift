import Foundation
import ApplicationServices

// 开启 Chromium/CEF 的完整可访问性树：AXEnhancedUserInterface + AXManualAccessibility
let a = CommandLine.arguments
guard a.count >= 2, let pid = Int32(a[1]) else { print("usage: axenh <pid>"); exit(2) }
let app = AXUIElementCreateApplication(pid)
for name in ["AXEnhancedUserInterface", "AXManualAccessibility"] {
    let r = AXUIElementSetAttributeValue(app, name as CFString, kCFBooleanTrue)
    print("set \(name) -> \(r.rawValue)")
}
usleep(600_000)
func attr(_ el: AXUIElement, _ n: String) -> AnyObject? {
    var v: CFTypeRef?
    return AXUIElementCopyAttributeValue(el, n as CFString, &v) == .success ? v : nil
}
func walk(_ el: AXUIElement, _ d: Int) {
    let role = (attr(el, kAXRoleAttribute) as? String) ?? ""
    let title = (attr(el, kAXTitleAttribute) as? String) ?? ""
    let desc = (attr(el, "AXDescription") as? String) ?? ""
    let actions = (attr(el, "AXActionNames") as? [String]) ?? []
    print(String(repeating: "  ", count: d) + role + (title.isEmpty ? "" : " title=\(title)") +
          (desc.isEmpty ? "" : " desc=\(desc)") + (actions.isEmpty ? "" : " actions=\(actions)"))
    if d >= 6 { return }
    if let kids = attr(el, kAXChildrenAttribute) as? [AXUIElement] {
        for k in kids.prefix(60) { walk(k, d + 1) }
    }
}
if let wins = attr(app, kAXWindowsAttribute) as? [AXUIElement] {
    for w in wins.prefix(2) { walk(w, 0) }
}
