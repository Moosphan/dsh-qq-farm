import Foundation
import ApplicationServices

let a = CommandLine.arguments
guard a.count >= 2, let pid = Int32(a[1]) else { print("usage: axtree <pid> [depth]"); exit(2) }
let maxDepth = a.count > 2 ? Int(a[2]) ?? 4 : 4
let app = AXUIElementCreateApplication(pid)

func attr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var v: CFTypeRef?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}
func str(_ el: AXUIElement, _ name: String) -> String {
    (attr(el, name) as? String) ?? ""
}
func walk(_ el: AXUIElement, _ depth: Int) {
    let role = str(el, kAXRoleAttribute)
    let title = str(el, kAXTitleAttribute)
    let desc = str(el, kAXDescriptionAttribute)
    let value = (attr(el, kAXValueAttribute) as? String) ?? ""
    let actions = (attr(el, "AXActionNames") as? [String]) ?? []
    var line = String(repeating: "  ", count: depth) + role
    if !title.isEmpty { line += " title=\(title)" }
    if !desc.isEmpty { line += " desc=\(desc)" }
    if !value.isEmpty { line += " value=\(String(value.prefix(24)))" }
    if !actions.isEmpty { line += " actions=\(actions)" }
    print(line)
    if depth >= maxDepth { return }
    if let kids = attr(el, kAXChildrenAttribute) as? [AXUIElement] {
        for k in kids.prefix(40) { walk(k, depth + 1) }
    }
}
walk(app, 0)
