import AppKit

struct ProjectChannel {
    let topic: String
    let name: String
}

struct AlertEntry {
    let id: String
    let topic: String
    let title: String
    let message: String
    let click: String?
    let time: Double
}

final class Alerts: NSObject, URLSessionDataDelegate {
    static let shared = Alerts()
    static let ownTopics: Set<String> = ["mac", "claude"]
    static let keep = 20

    private(set) var channels = [ProjectChannel]()
    private(set) var recent = [AlertEntry]()
    private var session: URLSession!
    private var task: URLSessionDataTask?
    private var buffer = Data()
    private var listening = [String]()
    private var seen = Set<String>()
    private var since = Date().timeIntervalSince1970 - 60
    private var retry = 2.0

    override init() {
        super.init()
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 120
        cfg.timeoutIntervalForResource = .infinity
        session = URLSession(configuration: cfg, delegate: self, delegateQueue: .main)
    }

    func start() {
        refresh()
        Timer.scheduledTimer(withTimeInterval: 600, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func name(of topic: String) -> String {
        channels.first { $0.topic == topic }?.name ?? topic
    }

    func isOn(_ topic: String) -> Bool {
        if let value = conf.alertTopics[topic] { return value }
        return !Alerts.ownTopics.contains(topic)
    }

    func refresh() {
        URLSession.shared.dataTask(with: request("v1/account")) { [weak self] data, response, _ in
            guard (response as? HTTPURLResponse)?.statusCode == 200, let data = data,
                  let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
            let server = conf.server
            let list = (json["subscriptions"] as? [[String: Any]] ?? []).compactMap { item -> ProjectChannel? in
                guard let topic = item["topic"] as? String, !topic.hasPrefix("tossy-"),
                      (item["base_url"] as? String ?? server).trimmingCharacters(in: CharacterSet(charactersIn: "/")) == server else { return nil }
                let name = (item["display_name"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? topic
                return ProjectChannel(topic: topic, name: name)
            }.sorted { $0.name.lowercased() < $1.name.lowercased() }
            DispatchQueue.main.async {
                self?.channels = list
                self?.reconnect()
            }
        }.resume()
    }

    func reconnect() {
        let wanted = channels.map(\.topic).filter(isOn)
        guard wanted != listening || task == nil else { return }
        task?.cancel()
        task = nil
        listening = wanted
        guard !wanted.isEmpty else { return }
        connect()
    }

    private func connect() {
        guard !listening.isEmpty else { return }
        buffer = Data()
        let path = listening.joined(separator: ",") + "/json?since=\(Int(since))"
        let next = session.dataTask(with: request(path))
        task = next
        next.resume()
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 200 {
            retry = 2
            completionHandler(.allow)
        } else {
            log("уведомления проектов: сервер ответил HTTP \(code)")
            retry = max(retry, 60)
            completionHandler(.cancel)
        }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard dataTask === task else { return }
        buffer.append(data)
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<nl]
            buffer.removeSubrange(buffer.startIndex...nl)
            if let event = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] { handle(event) }
        }
    }

    func urlSession(_ session: URLSession, task finished: URLSessionTask, didCompleteWithError error: Error?) {
        guard finished === task else { return }
        task = nil
        let delay = retry
        retry = min(retry * 2, 120)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self = self, self.task == nil else { return }
            self.connect()
        }
    }

    private func handle(_ event: [String: Any]) {
        guard event["event"] as? String == "message", let id = event["id"] as? String, !seen.contains(id),
              let topic = event["topic"] as? String, isOn(topic) else { return }
        seen.insert(id)
        let time = event["time"] as? Double ?? Date().timeIntervalSince1970
        since = max(since, time)
        let entry = AlertEntry(id: id, topic: topic, title: event["title"] as? String ?? "",
                               message: plain(event["message"] as? String ?? ""), click: event["click"] as? String, time: time)
        recent.insert(entry, at: 0)
        if recent.count > Alerts.keep { recent.removeLast(recent.count - Alerts.keep) }
        guard Date().timeIntervalSince1970 - time < 300 else { return }
        let project = name(of: topic)
        let title = entry.title.isEmpty ? project : "\(project): \(entry.title)"
        let priority = event["priority"] as? Int ?? 3
        notify(entry.message.isEmpty ? entry.title : entry.message, link: entry.click.flatMap(URL.init(string:)),
               title: title, id: "tossy-alert-\(id)", sound: priority >= 4)
    }

    private func plain(_ text: String) -> String {
        text.replacingOccurrences(of: #"\[([^\]]+)\]\([^)]+\)"#, with: "$1", options: .regularExpression)
            .replacingOccurrences(of: #"[*_`#>]+"#, with: "", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

func setAlertTopic(_ topic: String, _ on: Bool) {
    changeConfig { raw in
        var topics = raw["alert_topics"] as? [String: Bool] ?? [:]
        topics[topic] = on
        raw["alert_topics"] = topics
    }
    Alerts.shared.reconnect()
}
