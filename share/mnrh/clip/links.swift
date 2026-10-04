import AppKit

final class LinkHandler: NSObject {
    static let shared = LinkHandler()

    func install() {
        NSAppleEventManager.shared().setEventHandler(self, andSelector: #selector(handle(_:reply:)),
                                                     forEventClass: AEEventClass(kInternetEventClass), andEventID: AEEventID(kAEGetURL))
    }

    @objc private func handle(_ event: NSAppleEventDescriptor, reply: NSAppleEventDescriptor) {
        guard let text = event.paramDescriptor(forKeyword: keyDirectObject)?.stringValue,
              let parts = URLComponents(string: text), parts.scheme == "mnrh-tossy", parts.host == "send" else { return }
        let items = parts.queryItems ?? []
        let files = items.filter { $0.name == "f" }.compactMap(\.value).map { URL(fileURLWithPath: $0) }
            .filter { FileManager.default.fileExists(atPath: $0.path) }
        guard !files.isEmpty else {
            log("Finder: файлов из ссылки уже нет")
            return
        }
        log("Finder: отправить \(files.count == 1 ? files[0].lastPathComponent : "\(files.count) объекта")")
        let targets = items.filter { $0.name == "to" }.compactMap(\.value)
        if !targets.isEmpty {
            sendFiles(files, to: targets)
        } else if items.contains(where: { $0.name == "pick" }) {
            StatusMenu.shared.pickTargets { ids in sendFiles(files, to: ids) }
        } else {
            sendFiles(files, to: nil)
        }
    }
}

var finderDevicesJSON = Data()

func shareDevicesWithFinder() {
    let list: [[String: Any]] = members.filter { $0.key != conf.deviceID }
        .sorted { ($0.value["seen"] as? Double ?? 0) > ($1.value["seen"] as? Double ?? 0) }
        .map { id, value in ["id": id, "name": conf.aliases[id] ?? value["name"] as? String ?? id, "mac": value["src"] as? String == "mac"] }
    guard let data = try? JSONSerialization.data(withJSONObject: list), data != finderDevicesJSON else { return }
    finderDevicesJSON = data
    try? data.write(to: FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/mnrh/tossy-devices.json"), options: .atomic)
}

func sendFiles(_ files: [URL], to ids: [String]?) {
    let extra: [String: Any] = ids.map { ["to": $0] } ?? [:]
    sendQueue(files.map { url in { next in sendItem(url, extra: extra, announce: true, done: { _ in next() }) } })
}
