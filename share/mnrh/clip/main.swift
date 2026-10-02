import AppKit
import CryptoKit
import ServiceManagement
import SystemConfiguration
import UniformTypeIdentifiers

let maxText = 1_000_000
let maxImage = 15_000_000
let inlineLimit = 2_400
let staleAfter = 15.0 * 60
var configFile = ""
var stateFile = ""
var qrOut = ""
var sendItems = [String]()

var argv = CommandLine.arguments.dropFirst().makeIterator()
while let arg = argv.next() {
    switch arg {
    case "--config": configFile = argv.next() ?? ""
    case "--state": stateFile = argv.next() ?? ""
    case "--qr": qrOut = argv.next() ?? ""
    case "--send": sendItems.append(argv.next() ?? "")
    default: break
    }
}

let serviceCommands = ["--register", "--unregister", "--service-status"]
if let command = CommandLine.arguments.dropFirst().first(where: serviceCommands.contains) {
    let service = SMAppService.agent(plistName: (Bundle.main.bundleIdentifier ?? "com.mnrh.clip") + ".plist")
    do {
        if command == "--register" && service.status != .enabled { try service.register() }
        if command == "--unregister" && service.status != .notRegistered { try service.unregister() }
    } catch {
        print("error \(error.localizedDescription)")
        exit(1)
    }
    switch service.status {
    case .enabled: print("enabled")
    case .requiresApproval: print("requiresApproval")
    case .notFound: print("notFound")
    default: print("notRegistered")
    }
    exit(0)
}

if CommandLine.arguments.contains("--check") {
    print("помощник собран")
    exit(0)
}

func log(_ text: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    FileHandle.standardError.write("\(stamp) \(text)\n".data(using: .utf8)!)
}

struct Config {
    var server = ""
    var token = ""
    var toMac = ""
    var toPhone = ""
    var key = SymmetricKey(size: .bits256)
    var keyText = ""
    var pausedUntil = 0.0
    var images = true
    var notifyApp = ""
}

func loadConfig() -> Config? {
    guard let data = FileManager.default.contents(atPath: configFile),
          let raw = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
          let server = raw["server"] as? String,
          let toMac = raw["to_mac"] as? String, let toPhone = raw["to_phone"] as? String,
          let keyText = raw["key"] as? String, let keyData = Data(base64Encoded: keyText), keyData.count == 32
    else { return nil }
    var c = Config()
    c.server = server.hasSuffix("/") ? String(server.dropLast()) : server
    c.token = raw["token"] as? String ?? ""
    c.toMac = toMac
    c.toPhone = toPhone
    c.key = SymmetricKey(data: keyData)
    c.keyText = keyText
    c.pausedUntil = raw["paused_until"] as? Double ?? 0
    c.images = raw["images"] as? Bool ?? true
    c.notifyApp = raw["notify_app"] as? String ?? ""
    return c
}

var conf = Config()
if let loaded = loadConfig() {
    conf = loaded
} else {
    log("нет настроек в \(configFile): mnrh clip setup")
    print("нет настроек: mnrh clip setup")
    exit(2)
}

if !qrOut.isEmpty {
    let payload: [String: Any] = ["v": 1, "s": conf.server, "t": conf.token, "in": conf.toPhone,
                                  "out": conf.toMac, "k": conf.keyText,
                                  "n": (SCDynamicStoreCopyComputerName(nil, nil) as String?) ?? "Mac"]
    let json = try! JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    guard let filter = CIFilter(name: "CIQRCodeGenerator") else { exit(1) }
    filter.setValue(json, forKey: "inputMessage")
    filter.setValue("M", forKey: "inputCorrectionLevel")
    guard let image = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 12, y: 12)) else { exit(1) }
    let rep = NSBitmapImageRep(ciImage: image)
    guard let png = rep.representation(using: .png, properties: [:]),
          (try? png.write(to: URL(fileURLWithPath: qrOut))) != nil else { exit(1) }
    exit(0)
}

func seal(_ data: Data) -> Data? {
    try? AES.GCM.seal(data, using: conf.key).combined
}

func unseal(_ data: Data) -> Data? {
    guard let box = try? AES.GCM.SealedBox(combined: data) else { return nil }
    return try? AES.GCM.open(box, using: conf.key)
}

let started = Date()
var sentCount = 0
var receivedCount = 0
var lastSent = 0.0
var lastReceived = 0.0
var lastID = ""
var connected = false
var phone = ""
var phoneAt = 0.0

func readSavedID() {
    guard !stateFile.isEmpty, let data = FileManager.default.contents(atPath: stateFile),
          let raw = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
    lastID = raw["last_id"] as? String ?? ""
    phone = raw["phone"] as? String ?? ""
    phoneAt = raw["phone_at"] as? Double ?? 0
}

func writeState() {
    guard !stateFile.isEmpty else { return }
    let state: [String: Any] = ["pid": getpid(), "started": Int(started.timeIntervalSince1970),
                                "sent": sentCount, "received": receivedCount, "last_sent": Int(lastSent),
                                "last_received": Int(lastReceived), "last_id": lastID, "connected": connected,
                                "phone": phone, "phone_at": phoneAt]
    if let data = try? JSONSerialization.data(withJSONObject: state) {
        try? data.write(to: URL(fileURLWithPath: stateFile), options: .atomic)
    }
}

func request(_ path: String, method: String = "GET") -> URLRequest {
    var r = URLRequest(url: URL(string: "\(conf.server)/\(path)")!)
    r.httpMethod = method
    if !conf.token.isEmpty { r.setValue("Bearer \(conf.token)", forHTTPHeaderField: "Authorization") }
    return r
}

func describe(_ kind: String, _ size: Int, _ text: String?) -> String {
    if let text = text {
        let one = text.split(whereSeparator: \.isNewline).first.map(String.init) ?? ""
        return "текст \(text.count) зн. «\(one.prefix(40))\(one.count > 40 ? "…" : "")»"
    }
    return "\(kind == "image" ? "картинка" : kind) \(ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file))"
}

func publish(kind: String, mime: String, data: Data, text: String?, done: ((Bool) -> Void)? = nil) {
    var meta: [String: Any] = ["k": kind, "m": mime, "src": "mac", "n": (SCDynamicStoreCopyComputerName(nil, nil) as String?) ?? "Mac"]
    var body: Data? = nil
    if let text = text, data.count <= inlineLimit {
        meta["v"] = text
    } else {
        body = seal(data)
    }
    guard let metaJSON = try? JSONSerialization.data(withJSONObject: meta), let sealedMeta = seal(metaJSON) else {
        done?(false)
        return
    }
    var r = request(conf.toPhone, method: body == nil ? "POST" : "PUT")
    r.setValue("4", forHTTPHeaderField: "X-Priority")
    r.timeoutInterval = 60
    if let body = body {
        r.setValue(sealedMeta.base64EncodedString(), forHTTPHeaderField: "X-Message")
        r.setValue("clip.bin", forHTTPHeaderField: "X-Filename")
        r.httpBody = body
    } else {
        r.httpBody = sealedMeta.base64EncodedString().data(using: .utf8)
    }
    let what = describe(kind, data.count, text)
    URLSession.shared.dataTask(with: r) { reply, response, error in
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        DispatchQueue.main.async {
            if code == 200 {
                sentCount += 1
                lastSent = Date().timeIntervalSince1970
                log("→ телефон: \(what)")
            } else {
                let detail = error?.localizedDescription
                    ?? reply.flatMap { String(data: $0, encoding: .utf8) }?.trimmingCharacters(in: .whitespacesAndNewlines)
                    ?? ""
                log("не отправил \(what): HTTP \(code) \(detail)")
            }
            writeState()
            done?(code == 200)
        }
    }.resume()
}

let pasteboard = NSPasteboard.general
var seenCount = pasteboard.changeCount
var ownCount = -1
let skipTypes: Set<String> = ["org.nspasteboard.ConcealedType", "org.nspasteboard.TransientType",
                              "org.nspasteboard.AutoGeneratedType", "com.agilebits.onepassword",
                              "com.apple.is-remote-clipboard"]
let echoWindow = 60.0
var lastReceivedHash = ""
var lastReceivedAt = 0.0

func digest(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

func isEcho(_ data: Data) -> Bool {
    Date().timeIntervalSince1970 - lastReceivedAt < echoWindow && digest(data) == lastReceivedHash
}

func imagePayload(_ data: Data, _ type: UTType) -> (Data, String)? {
    if type.conforms(to: .png) || type.conforms(to: .jpeg) || type.conforms(to: .gif) || type.conforms(to: .heic) {
        if data.count <= maxImage { return (data, type.preferredMIMEType ?? "image/png") }
    }
    guard let rep = NSBitmapImageRep(data: data) else { return nil }
    if let png = rep.representation(using: .png, properties: [:]), png.count <= maxImage / 2 {
        return (png, "image/png")
    }
    if let jpeg = rep.representation(using: .jpeg, properties: [.compressionFactor: 0.85]), jpeg.count <= maxImage {
        return (jpeg, "image/jpeg")
    }
    return nil
}

func capture() {
    guard Date().timeIntervalSince1970 >= conf.pausedUntil else { return }
    let types = pasteboard.types ?? []
    if types.contains(where: { skipTypes.contains($0.rawValue) }) {
        log("пропустил: пароль или служебное содержимое")
        return
    }
    if types.contains(.fileURL) {
        let urls = pasteboard.readObjects(forClasses: [NSURL.self],
                                          options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
        guard conf.images, urls.count == 1, let url = urls.first,
              let type = UTType(filenameExtension: url.pathExtension), type.conforms(to: .image),
              let size = (try? url.resourceValues(forKeys: [.fileSizeKey]))?.fileSize, size <= maxImage * 2,
              let data = try? Data(contentsOf: url), let (body, mime) = imagePayload(data, type) else {
            log("пропустил: скопированы файлы, передаю только одну картинку")
            return
        }
        publish(kind: "image", mime: mime, data: body, text: nil)
        return
    }
    if conf.images {
        for (ptype, utype) in [(NSPasteboard.PasteboardType.png, UTType.png),
                               (NSPasteboard.PasteboardType("public.jpeg"), UTType.jpeg),
                               (NSPasteboard.PasteboardType("public.heic"), UTType.heic),
                               (NSPasteboard.PasteboardType.tiff, UTType.tiff)] where types.contains(ptype) {
            if let data = pasteboard.data(forType: ptype), isEcho(data) {
                log("пропустил: эта картинка только что пришла с телефона")
                return
            }
            if let data = pasteboard.data(forType: ptype), let (body, mime) = imagePayload(data, utype) {
                publish(kind: "image", mime: mime, data: body, text: nil)
                return
            }
        }
    }
    if let text = pasteboard.string(forType: .string), !text.isEmpty {
        let data = Data(text.utf8)
        guard data.count <= maxText else {
            log("пропустил: текст больше \(maxText / 1_000_000) МБ")
            return
        }
        if isEcho(data) {
            log("пропустил: это только что пришло с телефона")
            return
        }
        publish(kind: "text", mime: "text/plain", data: data, text: text)
    }
}

func notify(_ body: String) {
    guard !conf.notifyApp.isEmpty, FileManager.default.fileExists(atPath: conf.notifyApp) else { return }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    p.arguments = ["-g", "-n", "-a", conf.notifyApp, "--args", "post", "--title", "Tossy",
                   "--body", body, "--id", "mnrh-clip", "--sound", "off"]
    try? p.run()
}

func apply(_ meta: [String: Any], _ data: Data?) {
    let kind = meta["k"] as? String ?? ""
    let from = meta["n"] as? String ?? "телефона"
    if kind == "hello" {
        phone = from
        phoneAt = Date().timeIntervalSince1970
        log("телефон подключён: \(from)")
        notify("Подключён \(from)")
        writeState()
        return
    }
    pasteboard.clearContents()
    var what = ""
    if kind == "text" {
        let text = meta["v"] as? String ?? data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
        pasteboard.setString(text, forType: .string)
        lastReceivedHash = digest(Data(text.utf8))
        lastReceivedAt = Date().timeIntervalSince1970
        what = describe(kind, text.utf8.count, text)
    } else if kind == "image", let data = data {
        let mime = meta["m"] as? String ?? "image/png"
        let type = UTType(mimeType: mime) ?? .png
        if let image = NSImage(data: data) {
            pasteboard.writeObjects([image])
        }
        pasteboard.setData(data, forType: NSPasteboard.PasteboardType(type.identifier))
        lastReceivedHash = digest(data)
        lastReceivedAt = Date().timeIntervalSince1970
        what = describe("картинка", data.count, nil)
    } else {
        log("непонятное сообщение: \(kind)")
        return
    }
    ownCount = pasteboard.changeCount
    seenCount = ownCount
    receivedCount += 1
    lastReceived = Date().timeIntervalSince1970
    log("← \(from): \(what)")
    notify("С \(from): \(what) — можно вставлять")
    writeState()
}

func handle(_ event: [String: Any]) {
    guard event["event"] as? String == "message", let id = event["id"] as? String else { return }
    lastID = id
    let time = event["time"] as? Double ?? Date().timeIntervalSince1970
    guard Date().timeIntervalSince1970 - time < staleAfter else {
        log("пропустил старое сообщение \(id)")
        writeState()
        return
    }
    guard let message = event["message"] as? String, let sealedMeta = Data(base64Encoded: message),
          let metaJSON = unseal(sealedMeta),
          let meta = (try? JSONSerialization.jsonObject(with: metaJSON)) as? [String: Any] else {
        log("не расшифровал сообщение \(id): другой ключ? mnrh clip pair")
        writeState()
        return
    }
    guard let attachment = event["attachment"] as? [String: Any], let url = attachment["url"] as? String,
          let link = URL(string: url) else {
        apply(meta, nil)
        return
    }
    var r = URLRequest(url: link)
    if !conf.token.isEmpty { r.setValue("Bearer \(conf.token)", forHTTPHeaderField: "Authorization") }
    r.timeoutInterval = 120
    URLSession.shared.dataTask(with: r) { data, response, _ in
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        DispatchQueue.main.async {
            guard code == 200, let data = data, let plain = unseal(data) else {
                log("не скачал вложение \(id): HTTP \(code)")
                writeState()
                return
            }
            apply(meta, plain)
        }
    }.resume()
}

final class Stream: NSObject, URLSessionDataDelegate {
    var session: URLSession!
    var buffer = Data()
    var retry = 1.0

    override init() {
        super.init()
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 120
        cfg.timeoutIntervalForResource = .infinity
        session = URLSession(configuration: cfg, delegate: self, delegateQueue: .main)
    }

    func connect() {
        var path = "\(conf.toMac)/json"
        path += lastID.isEmpty ? "?since=\(Int(staleAfter))s" : "?since=\(lastID)"
        buffer = Data()
        session.dataTask(with: request(path)).resume()
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 200 {
            if !connected { log("подключился к \(conf.server)") }
            connected = true
            retry = 1
            writeState()
            completionHandler(.allow)
        } else {
            log("сервер ответил HTTP \(code)\(code == 401 || code == 403 ? ": токен не подходит или нет доступа к каналу" : "")")
            retry = max(retry, 30)
            completionHandler(.cancel)
        }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        buffer.append(data)
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<nl]
            buffer.removeSubrange(buffer.startIndex...nl)
            if let event = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] {
                handle(event)
            }
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if connected { log("связь с сервером прервалась\(error.map { ": \($0.localizedDescription)" } ?? "")") }
        connected = false
        writeState()
        let delay = retry
        retry = min(retry * 2, 60)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { self.connect() }
    }

    func reconnect() {
        session.getAllTasks { tasks in tasks.forEach { $0.cancel() } }
    }
}

if !sendItems.isEmpty {
    var pending = sendItems.count
    var failed = false
    let finish: (Bool) -> Void = { ok in
        failed = failed || !ok
        pending -= 1
        if pending == 0 { exit(failed ? 1 : 0) }
    }
    for item in sendItems {
        let url = URL(fileURLWithPath: (item as NSString).expandingTildeInPath)
        if FileManager.default.fileExists(atPath: url.path) {
            guard let data = try? Data(contentsOf: url),
                  let type = UTType(filenameExtension: url.pathExtension), type.conforms(to: .image),
                  let (body, mime) = imagePayload(data, type) else {
                print("не картинка или слишком большая: \(item)")
                finish(false)
                continue
            }
            publish(kind: "image", mime: mime, data: body, text: nil) { ok in
                print(ok ? "✓ отправил \(url.lastPathComponent)" : "✗ не отправил \(url.lastPathComponent)")
                finish(ok)
            }
        } else {
            publish(kind: "text", mime: "text/plain", data: Data(item.utf8), text: item) { ok in
                print(ok ? "✓ отправил текст" : "✗ не отправил текст")
                finish(ok)
            }
        }
    }
    RunLoop.main.run()
}

readSavedID()
let stream = Stream()
stream.connect()

Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { _ in
    let count = pasteboard.changeCount
    guard count != seenCount else { return }
    seenCount = count
    guard count != ownCount else { return }
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
        guard pasteboard.changeCount == count else { return }
        if let fresh = loadConfig() { conf = fresh }
        capture()
    }
}

NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil,
                                                  queue: .main) { _ in
    stream.reconnect()
}

Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in writeState() }
writeState()
log("слежу за буфером, сервер \(conf.server)")

signal(SIGTERM, SIG_IGN)
let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
term.setEventHandler { exit(0) }
term.resume()

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
app.run()
