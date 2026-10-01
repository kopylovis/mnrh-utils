import AppKit
import ServiceManagement

let blocked: Set<String> = ["com.apple.Music", "com.apple.iTunes"]
var configFile = ""
var stateFile = ""

var argv = CommandLine.arguments.dropFirst().makeIterator()
while let arg = argv.next() {
    switch arg {
    case "--config": configFile = argv.next() ?? ""
    case "--state": stateFile = argv.next() ?? ""
    default: break
    }
}

let serviceCommands = ["--register", "--unregister", "--service-status"]
if let command = CommandLine.arguments.dropFirst().first(where: serviceCommands.contains) {
    let service = SMAppService.agent(plistName: (Bundle.main.bundleIdentifier ?? "com.mnrh.notunes") + ".plist")
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

func log(_ text: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    FileHandle.standardError.write("\(stamp) \(text)\n".data(using: .utf8)!)
}

func config() -> [String: Any] {
    guard let data = FileManager.default.contents(atPath: configFile),
          let conf = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return [:] }
    return conf
}

let started = Date()
var blockedCount = 0
var lastBlocked: Double = 0
var skipped = Set<pid_t>()

func writeState() {
    guard !stateFile.isEmpty else { return }
    let state: [String: Any] = ["pid": getpid(), "started": Int(started.timeIntervalSince1970),
                                "blocked": blockedCount, "last_blocked": Int(lastBlocked)]
    if let data = try? JSONSerialization.data(withJSONObject: state) {
        try? data.write(to: URL(fileURLWithPath: stateFile), options: .atomic)
    }
}

func paused(_ conf: [String: Any]) -> Bool {
    if let until = conf["paused_until"] as? Double { return until > Date().timeIntervalSince1970 }
    return false
}

func openReplacement(_ conf: [String: Any]) {
    guard let path = conf["replacement"] as? String, !path.isEmpty,
          FileManager.default.fileExists(atPath: path) else { return }
    let url = URL(fileURLWithPath: path)
    if let id = Bundle(url: url)?.bundleIdentifier,
       !NSRunningApplication.runningApplications(withBundleIdentifier: id).isEmpty { return }
    let options = NSWorkspace.OpenConfiguration()
    options.activates = false
    NSWorkspace.shared.openApplication(at: url, configuration: options) { _, error in
        if let error = error { log("не открылся \(path): \(error.localizedDescription)") }
    }
}

func block(_ app: NSRunningApplication?, _ when: String) {
    guard let app = app, let id = app.bundleIdentifier, blocked.contains(id), !app.isTerminated else { return }
    let conf = config()
    if paused(conf) {
        if skipped.insert(app.processIdentifier).inserted {
            log("\(id) запущен (\(when)), но mnrh notunes на паузе — пропускаю")
        }
        return
    }
    app.forceTerminate()
    blockedCount += 1
    lastBlocked = Date().timeIntervalSince1970
    log("закрыл \(id) (\(when), pid \(app.processIdentifier))")
    openReplacement(conf)
    writeState()
}

if CommandLine.arguments.contains("--check") {
    print("помощник собран, следит за: \(blocked.sorted().joined(separator: ", "))")
    exit(0)
}

let workspace = NSWorkspace.shared.notificationCenter
for (name, when) in [(NSWorkspace.willLaunchApplicationNotification, "запуск"),
                     (NSWorkspace.didLaunchApplicationNotification, "после запуска")] {
    workspace.addObserver(forName: name, object: nil, queue: .main) { note in
        block(note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication, when)
    }
}
workspace.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { _ in
    NSWorkspace.shared.runningApplications.forEach { block($0, "после сна") }
}
NSWorkspace.shared.runningApplications.forEach { block($0, "при старте помощника") }

Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in writeState() }
writeState()
log("слежу за Music и iTunes")

signal(SIGTERM, SIG_IGN)
let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
term.setEventHandler { exit(0) }
term.resume()

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
app.run()
