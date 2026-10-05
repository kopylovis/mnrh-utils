import AppKit
import CryptoKit

func randomHex(_ bytes: Int) -> String {
    (0..<bytes).map { _ in String(format: "%02x", UInt8.random(in: 0...255)) }.joined()
}

func reloadConfig() {
    if let fresh = loadConfig() { conf = fresh }
    StatusMenu.shared.refresh()
}

func changeConfig(_ change: (inout [String: Any]) -> Void) {
    if rewriteConfig(change) { reloadConfig() }
}

func memberKey(_ info: [String: Any]) -> String? {
    guard let text = info["pk"] as? String, Data(base64Encoded: text)?.count == 32 else { return nil }
    return text
}

func createToken(label: String) -> String? {
    var r = request("v1/account/token", method: "POST")
    r.setValue("application/json", forHTTPHeaderField: "Content-Type")
    r.httpBody = try? JSONSerialization.data(withJSONObject: ["label": label])
    r.timeoutInterval = 20
    let (code, body) = runSync(r)
    guard code == 200, let json = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any],
          let token = json["token"] as? String, !token.isEmpty else {
        log("не выпустил новый токен: HTTP \(code)")
        return nil
    }
    return token
}

func retireTokens() {
    guard let data = FileManager.default.contents(atPath: configFile),
          let raw = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
          let pending = raw["retire"] as? [[String: Any]], !pending.isEmpty else { return }
    let now = Date().timeIntervalSince1970
    for entry in pending {
        guard let token = entry["t"] as? String, (entry["at"] as? Double ?? 0) <= now, token != conf.token else { continue }
        var r = request("v1/account/token", method: "DELETE")
        r.setValue(token, forHTTPHeaderField: "X-Token")
        r.timeoutInterval = 20
        URLSession.shared.dataTask(with: r) { _, response, _ in
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            guard (200..<500).contains(code) else { return }
            DispatchQueue.main.async {
                log(code == 200 ? "удалил старый токен ntfy" : "старый токен ntfy уже недействителен: HTTP \(code)")
                _ = rewriteConfig { raw in
                    raw["retire"] = (raw["retire"] as? [[String: Any]] ?? []).filter { $0["t"] as? String != token }
                }
            }
        }.resume()
    }
}

func revokeDevice(_ id: String) {
    guard id != conf.owner else {
        notify("Создателя комнаты нельзя отключить")
        return
    }
    let name = (members[id]?["name"] as? String).map { conf.aliases[id] ?? $0 } ?? "устройство"
    let others = members.filter { $0.key != id && $0.key != conf.deviceID && !$0.key.hasPrefix("legacy-") }
    let isLegacy = others.values.contains { memberKey($0) == nil }
    let room = "tossy-" + randomHex(12)
    let key = SymmetricKey(size: .bits256).withUnsafeBytes { Data($0) }.base64EncodedString()
    let oldToken = conf.token
    DispatchQueue.global(qos: .userInitiated).async {
        let token = isLegacy || oldToken.isEmpty ? nil : createToken(label: "tossy")
        DispatchQueue.main.async {
            publish(kind: "kick", mime: "text/plain", data: Data(), text: "", extra: ["to": [id]]) { kicked in
                guard kicked else {
                    notify("Не отключил \(name): нет связи с сервером")
                    return
                }
                let finish = {
                    let saved = rewriteConfig { raw in
                        raw["room"] = room
                        raw["key"] = key
                        raw["legacy_to_mac"] = ""
                        raw["legacy_to_phone"] = ""
                        if let token = token {
                            raw["token"] = token
                            var retire = raw["retire"] as? [[String: Any]] ?? []
                            retire.append(["t": oldToken, "at": Date().timeIntervalSince1970 + 24 * 3600])
                            raw["retire"] = retire
                        }
                    }
                    members[id] = nil
                    lastID = ""
                    writeState()
                    log("отключил \(name): новая комната и ключ\(token == nil ? "" : ", новый токен")")
                    notify("\(name) отключён от комнаты\(isLegacy ? ". Часть устройств без ключей: обнови их и отключи ещё раз, если нужно" : "")")
                    if saved { DispatchQueue.main.asyncAfter(deadline: .now() + 1) { exit(0) } }
                }
                guard !others.isEmpty else { return finish() }
                let keys = others.compactMapValues(memberKey)
                let sealed = sealRoomKey(room: room, key: key, token: token, for: keys)
                var extra: [String: Any] = ["to": Array(others.keys), "e": sealed.ephemeral, "keys": sealed.keys]
                if isLegacy {
                    extra["r"] = room
                    extra["key"] = key
                }
                publish(kind: "rekey", mime: "text/plain", data: Data(), text: "", extra: extra) { ok in
                    if ok { finish() } else { notify("Не разослал новый ключ: нет связи с сервером, \(name) всё ещё в комнате") }
                }
            }
        }
    }
}

func renameSelf(_ name: String) {
    changeConfig { $0["name"] = name }
    publish(kind: "hello", mime: "text/plain", data: Data(), text: "")
}

func setAlias(_ id: String, _ alias: String) {
    changeConfig { raw in
        var aliases = raw["aliases"] as? [String: String] ?? [:]
        aliases[id] = alias.isEmpty ? nil : alias
        raw["aliases"] = aliases
    }
}

func pause(until: Double) {
    changeConfig { $0["paused_until"] = until }
    log(until == 0 ? "снова отправляю" : "пауза до \(DateFormatter.localizedString(from: Date(timeIntervalSince1970: until), dateStyle: .none, timeStyle: .short))")
}

func setHotkeyKeys(_ keys: String) {
    changeConfig { $0["hotkey_keys"] = keys }
    unregisterHotKey()
    if conf.hotkey { registerHotKey() }
}

func setFlag(_ key: String, _ value: Bool) {
    changeConfig { $0[key] = value }
    if key == "hotkey" { value ? registerHotKey() : unregisterHotKey() }
    if key == "screenshots" { ScreenshotWatcher.shared.refresh() }
}

func helperCommand(_ args: [String], output: ((String) -> Void)? = nil, done: ((Int32) -> Void)? = nil) -> Process? {
    guard let path = Bundle.main.executablePath else { return nil }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: path)
    p.arguments = ["--config", configFile] + args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    if let output = output {
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            text.split(separator: "\n").forEach { line in DispatchQueue.main.async { output(String(line)) } }
        }
    }
    p.terminationHandler = { proc in
        pipe.fileHandleForReading.readabilityHandler = nil
        DispatchQueue.main.async { done?(proc.terminationStatus) }
    }
    do {
        try p.run()
    } catch {
        return nil
    }
    return p
}
