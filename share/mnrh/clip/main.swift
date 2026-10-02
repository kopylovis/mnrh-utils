import AppKit
import CommonCrypto
import CryptoKit
import ServiceManagement
import SystemConfiguration
import UniformTypeIdentifiers

let maxText = 1_000_000
let maxImage = 15_000_000
let maxFile = 24_000_000
let inlineLimit = 2_400
let staleAfter = 15.0 * 60
var configFile = ""
var stateFile = ""
var qrOut = ""
var sendItems = [String]()
var sendTexts = [String]()
var sendTo = [String]()
var inviteCode = ""
var joinServer = ""
var joinCode = ""
var joinOut = ""

var argv = CommandLine.arguments.dropFirst().makeIterator()
while let arg = argv.next() {
    switch arg {
    case "--config": configFile = argv.next() ?? ""
    case "--state": stateFile = argv.next() ?? ""
    case "--qr": qrOut = argv.next() ?? ""
    case "--send": sendItems.append(argv.next() ?? "")
    case "--text": sendTexts.append(argv.next() ?? "")
    case "--to": sendTo.append(argv.next() ?? "")
    case "--invite": inviteCode = argv.next() ?? ""
    case "--join":
        joinServer = argv.next() ?? ""
        joinCode = argv.next() ?? ""
    case "--out": joinOut = argv.next() ?? ""
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

let computerName = (SCDynamicStoreCopyComputerName(nil, nil) as String?) ?? "Mac"

func normalizedCode(_ code: String) -> String {
    code.uppercased().filter { $0.isLetter || $0.isNumber }
}

func inviteTopic(_ code: String) -> String {
    let hash = SHA256.hash(data: Data("tossy-invite-topic:\(normalizedCode(code))".utf8))
    return "tossy-inv-" + hash.prefix(12).map { String(format: "%02x", $0) }.joined()
}

func inviteKey(_ code: String) -> SymmetricKey {
    let password = Array(normalizedCode(code).utf8)
    let salt = Array("tossy-invite-v1".utf8)
    var derived = [UInt8](repeating: 0, count: 32)
    _ = password.withUnsafeBufferPointer { pw in
        CCKeyDerivationPBKDF(CCPBKDFAlgorithm(kCCPBKDF2), UnsafeRawPointer(pw.baseAddress!).assumingMemoryBound(to: Int8.self),
                             pw.count, salt, salt.count, CCPseudoRandomAlgorithm(kCCPRFHmacAlgSHA256), 300_000,
                             &derived, derived.count)
    }
    return SymmetricKey(data: Data(derived))
}

func runSync(_ request: URLRequest) -> (Int, Data) {
    var result = (0, Data())
    let done = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: request) { data, response, _ in
        result = ((response as? HTTPURLResponse)?.statusCode ?? 0, data ?? Data())
        done.signal()
    }.resume()
    done.wait()
    return result
}

if !joinCode.isEmpty {
    let server = joinServer.hasPrefix("http") ? joinServer : "https://" + joinServer
    var r = URLRequest(url: URL(string: "\(server.hasSuffix("/") ? String(server.dropLast()) : server)/\(inviteTopic(joinCode))/json?poll=1&since=30m")!)
    r.timeoutInterval = 20
    let (code, body) = runSync(r)
    guard code == 200 else {
        print(code == 401 || code == 403 ? "сервер не пускает к приглашениям: ntfy access everyone 'tossy-inv-*' read-only"
                                         : "нет связи с сервером: HTTP \(code)")
        exit(3)
    }
    let key = inviteKey(joinCode)
    for line in body.split(separator: 0x0A).reversed() {
        guard let event = (try? JSONSerialization.jsonObject(with: Data(line))) as? [String: Any],
              let message = event["message"] as? String, let sealed = Data(base64Encoded: message),
              let box = try? AES.GCM.SealedBox(combined: sealed), let plain = try? AES.GCM.open(box, using: key),
              let invite = (try? JSONSerialization.jsonObject(with: plain)) as? [String: Any] else { continue }
        guard (invite["exp"] as? Double ?? 0) > Date().timeIntervalSince1970 else {
            print("приглашение устарело: на первом Mac — mnrh tossy invite")
            exit(4)
        }
        let fd = open(joinOut, O_WRONLY | O_CREAT | O_TRUNC, 0o600)
        guard fd >= 0 else { exit(1) }
        _ = plain.withUnsafeBytes { write(fd, $0.baseAddress, plain.count) }
        close(fd)
        print(invite["n"] as? String ?? "Mac")
        exit(0)
    }
    print("приглашение не найдено: проверь код или создай новое — mnrh tossy invite")
    exit(5)
}

struct Config {
    var server = ""
    var token = ""
    var room = ""
    var publishTopic = ""
    var listenTopics = [String]()
    var legacyToPhone = ""
    var deviceID = ""
    var key = SymmetricKey(size: .bits256)
    var keyText = ""
    var pausedUntil = 0.0
    var images = true
    var files = true
    var notifyApp = ""
    var name = ""
    var aliases = [String: String]()

    var deviceName: String { name.isEmpty ? computerName : name }
}

func loadConfig() -> Config? {
    guard let data = FileManager.default.contents(atPath: configFile),
          let raw = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
          let server = raw["server"] as? String,
          let keyText = raw["key"] as? String, let keyData = Data(base64Encoded: keyText), keyData.count == 32
    else { return nil }
    var c = Config()
    c.server = server.hasSuffix("/") ? String(server.dropLast()) : server
    c.token = raw["token"] as? String ?? ""
    c.deviceID = raw["device_id"] as? String ?? ""
    if let room = raw["room"] as? String, !room.isEmpty {
        c.room = room
        c.publishTopic = room
        c.listenTopics = [room] + [raw["legacy_to_mac"] as? String].compactMap { $0 }.filter { !$0.isEmpty }
        c.legacyToPhone = raw["legacy_to_phone"] as? String ?? ""
    } else if let toMac = raw["to_mac"] as? String, let toPhone = raw["to_phone"] as? String {
        c.publishTopic = toPhone
        c.listenTopics = [toMac]
    } else {
        return nil
    }
    c.key = SymmetricKey(data: keyData)
    c.keyText = keyText
    c.pausedUntil = raw["paused_until"] as? Double ?? 0
    c.images = raw["images"] as? Bool ?? true
    c.files = raw["files"] as? Bool ?? true
    c.notifyApp = raw["notify_app"] as? String ?? ""
    c.name = (raw["name"] as? String ?? "").trimmingCharacters(in: .whitespaces)
    c.aliases = raw["aliases"] as? [String: String] ?? [:]
    return c
}

var conf = Config()
if let loaded = loadConfig() {
    conf = loaded
} else {
    log("нет настроек в \(configFile): mnrh tossy setup")
    print("нет настроек: mnrh tossy setup")
    exit(2)
}

if !inviteCode.isEmpty {
    guard !conf.room.isEmpty else {
        print("сначала mnrh tossy on: старые настройки без комнаты")
        exit(2)
    }
    let invite: [String: Any] = ["s": conf.server, "t": conf.token, "r": conf.room, "k": conf.keyText,
                                 "n": conf.deviceName, "exp": Date().timeIntervalSince1970 + 600]
    let plain = try! JSONSerialization.data(withJSONObject: invite)
    let sealed = try! AES.GCM.seal(plain, using: inviteKey(inviteCode)).combined!
    var r = URLRequest(url: URL(string: "\(conf.server)/\(inviteTopic(inviteCode))")!)
    r.httpMethod = "POST"
    if !conf.token.isEmpty { r.setValue("Bearer \(conf.token)", forHTTPHeaderField: "Authorization") }
    r.httpBody = sealed.base64EncodedString().data(using: .utf8)
    r.timeoutInterval = 20
    let (code, _) = runSync(r)
    print(code == 200 ? "ok" : "не отправил приглашение: HTTP \(code)")
    exit(code == 200 ? 0 : 1)
}

if !qrOut.isEmpty {
    var payload: [String: Any] = ["s": conf.server, "t": conf.token, "k": conf.keyText, "n": conf.deviceName]
    if conf.room.isEmpty {
        payload["v"] = 1
        payload["in"] = conf.publishTopic
        payload["out"] = conf.listenTopics.first ?? ""
    } else {
        payload["v"] = 2
        payload["r"] = conf.room
        payload["id"] = conf.deviceID
    }
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
var moved = false
var members = [String: [String: Any]]()

func readSavedID() {
    guard !stateFile.isEmpty, let data = FileManager.default.contents(atPath: stateFile),
          let raw = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
    lastID = raw["last_id"] as? String ?? ""
    phone = raw["phone"] as? String ?? ""
    phoneAt = raw["phone_at"] as? Double ?? 0
    moved = raw["moved"] as? Bool ?? false
    members = raw["members"] as? [String: [String: Any]] ?? [:]
}

func writeState() {
    guard !stateFile.isEmpty else { return }
    let state: [String: Any] = ["pid": getpid(), "started": Int(started.timeIntervalSince1970),
                                "sent": sentCount, "received": receivedCount, "last_sent": Int(lastSent),
                                "last_received": Int(lastReceived), "last_id": lastID, "connected": connected,
                                "phone": phone, "phone_at": phoneAt, "moved": moved, "members": members]
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

func publish(kind: String, mime: String, data: Data, text: String?, topic: String? = nil, extra: [String: Any] = [:],
             done: ((Bool) -> Void)? = nil) {
    var meta: [String: Any] = ["k": kind, "m": mime, "src": "mac", "n": conf.deviceName]
    if !conf.deviceID.isEmpty { meta["id"] = conf.deviceID }
    meta.merge(extra) { _, new in new }
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
    let content = kind == "text" || kind == "image"
    let legacy = topic == nil && content && !conf.legacyToPhone.isEmpty && !moved ? [conf.legacyToPhone] : []
    for extraTopic in legacy {
        var copy = request(extraTopic, method: body == nil ? "POST" : "PUT")
        copy.setValue("4", forHTTPHeaderField: "X-Priority")
        copy.timeoutInterval = 60
        if let body = body {
            copy.setValue(sealedMeta.base64EncodedString(), forHTTPHeaderField: "X-Message")
            copy.setValue("clip.bin", forHTTPHeaderField: "X-Filename")
            copy.httpBody = body
        } else {
            copy.httpBody = sealedMeta.base64EncodedString().data(using: .utf8)
        }
        URLSession.shared.dataTask(with: copy).resume()
    }
    var r = request(topic ?? conf.publishTopic, method: body == nil ? "POST" : "PUT")
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
                if kind == "text" || kind == "image" {
                    sentCount += 1
                    lastSent = Date().timeIntervalSince1970
                    log("→ \(conf.room.isEmpty ? "телефон" : "комната"): \(what)")
                }
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
        guard urls.count == 1, let url = urls.first else {
            log("пропустил: скопировано несколько файлов, передаю по одному")
            return
        }
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir), !isDir.boolValue else {
            log("пропустил: папки не передаю")
            return
        }
        let size = (try? url.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0
        let type = UTType(filenameExtension: url.pathExtension)
        if conf.images, let type = type, type.conforms(to: .image), size <= maxImage * 2,
           let data = try? Data(contentsOf: url), let (body, mime) = imagePayload(data, type) {
            publish(kind: "image", mime: mime, data: body, text: nil)
            return
        }
        guard conf.files else {
            log("пропустил: передача файлов выключена — mnrh tossy files on")
            return
        }
        guard size <= maxFile, let data = try? Data(contentsOf: url) else {
            log("пропустил: \(url.lastPathComponent) больше \(maxFile / 1_000_000) МБ")
            return
        }
        if isEcho(data) {
            log("пропустил: этот файл только что пришёл с другого устройства")
            return
        }
        publish(kind: "file", mime: type?.preferredMIMEType ?? "application/octet-stream", data: data, text: nil,
                extra: ["f": url.lastPathComponent])
        return
    }
    if conf.images {
        for (ptype, utype) in [(NSPasteboard.PasteboardType.png, UTType.png),
                               (NSPasteboard.PasteboardType("public.jpeg"), UTType.jpeg),
                               (NSPasteboard.PasteboardType("public.heic"), UTType.heic),
                               (NSPasteboard.PasteboardType.tiff, UTType.tiff)] where types.contains(ptype) {
            if let data = pasteboard.data(forType: ptype), isEcho(data) {
                log("пропустил: эта картинка только что пришла с другого устройства")
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
            log("пропустил: это только что пришло с другого устройства")
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

@discardableResult
func remember(_ meta: [String: Any]) -> Bool {
    let name = meta["n"] as? String ?? "?"
    let source = meta["src"] as? String ?? "android"
    let id = meta["id"] as? String ?? "legacy-\(source)-\(name)"
    let isNew = members[id] == nil
    members[id] = ["name": name, "src": source, "seen": Date().timeIntervalSince1970]
    if source != "mac" {
        if conf.listenTopics.first == conf.room && meta["id"] != nil { moved = true }
        phone = name
        if isNew || meta["k"] as? String == "hello" { phoneAt = Date().timeIntervalSince1970 }
    }
    return isNew
}

func rewriteConfig(_ change: (inout [String: Any]) -> Void) -> Bool {
    guard let data = FileManager.default.contents(atPath: configFile),
          var raw = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return false }
    change(&raw)
    guard let out = try? JSONSerialization.data(withJSONObject: raw) else { return false }
    let tmp = configFile + ".tmp"
    guard FileManager.default.createFile(atPath: tmp, contents: out, attributes: [.posixPermissions: 0o600]) else { return false }
    return rename(tmp, configFile) == 0
}

func saveDownload(_ name: String, _ data: Data) -> URL? {
    let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads/Tossy", isDirectory: true)
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let clean = String(name.split(separator: "/").last ?? "file").replacingOccurrences(of: ":", with: "_")
    let base = (clean as NSString).deletingPathExtension
    let ext = (clean as NSString).pathExtension
    var url = dir.appendingPathComponent(clean.isEmpty ? "file" : clean)
    var n = 2
    while FileManager.default.fileExists(atPath: url.path) {
        url = dir.appendingPathComponent(ext.isEmpty ? "\(base) \(n)" : "\(base) \(n).\(ext)")
        n += 1
    }
    return (try? data.write(to: url)) != nil ? url : nil
}

func shownName(_ meta: [String: Any], _ fallback: String) -> String {
    if let id = meta["id"] as? String, let alias = conf.aliases[id], !alias.isEmpty { return alias }
    return meta["n"] as? String ?? fallback
}

func control(_ kind: String, _ meta: [String: Any]) -> Bool {
    let from = shownName(meta, "устройство")
    let sender = meta["id"] as? String ?? ""
    switch kind {
    case "ping":
        remember(meta)
        if !sender.isEmpty {
            publish(kind: "hello", mime: "text/plain", data: Data(), text: "", extra: ["to": [sender]])
        }
        writeState()
    case "bye":
        members[sender] = nil
        log("вышел из комнаты: \(from)")
        writeState()
    case "rekey":
        guard let room = meta["r"] as? String, let key = meta["key"] as? String,
              let keyData = Data(base64Encoded: key), keyData.count == 32 else { return true }
        let keep = Set((meta["to"] as? [String] ?? []) + [sender])
        members = members.filter { keep.contains($0.key) }
        let saved = rewriteConfig { raw in
            raw["room"] = room
            raw["key"] = key
            raw["legacy_to_mac"] = ""
            raw["legacy_to_phone"] = ""
        }
        lastID = ""
        writeState()
        log(saved ? "\(from) отключил одно из устройств: комната сменила ключ, перезапускаюсь" : "не записал новый ключ комнаты в \(configFile)")
        if saved { DispatchQueue.main.asyncAfter(deadline: .now() + 1) { exit(0) } }
    case "kick":
        log("\(from) отключил этот Mac от комнаты")
        notify("\(from) отключил этот Mac от комнаты. Вернуться: mnrh tossy join или mnrh tossy setup --new")
        _ = rewriteConfig { raw in
            raw["room"] = nil
            raw["key"] = nil
            raw["legacy_to_mac"] = nil
            raw["legacy_to_phone"] = nil
        }
        try? SMAppService.agent(plistName: (Bundle.main.bundleIdentifier ?? "com.mnrh.clip") + ".plist").unregister()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { exit(0) }
    default:
        return false
    }
    return true
}

func apply(_ meta: [String: Any], _ data: Data?) {
    let kind = meta["k"] as? String ?? ""
    let from = shownName(meta, "устройства")
    if control(kind, meta) { return }
    let isNew = remember(meta)
    if kind == "hello" {
        if isNew {
            log("новое устройство: \(from)")
            notify("Подключён \(from)")
        } else {
            log("на связи: \(from)")
        }
        writeState()
        return
    }
    guard kind == "text" || ((kind == "image" || kind == "file") && data != nil) else {
        log("непонятное сообщение: \(kind)")
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
    } else if kind == "file", let data = data {
        guard let url = saveDownload(meta["f"] as? String ?? "file", data) else {
            log("не сохранил файл от \(from)")
            return
        }
        pasteboard.writeObjects([url as NSURL])
        lastReceivedHash = digest(data)
        lastReceivedAt = Date().timeIntervalSince1970
        what = "файл \(url.lastPathComponent) (\(ByteCountFormatter.string(fromByteCount: Int64(data.count), countStyle: .file))) — в Загрузках/Tossy"
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

func handle(_ event: [String: Any], primary: Bool) {
    guard event["event"] as? String == "message", let id = event["id"] as? String else { return }
    if primary { lastID = id }
    let time = event["time"] as? Double ?? Date().timeIntervalSince1970
    guard Date().timeIntervalSince1970 - time < staleAfter else {
        log("пропустил старое сообщение \(id)")
        writeState()
        return
    }
    guard let message = event["message"] as? String, let sealedMeta = Data(base64Encoded: message),
          let metaJSON = unseal(sealedMeta),
          let meta = (try? JSONSerialization.jsonObject(with: metaJSON)) as? [String: Any] else {
        log("не расшифровал сообщение \(id): другой ключ? mnrh tossy pair")
        writeState()
        return
    }
    if let sender = meta["id"] as? String, !conf.deviceID.isEmpty, sender == conf.deviceID { return }
    if let targets = meta["to"] as? [String], !conf.deviceID.isEmpty, !targets.contains(conf.deviceID) { return }
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
    let topic: String
    let primary: Bool

    init(topic: String, primary: Bool) {
        self.topic = topic
        self.primary = primary
        super.init()
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 120
        cfg.timeoutIntervalForResource = .infinity
        session = URLSession(configuration: cfg, delegate: self, delegateQueue: .main)
    }

    func connect() {
        var path = "\(topic)/json"
        path += lastID.isEmpty || !primary ? "?since=\(Int(staleAfter))s" : "?since=\(lastID)"
        buffer = Data()
        session.dataTask(with: request(path)).resume()
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 200 {
            if primary && !connected { log("подключился к \(conf.server)") }
            if primary { connected = true }
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
                handle(event, primary: primary)
            }
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if primary && connected { log("связь с сервером прервалась\(error.map { ": \($0.localizedDescription)" } ?? "")") }
        if primary { connected = false }
        writeState()
        let delay = retry
        retry = min(retry * 2, 60)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { self.connect() }
    }

    func reconnect() {
        session.getAllTasks { tasks in tasks.forEach { $0.cancel() } }
    }
}

if !sendItems.isEmpty || !sendTexts.isEmpty {
    var pending = sendItems.count + sendTexts.count
    var failed = false
    let target: [String: Any] = sendTo.isEmpty ? [:] : ["to": sendTo]
    let finish: (Bool) -> Void = { ok in
        failed = failed || !ok
        pending -= 1
        if pending == 0 { exit(failed ? 1 : 0) }
    }
    func sendText(_ text: String) {
        guard Data(text.utf8).count <= maxText else {
            print("текст больше \(maxText / 1_000_000) МБ")
            finish(false)
            return
        }
        publish(kind: "text", mime: "text/plain", data: Data(text.utf8), text: text, extra: target) { ok in
            print(ok ? "✓ отправил текст" : "✗ не отправил текст")
            finish(ok)
        }
    }
    for path in sendTexts {
        guard let data = FileManager.default.contents(atPath: path), let text = String(data: data, encoding: .utf8) else {
            print("не прочитал текст")
            finish(false)
            continue
        }
        sendText(text)
    }
    for item in sendItems {
        let url = URL(fileURLWithPath: (item as NSString).expandingTildeInPath)
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) else {
            sendText(item)
            continue
        }
        guard !isDir.boolValue else {
            print("папки не передаю: \(url.lastPathComponent)")
            finish(false)
            continue
        }
        let name = url.lastPathComponent
        let size = (try? url.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0
        let type = UTType(filenameExtension: url.pathExtension)
        if let type = type, type.conforms(to: .image), size <= maxImage * 2,
           let data = try? Data(contentsOf: url), let (body, mime) = imagePayload(data, type) {
            publish(kind: "image", mime: mime, data: body, text: nil, extra: target) { ok in
                print(ok ? "✓ отправил \(name)" : "✗ не отправил \(name)")
                finish(ok)
            }
            continue
        }
        guard size <= maxFile, let data = try? Data(contentsOf: url) else {
            print("файл больше \(maxFile / 1_000_000) МБ или не читается: \(name)")
            finish(false)
            continue
        }
        let mime = type?.preferredMIMEType ?? "application/octet-stream"
        publish(kind: "file", mime: mime, data: data, text: nil, extra: target.merging(["f": name]) { _, new in new }) { ok in
            print(ok ? "✓ отправил \(name)" : "✗ не отправил \(name)")
            finish(ok)
        }
    }
    RunLoop.main.run()
}

readSavedID()
let streams = conf.listenTopics.enumerated().map { Stream(topic: $0.element, primary: $0.offset == 0) }
streams.forEach { $0.connect() }

func announceMove() {
    guard !conf.room.isEmpty, !conf.legacyToPhone.isEmpty, !moved else { return }
    publish(kind: "move", mime: "text/plain", data: Data(), text: "", topic: conf.legacyToPhone,
            extra: ["r": conf.room]) { ok in
        if ok { log("старый канал телефона: отправил переезд в комнату") }
    }
}

if !conf.room.isEmpty {
    publish(kind: "hello", mime: "text/plain", data: Data(), text: "")
    announceMove()
    Timer.scheduledTimer(withTimeInterval: 6 * 3600, repeats: true) { _ in announceMove() }
}

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
    streams.forEach { $0.reconnect() }
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
