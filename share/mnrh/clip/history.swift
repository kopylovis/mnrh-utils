import AppKit
import UniformTypeIdentifiers

struct HistoryItem: Codable {
    var id: String
    var incoming: Bool
    var kind: String
    var text: String?
    var textFile: String?
    var file: String?
    var name: String?
    var mime: String?
    var size: Int64
    var device: String
    var time: Double
}

final class History {
    static let shared = History()
    static let limit = 30
    static let inlineText = 4_000

    let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/mnrh/clip-history", isDirectory: true)
    private(set) var items = [HistoryItem]()
    private var index: URL { dir.appendingPathComponent("history.json") }

    init() {
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        if let data = try? Data(contentsOf: index), let saved = try? JSONDecoder().decode([HistoryItem].self, from: data) {
            items = saved
        }
    }

    func addText(_ text: String, incoming: Bool, device: String) {
        let id = UUID().uuidString
        var item = HistoryItem(id: id, incoming: incoming, kind: "text", size: Int64(text.utf8.count), device: device,
                               time: Date().timeIntervalSince1970)
        if text.count > History.inlineText {
            let url = dir.appendingPathComponent("\(id).txt")
            if (try? Data(text.utf8).write(to: url)) != nil { item.textFile = url.path }
            item.text = String(text.prefix(History.inlineText))
        } else {
            item.text = text
        }
        add(item)
    }

    func addImage(_ data: Data, mime: String, incoming: Bool, device: String) {
        let id = UUID().uuidString
        let ext = mime.components(separatedBy: "/").last.map { $0 == "jpeg" ? "jpg" : $0 } ?? "png"
        let url = dir.appendingPathComponent("\(id).\(ext)")
        guard (try? data.write(to: url)) != nil else { return }
        add(HistoryItem(id: id, incoming: incoming, kind: "image", file: url.path, mime: mime, size: Int64(data.count),
                        device: device, time: Date().timeIntervalSince1970))
    }

    func addFile(_ url: URL, size: Int64, incoming: Bool, device: String) {
        add(HistoryItem(id: UUID().uuidString, incoming: incoming, kind: "file", file: url.path, name: url.lastPathComponent,
                        size: size, device: device, time: Date().timeIntervalSince1970))
    }

    func fullText(_ item: HistoryItem) -> String? {
        if let path = item.textFile, let data = FileManager.default.contents(atPath: path) {
            return String(data: data, encoding: .utf8)
        }
        return item.text
    }

    func exists(_ item: HistoryItem) -> Bool {
        item.file.map { FileManager.default.fileExists(atPath: $0) } ?? true
    }

    func clear() {
        items.forEach(drop)
        items = []
        save()
    }

    private func add(_ item: HistoryItem) {
        items.insert(item, at: 0)
        if items.count > History.limit {
            items[History.limit...].forEach(drop)
            items = Array(items.prefix(History.limit))
        }
        save()
        StatusMenu.shared.refresh()
    }

    private func drop(_ item: HistoryItem) {
        for path in [item.textFile, item.kind == "image" ? item.file : nil].compactMap({ $0 }) where path.hasPrefix(dir.path) {
            try? FileManager.default.removeItem(atPath: path)
        }
    }

    private func save() {
        guard let data = try? JSONEncoder().encode(items) else { return }
        let tmp = index.appendingPathExtension("tmp")
        guard FileManager.default.createFile(atPath: tmp.path, contents: data, attributes: [.posixPermissions: 0o600]) else { return }
        _ = rename(tmp.path, index.path)
    }
}

func copyAgain(_ item: HistoryItem) {
    pasteboard.clearContents()
    switch item.kind {
    case "text":
        guard let text = History.shared.fullText(item) else { return }
        pasteboard.setString(text, forType: .string)
    case "image":
        guard let path = item.file, let data = FileManager.default.contents(atPath: path) else { return }
        if let image = NSImage(data: data) { pasteboard.writeObjects([image]) }
        let type = UTType(mimeType: item.mime ?? "image/png") ?? .png
        pasteboard.setData(data, forType: NSPasteboard.PasteboardType(type.identifier))
    default:
        guard let path = item.file else { return }
        pasteboard.writeObjects([URL(fileURLWithPath: path) as NSURL])
    }
    ownCount = pasteboard.changeCount
    seenCount = ownCount
}
