// mnrh audio: звуковые устройства и «микрофон остаётся встроенным».
//
// Когда Bluetooth-наушники становятся микрофоном по умолчанию, macOS переводит их в режим
// гарнитуры (HFP): звук в них становится моно и глухим. В режиме --keep-mic помощник следит
// за устройством ввода по умолчанию и, если им стали Bluetooth-наушники, возвращает
// встроенный микрофон. Разрешение на микрофон не нужно: звук мы не читаем.

import CoreAudio
import Foundation

var stateFile = ""
var mode = ""
var target = ""
var argv = CommandLine.arguments.dropFirst().makeIterator()
while let arg = argv.next() {
    switch arg {
    case "--list": mode = "list"
    case "--set-output": mode = "output"; target = argv.next() ?? ""
    case "--set-input": mode = "input"; target = argv.next() ?? ""
    case "--keep-mic": mode = "keep"
    case "--state": stateFile = argv.next() ?? ""
    default: break
    }
}

func log(_ text: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    FileHandle.standardError.write("\(stamp) \(text)\n".data(using: .utf8)!)
}

let system = AudioObjectID(kAudioObjectSystemObject)

func address(_ selector: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    return AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func devices() -> [AudioDeviceID] {
    var addr = address(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(system, &addr, 0, nil, &size)
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &ids)
    return ids
}

func string(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String {
    var addr = address(selector)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value) == noErr, let v = value else { return "" }
    return v.takeRetainedValue() as String
}

func transport(_ id: AudioDeviceID) -> String {
    var addr = address(kAudioDevicePropertyTransportType)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value)
    switch value {
    case kAudioDeviceTransportTypeBuiltIn: return "builtin"
    case kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE: return "bluetooth"
    case kAudioDeviceTransportTypeUSB: return "usb"
    case kAudioDeviceTransportTypeVirtual, kAudioDeviceTransportTypeAggregate: return "virtual"
    case kAudioDeviceTransportTypeHDMI, kAudioDeviceTransportTypeDisplayPort: return "display"
    case kAudioDeviceTransportTypeAirPlay: return "airplay"
    default: return "other"
    }
}

func channels(_ id: AudioDeviceID, _ scope: AudioObjectPropertyScope) -> Int {
    var addr = address(kAudioDevicePropertyStreamConfiguration, scope)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, raw) == noErr else { return 0 }
    let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return list.reduce(0) { $0 + Int($1.mNumberChannels) }
}

func defaultDevice(_ selector: AudioObjectPropertySelector) -> AudioDeviceID {
    var addr = address(selector)
    var id: AudioDeviceID = 0
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &id)
    return id
}

func setDefault(_ selector: AudioObjectPropertySelector, _ id: AudioDeviceID) -> Bool {
    var addr = address(selector)
    var value = id
    return AudioObjectSetPropertyData(system, &addr, 0, nil, UInt32(MemoryLayout<AudioDeviceID>.size), &value) == noErr
}

func byUID(_ uid: String) -> AudioDeviceID? {
    return devices().first { string($0, kAudioDevicePropertyDeviceUID) == uid }
}

switch mode {
case "list":
    // uid, название, подключение, входных каналов, выходных, по умолчанию (in/out)
    let input = defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
    let output = defaultDevice(kAudioHardwarePropertyDefaultOutputDevice)
    for d in devices() {
        let marks = (d == input ? "in" : "") + (d == output ? "out" : "")
        print([string(d, kAudioDevicePropertyDeviceUID), string(d, kAudioObjectPropertyName), transport(d),
               String(channels(d, kAudioObjectPropertyScopeInput)), String(channels(d, kAudioObjectPropertyScopeOutput)),
               marks].joined(separator: "\t"))
    }
    exit(0)
case "output", "input":
    guard let id = byUID(target) else {
        print("нет устройства \(target)")
        exit(1)
    }
    let ok = setDefault(mode == "output" ? kAudioHardwarePropertyDefaultOutputDevice
                                         : kAudioHardwarePropertyDefaultInputDevice, id)
    if mode == "output" && ok {
        _ = setDefault(kAudioHardwarePropertyDefaultSystemOutputDevice, id)  // звуки системы туда же
    }
    exit(ok ? 0 : 1)
case "keep":
    break
default:
    print("mnrh-audio --list | --set-output UID | --set-input UID | --keep-mic")
    exit(2)
}

// ---------- помощник: встроенный микрофон вместо Bluetooth ----------

var restored = 0

func writeState() {
    guard !stateFile.isEmpty else { return }
    let state: [String: Any] = ["pid": getpid(), "restored": restored]
    if let data = try? JSONSerialization.data(withJSONObject: state) {
        try? data.write(to: URL(fileURLWithPath: stateFile), options: .atomic)
    }
}

func builtInMic() -> AudioDeviceID? {
    return devices().first { transport($0) == "builtin" && channels($0, kAudioObjectPropertyScopeInput) > 0 }
}

func check() {
    let input = defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
    guard transport(input) == "bluetooth", let mic = builtInMic(), mic != input else { return }
    if setDefault(kAudioHardwarePropertyDefaultInputDevice, mic) {
        restored += 1
        log("вход стал \(string(input, kAudioObjectPropertyName)), вернул \(string(mic, kAudioObjectPropertyName))")
        writeState()
    }
}

var watch = address(kAudioHardwarePropertyDefaultInputDevice)
AudioObjectAddPropertyListenerBlock(system, &watch, DispatchQueue.main) { _, _ in check() }
var devicesWatch = address(kAudioHardwarePropertyDevices)
AudioObjectAddPropertyListenerBlock(system, &devicesWatch, DispatchQueue.main) { _, _ in
    // Наушники подключились: macOS переключает вход не сразу, проверим ещё раз чуть позже.
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { check() }
}

signal(SIGTERM, SIG_IGN)
let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
term.setEventHandler {
    if !stateFile.isEmpty { try? FileManager.default.removeItem(atPath: stateFile) }
    exit(0)
}
term.resume()

log("слежу за микрофоном")
check()
writeState()
dispatchMain()
