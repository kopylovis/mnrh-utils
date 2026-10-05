import AppKit

final class ScreenshotWatcher {
    static let shared = ScreenshotWatcher()

    private var source: DispatchSourceFileSystemObject?
    private var folder: URL?
    private var sent = Set<String>()

    func refresh() {
        let wanted = conf.screenshots ? screenshotFolder() : nil
        guard wanted != folder else { return }
        source?.cancel()
        source = nil
        folder = wanted
        guard let wanted = wanted else { return }
        DispatchQueue.global(qos: .utility).async {
            let fd = open(wanted.path, O_EVTONLY)
            DispatchQueue.main.async { self.watch(wanted, fd) }
        }
    }

    private func watch(_ wanted: URL, _ fd: Int32) {
        guard fd >= 0 else {
            log("не слежу за снимками экрана: нет доступа к \(wanted.path)")
            return
        }
        guard folder == wanted, source == nil else {
            close(fd)
            return
        }
        let watch = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: .write, queue: .main)
        watch.setEventHandler { [weak self] in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self?.scan() }
        }
        watch.setCancelHandler { close(fd) }
        watch.resume()
        source = watch
        log("слежу за снимками экрана в \(wanted.path)")
    }

    private func screenshotFolder() -> URL {
        let saved = UserDefaults(suiteName: "com.apple.screencapture")?.string(forKey: "location")
        let path = (saved.map { ($0 as NSString).expandingTildeInPath }) ?? (NSHomeDirectory() + "/Desktop")
        return URL(fileURLWithPath: path, isDirectory: true)
    }

    private func scan() {
        guard conf.screenshots, Date().timeIntervalSince1970 >= conf.pausedUntil, let folder = folder else { return }
        let keys: [URLResourceKey] = [.creationDateKey, .isRegularFileKey]
        let files = (try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: keys)) ?? []
        let recent = files.filter { url in
            guard !sent.contains(url.path), isScreenCapture(url),
                  let created = (try? url.resourceValues(forKeys: Set(keys)))?.creationDate else { return false }
            return Date().timeIntervalSince(created) < 30
        }
        for url in recent {
            sent.insert(url.path)
            log("снимок экрана: \(url.lastPathComponent)")
            sendItem(url, extra: [:], announce: false) { _ in }
        }
    }

    private func isScreenCapture(_ url: URL) -> Bool {
        getxattr(url.path, "com.apple.metadata:kMDItemIsScreenCapture", nil, 0, 0, 0) > 0
    }
}

func isClipboardScreenshot(_ types: [NSPasteboard.PasteboardType]) -> Bool {
    (pasteboard.pasteboardItems?.count ?? 0) == 1 && types.map(\.rawValue) == ["public.png"]
}
