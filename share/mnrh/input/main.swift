// mnrh input: раскладка клавиатуры под приложение.
//
// Когда приложение выходит на передний план, ставим ему раскладку: либо заданную правилом
// (Android Studio, Xcode, терминал — английская), либо ту, что была у него в прошлый раз.
// Работает на публичных API: NSWorkspace сообщает о смене приложения, Text Input Sources
// переключает раскладку. Разрешения в Универсальном доступе не нужны.

import AppKit
import Carbon

var configFile = ""
var stateFile = ""
var argv = CommandLine.arguments.dropFirst().makeIterator()
var listOnly = false
while let arg = argv.next() {
    switch arg {
    case "--config": configFile = argv.next() ?? ""
    case "--state": stateFile = argv.next() ?? ""
    case "--list": listOnly = true
    default: break
    }
}

func log(_ text: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    FileHandle.standardError.write("\(stamp) \(text)\n".data(using: .utf8)!)
}

func property(_ source: TISInputSource, _ key: CFString) -> AnyObject? {
    guard let raw = TISGetInputSourceProperty(source, key) else { return nil }
    return Unmanaged<AnyObject>.fromOpaque(raw).takeUnretainedValue()
}

func keyboardSources() -> [TISInputSource] {
    let filter = [kTISPropertyInputSourceCategory as String: kTISCategoryKeyboardInputSource as String,
                  kTISPropertyInputSourceIsSelectCapable as String: true] as CFDictionary
    return (TISCreateInputSourceList(filter, false)?.takeRetainedValue() as? [TISInputSource]) ?? []
}

func sourceID(_ source: TISInputSource) -> String {
    return property(source, kTISPropertyInputSourceID) as? String ?? ""
}

func currentID() -> String {
    return sourceID(TISCopyCurrentKeyboardInputSource().takeRetainedValue())
}

// --list: раскладки для mnrh input (id, название, языки через запятую).
if listOnly {
    for s in keyboardSources() {
        let name = property(s, kTISPropertyLocalizedName) as? String ?? ""
        let langs = (property(s, kTISPropertyInputSourceLanguages) as? [String] ?? []).joined(separator: ",")
        print("\(sourceID(s))\t\(name)\t\(langs)")
    }
    print("current\t\(currentID())")
    exit(0)
}

struct Config {
    var rules: [String: String] = [:]   // bundle id -> id раскладки
    var remember = true
}

var config = Config()
var configStamp = Date.distantPast
var memory: [String: String] = [:]      // что было у приложения в прошлый раз
var lastApp: String?
var switches = 0

func reloadConfig() {
    guard let attrs = try? FileManager.default.attributesOfItem(atPath: configFile),
          let modified = attrs[.modificationDate] as? Date, modified != configStamp else { return }
    configStamp = modified
    guard let data = FileManager.default.contents(atPath: configFile),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
    config.rules = json["rules"] as? [String: String] ?? [:]
    config.remember = json["remember"] as? Bool ?? true
    log("настройки: правил \(config.rules.count), запоминать \(config.remember ? "да" : "нет")")
}

func select(_ id: String) -> Bool {
    guard id != currentID() else { return false }
    guard let source = keyboardSources().first(where: { sourceID($0) == id }) else {
        log("раскладки \(id) нет среди включённых")
        return false
    }
    return TISSelectInputSource(source) == noErr
}

func writeState() {
    guard !stateFile.isEmpty else { return }
    let state: [String: Any] = ["pid": getpid(), "switches": switches, "remembered": memory.count,
                                "current": currentID(), "app": lastApp ?? ""]
    if let data = try? JSONSerialization.data(withJSONObject: state) {
        try? data.write(to: URL(fileURLWithPath: stateFile), options: .atomic)
    }
}

func activated(_ app: NSRunningApplication) {
    guard let bundle = app.bundleIdentifier else { return }
    reloadConfig()
    // Уходящему приложению запоминаем, на какой раскладке его оставили.
    if let previous = lastApp, config.remember, config.rules[previous] == nil {
        memory[previous] = currentID()
    }
    lastApp = bundle
    let want = config.rules[bundle] ?? (config.remember ? memory[bundle] : nil)
    if let want = want {
        // Небольшая пауза: сразу после активации окно иногда ещё не приняло фокус ввода.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
            if select(want) {
                switches += 1
            }
            writeState()
        }
    } else {
        writeState()
    }
}

// Без NSApplication фоновому процессу не приходят уведомления о смене активного приложения.
let application = NSApplication.shared
application.setActivationPolicy(.prohibited)

reloadConfig()
if let front = NSWorkspace.shared.frontmostApplication { lastApp = front.bundleIdentifier }
NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didActivateApplicationNotification,
                                                  object: nil, queue: .main) { note in
    if let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication {
        activated(app)
    }
}

signal(SIGTERM, SIG_IGN)
let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
term.setEventHandler {
    if !stateFile.isEmpty { try? FileManager.default.removeItem(atPath: stateFile) }
    exit(0)
}
term.resume()

log("запущен")
writeState()
application.run()
