import AppKit
import SwiftUI

final class PanelModel: ObservableObject {
    @Published var status: String
    @Published var isDone = false
    @Published var isFailed = false
    @Published var command: String?

    init(status: String) {
        self.status = status
    }

    func done(_ text: String) {
        status = text
        isDone = true
        isFailed = false
    }

    func failed(_ text: String) {
        status = text
        isFailed = true
    }
}

struct PanelView: View {
    let title: String
    let image: NSImage?
    let note: String
    @ObservedObject var model: PanelModel
    let close: () -> Void
    @State private var copied = false

    var body: some View {
        VStack(spacing: 18) {
            Text(title)
                .font(.title2.weight(.semibold))
            if let image = image {
                Image(nsImage: image)
                    .interpolation(.none)
                    .resizable()
                    .frame(width: 240, height: 240)
                    .padding(16)
                    .background(Color.white, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
            }
            if let command = model.command {
                HStack(spacing: 10) {
                    Text(command)
                        .font(.system(.body, design: .monospaced))
                        .textSelection(.enabled)
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                    Spacer(minLength: 0)
                    Button {
                        pasteboard.clearContents()
                        pasteboard.setString(command, forType: .string)
                        ownCount = pasteboard.changeCount
                        seenCount = ownCount
                        copied = true
                    } label: {
                        Image(systemName: copied ? "checkmark" : "doc.on.doc")
                    }
                    .help("Скопировать команду")
                    .modifier(GlassButton(prominent: false))
                }
                .padding(.leading, 14)
                .padding(.trailing, 6)
                .padding(.vertical, 6)
                .modifier(GlassCard())
            }
            Text(note)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            Label(model.status, systemImage: model.isDone ? "checkmark.circle.fill" : model.isFailed ? "exclamationmark.triangle.fill" : "hourglass")
                .font(.callout.weight(.medium))
                .foregroundStyle(model.isDone ? Color.green : model.isFailed ? Color.orange : Color.secondary)
                .animation(.default, value: model.isDone)
            Button(model.isDone ? "Готово" : "Закрыть", action: close)
                .keyboardShortcut(.defaultAction)
                .controlSize(.large)
                .modifier(GlassButton(prominent: model.isDone))
        }
        .padding(.horizontal, 28)
        .padding(.top, 34)
        .padding(.bottom, 24)
        .frame(width: 380)
    }
}

struct GlassButton: ViewModifier {
    let prominent: Bool

    func body(content: Content) -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26, *) {
            if prominent {
                content.buttonStyle(.glassProminent)
            } else {
                content.buttonStyle(.glass)
            }
        } else {
            content.buttonStyle(.bordered)
        }
        #else
        content.buttonStyle(.bordered)
        #endif
    }
}

struct GlassCard: ViewModifier {
    func body(content: Content) -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26, *) {
            content.glassEffect(.regular, in: .capsule)
        } else {
            content.background(.quaternary, in: Capsule())
        }
        #else
        content.background(.quaternary, in: Capsule())
        #endif
    }
}

struct WindowBackground: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .hudWindow
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }

    func updateNSView(_ view: NSVisualEffectView, context: Context) {}
}

func makePanelWindow(_ view: some View) -> NSPanel {
    let controller = NSHostingController(rootView: view.background(WindowBackground().ignoresSafeArea()))
    controller.sizingOptions = [.preferredContentSize]
    let panel = NSPanel(contentViewController: controller)
    panel.styleMask = [.titled, .closable, .fullSizeContentView]
    panel.titlebarAppearsTransparent = true
    panel.titleVisibility = .hidden
    panel.isMovableByWindowBackground = true
    panel.isReleasedWhenClosed = false
    return panel
}
