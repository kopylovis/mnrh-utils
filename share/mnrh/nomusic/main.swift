import AppKit
import ServiceManagement

let musicID = "com.apple.Music"
let manualWindow = 2.0
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
    let service = SMAppService.agent(plistName: (Bundle.main.bundleIdentifier ?? "com.mnrh.nomusic") + ".plist")
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
    print("помощник собран, следит за \(musicID)")
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
var allowedCount = 0
var lastBlocked: Double = 0
var decided = [pid_t: Bool]()

func writeState() {
    guard !stateFile.isEmpty else { return }
    let state: [String: Any] = ["pid": getpid(), "started": Int(started.timeIntervalSince1970),
                                "blocked": blockedCount, "allowed": allowedCount, "last_blocked": Int(lastBlocked)]
    if let data = try? JSONSerialization.data(withJSONObject: state) {
        try? data.write(to: URL(fileURLWithPath: stateFile), options: .atomic)
    }
}

func sinceUserInput() -> Double {
    let types: [CGEventType] = [.leftMouseDown, .leftMouseUp, .rightMouseDown, .keyDown, .keyUp]
    return types.map { CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: $0) }.min() ?? .infinity
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

func handle(_ app: NSRunningApplication?, _ when: String, sweep: Bool = false) {
    guard let app = app, app.bundleIdentifier == musicID, !app.isTerminated else { return }
    let pid = app.processIdentifier
    if decided[pid] != nil { return }
    let conf = config()
    let always = (conf["mode"] as? String) == "always"
    let idle = sinceUserInput()
    let front = NSWorkspace.shared.frontmostApplication?.localizedName ?? "?"
    let detail = "\(when), ввод \(idle < 60 ? String(format: "%.1f с", idle) : "давно") назад, впереди \(front)"
    if let until = conf["paused_until"] as? Double, until > Date().timeIntervalSince1970 {
        decided[pid] = true
        log("Music открыт (\(detail)) — пауза, не трогаю")
        return
    }
    if sweep, !always, let launched = app.launchDate, Date().timeIntervalSince(launched) > 10 {
        decided[pid] = true
        log("Music уже работал (\(detail)) — не трогаю")
        return
    }
    if !always && !sweep && idle < manualWindow {
        decided[pid] = true
        allowedCount += 1
        log("Music открыт вручную (\(detail)) — оставил")
        writeState()
        return
    }
    decided[pid] = false
    app.forceTerminate()
    blockedCount += 1
    lastBlocked = Date().timeIntervalSince1970
    log("закрыл Music (\(detail), pid \(pid))")
    openReplacement(conf)
    writeState()
}

let workspace = NSWorkspace.shared.notificationCenter
for (name, when) in [(NSWorkspace.willLaunchApplicationNotification, "запуск"),
                     (NSWorkspace.didLaunchApplicationNotification, "после запуска")] {
    workspace.addObserver(forName: name, object: nil, queue: .main) { note in
        handle(note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication, when)
    }
}
workspace.addObserver(forName: NSWorkspace.didTerminateApplicationNotification, object: nil, queue: .main) { note in
    if let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication {
        decided.removeValue(forKey: app.processIdentifier)
    }
}
workspace.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { _ in
    NSWorkspace.shared.runningApplications.forEach { handle($0, "после сна", sweep: true) }
}
NSWorkspace.shared.runningApplications.forEach { handle($0, "при старте помощника", sweep: true) }

Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in writeState() }
writeState()
log((config()["mode"] as? String) == "always" ? "слежу за Music: закрываю всегда"
    : "слежу за Music: закрываю автозапуск, ручной оставляю")

signal(SIGTERM, SIG_IGN)
let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
term.setEventHandler { exit(0) }
term.resume()

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
app.run()
