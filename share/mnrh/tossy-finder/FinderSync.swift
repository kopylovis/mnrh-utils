import Cocoa
import FinderSync

final class FinderSync: FIFinderSync {

    override init() {
        super.init()
        FIFinderSyncController.default().directoryURLs = [URL(fileURLWithPath: "/")]
    }

    override func menu(for menuKind: FIMenuKind) -> NSMenu {
        let menu = NSMenu(title: "")
        guard menuKind == .contextualMenuForItems || menuKind == .contextualMenuForContainer else { return menu }
        let russian = Locale.preferredLanguages.first?.hasPrefix("ru") == true
        let all = NSMenuItem(title: russian ? "Отправить в Tossy" : "Send via Tossy", action: #selector(sendAll(_:)), keyEquivalent: "")
        all.image = icon()
        menu.addItem(all)
        let pick = NSMenuItem(title: russian ? "Отправить в Tossy…" : "Send via Tossy…", action: #selector(sendPick(_:)), keyEquivalent: "")
        pick.image = icon()
        menu.addItem(pick)
        return menu
    }

    private func icon() -> NSImage {
        let isDark = UserDefaults.standard.string(forKey: "AppleInterfaceStyle") == "Dark"
        let color = isDark ? NSColor.white : NSColor.black
        let image = NSImage(size: NSSize(width: 16, height: 16), flipped: true) { _ in
            color.setFill()
            let scale = 16.0 / 18.0
            let dot = 1.3 * scale
            for (x, y) in [(5.0, 18.0), (6.47, 14.31), (8.6, 10.89), (11.8, 8.63), (15.72, 8.89)] {
                NSBezierPath(ovalIn: NSRect(x: (x - 3) * scale - dot, y: (y - 3) * scale - dot, width: 2 * dot, height: 2 * dot)).fill()
            }
            let ball = 3.2 * scale
            NSBezierPath(ovalIn: NSRect(x: 14.5 * scale - ball, y: 4.5 * scale - ball, width: 2 * ball, height: 2 * ball)).fill()
            return true
        }
        return image
    }

    @objc func sendAll(_ sender: AnyObject?) {
        send(pick: false)
    }

    @objc func sendPick(_ sender: AnyObject?) {
        send(pick: true)
    }

    private func send(pick: Bool) {
        let controller = FIFinderSyncController.default()
        let urls = controller.selectedItemURLs() ?? controller.targetedURL().map { [$0] } ?? []
        guard !urls.isEmpty else { return }
        var parts = URLComponents()
        parts.scheme = "mnrh-tossy"
        parts.host = "send"
        parts.queryItems = urls.map { URLQueryItem(name: "f", value: $0.path) } + (pick ? [URLQueryItem(name: "pick", value: "1")] : [])
        if let url = parts.url { NSWorkspace.shared.open(url) }
    }
}
