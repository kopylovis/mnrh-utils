import os
import re
import signal
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import OK, WARN, has_flag, paint, run

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or (args and args[0] not in ("fix", "-a", "--all")):
    print("mnrh sleep        что сейчас не даёт Mac уснуть и когда он засыпал и просыпался")
    print("mnrh sleep -a     показать и служебные блокировки macOS (дисплей включён, активность ввода)")
    print("mnrh sleep fix    снять зависшие caffeinate: те, чья программа уже закрыта")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)
show_all = has_flag(args, "-a", "--all")

# Что именно блокирует каждый тип.
KINDS = {
    "PreventSystemSleep": "не даст уснуть совсем, даже с закрытой крышкой от сети",
    "PreventUserIdleSystemSleep": "не даёт уснуть от простоя",
    "NoIdleSleepAssertion": "не даёт уснуть от простоя",
    "PreventUserIdleDisplaySleep": "не даёт погаснуть экрану",
    "NoDisplaySleepAssertion": "не даёт погаснуть экрану",
    "BackgroundTask": "короткая фоновая задача",
}
# Служебное: есть почти всегда, спать не мешает сверх обычного.
ROUTINE = ("UserIsActive", "Powerd - Prevent sleep while display is on", "BackgroundTask", "ApplePushServiceTask")


def ps(pid):
    out = run(["ps", "-o", "ppid=,etime=,command=", "-p", str(pid)]).strip()
    if not out:
        return None
    ppid, etime, command = out.split(None, 2)
    return {"ppid": int(ppid), "etime": etime, "command": command}


def short(command):
    first = command.split(" ")[0]
    app = re.search(r"/([^/]+)\.app/", command)
    if app:
        return app.group(1)
    if ".simruntime/" in command:
        return f"{first.rsplit('/', 1)[-1]} (в симуляторе)"
    return first.rsplit("/", 1)[-1]


def assertions():
    items, cur = [], None
    for line in run(["pmset", "-g", "assertions"]).splitlines():
        m = re.match(r"\s+pid (\d+)\(([^)]*)\): \[\S+\] (\S+) (\S+) named: \"(.*)\"", line)
        if m:
            cur = {"pid": int(m.group(1)), "proc": m.group(2), "age": m.group(3), "type": m.group(4),
                   "name": m.group(5), "details": "", "for_pid": None, "timeout": None}
            items.append(cur)
            continue
        if cur is None or not line.startswith("\t"):
            continue
        text = line.strip()
        if text.startswith("Details:"):
            cur["details"] = text[len("Details:"):].strip()
        elif text.startswith("Created for PID:"):
            cur["for_pid"] = int(re.sub(r"\D", "", text))
        elif text.startswith("Timeout will fire in"):
            cur["timeout"] = int(re.sub(r"\D", "", text.split("secs")[0]))
    return items


def explain(a):
    """(кто, пояснение, зависший ли)"""
    if a["proc"] == "caffeinate":
        info = ps(a["pid"]) or {}
        behalf = re.search(r"on behalf of '([^']*)' \(pid (\d+)\)", a["details"])
        if behalf:
            owner = ps(int(behalf.group(2)))
            if not owner:
                return "caffeinate", f"держал сон для «{behalf.group(1)}», а тот уже закрыт", True
            what = "mnrh claude -k" if behalf.group(1) == "claude" else behalf.group(1)
            return "caffeinate", f"{what}: пока открыт {short(owner['command'])} (pid {behalf.group(2)})", False
        if "-t" in info.get("command", ""):
            parent = ps(info["ppid"]) if info else None
            who = short(parent["command"]) if parent else "?"
            left = f", ещё {a['timeout'] // 60} мин" if a["timeout"] else ""
            return "caffeinate", f"на время от {who}{left}", False
        parent = ps(info["ppid"]) if info else None
        if not parent or info.get("ppid") == 1:
            return "caffeinate", f"висит сам по себе с {a['age']}, без срока и владельца", True
        return "caffeinate", f"запущен из {short(parent['command'])}", False
    if a["proc"] == "coreaudiod":
        src = ps(a["for_pid"]) if a["for_pid"] else None
        who = short(src["command"]) if src else "какое-то приложение"
        hint = "   -> mnrh sim stop" if src and "CoreSimulator" in src["command"] + a["name"] else ""
        if "Simulator" in a["name"] and not hint:
            hint = "   -> mnrh sim stop"
        return "звук", f"играет звук: {who}" + paint(hint, "2"), False
    if a["proc"] == "xcodebuild" or "Xcode" in a["name"]:
        return a["proc"], "идёт сборка Xcode", False
    if a["name"] == "Electron":
        return a["proc"], "приложение что-то проигрывает, звонит или качает", False
    return a["proc"], a["name"], False


def recent_events(limit=6):
    lines = [l for l in run(["pmset", "-g", "log"], timeout=60).splitlines()
             if re.match(r"\S+ \S+ \S+ (Sleep|Wake|DarkWake)\s", l)]
    out = []
    for l in lines[-limit:]:
        date, time_, _, kind, rest = l.split(None, 4)
        rest = re.sub(r"\s+", " ", rest).strip()
        reason = re.search(r"due to (.*?)(?: Using| \(|$)", rest)
        why = reason.group(1).strip(" :") if reason else rest[:80]
        label = {"Sleep": "уснул", "Wake": "проснулся", "DarkWake": "тёмное пробуждение"}[kind]
        if kind == "Sleep" and "DarkWake" in rest:
            label = "уснул (с тёмными пробуждениями)"
        out.append(f"  {date[5:]} {time_[:5]}  {label}: {why}")
    return out


def status():
    items = [a for a in assertions() if show_all or not any(r in (a["type"], a["name"]) for r in ROUTINE)]
    blocking = [a for a in items if a["type"] != "BackgroundTask"]
    stale = []
    if not blocking:
        print(f"{OK} Уснуть ничто не мешает.")
    else:
        full = any(a["type"] == "PreventSystemSleep" for a in blocking)
        print(paint("Сейчас не дают уснуть:", "1")
              + (" (Mac не уснёт и с закрытой крышкой, если он от сети)" if full else ""))
        # У одного процесса бывает несколько блокировок (caffeinate -is): показываем самую сильную.
        strength = list(KINDS)
        by_pid = {}
        for a in blocking:
            best = by_pid.get(a["pid"])
            rank = strength.index(a["type"]) if a["type"] in strength else len(strength)
            if not best or rank < best[0]:
                by_pid[a["pid"]] = (rank, a)
        for _, a in sorted(by_pid.values(), key=lambda x: x[0]):
            who, what, is_stale = explain(a)
            mark = WARN if is_stale else " "
            print(f" {mark} {who:<14} {what}  " + paint(f"[{KINDS.get(a['type'], a['type'])}, {a['age']}]", "2"))
            if is_stale:
                stale.append(a["pid"])
    if stale:
        print(f"\nЗависших caffeinate: {len(stale)}." + paint("   -> mnrh sleep fix", "2"))
    events = recent_events()
    if events:
        print(f"\n{paint('Последние засыпания и пробуждения:', '1')}")
        print("\n".join(events))
    settings = run(["pmset", "-g"])
    m = re.search(r"^\s*sleep\s+(\d+)", settings, re.M)
    d = re.search(r"^\s*displaysleep\s+(\d+)", settings, re.M)
    if m and d:
        sleep_min, display_min = int(m.group(1)), int(d.group(1))
        when = "никогда" if sleep_min == 0 else f"через {max(sleep_min, display_min)} мин"
        print(f"\nБез блокировок экран гаснет через {display_min} мин простоя, Mac засыпает {when}.")


def fix():
    stale = [a["pid"] for a in assertions() if a["proc"] == "caffeinate" and explain(a)[2]]
    stale = sorted(set(stale))
    if not stale:
        print("Зависших caffeinate нет. Остальные блокировки — у живых программ: mnrh sleep")
        return
    for pid in stale:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    print(f"Сняты зависшие caffeinate: {', '.join(map(str, stale))}")


fix() if args and args[0] == "fix" else status()
