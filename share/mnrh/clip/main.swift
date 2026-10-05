import AppKit
import Carbon.HIToolbox
import CommonCrypto
import CryptoKit
import ServiceManagement
import SystemConfiguration
import UniformTypeIdentifiers

let maxText = 1_000_000
let maxImage = 15_000_000
let maxFile = 500_000_000
let streamChunk = 1 << 20
let streamMagic = Data("TSY2".utf8)
let bigFile: Int64 = 5_000_000
let inlineLimit = 2_400
let staleAfter = 15.0 * 60
let fileStaleAfter = 3.0 * 60 * 60
let gone = "сервер его уже удалил, отправьте ещё раз"
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
var sendNotify = false
var joinID = ""
var joinName = ""

let launchedBare = CommandLine.arguments.count == 1 || CommandLine.arguments.dropFirst().allSatisfy { $0.hasPrefix("-psn") }
if launchedBare {
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    configFile = home + "/.config/mnrh/clip.json"
    stateFile = home + "/.cache/mnrh/clip.json"
}

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
    case "--notify": sendNotify = true
    case "--id": joinID = argv.next() ?? ""
    case "--name": joinName = argv.next() ?? ""
    default: break
    }
}

let serviceLabel = Bundle.main.object(forInfoDictionaryKey: "MnrhServiceLabel") as? String ?? Bundle.main.bundleIdentifier ?? "com.kopylovis.tossy.desktop"
let serviceCommands = ["--register", "--unregister", "--service-status"]
if let command = CommandLine.arguments.dropFirst().first(where: serviceCommands.contains) {
    let service = SMAppService.agent(plistName: serviceLabel + ".plist")
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

let rekeyInfo = Data("tossy-rekey-v1".utf8)

func roomKeyKEK(_ shared: SharedSecret, ephemeral: Data, recipient: Data) -> SymmetricKey {
    shared.hkdfDerivedSymmetricKey(using: SHA256.self, salt: ephemeral + recipient, sharedInfo: rekeyInfo, outputByteCount: 32)
}

func sealRoomKey(room: String, key: String, token: String? = nil, for recipients: [String: String],
                 ephemeral: Curve25519.KeyAgreement.PrivateKey = .init(), nonce: AES.GCM.Nonce = .init()) -> (ephemeral: String, keys: [String: String]) {
    let ephemeralData = ephemeral.publicKey.rawRepresentation
    var secret = ["r": room, "key": key]
    if let token = token, !token.isEmpty { secret["t"] = token }
    let plain = (try? JSONSerialization.data(withJSONObject: secret, options: [.sortedKeys, .withoutEscapingSlashes])) ?? Data()
    var keys = [String: String]()
    for (id, text) in recipients {
        guard let data = Data(base64Encoded: text), let recipient = try? Curve25519.KeyAgreement.PublicKey(rawRepresentation: data),
              let shared = try? ephemeral.sharedSecretFromKeyAgreement(with: recipient),
              let box = try? AES.GCM.seal(plain, using: roomKeyKEK(shared, ephemeral: ephemeralData, recipient: data),
                                          nonce: nonce, authenticating: Data(id.utf8)).combined else { continue }
        keys[id] = box.base64EncodedString()
    }
    return (ephemeralData.base64EncodedString(), keys)
}

func openRoomKey(identity: Curve25519.KeyAgreement.PrivateKey, id: String, ephemeral: String, box: String) -> (room: String, key: String, token: String?)? {
    guard let ephemeralData = Data(base64Encoded: ephemeral), let sealed = Data(base64Encoded: box),
          let ephemeralKey = try? Curve25519.KeyAgreement.PublicKey(rawRepresentation: ephemeralData),
          let shared = try? identity.sharedSecretFromKeyAgreement(with: ephemeralKey),
          let sealedBox = try? AES.GCM.SealedBox(combined: sealed),
          let plain = try? AES.GCM.open(sealedBox, using: roomKeyKEK(shared, ephemeral: ephemeralData,
                                                                     recipient: identity.publicKey.rawRepresentation),
                                        authenticating: Data(id.utf8)),
          let json = (try? JSONSerialization.jsonObject(with: plain)) as? [String: Any],
          let room = json["r"] as? String, let key = json["key"] as? String else { return nil }
    return (room, key, json["t"] as? String)
}

if let at = CommandLine.arguments.firstIndex(of: "--selftest"), at + 1 < CommandLine.arguments.count {
    guard let data = FileManager.default.contents(atPath: CommandLine.arguments[at + 1]),
          let vectors = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
        print("нет векторов")
        exit(1)
    }
    var failed = 0
    let check: (String, Bool) -> Void = { name, ok in
        print("\(ok ? "ok" : "FAIL") \(name)")
        if !ok { failed += 1 }
    }
    for v in vectors["rekey"] as? [[String: String]] ?? [] {
        let name = "rekey \(v["id"] ?? "")"
        guard let ephemeral = Data(base64Encoded: v["ephemeral_private"] ?? "").flatMap({ try? Curve25519.KeyAgreement.PrivateKey(rawRepresentation: $0) }),
              let recipient = Data(base64Encoded: v["recipient_private"] ?? "").flatMap({ try? Curve25519.KeyAgreement.PrivateKey(rawRepresentation: $0) }),
              let nonce = Data(base64Encoded: v["nonce"] ?? "").flatMap({ try? AES.GCM.Nonce(data: $0) }),
              let id = v["id"], let room = v["room"], let key = v["key"] else {
            check(name, false)
            continue
        }
        let sealed = sealRoomKey(room: room, key: key, for: [id: recipient.publicKey.rawRepresentation.base64EncodedString()],
                                 ephemeral: ephemeral, nonce: nonce)
        check(name + " seal", sealed.ephemeral == v["ephemeral"] && sealed.keys[id] == v["box"])
        let opened = openRoomKey(identity: recipient, id: id, ephemeral: v["ephemeral"] ?? "", box: v["box"] ?? "")
        check(name + " open", opened?.room == room && opened?.key == key)
        check(name + " wrong id", openRoomKey(identity: recipient, id: id + "x", ephemeral: v["ephemeral"] ?? "", box: v["box"] ?? "") == nil)
    }
    let scratch = FileManager.default.temporaryDirectory.appendingPathComponent("mnrh-clip-selftest-\(getpid())")
    try? FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: scratch) }
    for v in vectors["tsy2"] as? [[String: Any]] ?? [] {
        guard let key = Data(base64Encoded: v["key"] as? String ?? ""), let prefix = Data(base64Encoded: v["prefix"] as? String ?? ""),
              let plain = Data(base64Encoded: v["plain"] as? String ?? ""), let sealed = Data(base64Encoded: v["sealed"] as? String ?? ""),
              let chunk = v["chunk"] as? Int else {
            check("tsy2", false)
            continue
        }
        let name = "tsy2 \(plain.count) bytes"
        let src = scratch.appendingPathComponent("plain"), out = scratch.appendingPathComponent("sealed")
        let back = scratch.appendingPathComponent("back"), given = scratch.appendingPathComponent("given")
        try? plain.write(to: src)
        try? sealed.write(to: given)
        _ = try? sealFile(src, out, key: SymmetricKey(data: key), chunkSize: chunk, prefix: [UInt8](prefix)) { _ in }
        check(name + " seal", (try? Data(contentsOf: out)) == sealed)
        _ = try? openFile(given, back, key: SymmetricKey(data: key))
        check(name + " open", (try? Data(contentsOf: back)) == plain)
        [src, out, back, given].forEach { try? FileManager.default.removeItem(at: $0) }
    }
    exit(failed == 0 ? 0 : 1)
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

func waitForEvent(_ request: URLRequest, seconds: Double, _ match: @escaping ([String: Any]) -> Bool) -> (found: Bool, code: Int) {
    var found = false
    var code = 0
    let done = DispatchSemaphore(value: 0)
    let task = Task.detached {
        defer { done.signal() }
        guard let (bytes, response) = try? await URLSession.shared.bytes(for: request) else { return }
        code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else { return }
        do {
            for try await line in bytes.lines {
                guard let event = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any],
                      event["event"] as? String == "message" else { continue }
                if match(event) {
                    found = true
                    return
                }
            }
        } catch {}
    }
    if done.wait(timeout: .now() + seconds) == .timedOut {
        task.cancel()
        _ = done.wait(timeout: .now() + 2)
    }
    return (found, code)
}

func trimmedServer(_ server: String) -> String {
    server.hasSuffix("/") ? String(server.dropLast()) : server
}

if !joinCode.isEmpty {
    let server = trimmedServer(joinServer.hasPrefix("http") ? joinServer : "https://" + joinServer)
    let topic = inviteTopic(joinCode)
    guard let url = URL(string: "\(server)/\(topic)/json") else {
        print("не понял адрес сервера: \(joinServer)")
        exit(3)
    }
    var r = URLRequest(url: url)
    r.timeoutInterval = 90
    let key = inviteKey(joinCode)
    var plain = Data()
    var invite = [String: Any]()
    let result = waitForEvent(r, seconds: 70) { event in
        guard let message = event["message"] as? String, let sealed = Data(base64Encoded: message),
              let box = try? AES.GCM.SealedBox(combined: sealed), let opened = try? AES.GCM.open(box, using: key),
              let parsed = (try? JSONSerialization.jsonObject(with: opened)) as? [String: Any] else { return false }
        plain = opened
        invite = parsed
        return true
    }
    guard result.found else {
        switch result.code {
        case 401, 403: print("сервер не пускает к приглашениям: ntfy access everyone 'tossy-inv-*' read-only")
        case 200: print("приглашение не пришло: на первом Mac должна идти mnrh tossy invite с этим кодом")
        default: print("нет связи с сервером: HTTP \(result.code)")
        }
        exit(result.code == 200 ? 5 : 3)
    }
    guard (invite["exp"] as? Double ?? 0) > Date().timeIntervalSince1970 else {
        print("приглашение устарело: на первом Mac — mnrh tossy invite")
        exit(4)
    }
    let fd = open(joinOut, O_WRONLY | O_CREAT | O_TRUNC, 0o600)
    guard fd >= 0 else { exit(1) }
    _ = plain.withUnsafeBytes { write(fd, $0.baseAddress, plain.count) }
    close(fd)
    if let room = invite["r"] as? String, let keyText = invite["k"] as? String, let roomKey = Data(base64Encoded: keyText),
       let target = URL(string: "\(trimmedServer(invite["s"] as? String ?? server))/\(room)") {
        var meta: [String: Any] = ["k": "hello", "m": "text/plain", "src": "mac", "n": joinName.isEmpty ? computerName : joinName, "inv": topic]
        if !joinID.isEmpty { meta["id"] = joinID }
        if let metaJSON = try? JSONSerialization.data(withJSONObject: meta),
           let sealedMeta = try? AES.GCM.seal(metaJSON, using: SymmetricKey(data: roomKey)).combined {
            var hello = URLRequest(url: target)
            hello.httpMethod = "POST"
            if let token = invite["t"] as? String, !token.isEmpty { hello.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
            hello.httpBody = sealedMeta.base64EncodedString().data(using: .utf8)
            hello.timeoutInterval = 20
            _ = runSync(hello)
        }
    }
    print(invite["n"] as? String ?? "Mac")
    exit(0)
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
    var screenshots = false
    var alertTopics = [String: Bool]()
    var auto = true
    var hotkey = true
    var hotkeyKeys = "ctrl-opt-cmd-c"
    var notifications = true
    var notifyApp = ""
    var name = ""
    var aliases = [String: String]()
    var identity: Curve25519.KeyAgreement.PrivateKey?
    var owner = ""

    var isOwner: Bool { !owner.isEmpty && owner == deviceID }

    var publicKey: String { identity?.publicKey.rawRepresentation.base64EncodedString() ?? "" }

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
    c.screenshots = raw["screenshots"] as? Bool ?? false
    c.alertTopics = raw["alert_topics"] as? [String: Bool] ?? [:]
    c.auto = raw["auto"] as? Bool ?? true
    c.hotkey = raw["hotkey"] as? Bool ?? true
    c.hotkeyKeys = raw["hotkey_keys"] as? String ?? "ctrl-opt-cmd-c"
    c.notifications = raw["notifications"] as? Bool ?? true
    c.owner = raw["owner"] as? String ?? ""
    c.notifyApp = raw["notify_app"] as? String ?? ""
    c.name = (raw["name"] as? String ?? "").trimmingCharacters(in: .whitespaces)
    c.aliases = raw["aliases"] as? [String: String] ?? [:]
    if let text = raw["identity"] as? String, let data = Data(base64Encoded: text) {
        c.identity = try? Curve25519.KeyAgreement.PrivateKey(rawRepresentation: data)
    }
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

if conf.identity == nil {
    let identity = Curve25519.KeyAgreement.PrivateKey()
    if rewriteConfig({ $0["identity"] = identity.rawRepresentation.base64EncodedString() }) { conf.identity = identity }
}


if !inviteCode.isEmpty {
    guard !conf.room.isEmpty else {
        print("сначала mnrh tossy on: старые настройки без комнаты")
        exit(2)
    }
    let topic = inviteTopic(inviteCode)
    var invite: [String: Any] = ["s": conf.server, "t": conf.token, "r": conf.room, "k": conf.keyText,
                                 "n": conf.deviceName, "exp": Date().timeIntervalSince1970 + 600]
    if !conf.owner.isEmpty { invite["o"] = conf.owner }
    guard let plain = try? JSONSerialization.data(withJSONObject: invite),
          let sealed = try? AES.GCM.seal(plain, using: inviteKey(inviteCode)).combined,
          let url = URL(string: "\(conf.server)/\(topic)"), let roomURL = URL(string: "\(conf.server)/\(conf.room)/json") else {
        print("не собрал приглашение")
        exit(1)
    }
    let offer: () -> Int = {
        var r = URLRequest(url: url)
        r.httpMethod = "POST"
        r.setValue("no", forHTTPHeaderField: "X-Cache")
        if !conf.token.isEmpty { r.setValue("Bearer \(conf.token)", forHTTPHeaderField: "Authorization") }
        r.httpBody = sealed.base64EncodedString().data(using: .utf8)
        r.timeoutInterval = 20
        return runSync(r).0
    }
    let first = offer()
    guard first == 200 else {
        print("не отправил приглашение: HTTP \(first)")
        exit(1)
    }
    print("ok")
    fflush(stdout)
    let timer = DispatchSource.makeTimerSource(queue: .global())
    timer.schedule(deadline: .now() + 15, repeating: 15)
    timer.setEventHandler { _ = offer() }
    timer.resume()
    var room = URLRequest(url: roomURL)
    if !conf.token.isEmpty { room.setValue("Bearer \(conf.token)", forHTTPHeaderField: "Authorization") }
    room.timeoutInterval = 700
    var joined = ""
    let result = waitForEvent(room, seconds: 600) { event in
        guard let message = event["message"] as? String, let sealedMeta = Data(base64Encoded: message),
              let box = try? AES.GCM.SealedBox(combined: sealedMeta), let metaJSON = try? AES.GCM.open(box, using: conf.key),
              let meta = (try? JSONSerialization.jsonObject(with: metaJSON)) as? [String: Any],
              meta["k"] as? String == "hello", meta["inv"] as? String == topic else { return false }
        joined = meta["n"] as? String ?? "Mac"
        return true
    }
    timer.cancel()
    if result.found {
        print(joined)
        exit(0)
    }
    print(result.code == 200 || result.code == 0 ? "timeout" : "HTTP \(result.code)")
    exit(6)
}

func pairingQR() -> Data? {
    var payload: [String: Any] = ["s": conf.server, "t": conf.token, "k": conf.keyText, "n": conf.deviceName]
    if conf.room.isEmpty {
        payload["v"] = 1
        payload["in"] = conf.publishTopic
        payload["out"] = conf.listenTopics.first ?? ""
    } else {
        payload["v"] = 2
        payload["r"] = conf.room
        payload["id"] = conf.deviceID
        if !conf.publicKey.isEmpty { payload["pk"] = conf.publicKey }
        if !conf.owner.isEmpty { payload["o"] = conf.owner }
    }
    guard let json = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
          let filter = CIFilter(name: "CIQRCodeGenerator") else { return nil }
    filter.setValue(json, forKey: "inputMessage")
    filter.setValue("M", forKey: "inputCorrectionLevel")
    guard let image = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 12, y: 12)) else { return nil }
    return NSBitmapImageRep(ciImage: image).representation(using: .png, properties: [:])
}

if !qrOut.isEmpty {
    guard let png = pairingQR(), (try? png.write(to: URL(fileURLWithPath: qrOut))) != nil else { exit(1) }
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
    StatusMenu.shared.refresh()
    shareDevicesWithFinder()
}

func request(_ path: String, method: String = "GET") -> URLRequest {
    var r = URLRequest(url: URL(string: "\(conf.server)/\(path)") ?? URL(fileURLWithPath: "/dev/null"))
    r.httpMethod = method
    if !conf.token.isEmpty { r.setValue("Bearer \(conf.token)", forHTTPHeaderField: "Authorization") }
    return r
}

func describe(_ kind: String, _ size: Int, _ text: String?) -> String {
    if let text = text { return "текст \(text.count) зн." }
    return "\(kind == "image" ? "картинка" : kind) \(sizeText(Int64(size)))"
}

func sizeText(_ size: Int64) -> String {
    ByteCountFormatter.string(fromByteCount: size, countStyle: .file)
}

func sealedSize(_ size: Int64) -> Int64 {
    16 + size + 16 * (size / Int64(streamChunk) + 1)
}

func chunkNonce(_ header: Data, _ index: UInt32, _ last: Bool) throws -> AES.GCM.Nonce {
    var nonce = Data(header[header.startIndex + 8 ..< header.startIndex + 15])
    withUnsafeBytes(of: index.bigEndian) { nonce.append(contentsOf: $0) }
    nonce.append(last ? 1 : 0)
    return try AES.GCM.Nonce(data: nonce)
}

func readExactly(_ handle: FileHandle, _ count: Int) throws -> Data {
    var data = Data()
    while data.count < count {
        guard let part = try handle.read(upToCount: count - data.count), !part.isEmpty else { break }
        data.append(part)
    }
    return data
}

struct StreamResult {
    let size: Int64
    let hash: String
}

struct StreamError: Error {
    let text: String
}

func sealFile(_ src: URL, _ dst: URL, key: SymmetricKey = conf.key, chunkSize: Int = streamChunk, prefix fixed: [UInt8]? = nil,
              progress: (Int64) -> Void) throws -> StreamResult {
    let input = try FileHandle(forReadingFrom: src)
    defer { try? input.close() }
    let size = (try FileManager.default.attributesOfItem(atPath: src.path)[.size] as? NSNumber)?.int64Value ?? 0
    guard FileManager.default.createFile(atPath: dst.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
        throw StreamError(text: "не создал временный файл")
    }
    let output = try FileHandle(forWritingTo: dst)
    defer { try? output.close() }
    var header = streamMagic
    withUnsafeBytes(of: UInt32(chunkSize).bigEndian) { header.append(contentsOf: $0) }
    var prefix = fixed ?? [UInt8](repeating: 0, count: 7)
    if fixed == nil {
        guard SecRandomCopyBytes(kSecRandomDefault, prefix.count, &prefix) == errSecSuccess else { throw StreamError(text: "нет случайных чисел") }
    }
    header.append(contentsOf: prefix)
    header.append(0)
    try output.write(contentsOf: header)
    var hasher = SHA256()
    let chunks = size / Int64(chunkSize) + 1
    var done: Int64 = 0
    for index in 0 ..< chunks {
        try autoreleasepool {
            let last = index == chunks - 1
            let length = last ? Int(size % Int64(chunkSize)) : chunkSize
            let plain = try readExactly(input, length)
            guard plain.count == length else { throw StreamError(text: "файл изменился во время отправки") }
            hasher.update(data: plain)
            let box = try AES.GCM.seal(plain, using: key, nonce: chunkNonce(header, UInt32(index), last), authenticating: header)
            try output.write(contentsOf: box.ciphertext + box.tag)
            done += Int64(length)
            progress(done)
        }
    }
    return StreamResult(size: size, hash: hasher.finalize().map { String(format: "%02x", $0) }.joined())
}

func openFile(_ src: URL, _ dst: URL, key: SymmetricKey = conf.key) throws -> StreamResult {
    let input = try FileHandle(forReadingFrom: src)
    defer { try? input.close() }
    let header = Data(try readExactly(input, 16))
    guard header.count == 16, header.prefix(4) == streamMagic else { throw StreamError(text: "это не файл Tossy") }
    let chunk = Int(header[4 ..< 8].reduce(UInt32(0)) { $0 << 8 | UInt32($1) })
    guard chunk > 0, chunk <= 16 << 20 else { throw StreamError(text: "неверный размер куска") }
    guard FileManager.default.createFile(atPath: dst.path, contents: nil, attributes: nil) else {
        throw StreamError(text: "не создал \(dst.lastPathComponent)")
    }
    let output = try FileHandle(forWritingTo: dst)
    defer { try? output.close() }
    var hasher = SHA256()
    var index: UInt32 = 0
    var size: Int64 = 0
    var finished = false
    while !finished {
        try autoreleasepool {
            let frame = try readExactly(input, chunk + 16)
            guard frame.count >= 16 else { throw StreamError(text: "файл обрезан") }
            let last = frame.count < chunk + 16
            let box = try AES.GCM.SealedBox(nonce: chunkNonce(header, index, last), ciphertext: frame.prefix(frame.count - 16),
                                            tag: frame.suffix(16))
            let plain = try AES.GCM.open(box, using: key, authenticating: header)
            try output.write(contentsOf: plain)
            hasher.update(data: plain)
            size += Int64(plain.count)
            finished = last
            index += 1
        }
    }
    return StreamResult(size: size, hash: hasher.finalize().map { String(format: "%02x", $0) }.joined())
}

var isAgent = false
var contentSeq = 0

func targetNames(_ ids: [String]?) -> String {
    guard let ids = ids else { return "" }
    return ids.map { id in conf.aliases[id] ?? members[id]?["name"] as? String ?? "?" }.joined(separator: ", ")
}
let retryDelays: [Double] = [3, 10, 30, 60, 120, 300]

func publish(kind: String, mime: String, data: Data, text: String?, topic: String? = nil, extra: [String: Any] = [:],
             attempt: Int = 0, seq: Int? = nil, done: ((Bool) -> Void)? = nil) {
    let isContent = kind == "text" || kind == "image"
    if isContent && seq == nil { contentSeq += 1 }
    let mySeq = seq ?? contentSeq
    var meta: [String: Any] = ["k": kind, "m": mime, "src": "mac", "n": conf.deviceName]
    if !conf.deviceID.isEmpty { meta["id"] = conf.deviceID }
    if !["text", "image", "file"].contains(kind), !conf.publicKey.isEmpty { meta["pk"] = conf.publicKey }
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
                    if isAgent {
                        let device = targetNames(extra["to"] as? [String])
                        if kind == "text" {
                            History.shared.addText(text ?? String(decoding: data, as: UTF8.self), incoming: false, device: device)
                        } else {
                            History.shared.addImage(data, mime: mime, incoming: false, device: device)
                        }
                    }
                    log("→ \(conf.room.isEmpty ? "телефон" : "комната"): \(what)")
                }
            } else {
                let detail = error?.localizedDescription
                    ?? reply.flatMap { String(data: $0, encoding: .utf8) }?.trimmingCharacters(in: .whitespacesAndNewlines)
                    ?? ""
                let transient = code == 0 || code == 429 || code >= 500
                if isContent && transient && attempt < retryDelays.count {
                    let delay = retryDelays[attempt]
                    log("не отправил \(what): \(code == 0 ? detail : "HTTP \(code)"), повторю через \(Int(delay)) с")
                    writeState()
                    DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                        guard mySeq == contentSeq else {
                            log("не повторяю \(what): уже скопировано новое")
                            done?(false)
                            return
                        }
                        publish(kind: kind, mime: mime, data: data, text: text, topic: topic, extra: extra,
                                attempt: attempt + 1, seq: mySeq, done: done)
                    }
                    return
                }
                log("не отправил \(what): HTTP \(code) \(detail)")
            }
            writeState()
            done?(code == 200)
        }
    }.resume()
}

final class NetTask: NSObject, URLSessionDataDelegate, URLSessionDownloadDelegate {
    var session: URLSession!
    let onProgress: (Int64, Int64) -> Void
    let onDone: (Int, Data, URL?, Error?) -> Void
    var body = Data()
    var downloaded: URL?

    init(onProgress: @escaping (Int64, Int64) -> Void, onDone: @escaping (Int, Data, URL?, Error?) -> Void) {
        self.onProgress = onProgress
        self.onDone = onDone
        super.init()
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 300
        cfg.timeoutIntervalForResource = 6 * 3600
        session = URLSession(configuration: cfg, delegate: self, delegateQueue: .main)
    }

    func upload(_ request: URLRequest, file: URL) {
        session.uploadTask(with: request, fromFile: file).resume()
    }

    func download(_ request: URLRequest) {
        session.downloadTask(with: request).resume()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64, totalBytesSent: Int64,
                    totalBytesExpectedToSend: Int64) {
        onProgress(totalBytesSent, totalBytesExpectedToSend)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        body.append(data)
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64,
                    totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        onProgress(totalBytesWritten, totalBytesExpectedToWrite)
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        let keep = scratchFile("download")
        if (try? FileManager.default.moveItem(at: location, to: keep)) != nil { downloaded = keep }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let code = (task.response as? HTTPURLResponse)?.statusCode ?? 0
        onDone(code, body, downloaded, error)
        session.finishTasksAndInvalidate()
    }
}

func scratchFile(_ kind: String) -> URL {
    let dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("mnrh-tossy", isDirectory: true)
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    return dir.appendingPathComponent("\(kind)-\(UUID().uuidString)")
}

final class Meter {
    let name: String
    let total: Int64
    let terminal = isatty(STDERR_FILENO) != 0
    var lastAt = 0.0
    var started = Date()

    init(name: String, total: Int64) {
        self.name = name
        self.total = total
    }

    func update(_ done: Int64) {
        guard terminal else { return }
        let now = Date().timeIntervalSince1970
        guard now - lastAt >= 0.2 || done >= total else { return }
        lastAt = now
        let percent = total > 0 ? Int(done * 100 / total) : 0
        let width = 24
        let filled = total > 0 ? Int(done * Int64(width) / total) : 0
        let bar = String(repeating: "█", count: filled) + String(repeating: "░", count: width - filled)
        let speed = Double(done) / max(Date().timeIntervalSince(started), 0.1)
        let line = "  \(bar) \(percent)%  \(sizeText(done)) из \(sizeText(total))  \(sizeText(Int64(speed)))/с  \(name)"
        FileHandle.standardError.write("\r\u{1B}[2K\(line)".data(using: .utf8)!)
    }

    func finish() {
        guard terminal else { return }
        FileHandle.standardError.write("\r\u{1B}[2K".data(using: .utf8)!)
    }
}

func publishFile(_ src: URL, extra: [String: Any], announce: Bool, done: ((Bool) -> Void)?) {
    let name = src.lastPathComponent
    let type = UTType(filenameExtension: src.pathExtension)
    let tmp = scratchFile("send")
    let size = (try? src.resourceValues(forKeys: [.fileSizeKey]))?.fileSize.map(Int64.init) ?? 0
    let meter = Meter(name: name, total: size)
    let where_ = extra["to"] == nil ? "в комнату" : "выбранным устройствам"
    if announce { notify("Отправляю «\(name)» (\(sizeText(size))) \(where_)…") }
    let fail: (String) -> Void = { reason in
        meter.finish()
        try? FileManager.default.removeItem(at: tmp)
        log("не отправил файл \(name) (\(sizeText(size))): \(reason)")
        if announce { notify("Не отправил «\(name)»: \(reason)") }
        writeState()
        done?(false)
    }
    DispatchQueue.global(qos: .userInitiated).async {
        let sealed: StreamResult
        do {
            sealed = try sealFile(src, tmp) { _ in }
        } catch {
            DispatchQueue.main.async { fail((error as? StreamError)?.text ?? error.localizedDescription) }
            return
        }
        DispatchQueue.main.async {
            var meta: [String: Any] = ["k": "file", "m": type?.preferredMIMEType ?? "application/octet-stream", "src": "mac",
                                       "n": conf.deviceName, "f": name, "x": 2, "s": sealed.size]
            if !conf.deviceID.isEmpty { meta["id"] = conf.deviceID }
            meta.merge(extra) { _, new in new }
            guard let metaJSON = try? JSONSerialization.data(withJSONObject: meta), let sealedMeta = seal(metaJSON) else {
                fail("не зашифровал описание")
                return
            }
            var r = request(conf.publishTopic, method: "PUT")
            r.setValue("4", forHTTPHeaderField: "X-Priority")
            r.setValue(sealedMeta.base64EncodedString(), forHTTPHeaderField: "X-Message")
            r.setValue("clip.bin", forHTTPHeaderField: "X-Filename")
            let task = NetTask(onProgress: { sent, _ in meter.update(min(sent, sealed.size)) }) { code, reply, _, error in
                guard code == 200 else {
                    let detail = error?.localizedDescription
                        ?? String(data: reply, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                    fail("HTTP \(code) \(detail)".trimmingCharacters(in: .whitespaces))
                    return
                }
                meter.update(sealed.size)
                meter.finish()
                try? FileManager.default.removeItem(at: tmp)
                sentCount += 1
                lastSent = Date().timeIntervalSince1970
                log("→ \(conf.room.isEmpty ? "телефон" : "комната"): файл \(name) (\(sizeText(sealed.size)))")
                if announce { notify("Отправил «\(name)» \(where_)") }
                if isAgent { History.shared.addFile(src, size: sealed.size, incoming: false, device: targetNames(extra["to"] as? [String])) }
                writeState()
                done?(true)
            }
            task.upload(r, file: tmp)
        }
    }
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
var lastAutoHash = ""

func digest(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

func isEcho(_ data: Data) -> Bool {
    Date().timeIntervalSince1970 - lastReceivedAt < echoWindow && digest(data) == lastReceivedHash
}

func clipboardDigest(_ types: [NSPasteboard.PasteboardType]) -> String? {
    for (ptype, _) in imageTypes where types.contains(ptype) {
        if let data = pasteboard.data(forType: ptype) { return digest(data) }
    }
    return pasteboard.string(forType: .string).map { digest(Data($0.utf8)) }
}

func isRepeat(_ types: [NSPasteboard.PasteboardType]) -> Bool {
    guard let hash = clipboardDigest(types) else { return false }
    if hash == lastAutoHash { return true }
    lastAutoHash = hash
    return false
}

func isAlreadyOnClipboard(kind: String, meta: [String: Any], data: Data?) -> Bool {
    if kind == "image", let data = data {
        let incoming = digest(data)
        return imageTypes.contains { pasteboard.data(forType: $0.0).map(digest) == incoming }
    }
    if kind == "text", let current = pasteboard.string(forType: .string) {
        let text = meta["v"] as? String ?? data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
        return !text.isEmpty && text == current
    }
    return false
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
    let side = Double(max(rep.pixelsWide, rep.pixelsHigh))
    guard side > 0, let source = rep.cgImage else { return nil }
    for limit in [4096.0, 2048.0] where side > limit {
        let scale = limit / side
        let width = Int(Double(rep.pixelsWide) * scale), height = Int(Double(rep.pixelsHigh) * scale)
        guard let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                      space: CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        context.interpolationQuality = .high
        context.draw(source, in: CGRect(x: 0, y: 0, width: width, height: height))
        if let scaled = context.makeImage(),
           let jpeg = NSBitmapImageRep(cgImage: scaled).representation(using: .jpeg, properties: [.compressionFactor: 0.85]),
           jpeg.count <= maxImage {
            log("картинка большая: уменьшил до \(width)×\(height)")
            return (jpeg, "image/jpeg")
        }
    }
    return nil
}

let imageTypes: [(NSPasteboard.PasteboardType, UTType)] = [(.png, .png), (NSPasteboard.PasteboardType("public.jpeg"), .jpeg),
                                                            (NSPasteboard.PasteboardType("public.heic"), .heic), (.tiff, .tiff)]

func capture() {
    guard Date().timeIntervalSince1970 >= conf.pausedUntil else { return }
    let types = pasteboard.types ?? []
    if types.contains(where: { skipTypes.contains($0.rawValue) }) {
        log("пропустил: пароль, служебное содержимое или буфер с другого Mac Apple")
        return
    }
    guard conf.auto else {
        if conf.screenshots && isClipboardScreenshot(types) {
            if isRepeat(types) {
                log("пропустил: этот снимок уже отправлял")
                return
            }
            log("снимок экрана в буфере: отправляю")
            copyContent(types: types, explicit: true)
        }
        return
    }
    let fromFinder = NSWorkspace.shared.frontmostApplication?.bundleIdentifier == "com.apple.finder"
    if types.contains(.fileURL) && (fromFinder || !types.contains(where: { t in imageTypes.contains { $0.0 == t } })) {
        log("пропустил: файлы отправляю только явно — ⌃⌥⌘C или «Отправить в Tossy» в меню Finder")
        return
    }
    if isRepeat(types) {
        log("пропустил: буфер не изменился, это уже отправлял")
        return
    }
    copyContent(types: types)
}

func sendNow() {
    if let fresh = loadConfig() { conf = fresh }
    let types = pasteboard.types ?? []
    lastAutoHash = clipboardDigest(types) ?? lastAutoHash
    if types.contains(where: { skipTypes.contains($0.rawValue) }) {
        notify("Не отправил: в буфере пароль или служебные данные")
        return
    }
    if types.contains(.fileURL) {
        let urls = pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
        let files = urls.filter { FileManager.default.fileExists(atPath: $0.path) }
        guard !files.isEmpty else {
            notify("Не отправил: файлов уже нет")
            return
        }
        sendQueue(files.map { url in { next in sendItem(url, extra: [:], announce: true, done: { _ in next() }) } })
        return
    }
    copyContent(types: types, explicit: true)
}

func sendQueue(_ jobs: [(@escaping () -> Void) -> Void]) {
    guard let first = jobs.first else { return }
    first { sendQueue(Array(jobs.dropFirst())) }
}

func sendFolder(_ url: URL, extra: [String: Any], announce: Bool, done: @escaping (Bool) -> Void) {
    let name = url.lastPathComponent
    let dir = scratchFile("zip")
    let zip = dir.appendingPathComponent(name + ".zip")
    if announce { notify("Упаковываю папку «\(name)»…") }
    DispatchQueue.global(qos: .userInitiated).async {
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/ditto")
        p.arguments = ["-c", "-k", "--sequesterRsrc", "--keepParent", url.path, zip.path]
        let ok = (try? p.run()) != nil && { p.waitUntilExit(); return p.terminationStatus == 0 }()
        DispatchQueue.main.async {
            let cleanup = { try? FileManager.default.removeItem(at: dir) }
            guard ok else {
                cleanup()
                log("не упаковал папку \(name)")
                if announce { notify("Не упаковал папку «\(name)»") }
                done(false)
                return
            }
            sendItem(zip, extra: extra, announce: announce) { sent in
                cleanup()
                done(sent)
            }
        }
    }
}

func isFolder(_ url: URL) -> Bool {
    var isDir: ObjCBool = false
    return FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) && isDir.boolValue
}

func sendItem(_ url: URL, extra: [String: Any], announce: Bool, done: @escaping (Bool) -> Void) {
    if isFolder(url) {
        return sendFolder(url, extra: extra, announce: announce, done: done)
    }
    let name = url.lastPathComponent
    let size = (try? url.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0
    let type = UTType(filenameExtension: url.pathExtension)
    if conf.images, let type = type, type.conforms(to: .image), size <= maxImage * 2,
       let data = try? Data(contentsOf: url), let (body, mime) = imagePayload(data, type) {
        publish(kind: "image", mime: mime, data: body, text: nil, extra: extra) { ok in
            if announce { notify(ok ? "Отправил «\(name)»" : "Не отправил «\(name)»") }
            done(ok)
        }
        return
    }
    guard size <= maxFile else {
        let reason = "«\(name)» больше \(maxFile / 1_000_000) МБ"
        log("не отправил: \(reason)")
        if announce { notify("Не отправил: \(reason)") }
        done(false)
        return
    }
    publishFile(url, extra: extra, announce: announce || Int64(size) >= bigFile, done: done)
}

func copyContent(types: [NSPasteboard.PasteboardType], explicit: Bool = false) {
    if conf.images || explicit {
        for (ptype, utype) in imageTypes where types.contains(ptype) {
            if !explicit, let data = pasteboard.data(forType: ptype), isEcho(data) {
                log("пропустил: эта картинка только что пришла с другого устройства")
                return
            }
            guard let data = pasteboard.data(forType: ptype) else { continue }
            if let (body, mime) = imagePayload(data, utype) {
                publish(kind: "image", mime: mime, data: body, text: nil) { ok in
                    if explicit { notify(ok ? "Отправил картинку" : "Не отправил картинку") }
                }
                return
            }
            log("картинку не ужал до \(maxImage / 1_000_000) МБ, отправляю текст, если он есть")
            break
        }
    }
    if let text = pasteboard.string(forType: .string), !text.isEmpty {
        let data = Data(text.utf8)
        guard data.count <= maxText else {
            log("пропустил: текст больше \(maxText / 1_000_000) МБ")
            return
        }
        if !explicit && isEcho(data) {
            log("пропустил: это только что пришло с другого устройства")
            return
        }
        publish(kind: "text", mime: "text/plain", data: data, text: text) { ok in
            if explicit { notify(ok ? "Отправил текст" : "Не отправил текст") }
        }
    } else if explicit {
        notify("Нечего отправлять: буфер пуст")
    }
}

func shellQuote(_ text: String) -> String {
    "'" + text.replacingOccurrences(of: "'", with: "'\\''") + "'"
}

func linkIn(_ text: String) -> URL? {
    let clean = text.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !clean.isEmpty, !clean.contains(where: { $0.isWhitespace }), let url = URL(string: clean),
          ["http", "https"].contains(url.scheme?.lowercased() ?? ""), url.host != nil else { return nil }
    return url
}

func notify(_ body: String, file: URL? = nil, link: URL? = nil, title: String = "Tossy", id: String? = nil, sound: Bool = false) {
    guard conf.notifications else { return }
    if Notifier.shared.isReady {
        Notifier.shared.post(body, title: title, id: id ?? (file == nil ? "mnrh-clip" : "mnrh-clip-\(file!.path.hashValue)"),
                             sound: sound, file: file, link: link)
        return
    }
    guard !conf.notifyApp.isEmpty, FileManager.default.fileExists(atPath: conf.notifyApp) else { return }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    var args = ["-g", "-n", "-a", conf.notifyApp, "--args", "post", "--title", title,
                "--body", body, "--id", id ?? (file == nil ? "mnrh-clip" : "mnrh-clip-\(file!.path.hashValue)"), "--sound", sound ? "on" : "off"]
    if let file = file {
        let path = shellQuote(file.path)
        args += ["--click", "open \(path)", "--action", "Открыть=open \(path)", "--action", "Показать в Finder=open -R \(path)"]
    } else if let link = link {
        let url = shellQuote(link.absoluteString)
        args += ["--click", "open \(url)", "--action", "Открыть ссылку=open \(url)"]
    }
    p.arguments = args
    try? p.run()
}

@discardableResult
func remember(_ meta: [String: Any]) -> Bool {
    let name = meta["n"] as? String ?? "?"
    let source = meta["src"] as? String ?? "android"
    let id = meta["id"] as? String ?? "legacy-\(source)-\(name)"
    let isNew = members[id] == nil
    let known = members[id]?["pk"] as? String ?? ""
    let offered = (meta["pk"] as? String).flatMap { Data(base64Encoded: $0)?.count == 32 ? $0 : nil } ?? ""
    members[id] = ["name": name, "src": source, "seen": Date().timeIntervalSince1970, "pk": known.isEmpty ? offered : known]
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

func downloadURL(_ name: String) -> URL {
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
    return url
}

func saveDownload(_ name: String, _ data: Data) -> URL? {
    let url = downloadURL(name)
    return (try? data.write(to: url)) != nil ? url : nil
}

func deliverFile(_ meta: [String: Any], _ url: URL, size: Int64, hash: String, fresh: Bool) {
    let from = shownName(meta, "устройства")
    if fresh {
        pasteboard.clearContents()
        pasteboard.writeObjects([url as NSURL])
        lastReceivedHash = hash
        lastReceivedAt = Date().timeIntervalSince1970
        ownCount = pasteboard.changeCount
        seenCount = ownCount
    }
    receivedCount += 1
    lastReceived = Date().timeIntervalSince1970
    let what = "файл \(url.lastPathComponent) (\(sizeText(size))) — в Загрузках/Tossy"
    log("← \(from): \(what)")
    notify(fresh ? "С \(from): \(what) — можно вставлять" : "С \(from): \(what)", file: url)
    if isAgent { History.shared.addFile(url, size: size, incoming: true, device: from) }
    writeState()
}

func receiveStream(_ meta: [String: Any], _ link: URL, _ id: String, fresh: Bool) {
    remember(meta)
    let from = shownName(meta, "устройства")
    let name = meta["f"] as? String ?? "file"
    let size = (meta["s"] as? NSNumber)?.int64Value ?? 0
    log("← \(from): получаю файл \(name) (\(sizeText(size)))")
    if size >= bigFile { notify("Получаю «\(name)» (\(sizeText(size))) от \(from)…") }
    var r = URLRequest(url: link)
    if !conf.token.isEmpty { r.setValue("Bearer \(conf.token)", forHTTPHeaderField: "Authorization") }
    let fail: (String) -> Void = { reason in
        log("не получил файл \(name) от \(from): \(reason)")
        notify("Не получил «\(name)» от \(from): \(reason)")
        writeState()
    }
    NetTask(onProgress: { _, _ in }) { code, _, file, error in
        guard code == 200, let file = file else {
            fail(code == 404 || code == 410 ? gone : error?.localizedDescription ?? "HTTP \(code)")
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let dst = downloadURL(name)
            do {
                let result = try openFile(file, dst)
                try? FileManager.default.removeItem(at: file)
                DispatchQueue.main.async { deliverFile(meta, dst, size: result.size, hash: result.hash, fresh: fresh) }
            } catch {
                try? FileManager.default.removeItem(at: file)
                try? FileManager.default.removeItem(at: dst)
                DispatchQueue.main.async { fail((error as? StreamError)?.text ?? "не расшифровал: другой ключ?") }
            }
        }
    }.download(r)
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
        var opened: (room: String, key: String, token: String?)? = nil
        if let keys = meta["keys"] as? [String: String], let box = keys[conf.deviceID], let ephemeral = meta["e"] as? String {
            opened = conf.identity.flatMap { openRoomKey(identity: $0, id: conf.deviceID, ephemeral: ephemeral, box: box) }
        }
        guard let room = opened?.room ?? meta["r"] as? String, let key = opened?.key ?? meta["key"] as? String,
              let keyData = Data(base64Encoded: key), keyData.count == 32 else {
            log("\(from) сменил ключ комнаты, но новый ключ не для этого Mac")
            return true
        }
        let keep = Set((meta["to"] as? [String] ?? []) + [sender])
        members = members.filter { keep.contains($0.key) }
        let saved = rewriteConfig { raw in
            raw["room"] = room
            raw["key"] = key
            if let token = opened?.token, !token.isEmpty { raw["token"] = token }
            raw["legacy_to_mac"] = ""
            raw["legacy_to_phone"] = ""
        }
        lastID = ""
        writeState()
        log(saved ? "\(from) отключил одно из устройств: комната сменила ключ, перезапускаюсь" : "не записал новый ключ комнаты в \(configFile)")
        if saved { DispatchQueue.main.asyncAfter(deadline: .now() + 1) { exit(0) } }
    case "kick":
        guard !conf.isOwner else {
            log("\(from) пытался отключить этот Mac, но он создатель комнаты: остаюсь")
            return true
        }
        log("\(from) отключил этот Mac от комнаты")
        notify("\(from) отключил этот Mac от комнаты. Вернуться: mnrh tossy join или mnrh tossy setup --new")
        _ = rewriteConfig { raw in
            raw["room"] = nil
            raw["key"] = nil
            raw["legacy_to_mac"] = nil
            raw["legacy_to_phone"] = nil
        }
        try? SMAppService.agent(plistName: serviceLabel + ".plist").unregister()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { exit(0) }
    default:
        return false
    }
    return true
}

func apply(_ meta: [String: Any], _ data: Data?, fresh: Bool = true) {
    let kind = meta["k"] as? String ?? ""
    let from = shownName(meta, "устройства")
    if control(kind, meta) { return }
    let isNew = remember(meta)
    if kind == "hello" {
        if isNew {
            log("новое устройство: \(from)")
            notify("Подключён \(from)")
            StatusMenu.shared.deviceJoined(from, isMac: meta["src"] as? String == "mac")
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
    if isAlreadyOnClipboard(kind: kind, meta: meta, data: data) {
        log("← \(from): \(kind == "image" ? "картинка" : "текст") уже в буфере, пропустил")
        writeState()
        return
    }
    pasteboard.clearContents()
    var what = ""
    var link: URL? = nil
    if kind == "text" {
        let text = meta["v"] as? String ?? data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
        pasteboard.setString(text, forType: .string)
        History.shared.addText(text, incoming: true, device: from)
        lastReceivedHash = digest(Data(text.utf8))
        lastReceivedAt = Date().timeIntervalSince1970
        what = describe(kind, text.utf8.count, text)
        link = linkIn(text)
        if link != nil { what = "ссылка \(text.trimmingCharacters(in: .whitespacesAndNewlines).prefix(80))" }
    } else if kind == "image", let data = data {
        let mime = meta["m"] as? String ?? "image/png"
        let type = UTType(mimeType: mime) ?? .png
        if let image = NSImage(data: data) {
            pasteboard.writeObjects([image])
        }
        pasteboard.setData(data, forType: NSPasteboard.PasteboardType(type.identifier))
        History.shared.addImage(data, mime: mime, incoming: true, device: from)
        lastReceivedHash = digest(data)
        lastReceivedAt = Date().timeIntervalSince1970
        what = describe("картинка", data.count, nil)
    } else if kind == "file", let data = data {
        guard let url = saveDownload(meta["f"] as? String ?? "file", data) else {
            log("не сохранил файл от \(from)")
            return
        }
        deliverFile(meta, url, size: Int64(data.count), hash: digest(data), fresh: fresh)
        return
    } else {
        log("непонятное сообщение: \(kind)")
        return
    }
    ownCount = pasteboard.changeCount
    seenCount = ownCount
    lastAutoHash = clipboardDigest(pasteboard.types ?? []) ?? lastAutoHash
    receivedCount += 1
    lastReceived = Date().timeIntervalSince1970
    log("← \(from): \(what)")
    notify("С \(from): \(what) — можно вставлять", link: link)
    writeState()
}

func handle(_ event: [String: Any], primary: Bool) {
    guard event["event"] as? String == "message", let id = event["id"] as? String else { return }
    if primary { lastID = id }
    let time = event["time"] as? Double ?? Date().timeIntervalSince1970
    let age = Date().timeIntervalSince1970 - time
    guard let message = event["message"] as? String, let sealedMeta = Data(base64Encoded: message),
          let metaJSON = unseal(sealedMeta),
          let meta = (try? JSONSerialization.jsonObject(with: metaJSON)) as? [String: Any] else {
        log("не расшифровал сообщение \(id): другой ключ? mnrh tossy pair")
        writeState()
        return
    }
    if let sender = meta["id"] as? String, !conf.deviceID.isEmpty, sender == conf.deviceID { return }
    if let targets = meta["to"] as? [String], !conf.deviceID.isEmpty, !targets.contains(conf.deviceID) { return }
    let kind = meta["k"] as? String ?? ""
    if age >= staleAfter && !["bye", "rekey", "kick", "file"].contains(kind) {
        log("пропустил старое сообщение \(id)")
        writeState()
        return
    }
    if kind == "file" && age >= fileStaleAfter {
        let name = meta["f"] as? String ?? "file"
        let from = shownName(meta, "устройства")
        log("не получил файл \(name) от \(from): отправлен больше 3 часов назад")
        notify("Не получил «\(name)» от \(from): \(gone)")
        writeState()
        return
    }
    guard let attachment = event["attachment"] as? [String: Any], let url = attachment["url"] as? String,
          let link = URL(string: url) else {
        apply(meta, nil)
        return
    }
    if meta["k"] as? String == "file", (meta["x"] as? NSNumber)?.intValue == 2 {
        receiveStream(meta, link, id, fresh: age < staleAfter)
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
                if kind == "file", code == 404 || code == 410 {
                    notify("Не получил «\(meta["f"] as? String ?? "file")» от \(shownName(meta, "устройства")): \(gone)")
                }
                writeState()
                return
            }
            apply(meta, plain, fresh: age < staleAfter)
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
    var failed = false
    let target: [String: Any] = sendTo.isEmpty ? [:] : ["to": sendTo]
    var jobs = [(@escaping () -> Void) -> Void]()
    func report(_ ok: Bool, _ what: String) {
        failed = failed || !ok
        print(ok ? "✓ отправил \(what)" : "✗ не отправил \(what)")
    }
    func textJob(_ text: String) -> (@escaping () -> Void) -> Void {
        { next in
            guard Data(text.utf8).count <= maxText else {
                print("текст больше \(maxText / 1_000_000) МБ")
                failed = true
                next()
                return
            }
            publish(kind: "text", mime: "text/plain", data: Data(text.utf8), text: text, extra: target) { ok in
                report(ok, "текст")
                if sendNotify { notify(ok ? "Отправил текст" : "Не отправил текст") }
                next()
            }
        }
    }
    for path in sendTexts {
        guard let data = FileManager.default.contents(atPath: path), let text = String(data: data, encoding: .utf8) else {
            print("не прочитал текст")
            failed = true
            continue
        }
        jobs.append(textJob(text))
    }
    for item in sendItems {
        let url = URL(fileURLWithPath: (item as NSString).expandingTildeInPath)
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) else {
            jobs.append(textJob(item))
            continue
        }
        jobs.append { next in
            sendItem(url, extra: target, announce: sendNotify) { ok in
                report(ok, url.lastPathComponent)
                next()
            }
        }
    }
    jobs.append { _ in exit(failed ? 1 : 0) }
    sendQueue(jobs)
    RunLoop.main.run()
}

let showMenuNotice = Notification.Name("com.kopylovis.tossy.desktop.show-menu")
let others = NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
    .filter { $0.processIdentifier != getpid() }
if !others.isEmpty {
    DistributedNotificationCenter.default().postNotificationName(showMenuNotice, object: nil, userInfo: nil, deliverImmediately: true)
    exit(0)
}

isAgent = true
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

var hotKeyRef: EventHotKeyRef?
var pressed = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
    DispatchQueue.main.async { copyAndSend() }
    return noErr
}, 1, &pressed, nil, nil)

let hotkeyChoices: [(keys: String, title: String, carbon: Int, menu: NSEvent.ModifierFlags)] = [
    ("ctrl-opt-cmd-c", "⌃⌥⌘C", cmdKey | optionKey | controlKey, [.control, .option, .command]),
    ("shift-cmd-c", "⇧⌘C", cmdKey | shiftKey, [.shift, .command]),
]

var hotkeyChoice: (keys: String, title: String, carbon: Int, menu: NSEvent.ModifierFlags) {
    hotkeyChoices.first { $0.keys == conf.hotkeyKeys } ?? hotkeyChoices[0]
}

var askedAccessibility = false

func copyAndSend() {
    guard AXIsProcessTrusted() else {
        if !askedAccessibility {
            askedAccessibility = true
            let prompt = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
            _ = AXIsProcessTrustedWithOptions([prompt: true] as CFDictionary)
            notify("Разреши Tossy «Универсальный доступ»: тогда \(hotkeyChoice.title) будет само копировать выделенное. Пока отправляю то, что уже в буфере")
        }
        sendNow()
        return
    }
    let before = pasteboard.changeCount
    let source = CGEventSource(stateID: .combinedSessionState)
    for down in [true, false] {
        let event = CGEvent(keyboardEventSource: source, virtualKey: CGKeyCode(kVK_ANSI_C), keyDown: down)
        event?.flags = .maskCommand
        event?.post(tap: .cghidEventTap)
    }
    var waited = 0.0
    func check() {
        if pasteboard.changeCount != before || waited >= 0.6 {
            sendNow()
            return
        }
        waited += 0.05
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05, execute: check)
    }
    check()
}

func registerHotKey() {
    guard hotKeyRef == nil else { return }
    let choice = hotkeyChoice
    let status = RegisterEventHotKey(UInt32(kVK_ANSI_C), UInt32(choice.carbon),
                                     EventHotKeyID(signature: OSType(0x54535359), id: 1), GetApplicationEventTarget(), 0, &hotKeyRef)
    log(status == noErr ? "\(choice.title) — отправить буфер вручную" : "не занял \(choice.title): \(status)")
}

func unregisterHotKey() {
    guard let ref = hotKeyRef else { return }
    UnregisterEventHotKey(ref)
    hotKeyRef = nil
}

if conf.hotkey { registerHotKey() }
log("слежу за буфером, сервер \(conf.server)")

signal(SIGTERM, SIG_IGN)
let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
term.setEventHandler { exit(0) }
term.resume()

retireTokens()
Timer.scheduledTimer(withTimeInterval: 3600, repeats: true) { _ in retireTokens() }

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        StatusMenu.shared.openMenu()
        return false
    }
}

let appDelegate = AppDelegate()
app.delegate = appDelegate
DistributedNotificationCenter.default().addObserver(forName: showMenuNotice, object: nil, queue: .main) { _ in
    StatusMenu.shared.openMenu()
}
Notifier.shared.start()
_ = History.shared
Alerts.shared.start()
StatusMenu.shared.install()
LinkHandler.shared.install()
ScreenshotWatcher.shared.refresh()
app.run()
