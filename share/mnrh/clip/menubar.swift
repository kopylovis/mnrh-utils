import AppKit
import ServiceManagement

final class ActionItem: NSMenuItem {
    private let handler: () -> Void

    init(_ title: String, symbol: String? = nil, _ handler: @escaping () -> Void) {
        self.handler = handler
        super.init(title: title, action: #selector(fire), keyEquivalent: "")
        target = self
        if let symbol = symbol { image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) }
    }

    required init(coder: NSCoder) {
        fatalError()
    }

    @objc private func fire() {
        handler()
    }
}

func ago(_ time: Double) -> String {
    let seconds = max(0, Date().timeIntervalSince1970 - time)
    switch seconds {
    case ..<60: return "только что"
    case ..<3600: return "\(Int(seconds / 60)) мин назад"
    case ..<86400: return "\(Int(seconds / 3600)) ч назад"
    default: return DateFormatter.localizedString(from: Date(timeIntervalSince1970: time), dateStyle: .short, timeStyle: .none)
    }
}

func clock(_ time: Double) -> String {
    DateFormatter.localizedString(from: Date(timeIntervalSince1970: time), dateStyle: .none, timeStyle: .short)
}

final class StatusMenu: NSObject, NSMenuDelegate, NSWindowDelegate {
    static let shared = StatusMenu()
    static let onlineWindow = 5.0 * 60
    static let shownHistory = 8

    private var item: NSStatusItem?
    private var panel: NSPanel?
    private var panelText: NSTextField?
    private var inviteProcess: Process?
    private var waitsForPhone = false

    func install() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = StatusMenu.icon()
        let menu = NSMenu()
        menu.delegate = self
        menu.autoenablesItems = false
        item.menu = menu
        self.item = item
        refresh()
    }

    func refresh() {
        guard let button = item?.button else { return }
        button.appearsDisabled = !connected || isPaused
        button.toolTip = "Tossy — \(statusLine())"
    }

    func deviceJoined(_ name: String, isMac: Bool) {
        guard panel != nil, waitsForPhone, !isMac else { return }
        waitsForPhone = false
        showDone("\(name) подключён. Можно закрыть окно.")
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        if let fresh = loadConfig() { conf = fresh }
        menu.removeAllItems()
        menu.addItem(info(statusLine(), symbol: connected ? (isPaused ? "pause.circle" : "checkmark.circle") : "wifi.exclamationmark"))
        menu.addItem(.separator())
        addHistory(to: menu)
        menu.addItem(.separator())
        let send = ActionItem("Отправить буфер сейчас", symbol: "paperplane") { sendNow() }
        if conf.hotkey {
            send.keyEquivalent = "c"
            send.keyEquivalentModifierMask = [.control, .option, .command]
        }
        menu.addItem(send)
        menu.addItem(ActionItem("Отправить файл…", symbol: "doc.badge.arrow.up") { [weak self] in self?.pickFiles() })
        menu.addItem(.separator())
        menu.addItem(submenu("Устройства", symbol: "laptopcomputer.and.iphone", devicesMenu()))
        menu.addItem(submenu(isPaused ? "Пауза до \(clock(conf.pausedUntil))" : "Пауза", symbol: "pause", pauseMenu()))
        menu.addItem(submenu("Настройки", symbol: "gearshape", settingsMenu()))
        menu.addItem(.separator())
        menu.addItem(ActionItem("Открыть Загрузки/Tossy", symbol: "folder") {
            let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads/Tossy", isDirectory: true)
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            NSWorkspace.shared.open(dir)
        })
        menu.addItem(ActionItem("Журнал", symbol: "list.bullet.rectangle") {
            NSWorkspace.shared.open(FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/mnrh-clip.log"))
        })
        menu.addItem(.separator())
        menu.addItem(ActionItem("Выключить Tossy на этом Mac…") { [weak self] in self?.turnOff() })
    }

    private var isPaused: Bool { conf.pausedUntil > Date().timeIntervalSince1970 }

    private func statusLine() -> String {
        let host = conf.server.components(separatedBy: "://").last ?? conf.server
        if !connected { return "Нет связи с \(host), переподключаюсь" }
        if isPaused { return "На паузе до \(clock(conf.pausedUntil)), принимаю" }
        if !conf.auto { return "На связи · отправляю только по ⌃⌥⌘C" }
        return "На связи · \(host)"
    }

    private func info(_ title: String, symbol: String? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        if let symbol = symbol { item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) }
        return item
    }

    private func submenu(_ title: String, symbol: String, _ menu: NSMenu) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        menu.autoenablesItems = false
        item.submenu = menu
        return item
    }

    private func styled(_ title: String, _ detail: String) -> NSAttributedString {
        let text = NSMutableAttributedString(string: title, attributes: [.font: NSFont.menuFont(ofSize: 0)])
        text.append(NSAttributedString(string: "   " + detail, attributes: [
            .font: NSFont.menuFont(ofSize: NSFont.smallSystemFontSize),
            .foregroundColor: NSColor.secondaryLabelColor,
        ]))
        return text
    }

    private func addHistory(to menu: NSMenu) {
        let items = History.shared.items
        guard !items.isEmpty else {
            menu.addItem(info("Здесь будет недавнее: скопируй что-нибудь"))
            return
        }
        menu.addItem(info("Недавнее"))
        items.prefix(StatusMenu.shownHistory).forEach { addEntry($0, to: menu) }
        if items.count > StatusMenu.shownHistory {
            let more = NSMenu()
            items.dropFirst(StatusMenu.shownHistory).forEach { addEntry($0, to: more) }
            menu.addItem(submenu("Ещё \(items.count - StatusMenu.shownHistory)", symbol: "clock.arrow.circlepath", more))
        }
        menu.addItem(ActionItem("Очистить историю") { History.shared.clear() })
    }

    private func addEntry(_ entry: HistoryItem, to menu: NSMenu) {
        let direction = entry.incoming ? "← \(entry.device.isEmpty ? "?" : entry.device)" : (entry.device.isEmpty ? "→ всем" : "→ \(entry.device)")
        let detail = "\(direction) · \(clock(entry.time))"
        let exists = History.shared.exists(entry)
        switch entry.kind {
        case "text":
            let line = (entry.text ?? "").split(whereSeparator: \.isNewline).first.map(String.init) ?? ""
            let short = line.count > 42 ? String(line.prefix(42)) + "…" : line
            let item = ActionItem(short.isEmpty ? "(пустой текст)" : short, symbol: "text.alignleft") { copyAgain(entry) }
            item.attributedTitle = styled(item.title, detail)
            item.toolTip = String((entry.text ?? "").prefix(600))
            menu.addItem(item)
        case "image":
            let item = ActionItem("Картинка, \(sizeText(entry.size))", symbol: "photo") { copyAgain(entry) }
            item.attributedTitle = styled(item.title, detail)
            if let path = entry.file, let image = NSImage(contentsOfFile: path) {
                let side = 22.0
                let scale = side / max(image.size.width, image.size.height, 1)
                image.size = NSSize(width: image.size.width * scale, height: image.size.height * scale)
                item.image = image
            }
            item.isEnabled = exists
            menu.addItem(item)
        default:
            let name = entry.name ?? "файл"
            let url = URL(fileURLWithPath: entry.file ?? "")
            let reveal = ActionItem(name, symbol: "doc") { NSWorkspace.shared.activateFileViewerSelecting([url]) }
            reveal.attributedTitle = styled(name, "\(sizeText(entry.size)) · \(detail)")
            reveal.toolTip = "Показать в Finder, с ⌥ — открыть"
            reveal.isEnabled = exists
            menu.addItem(reveal)
            let open = ActionItem(name, symbol: "arrow.up.forward.app") { NSWorkspace.shared.open(url) }
            open.attributedTitle = styled(name, "открыть")
            open.isAlternate = true
            open.keyEquivalentModifierMask = .option
            open.isEnabled = exists
            menu.addItem(open)
        }
    }

    private func devicesMenu() -> NSMenu {
        let menu = NSMenu()
        let now = Date().timeIntervalSince1970
        let others = members.filter { $0.key != conf.deviceID }.sorted {
            ($0.value["seen"] as? Double ?? 0) > ($1.value["seen"] as? Double ?? 0)
        }
        if others.isEmpty { menu.addItem(info("В комнате пока только этот Mac")) }
        for (id, value) in others {
            let name = conf.aliases[id] ?? value["name"] as? String ?? "?"
            let seen = value["seen"] as? Double ?? 0
            let isMac = value["src"] as? String == "mac"
            let state = now - seen < StatusMenu.onlineWindow ? "на связи" : "был \(ago(seen))"
            let item = submenu(name, symbol: isMac ? "laptopcomputer" : "iphone", deviceMenu(id: id, name: name))
            let role = id == conf.owner ? " · создатель комнаты" : ""
            item.attributedTitle = styled(name, (memberKey(value) == nil ? "\(state) · старая версия" : state) + role)
            menu.addItem(item)
        }
        menu.addItem(.separator())
        let me = NSMenu()
        me.addItem(ActionItem("Переименовать…") { [weak self] in
            self?.ask("Как назвать этот Mac?", value: conf.deviceName) { renameSelf($0) }
        })
        let self_ = submenu(conf.deviceName, symbol: "desktopcomputer", me)
        self_.attributedTitle = styled(conf.deviceName, conf.isOwner ? "этот Mac · создатель комнаты" : "этот Mac")
        menu.addItem(self_)
        menu.addItem(.separator())
        menu.addItem(ActionItem("Подключить телефон…", symbol: "qrcode") { [weak self] in self?.showQR() })
        menu.addItem(ActionItem("Пригласить ещё один Mac…", symbol: "person.badge.plus") { [weak self] in self?.showInvite() })
        return menu
    }

    private func deviceMenu(id: String, name: String) -> NSMenu {
        let menu = NSMenu()
        menu.addItem(ActionItem("Переименовать…") { [weak self] in
            self?.ask("Как подписать «\(name)» на этом Mac?", value: name) { setAlias(id, $0) }
        })
        guard id != conf.owner else {
            menu.addItem(.separator())
            menu.addItem(info("Создатель комнаты: отключить нельзя"))
            return menu
        }
        menu.addItem(.separator())
        menu.addItem(ActionItem("Отключить от комнаты…") { [weak self] in
            self?.confirm("Отключить \(name)?",
                          "Комната получит новый ключ, а при поддержке сервера и новый токен. \(name) больше не увидит буфер, пока его не подключат заново.",
                          button: "Отключить") { revokeDevice(id) }
        })
        return menu
    }

    private func pauseMenu() -> NSMenu {
        let menu = NSMenu()
        if isPaused {
            menu.addItem(ActionItem("Снова отправлять", symbol: "play") { pause(until: 0) })
            menu.addItem(.separator())
        }
        let now = Date().timeIntervalSince1970
        menu.addItem(ActionItem("30 минут") { pause(until: now + 30 * 60) })
        menu.addItem(ActionItem("1 час") { pause(until: now + 3600) })
        menu.addItem(ActionItem("3 часа") { pause(until: now + 3 * 3600) })
        let morning = Calendar.current.nextDate(after: Date(), matching: DateComponents(hour: 8), matchingPolicy: .nextTime)
        if let morning = morning {
            menu.addItem(ActionItem("До утра, \(clock(morning.timeIntervalSince1970))") { pause(until: morning.timeIntervalSince1970) })
        }
        menu.addItem(.separator())
        menu.addItem(info("На паузе этот Mac не отправляет, но принимает"))
        return menu
    }

    private func settingsMenu() -> NSMenu {
        let menu = NSMenu()
        let flags: [(String, String, Bool)] = [
            ("Отправлять сам после ⌘C", "auto", conf.auto),
            ("Передавать картинки", "images", conf.images),
            ("⌃⌥⌘C — отправить вручную", "hotkey", conf.hotkey),
            ("Уведомления", "notifications", conf.notifications),
        ]
        for (title, key, value) in flags {
            let item = ActionItem(title) { setFlag(key, !value) }
            item.state = value ? .on : .off
            menu.addItem(item)
        }
        return menu
    }

    private func pickFiles() {
        NSApp.activate(ignoringOtherApps: true)
        let open = NSOpenPanel()
        open.allowsMultipleSelection = true
        open.canChooseDirectories = true
        open.message = "Файлы уйдут на все устройства комнаты, папки — zip-архивом"
        open.prompt = "Отправить"
        guard open.runModal() == .OK, !open.urls.isEmpty else { return }
        sendQueue(open.urls.map { url in { next in sendItem(url, extra: [:], announce: true, done: { _ in next() }) } })
    }

    private func turnOff() {
        confirm("Выключить Tossy на этом Mac?", "Буфер перестанет синхронизироваться, пока не выполнишь mnrh tossy on.", button: "Выключить") {
            log("выключен из строки меню")
            let plist = (Bundle.main.bundleIdentifier ?? "com.mnrh.clip") + ".plist"
            try? SMAppService.agent(plistName: plist).unregister()
            exit(0)
        }
    }

    private func ask(_ title: String, value: String, _ done: @escaping (String) -> Void) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = title
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        field.stringValue = value
        alert.accessoryView = field
        alert.addButton(withTitle: "Сохранить")
        alert.addButton(withTitle: "Отмена")
        alert.window.initialFirstResponder = field
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        done(field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    private func confirm(_ title: String, _ text: String, button: String, _ done: @escaping () -> Void) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.alertStyle = .warning
        alert.addButton(withTitle: button)
        alert.addButton(withTitle: "Отмена")
        if alert.runModal() == .alertFirstButtonReturn { done() }
    }

    private func showPanel(title: String, image: NSImage?, text: String, copy: String? = nil) {
        closePanel()
        let width = 380.0
        let imageSide = image == nil ? 0.0 : 300.0
        let height = imageSide + 150
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                            styleMask: [.titled, .closable], backing: .buffered, defer: false)
        panel.title = title
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        let content = NSView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        if let image = image {
            let view = NSImageView(frame: NSRect(x: (width - imageSide) / 2, y: height - imageSide - 16, width: imageSide, height: imageSide))
            view.image = image
            view.imageScaling = .scaleProportionallyUpOrDown
            content.addSubview(view)
        }
        let label = NSTextField(wrappingLabelWithString: text)
        label.frame = NSRect(x: 20, y: copy == nil ? 20 : 56, width: width - 40, height: copy == nil ? 110 : 76)
        label.isSelectable = true
        content.addSubview(label)
        if let copy = copy {
            let button = NSButton(title: "Скопировать команду", target: nil, action: nil)
            button.frame = NSRect(x: 20, y: 16, width: 180, height: 30)
            button.bezelStyle = .rounded
            let action = ActionItem("") {
                pasteboard.clearContents()
                pasteboard.setString(copy, forType: .string)
            }
            button.target = action
            button.action = action.action
            objc_setAssociatedObject(button, "action", action, .OBJC_ASSOCIATION_RETAIN)
            content.addSubview(button)
        }
        panel.contentView = content
        panel.center()
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        self.panel = panel
        panelText = label
    }

    private func showDone(_ text: String) {
        panelText?.stringValue = "✓ " + text
    }

    private func closePanel() {
        inviteProcess?.terminate()
        inviteProcess = nil
        waitsForPhone = false
        panel?.delegate = nil
        panel?.close()
        panel = nil
    }

    func windowWillClose(_ notification: Notification) {
        inviteProcess?.terminate()
        inviteProcess = nil
        waitsForPhone = false
        panel = nil
    }

    private func showQR() {
        guard let png = pairingQR(), let image = NSImage(data: png) else {
            notify("Не собрал QR-код")
            return
        }
        showPanel(title: "Подключить телефон", image: image,
                  text: "В Tossy на телефоне: «Добавить Mac» → наведи камеру на код. В коде ключ комнаты и токен — не показывай его посторонним.")
        waitsForPhone = true
    }

    private func showInvite() {
        let alphabet = Array("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
        let raw = String((0..<8).map { _ in alphabet[Int.random(in: 0..<alphabet.count)] })
        let code = "\(raw.prefix(4))-\(raw.suffix(4))"
        let host = conf.server.components(separatedBy: "://").last ?? conf.server
        let command = "mnrh tossy join \(host)/\(code)"
        showPanel(title: "Пригласить Mac", image: nil, text: "Готовлю приглашение…", copy: command)
        var lines = 0
        inviteProcess = helperCommand(["--invite", code], output: { [weak self] line in
            lines += 1
            if lines == 1 {
                guard line == "ok" else {
                    self?.panelText?.stringValue = "Не отправил приглашение: \(line)"
                    return
                }
                self?.panelText?.stringValue = "На втором Mac выполни:\n\n\(command)\n\nЖду до 10 минут. Код никому не показывай."
            } else if line == "timeout" {
                self?.panelText?.stringValue = "За 10 минут никто не подключился. Закрой окно и пригласи заново."
            } else {
                self?.showDone("\(line) в комнате.")
            }
        })
    }

    static func icon() -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: true) { _ in
            NSColor.black.setFill()
            let dot = 1.3
            for (x, y) in [(5.0, 18.0), (6.47, 14.31), (8.6, 10.89), (11.8, 8.63), (15.72, 8.89)] {
                NSBezierPath(ovalIn: NSRect(x: x - 3 - dot, y: y - 3 - dot, width: 2 * dot, height: 2 * dot)).fill()
            }
            NSBezierPath(ovalIn: NSRect(x: 17.5 - 3 - 3.2, y: 7.5 - 3 - 3.2, width: 6.4, height: 6.4)).fill()
            return true
        }
        image.isTemplate = true
        return image
    }
}
