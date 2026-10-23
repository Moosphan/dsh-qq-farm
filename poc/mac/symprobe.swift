import Foundation

let targets: [(String, String)] = [
  ("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", "SLEventPostToPid"),
  ("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", "SLEventSetIntegerValueField"),
  ("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", "SLEventSetAuthenticationMessage"),
  ("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", "SLPSPostEventRecordTo"),
  ("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", "SLPSGetFrontProcess"),
  ("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", "SLSGetFrontWindowOfProcess"),
  ("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics", "CGEventSetWindowLocation"),
  ("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics", "CGEventGetWindowLocation"),
  ("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices", "GetProcessForPID"),
  ("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", "SLEventPostToPSN"),
  ("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", "SLSUpdateSystemActivityWithLocation"),
  ("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", "CGSTickleActivityMonitor"),
  ("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", "CGSMainConnectionID"),
  ("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", "SLSEventAuthenticationMessageNew"),
  ("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", "SLEventSetAuthenticationMessage"),
  ("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", "SLPSGetProcessForPID"),
]
var handles: [String: UnsafeMutableRawPointer] = [:]
for (path, name) in targets {
  if handles[path] == nil { handles[path] = dlopen(path, RTLD_NOW) }
  let h = handles[path]
  let sym = h != nil ? dlsym(h!, name) : nil
  if let s = sym {
    print("✓ \(name)  \(String(format: "%p", Int(bitPattern: s)))")
  } else {
    print("✗ \(name)  \(h == nil ? "(dlopen 失败)" : "(无此符号)")")
  }
}
