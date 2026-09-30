import AppKit
import UserNotifications

func option(_ name: String, _ args: [String]) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

func report(_ path: String?, _ text: String) {
    guard let path = path else { return }
    try? text.write(toFile: path, atomically: true, encoding: .utf8)
}

final class Delegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let args = Array(CommandLine.arguments.dropFirst())

    func applicationWillFinishLaunching(_ note: Notification) {
        UNUserNotificationCenter.current().delegate = self
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        switch args.first {
        case "post": post()
        case "remove": remove()
        default:
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) { NSApp.terminate(nil) }
        }
    }

    func post() {
        let result = option("--result", args)
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { granted, error in
            guard granted else {
                report(result, "denied \(error?.localizedDescription ?? "")")
                DispatchQueue.main.async { NSApp.terminate(nil) }
                return
            }
            let content = UNMutableNotificationContent()
            content.title = option("--title", self.args) ?? "Claude Code"
            content.subtitle = option("--subtitle", self.args) ?? ""
            content.body = option("--body", self.args) ?? ""
            if option("--sound", self.args) != "off" {
                content.sound = .default
            }
            let id = option("--id", self.args) ?? UUID().uuidString
            content.threadIdentifier = id
            var info: [String: String] = [:]
            if let cmd = option("--click", self.args) { info["click"] = cmd }
            content.userInfo = info
            center.add(UNNotificationRequest(identifier: id, content: content, trigger: nil)) { error in
                report(result, error.map { "error \($0.localizedDescription)" } ?? "ok")
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { NSApp.terminate(nil) }
            }
        }
    }

    func remove() {
        if let id = option("--id", args) {
            UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [id])
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { NSApp.terminate(nil) }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        if response.actionIdentifier == UNNotificationDefaultActionIdentifier,
           let cmd = response.notification.request.content.userInfo["click"] as? String {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/sh")
            p.arguments = ["-c", cmd]
            try? p.run()
            p.waitUntilExit()
        }
        done()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { NSApp.terminate(nil) }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .list, .sound])
    }
}

let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
