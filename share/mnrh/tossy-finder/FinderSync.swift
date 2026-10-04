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
        let devices = roomDevices()
        let send = NSMenuItem(title: russian ? "Отправить в Tossy" : "Send via Tossy", action: devices.isEmpty ? #selector(sendAll(_:)) : nil, keyEquivalent: "")
        send.image = icon()
        if !devices.isEmpty {
            let targets = NSMenu(title: "")
            targets.addItem(NSMenuItem(title: russian ? "Всем устройствам" : "All devices", action: #selector(sendAll(_:)), keyEquivalent: ""))
            targets.addItem(.separator())
            for (index, device) in devices.enumerated() {
                let item = NSMenuItem(title: device.name, action: #selector(sendOne(_:)), keyEquivalent: "")
                item.tag = index
                item.image = NSImage(systemSymbolName: device.isMac ? "laptopcomputer" : "iphone", accessibilityDescription: nil)
                targets.addItem(item)
            }
            send.submenu = targets
        }
        menu.addItem(send)
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

    @objc func sendOne(_ sender: AnyObject?) {
        guard let item = sender as? NSMenuItem else { return }
        let devices = roomDevices()
        guard devices.indices.contains(item.tag) else { return }
        send(pick: false, to: devices[item.tag].id)
    }

    private func roomDevices() -> [(id: String, name: String, isMac: Bool)] {
        guard let home = getpwuid(getuid())?.pointee.pw_dir else { return [] }
        let file = URL(fileURLWithPath: String(cString: home)).appendingPathComponent(".cache/mnrh/tossy-devices.json")
        guard let data = try? Data(contentsOf: file),
              let list = (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]] else { return [] }
        return list.compactMap { entry in
            guard let id = entry["id"] as? String, let name = entry["name"] as? String else { return nil }
            return (id, name, entry["mac"] as? Bool ?? false)
        }
    }

    private func send(pick: Bool, to target: String? = nil) {
        let controller = FIFinderSyncController.default()
        let urls = controller.selectedItemURLs() ?? controller.targetedURL().map { [$0] } ?? []
        guard !urls.isEmpty else { return }
        var parts = URLComponents()
        parts.scheme = "mnrh-tossy"
        parts.host = "send"
        parts.queryItems = urls.map { URLQueryItem(name: "f", value: $0.path) } + (pick ? [URLQueryItem(name: "pick", value: "1")] : [])
            + (target.map { [URLQueryItem(name: "to", value: $0)] } ?? [])
        if let url = parts.url { NSWorkspace.shared.open(url) }
    }
}
