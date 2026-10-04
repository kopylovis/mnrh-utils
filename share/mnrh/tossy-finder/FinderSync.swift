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
        let all = NSMenuItem(title: "Отправить в Tossy", action: #selector(sendAll(_:)), keyEquivalent: "")
        all.image = NSImage(systemSymbolName: "paperplane", accessibilityDescription: nil)
        menu.addItem(all)
        menu.addItem(NSMenuItem(title: "Отправить в Tossy…", action: #selector(sendPick(_:)), keyEquivalent: ""))
        return menu
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
