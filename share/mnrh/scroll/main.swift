// mnrh scroll: переворачивает прокрутку мыши, не трогая трекпад.
//
// Scroll Reverser отличает трекпад от мыши по касаниям пальцев из отдельного потока
// жестов. После сна этот поток иногда пропадает, и тогда вся прокрутка считается
// мышиной. Здесь источник берётся из самого события: CGEventCopyIOHIDEvent даёт
// исходное HID-событие, IOHIDEventGetSenderID — id сервиса устройства в IORegistry,
// а по классу и имени сервиса видно, трекпад это или нет. От жестов ничего не зависит.

import AppKit
import ApplicationServices
import IOKit
import ServiceManagement

struct Axes {
    var vertical = false
    var horizontal = false

    init(_ spec: String) {
        vertical = spec.contains("v")
        horizontal = spec.contains("h")
    }
}

var mouse = Axes("vh")
var trackpad = Axes("")
var wheelStep: Int64 = 3
var stateFile = ""
var quiet = false

var argv = CommandLine.arguments.dropFirst().makeIterator()
while let arg = argv.next() {
    switch arg {
    case "--mouse": mouse = Axes(argv.next() ?? "")
    case "--trackpad": trackpad = Axes(argv.next() ?? "")
    case "--step": wheelStep = Int64(argv.next() ?? "") ?? 3
    case "--state": stateFile = argv.next() ?? ""
    case "--config":
        let path = argv.next() ?? ""
        if let data = FileManager.default.contents(atPath: path),
           let conf = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            mouse = Axes(conf["mouse"] as? String ?? "vh")
            trackpad = Axes(conf["trackpad"] as? String ?? "")
            wheelStep = Int64(conf["step"] as? Int ?? 3)
        }
    default: break
    }
}

let serviceCommands = ["--register", "--unregister", "--service-status"]
if let command = CommandLine.arguments.dropFirst().first(where: serviceCommands.contains) {
    let service = SMAppService.agent(plistName: (Bundle.main.bundleIdentifier ?? "com.mnrh.scroll") + ".plist")
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

// Закрытые, но стабильные функции IOKit/CoreGraphics, ими же пользуются Scroll Reverser и LinearMouse.
let handle = dlopen(nil, RTLD_NOW)
typealias CopyHIDFn = @convention(c) (CGEvent) -> Unmanaged<CFTypeRef>?
typealias SenderFn = @convention(c) (CFTypeRef) -> UInt64
typealias GetFloatFn = @convention(c) (CFTypeRef, UInt32) -> Double
typealias SetFloatFn = @convention(c) (CFTypeRef, UInt32, Double) -> Void
func symbol<T>(_ name: String, _ type: T.Type) -> T? {
    guard let p = dlsym(handle, name) else { return nil }
    return unsafeBitCast(p, to: type)
}
let copyHIDEvent = symbol("CGEventCopyIOHIDEvent", CopyHIDFn.self)
let senderID = symbol("IOHIDEventGetSenderID", SenderFn.self)
let getFloat = symbol("IOHIDEventGetFloatValue", GetFloatFn.self)
let setFloat = symbol("IOHIDEventSetFloatValue", SetFloatFn.self)
let fieldScrollX: UInt32 = 6 << 16
let fieldScrollY: UInt32 = (6 << 16) | 1

var deviceCache: [UInt64: Bool] = [:]

func registryString(_ service: io_service_t, _ key: String) -> String {
    let options = IOOptionBits(kIORegistryIterateRecursively | kIORegistryIterateParents)
    let value = IORegistryEntrySearchCFProperty(service, kIOServicePlane, key as CFString, kCFAllocatorDefault, options)
    return value as? String ?? ""
}

func isTrackpadDevice(_ id: UInt64) -> Bool? {
    if let known = deviceCache[id] { return known }
    let service = IOServiceGetMatchingService(kIOMainPortDefault, IORegistryEntryIDMatching(id))
    guard service != 0 else { return nil }
    defer { IOObjectRelease(service) }
    var name = [CChar](repeating: 0, count: 128)
    IOObjectGetClass(service, &name)
    let cls = String(cString: name)
    let product = registryString(service, "Product")
    let result = cls.contains("Trackpad") || product.contains("Trackpad")
    deviceCache[id] = result
    if !quiet { log("устройство \(id): \(cls), «\(product)» -> \(result ? "трекпад" : "мышь")") }
    return result
}

var verboseUntil = Date.distantPast
var counts = ["mouse": 0, "trackpad": 0, "other": 0]

func handleScroll(_ event: CGEvent) {
    let hid = copyHIDEvent?(event)?.takeRetainedValue()
    let continuous = event.getIntegerValueField(.scrollWheelEventIsContinuous) != 0
    let fromApp = event.getIntegerValueField(.eventSourceUnixProcessID) != 0

    var isTrackpad: Bool?
    if let hid = hid, let senderID = senderID {
        let id = senderID(hid)
        if id != 0 { isTrackpad = isTrackpadDevice(id) }
    }
    if isTrackpad == nil && !fromApp {
        // Нет HID-события: колесо с щелчками — точно мышь, плавное — считаем трекпадом и не трогаем.
        isTrackpad = continuous
    }
    guard let trackpadSource = isTrackpad else {
        counts["other"]! += 1
        return
    }
    counts[trackpadSource ? "trackpad" : "mouse"]! += 1
    let axes = trackpadSource ? trackpad : mouse
    let dy = event.getIntegerValueField(.scrollWheelEventDeltaAxis1)
    let stepWheel = !trackpadSource && !continuous && wheelStep > 1 && abs(dy) == 1
    let vmul: Int64 = axes.vertical ? -1 : 1
    let hmul: Int64 = axes.horizontal ? -1 : 1

    if Date() < verboseUntil {
        let what = trackpadSource ? "трекпад" : "мышь"
        let action = axes.vertical || axes.horizontal ? "переворачиваю" : "без изменений"
        log("scroll \(what) \(continuous ? "плавная" : "щелчки") y=\(dy): \(action)")
    }

    // Порядок как у Scroll Reverser: сначала DeltaAxis (macOS пересчитывает из неё
    // остальные поля), потом точные значения, иначе пропадает плавность.
    if stepWheel || vmul != 1 {
        event.setIntegerValueField(.scrollWheelEventDeltaAxis1, value: dy * vmul * (stepWheel ? wheelStep : 1))
    }
    if !stepWheel && vmul != 1 {
        event.setDoubleValueField(.scrollWheelEventFixedPtDeltaAxis1,
                                  value: -event.getDoubleValueField(.scrollWheelEventFixedPtDeltaAxis1))
        event.setIntegerValueField(.scrollWheelEventPointDeltaAxis1,
                                   value: -event.getIntegerValueField(.scrollWheelEventPointDeltaAxis1))
        if let hid = hid, let getFloat = getFloat, let setFloat = setFloat {
            setFloat(hid, fieldScrollY, -getFloat(hid, fieldScrollY))
        }
    }
    if hmul != 1 {
        event.setIntegerValueField(.scrollWheelEventDeltaAxis2,
                                   value: -event.getIntegerValueField(.scrollWheelEventDeltaAxis2))
        event.setDoubleValueField(.scrollWheelEventFixedPtDeltaAxis2,
                                  value: -event.getDoubleValueField(.scrollWheelEventFixedPtDeltaAxis2))
        event.setIntegerValueField(.scrollWheelEventPointDeltaAxis2,
                                   value: -event.getIntegerValueField(.scrollWheelEventPointDeltaAxis2))
        if let hid = hid, let getFloat = getFloat, let setFloat = setFloat {
            setFloat(hid, fieldScrollX, -getFloat(hid, fieldScrollX))
        }
    }
}

var tap: CFMachPort?
var tapSource: CFRunLoopSource?
var reenabled = 0
var wakes = 0
let started = Date()

let callback: CGEventTapCallBack = { _, type, event, _ in
    switch type {
    case .scrollWheel:
        handleScroll(event)
    case .tapDisabledByTimeout, .tapDisabledByUserInput:
        reenabled += 1
        log("система отключила перехват (\(type == .tapDisabledByTimeout ? "таймаут" : "ввод")), включаю снова")
        if let tap = tap { CGEvent.tapEnable(tap: tap, enable: true) }
        writeState()
    default:
        break
    }
    return Unmanaged.passUnretained(event)
}

func startTap() {
    stopTap()
    let mask = CGEventMask(1 << CGEventType.scrollWheel.rawValue)
    tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .tailAppendEventTap, options: .defaultTap,
                            eventsOfInterest: mask, callback: callback, userInfo: nil)
    guard let tap = tap else {
        log("перехват не создан: нет доступа в Универсальном доступе")
        return
    }
    tapSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetMain(), tapSource, .commonModes)
    CGEvent.tapEnable(tap: tap, enable: true)
    log("перехват включён: мышь \(describe(mouse)), трекпад \(describe(trackpad)), шаг колеса \(wheelStep)")
}

func stopTap() {
    if let source = tapSource { CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes) }
    if let tap = tap { CFMachPortInvalidate(tap) }
    tap = nil
    tapSource = nil
}

func describe(_ axes: Axes) -> String {
    switch (axes.vertical, axes.horizontal) {
    case (true, true): return "переворачивается по обеим осям"
    case (true, false): return "переворачивается по вертикали"
    case (false, true): return "переворачивается по горизонтали"
    default: return "не трогается"
    }
}

func writeState() {
    guard !stateFile.isEmpty else { return }
    let active = tap.map { CGEvent.tapIsEnabled(tap: $0) } ?? false
    let state: [String: Any] = [
        "pid": getpid(), "trusted": AXIsProcessTrusted(), "active": active,
        "started": Int(started.timeIntervalSince1970), "wakes": wakes, "reenabled": reenabled,
        "mouse_events": counts["mouse"]!, "trackpad_events": counts["trackpad"]!,
    ]
    if let data = try? JSONSerialization.data(withJSONObject: state) {
        try? data.write(to: URL(fileURLWithPath: stateFile), options: .atomic)
    }
}

// --check: самопроверка без перехвата, для mnrh scroll test и make test.
if CommandLine.arguments.contains("--check") {
    quiet = true
    let missing = [("CGEventCopyIOHIDEvent", copyHIDEvent != nil), ("IOHIDEventGetSenderID", senderID != nil),
                   ("IOHIDEventGetFloatValue", getFloat != nil), ("IOHIDEventSetFloatValue", setFloat != nil)]
        .filter { !$0.1 }.map { $0.0 }
    print(missing.isEmpty ? "функции IOKit: есть" : "функции IOKit: нет \(missing.joined(separator: ", "))")
    var iterator: io_iterator_t = 0
    IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("IOHIDEventService"), &iterator)
    var found = 0
    while case let service = IOIteratorNext(iterator), service != 0 {
        var id: UInt64 = 0
        IORegistryEntryGetRegistryEntryID(service, &id)
        if isTrackpadDevice(id) == true { found += 1 }
        IOObjectRelease(service)
    }
    IOObjectRelease(iterator)
    print(found > 0 ? "трекпад: найден" : "трекпад: не найден")
    exit(missing.isEmpty ? 0 : 1)
}

// Доступ: при первом запуске macOS сама покажет окно со ссылкой на настройки.
let promptKey = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
if AXIsProcessTrustedWithOptions([promptKey: true] as CFDictionary) {
    startTap()
} else {
    log("жду доступ в Универсальном доступе")
}

// Раз в несколько секунд: появился ли доступ, жив ли перехват, свежее состояние на диск.
Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { _ in
    if tap == nil {
        if AXIsProcessTrusted() { startTap() }
    } else if let tap = tap, !CGEvent.tapIsEnabled(tap: tap) {
        reenabled += 1
        log("перехват оказался выключен, включаю снова")
        CGEvent.tapEnable(tap: tap, enable: true)
    }
    writeState()
}

// После сна устройства могут получить новые id, а перехват — тихо умереть. Пересоздаём всё.
NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil,
                                                  queue: .main) { _ in
    wakes += 1
    deviceCache.removeAll()
    log("пробуждение после сна, пересоздаю перехват")
    if AXIsProcessTrusted() { startTap() }
    writeState()
}

// SIGUSR1: подробный лог каждого события на 20 секунд, для mnrh scroll test.
signal(SIGUSR1, SIG_IGN)
let usr1 = DispatchSource.makeSignalSource(signal: SIGUSR1, queue: .main)
usr1.setEventHandler {
    verboseUntil = Date().addingTimeInterval(20)
    log("подробный лог на 20 секунд")
}
usr1.resume()

signal(SIGTERM, SIG_IGN)
let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
term.setEventHandler {
    stopTap()
    if !stateFile.isEmpty { try? FileManager.default.removeItem(atPath: stateFile) }
    exit(0)
}
term.resume()

writeState()
RunLoop.main.run()
