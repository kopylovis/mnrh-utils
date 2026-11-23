import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import (HOME, adb_path, bundle_info, confirm, gradle_versions_in_use, has_flag, parse_env, projects,
                     tilde)

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or (args and args[0].isalpha() and args[0] not in ("clean", "status", "watch", "tune")):
    print("mnrh ram                 меню с действиями (без терминала — то же, что status)")
    print("mnrh ram status          кто ест оперативную память, сгруппировано по приложениям")
    print("mnrh ram -p              то же по отдельным процессам")
    print("mnrh ram -n 30           показать больше строк (по умолчанию 15)")
    print("mnrh ram watch           следить, кто растет в памяти с начала наблюдения; -i 2 обновлять раз в 2 с")
    print("                         (по умолчанию 5), Ctrl+C выход и итог")
    print("mnrh ram tune            сколько памяти разрешено демонам Gradle и Kotlin в проектах и как долго они живут")
    print("mnrh ram tune --apply    записать в ~/.gradle/gradle.properties выход демона через 30 минут простоя")
    print("mnrh ram clean           отметить галочками, что остановить: демоны Gradle и Kotlin, каждый iOS-симулятор")
    print("                         и эмулятор Android отдельной строкой; потом показать, сколько вернулось")
    print("mnrh ram clean -y        без меню: демоны и симуляторы (без терминала -y обязателен)")
    print("                         Демоны Gradle версий, которых нет в проектах, отмечены всегда")
    print("                         Сироты: node, MCP-серверы, vite, LSP и скрипты, чья программа уже закрыта")
    print("                         Сессии Claude Code и Codex, которые давно молчат, показаны без отметки")
    print("  --daemons, --gradle, --kotlin, --sim, --orphans  только это; --emulator ещё эмулятор Android,")
    print("  --sessions ещё простаивающие сессии Claude Code и Codex")
    print("  -f                     снять и занятые сборкой демоны")
    print("  --apps                 ещё тяжелые фоновые приложения (от 1 ГБ): им уходит обычная команда «Закрыть»,")
    print("                         несохраненное приложение спросит само")
    sys.exit(0)
limit = 15
interval = 5.0
if "-i" in args:
    try:
        interval = max(1.0, float(args[args.index("-i") + 1]))
    except (IndexError, ValueError):
        sys.exit("mnrh ram: после -i нужно число секунд")
if "-n" in args:
    try:
        limit = int(args[args.index("-n") + 1])
    except (IndexError, ValueError):
        sys.exit("mnrh ram: после -n нужно число")

CLEAN_HINT = "mnrh ram clean"
NO_PROCESSES = "Список процессов прочитать не удалось: ps и top ничего не вернули (песочница или нет прав)."
UNITS = {"B": 1 / 1048576, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1048576}
BUSY_CPU = 5.0
DEFAULT_KEYS = {"gradle", "gradle-stale", "kotlin", "sim", "orphan"}
ADDED_ONLY = {"emulator", "session", "app"}  # эти флаги добавляют к отмеченному по умолчанию, а не заменяют его


def run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=parse_env()).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
        return ""


def to_mb(value):
    m = re.match(r"([\d.]+)([BKMGT]?)", value.strip())
    if not m:
        return 0.0
    return float(m.group(1)) * UNITS.get(m.group(2) or "B", 0)


def fmt(mb):
    return f"{mb / 1024:5.2f} ГБ" if mb >= 1024 else f"{mb:5.0f} МБ"


def classify(cmd):
    """(подпись, вид): вид daemon, sim, emulator, app или None."""
    if "GradleDaemon" in cmd:
        return "Gradle-демоны", "daemon"
    if "KotlinCompileDaemon" in cmd or "kotlin-daemon" in cmd:
        return "Kotlin-демоны", "daemon"
    if "GradleWorkerMain" in cmd:
        return "Gradle worker", "daemon"
    if "GradleWrapperMain" in cmd:
        return "Gradle wrapper", "daemon"
    if "/CoreSimulator/" in cmd or "CoreSimulator.framework" in cmd:
        return "iOS-симулятор", "sim"
    first = cmd.split(" ")[0]
    base = first.rsplit("/", 1)[-1]
    if base.startswith("qemu-system-") or "/emulator/crashpad_handler" in first:
        return "Android-эмулятор", "emulator"
    app = re.search(r"/([^/]+)\.app/", cmd)
    if app:
        return app.group(1), "app"
    if base == "claude" or "/claude/versions/" in first:
        return "Claude Code (CLI)", None
    if "@openai/codex" in cmd or re.match(r"codex(-[\w-]+)?$", base):
        return "Codex (CLI)", None
    if base == "java":
        return "Java (прочее)", None
    return base or "неизвестно", None


def collect():
    commands, cpu = {}, {}
    info = {}
    for line in run(["ps", "-axo", "pid=,ppid=,uid=,pcpu=,etime=,command="]).splitlines():
        parts = line.strip().split(None, 5)
        if len(parts) == 6:
            commands[parts[0]] = parts[5]
            cpu[parts[0]] = float(parts[3])
            info[parts[0]] = {"ppid": parts[1], "uid": int(parts[2]), "age": seconds(parts[4])}
    rows, started = [], False
    for line in run(["top", "-l", "1", "-stats", "pid,mem,cmprs"]).splitlines():
        if line.startswith("PID"):
            started = True
            continue
        cols = line.split()
        if not started or len(cols) < 3:
            continue
        pid = cols[0]
        label, kind = classify(commands.get(pid, ""))
        extra = info.get(pid, {"ppid": "", "uid": -1, "age": 0})
        rows.append({"pid": pid, "mem": to_mb(cols[1]), "cmprs": to_mb(cols[2]), "label": label,
                     "kind": kind, "cpu": cpu.get(pid, 0.0), "cmd": commands.get(pid, ""), **extra})
    return rows


def seconds(etime):
    """Время работы из ps etime ([[дд-]чч:]мм:сс) в секундах."""
    days, _, rest = etime.rpartition("-")
    total = 0
    for part in rest.split(":"):
        total = total * 60 + int(part or 0)
    return total + int(days or 0) * 86400 if rest.replace(":", "").isdigit() else 0


def descendants(rows, roots):
    """{pid: корень} для корней и всех их потомков по ppid."""
    owner = dict(roots)
    children = defaultdict(list)
    for r in rows:
        children[r["ppid"]].append(r["pid"])
    todo = list(owner)
    while todo:
        pid = todo.pop()
        for child in children.get(pid, []):
            if child not in owner:
                owner[child] = owner[pid]
                todo.append(child)
    return owner


DEVICE_DIR = re.compile(r"/CoreSimulator/Devices/([0-9A-F-]{36})/")


def owned_mb(rows, roots):
    """{хозяин: МБ}: корневые процессы {pid: хозяин} вместе со всеми потомками."""
    owner = descendants(rows, roots)
    mb = defaultdict(float)
    for r in rows:
        if r["pid"] in owner:
            mb[owner[r["pid"]]] += r["mem"]
    return mb


def simulator_mb(rows):
    """{udid: МБ}: процессы, в чьей команде папка устройства, и их потомки (их запускает launchd_sim)."""
    return owned_mb(rows, {r["pid"]: m.group(1) for r in rows for m in [DEVICE_DIR.search(r["cmd"])] if m})


def emulator_mb(rows):
    """{имя AVD: МБ} по процессам qemu с -avd и их потомкам."""
    return owned_mb(rows, {r["pid"]: m.group(1) for r in rows if r["kind"] == "emulator"
                           for m in [re.search(r"(?:^|\s)-avd\s+(\S+)", r["cmd"])] if m})


PRESSURE = {"1": "норма", "2": "повышенное", "4": "критическое"}


def memory_state():
    vm = run(["vm_stat"])
    page = int(re.search(r"page size of (\d+)", vm).group(1))

    def pages(name):
        m = re.search(r"^" + re.escape(name) + r":\s+(\d+)", vm, re.M)
        return int(m.group(1)) if m else 0

    gb = 1073741824
    total = int(run(["sysctl", "-n", "hw.memsize"])) / gb
    wired = pages("Pages wired down") * page / gb
    compressed = pages("Pages occupied by compressor") * page / gb
    app = (pages("Anonymous pages") - pages("Pages purgeable")) * page / gb
    swap = re.search(r"used = ([\d.]+)M", run(["sysctl", "-n", "vm.swapusage"]))
    stored = pages("Pages stored in compressor")
    occupied = pages("Pages occupied by compressor")
    level = run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip()
    return {"total": total, "wired": wired, "compressed": compressed, "app": app,
            "pressure": PRESSURE.get(level, "неизвестно"),
            "used": app + wired + compressed, "swap": float(swap.group(1)) / 1024 if swap else 0,
            "ratio": stored / occupied if occupied else 0}


def mark(kind, sims_booted):
    if kind in ("daemon", "emulator") or (kind == "sim" and sims_booted):
        return f"   <- {CLEAN_HINT}"
    return ""


def grouped(rows):
    """{подпись: [память, сжато, процессов, вид]} по приложениям."""
    groups = defaultdict(lambda: [0.0, 0.0, 0, None])
    for r in rows:
        g = groups[r["label"]]
        g[0] += r["mem"]
        g[1] += r["cmprs"]
        g[2] += 1
        g[3] = g[3] or r["kind"]
    return groups


def header(s):
    return (f"Давление памяти: {s['pressure']}. Занято {s['used']:.1f} из {s['total']:.0f} ГБ · приложения {s['app']:.1f}"
            f" · wired {s['wired']:.1f} · сжато {s['compressed']:.1f} · свободно {max(s['total'] - s['used'], 0):.1f}"
            f" · swap {s['swap']:.1f}")


def growth(start, now):
    """[(подпись, память, прирост, новое ли)] по убыванию прироста."""
    out = [(label, mb, mb - start.get(label, 0.0), label not in start) for label, mb in now.items()]
    return sorted(out, key=lambda x: -x[2])


def signed(mb):
    return ("+" if mb >= 0 else "-") + fmt(abs(mb)).strip()


def watch():
    rows = collect()
    if not rows:
        sys.exit(NO_PROCESSES)
    start = {label: g[0] for label, g in grouped(rows).items()}
    first = memory_state()
    if not sys.stdout.isatty():
        print("mnrh ram watch следит в терминале. Без него один снимок:\n")
        show()
        return
    began = time.time()
    now = start
    try:
        while True:
            time.sleep(interval)
            rows = collect()
            now = {label: g[0] for label, g in grouped(rows).items()}
            s = memory_state()
            spent = int(time.time() - began)
            lines = [header(s), f"Слежу {spent // 60} мин {spent % 60:02d} с, обновление раз в {interval:g} с, "
                                f"занято {signed((s['used'] - first['used']) * 1024)} с начала. Ctrl+C выход", "",
                     f"{'ПАМЯТЬ':>9}  {'РОСТ':>10}  ПРИЛОЖЕНИЕ"]
            for label, mem, delta, new in growth(start, now)[:limit]:
                lines.append(f"{fmt(mem):>9}  {signed(delta):>10}  {label}" + ("  (новое)" if new else ""))
            sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(lines) + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        top = [x for x in growth(start, now) if x[2] >= 50][:5]
        print("\nЗа время наблюдения выросли больше всех: " + (", ".join(f"{label} {signed(d)}" for label, _, d, _ in top)
                                                            if top else "никто заметно не вырос"))


def show():
    rows = collect()
    s = memory_state()
    sims_booted = bool(booted_simulators())
    print(header(s))
    if s["pressure"] in ("повышенное", "критическое"):
        print("macOS уже не хватает памяти, она сжимает страницы и уводит их в swap. Освободить: " + CLEAN_HINT)
    print()
    if not rows:
        print(NO_PROCESSES)
        print("Общие цифры выше верные, разбивки по приложениям нет.")
        return

    if "-p" in args:
        rows.sort(key=lambda r: r["mem"], reverse=True)
        print(f"{'ПАМЯТЬ':>9}  {'СЖАТО':>9}  {'PID':>6}  ПРОЦЕСС")
        for r in rows[:limit]:
            print(f"{fmt(r['mem']):>9}  {fmt(r['cmprs']):>9}  {r['pid']:>6}  {r['label']}{mark(r['kind'], sims_booted)}")
    else:
        ordered = sorted(grouped(rows).items(), key=lambda kv: kv[1][0], reverse=True)
        print(f"{'ПАМЯТЬ':>9}  {'СЖАТО':>9}  {'ПРОЦ':>4}  ПРИЛОЖЕНИЕ")
        for label, (mem, cmprs, count, kind) in ordered[:limit]:
            print(f"{fmt(mem):>9}  {fmt(cmprs):>9}  {count:>4}  {label}{mark(kind, sims_booted)}")

    print()
    print("ПАМЯТЬ — полный объём процесса, сжатая часть в нём учтена в несжатом размере.")
    if s["ratio"]:
        print(f"Сейчас macOS сжимает в {s['ratio']:.1f} раза, поэтому сумма колонки больше, чем «Занято».")
    print("В Stats всё сжатое — одна полоса Compressed без разбивки по процессам.")
    daemon_mb = sum(r["mem"] for r in rows if r["kind"] == "daemon")
    sim_mb = sum(r["mem"] for r in rows if r["kind"] == "sim") if sims_booted else 0
    held = []
    if daemon_mb >= 512:
        held.append(f"демоны сборки {fmt(daemon_mb).strip()}")
    if sim_mb >= 512:
        held.append(f"iOS-симуляторы {fmt(sim_mb).strip()}")
    orphan_mb = sum(r["mem"] for r, _, dev in orphans(rows) if dev)
    if orphan_mb >= 256:
        held.append(f"процессы-сироты {fmt(orphan_mb).strip()}")
    if held:
        print(f"\nМожно вернуть: {', '.join(held)}. Освободить: {CLEAN_HINT}")


def booted_simulators():
    """[(udid, имя, «iOS 27.0»)] запущенных симуляторов."""
    data = json.loads(run(["xcrun", "simctl", "list", "devices", "booted", "-j"]) or "{}")
    return [(d["udid"], d["name"], rt.split(".")[-1].replace("-", " ", 1).replace("-", "."))
            for rt, lst in data.get("devices", {}).items() for d in lst if d.get("state") == "Booted"]


def running_emulators(adb):
    if not adb:
        return []
    return [line.split()[0] for line in run([adb, "devices"]).splitlines()
            if line.startswith("emulator-") and line.split()[-1] == "device"]


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def stop_daemons(pids):
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    for _ in range(10):
        if not any(alive(p) for p in pids):
            return
        time.sleep(1)
    for pid in [p for p in pids if alive(p)]:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def avd_name(adb, serial):
    lines = run([adb, "-s", serial, "emu", "avd", "name"], timeout=10).splitlines()
    return lines[0].strip() if lines and lines[0].strip() else serial


SYSTEM_PREFIXES = ("/System/", "/usr/libexec/", "/usr/sbin/", "/usr/bin/", "/sbin/", "/bin/", "/Library/Apple/",
                   "/Library/Developer/", "/Applications/Xcode")
DEV_TOOLS = (("mcp", "MCP-серверы"), ("vite", "vite"), ("metro", "metro"), ("webpack", "webpack"),
             ("next-server", "next dev"), ("next dev", "next dev"), ("esbuild", "esbuild"), ("tsserver", "tsserver"),
             ("typescript-language-server", "TypeScript LSP"), ("eslint_d", "eslint_d"), ("kotlin-lsp", "kotlin-lsp"),
             ("sourcekit-lsp", "sourcekit-lsp"), ("gopls", "gopls"), ("rust-analyzer", "rust-analyzer"),
             ("jdtls", "jdtls"), ("node_modules/.bin/", None))
INTERPRETERS = re.compile(r"^(node|bun|deno|python[\d.]*|ruby|java)$")


def launchd_jobs():
    """PID процессов, которые запустил и держит launchd: у них PPID 1 законно."""
    return {line.split()[0] for line in run(["launchctl", "list"]).splitlines()[1:] if line.split()[:1] != ["-"]}


def orphan_tool(cmd):
    """(название, явный dev-инструмент) или None, если на инструмент разработки не похоже."""
    low = cmd.lower()
    for needle, name in DEV_TOOLS:
        if needle in low:
            if name is None:
                name = low.split("node_modules/.bin/", 1)[1].split()[0].split("/")[0]
            return name, True
    words = cmd.split()
    base = words[0].rsplit("/", 1)[-1]
    if INTERPRETERS.match(base):
        script = next((w.rsplit("/", 1)[-1] for w in words[1:] if not w.startswith("-")), "")
        return f"{base} {script}".strip(), False
    return None


def orphans(rows):
    """Процессы пользователя, чей родитель умер (PPID 1), не из launchd и похожие на инструменты разработки.
    Если launchctl ничего не ответил, сервисы launchd от сирот не отличить, и отмеченных по умолчанию нет."""
    jobs = launchd_jobs()
    known = bool(jobs)
    found = []
    for r in rows:
        if r["ppid"] != "1" or r["uid"] != os.getuid() or r["kind"] is not None or r["pid"] in jobs \
                or r["label"] in SESSIONS or r["cmd"].startswith(SYSTEM_PREFIXES):
            continue
        tool = orphan_tool(r["cmd"])
        if tool:
            found.append((r, tool[0], known and tool[1] and r["cpu"] <= BUSY_CPU))
    return found


IDES = {"Android Studio", "IntelliJ IDEA", "IntelliJ IDEA CE", "IntelliJ IDEA Ultimate", "Fleet"}


def gradle_version(cmd):
    """Версия Gradle по команде демона: GradleDaemon 9.9.1 в конце или gradle-9.9.1 в classpath."""
    m = re.search(r"GradleDaemon (\d+\.\d+(?:\.\d+)?)", cmd) or re.search(r"gradle-(\d+\.\d+(?:\.\d+)?)-", cmd)
    return m.group(1) if m else None


SESSIONS = ("Claude Code (CLI)", "Codex (CLI)")
CLAUDE_SESSIONS = os.path.join(os.path.expanduser("~"), ".claude", "sessions")
IDLE_SESSION = 30 * 60


def ancestors(rows, pid):
    """PID процесса и всех его родителей: свою сессию не предлагаем закрыть."""
    parent = {r["pid"]: r["ppid"] for r in rows}
    chain = set()
    while pid and pid not in chain and pid != "1":
        chain.add(pid)
        pid = parent.get(pid)
    return chain


def idle_sessions(rows):
    """[(строка ps, подпись, сколько секунд молчит)] сессий Claude Code и Codex, которые давно простаивают.
    У Claude Code статус и время его смены лежат в ~/.claude/sessions/<pid>.json; у Codex их нет,
    там простой это CPU около нуля и больше часа работы."""
    mine = ancestors(rows, str(os.getpid()))
    tops = {r["pid"] for r in rows if r["label"] in SESSIONS}
    found = []
    now = time.time()
    for r in rows:
        if r["label"] not in SESSIONS or r["pid"] in mine or r["ppid"] in tops or r["uid"] != os.getuid():
            continue
        try:
            with open(os.path.join(CLAUDE_SESSIONS, r["pid"] + ".json")) as f:
                info = json.load(f)
        except (OSError, ValueError):
            info = None
        if info and info.get("pid") == int(r["pid"]):
            quiet = now - info.get("statusUpdatedAt", now * 1000) / 1000
            if info.get("status") != "idle" or quiet < IDLE_SESSION:
                continue
            where = info.get("name") or info.get("cwd", "").replace(os.path.expanduser("~"), "~", 1)
            found.append((r, f"Claude Code {where}".strip(), quiet))
        elif r["cpu"] < 1 and r["age"] >= 2 * IDLE_SESSION:
            name = "Claude Code" if r["label"].startswith("Claude") else "Codex"
            found.append((r, f"{name}, PID {r['pid']}", None))
    return found


HEAVY_APP = 1024
KEEP_APPS = ("/System/", "/Library/Developer/", "/Applications/Xcode.app/Contents/Developer/")


def front_pid():
    asn = run(["lsappinfo", "front"], timeout=5).strip()
    m = re.search(r'"pid"=(\d+)', run(["lsappinfo", "info", "-only", "pid", asn], timeout=5)) if asn else None
    return m.group(1) if m else None


def heavy_apps(rows):
    """[(путь .app, bundle id, имя, МБ, pids)] приложений от HEAVY_APP, кроме активного, системных
    и того, в чьем окне запущен mnrh (терминал)."""
    mine = ancestors(rows, str(os.getpid()))
    front = front_pid()
    apps = {}
    for r in rows:
        m = re.match(r"(.*?/[^/]+\.app)/", r["cmd"])
        if r["kind"] != "app" or not m or r["uid"] != os.getuid():
            continue
        a = apps.setdefault(m.group(1), {"mb": 0.0, "pids": [], "skip": m.group(1).startswith(KEEP_APPS)})
        a["mb"] += r["mem"]
        a["pids"].append(r["pid"])
        a["skip"] |= r["pid"] in mine or r["pid"] == front
    found = []
    for path, a in apps.items():
        if a["skip"] or a["mb"] < HEAVY_APP:
            continue
        bid, name = bundle_info(path)
        if bid:
            found.append((path, bid, name, a["mb"], a["pids"]))
    return sorted(found, key=lambda x: -x[3])


def quit_app(bid):
    """Обычная команда «Закрыть», без ожидания ответа: если есть несохраненное, приложение спросит само."""
    script = f'ignoring application responses\ntell application id "{bid}" to quit\nend ignoring'
    run(["osascript", "-e", script], timeout=15)


def find_targets(rows):
    """Что можно остановить, по строке на пункт меню."""
    daemons = [r for r in rows if r["kind"] == "daemon" and r["cmd"].split(" ")[0].rsplit("/", 1)[-1] == "java"]
    targets = []
    used = gradle_versions_in_use()
    stale = [r for r in daemons if not r["label"].startswith("Kotlin") and used
             and gradle_version(r["cmd"]) not in (None, *used)]
    if stale:
        versions = sorted({gradle_version(r["cmd"]) for r in stale}, key=lambda v: [int(n) for n in v.split(".")])
        busy = sum(r["cpu"] > BUSY_CPU for r in stale)
        targets.append({"key": "gradle-stale", "label": f"Gradle-демоны {', '.join(versions)}: {len(stale)}",
                        "note": "этих версий нет ни в одном проекте" + (", но сборка идет" if busy else ""),
                        "mb": sum(r["mem"] for r in stale), "pids": [r["pid"] for r in stale]})
    gradle_alive = any(not r["label"].startswith("Kotlin") for r in daemons)
    ide_alive = any(r["kind"] == "app" and r["label"] in IDES for r in rows)
    for key, title in (("gradle", "Gradle-демоны"), ("kotlin", "Kotlin-демоны")):
        own = [r for r in daemons if (r["label"].startswith("Kotlin")) == (key == "kotlin") and r not in stale]
        lonely = key == "kotlin" and not gradle_alive and not ide_alive
        idle = [r for r in own if r["cpu"] <= BUSY_CPU or lonely]
        busy = [r for r in own if r not in idle]
        for part, group, note in ((key, idle, "ни Gradle, ни IDE не запущены" if lonely else ""),
                                  (key + "-busy", busy, "заняты сборкой, она упадёт")):
            if group:
                targets.append({"key": part, "label": f"{title}: {len(group)}", "note": note,
                                "mb": sum(r["mem"] for r in group), "pids": [r["pid"] for r in group]})
    sim_mb = simulator_mb(rows)
    for udid, name, runtime in booted_simulators():
        targets.append({"key": "sim", "id": "sim:" + udid, "label": f"iOS-симулятор {name} ({runtime})",
                        "mb": sim_mb.get(udid, 0.0), "note": "данные и входы в аккаунты сохранятся", "udid": udid})
    adb = adb_path()
    emus = running_emulators(adb)
    emu_mb = emulator_mb(rows)
    for serial in emus:
        name = avd_name(adb, serial)
        mb = emu_mb.get(name, 0.0)
        if not mb and len(emus) == 1:
            mb = sum(r["mem"] for r in rows if r["kind"] == "emulator")
        targets.append({"key": "emulator", "id": "emulator:" + serial, "label": f"Android-эмулятор {name}",
                        "mb": mb, "note": "состояние сохранится в Quick Boot", "adb": adb, "serial": serial})
    groups = {}
    for r, name, dev in orphans(rows):
        key = "orphan" if dev else "orphan-other"
        g = groups.setdefault((key, name), {"key": key, "id": f"{key}:{name}", "mb": 0.0, "pids": [],
                                            "note": "родитель закрыт, их никто не ждет" if dev
                                            else "родитель закрыт; проверь, не твоя ли это фоновая задача"})
        g["mb"] += r["mem"]
        g["pids"].append(r["pid"])
    sessions = idle_sessions(rows)
    session_mb = owned_mb(rows, {r["pid"]: r["pid"] for r, _, _ in sessions})
    for r, label, quiet in sessions:
        note = (f"молчит {int(quiet // 3600)} ч" if quiet >= 3600 else f"молчит {int(quiet // 60)} мин") if quiet \
            else f"CPU около нуля, работает {r['age'] // 3600} ч"
        targets.append({"key": "session", "id": "session:" + r["pid"], "label": f"сессия {label}",
                        "mb": session_mb.get(r["pid"], r["mem"]),
                        "note": note + ("; вернуться: claude --resume" if label.startswith("Claude") else
                                        "; вернуться: codex resume"),
                        "term": [r["pid"]]})
    for g in sorted(groups.values(), key=lambda g: (g["key"], -g["mb"])):
        g["label"] = f"сироты {g['id'].split(':', 1)[1]}" + (f" ×{len(g['pids'])}" if len(g["pids"]) > 1 else "")
        targets.append(g)
    for path, bid, name, mb, pids in heavy_apps(rows):
        targets.append({"key": "app", "id": "app:" + bid, "label": f"приложение {name}", "mb": mb,
                        "note": "закроется как по ⌘Q, несохраненное спросит само", "bundle": bid, "name": name})
    for t in targets:
        t.setdefault("id", t["key"])
    return targets


def wanted_keys():
    picked = set()
    for flag, keys in (("--daemons", ("gradle", "gradle-stale", "kotlin")), ("--gradle", ("gradle", "gradle-stale")),
                       ("--kotlin", ("kotlin",)), ("--sim", ("sim",)), ("--emulator", ("emulator",)), ("--orphans", ("orphan",)),
                       ("--sessions", ("session",)),
                       ("--apps", ("app",))):
        if flag in args:
            picked.update(keys)
    if picked <= ADDED_ONLY:
        picked |= DEFAULT_KEYS
    if has_flag(args, "-f", "--force"):
        picked |= {k + "-busy" for k in ("gradle", "kotlin") if k in picked}
    return picked


def choose(targets, marked):
    from menu import pick_many
    rows = [(t["label"], f"{fmt(t['mb']):>9}  {t['label']}" + (f"  \x1b[2m{t['note']}\x1b[0m" if t["note"] else ""))
            for t in targets]

    def summary(marks):
        return f"отмечено {len(marks)} · {fmt(sum(targets[i]['mb'] for i in marks)).strip()}"

    picked = pick_many(rows, title="\x1b[1;35mmnrh ram\x1b[0m  \x1b[2mчто остановить?\x1b[0m",
                       marked={i for i, t in enumerate(targets) if t["key"] in marked or t["id"] in marked}, summary=summary)
    return None if picked is None else [targets[i] for i in sorted(picked)]


def show_plan(chosen):
    for t in chosen:
        note = f"  ({t['note']})" if t["note"] else ""
        print(f"  {fmt(t['mb'])}  {t['label']}{note}")


def stop(chosen):
    before = memory_state()
    pids = [p for t in chosen for p in t.get("pids", [])]
    if pids:
        stop_daemons(pids)
    apps = [t for t in chosen if t["key"] == "app"]
    for t in apps:
        quit_app(t["bundle"])
    for t in chosen:
        for pid in t.get("term", []):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    for t in chosen:
        if t["key"] == "sim":
            run(["xcrun", "simctl", "shutdown", t["udid"]], timeout=120)
    emus = [t for t in chosen if t["key"] == "emulator"]
    for t in emus:
        run([t["adb"], "-s", t["serial"], "emu", "kill"], timeout=60)
    for _ in range(15 if emus else 0):
        if not set(t["serial"] for t in emus) & set(running_emulators(emus[0]["adb"])):
            break
        time.sleep(1)
    time.sleep(3)
    still = {t["label"] for t in apps if any(t["bundle"] == bid for _, bid, _, _, _ in heavy_apps(collect()))}
    if still:
        print("Еще не закрылись (наверное, ждут ответа про несохраненное): " + ", ".join(sorted(still)))
    after = memory_state()
    print(f"Свободно: {max(before['total'] - before['used'], 0):.1f} -> "
          f"{max(after['total'] - after['used'], 0):.1f} ГБ   |   swap: {before['swap']:.1f} -> {after['swap']:.1f} ГБ"
          f"   |   давление: {before['pressure']}"
          + (f" -> {after['pressure']}" if after["pressure"] != before["pressure"] else ""))
    if after["swap"] > 0.5 and after["swap"] >= before["swap"] - 0.1:
        print("Swap сам уменьшается не сразу: macOS подтягивает страницы обратно по мере обращения к ним.")


def clean():
    rows = collect()
    if not rows:
        print(NO_PROCESSES)
        print("Демоны не видны, остановить можно только симуляторы и эмуляторы.\n")
    targets = find_targets(rows)
    marked = wanted_keys()
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not has_flag(args, "-y", "--yes")
    chosen = []
    if not targets:
        print("Останавливать нечего.")
    elif interactive:
        from menu import choice
        while True:
            chosen = choose(targets, marked)
            if not chosen:
                print("Ничего не остановлено.")
                chosen = []
                break
            print("Будет остановлено:")
            show_plan(chosen)
            print()
            answer = choice("Остановить?", [("go", "Остановить", "yд"), ("back", "← К списку", "nн"),
                                            ("quit", "Выйти", "qй")], default=0, back="back")
            if answer == "go":
                break
            if answer == "quit":
                print("Ничего не остановлено.")
                chosen = []
                break
            marked = {t["id"] for t in chosen}
    else:
        chosen = [t for t in targets if t["key"] in marked]
        skipped = [t for t in targets if t["key"].endswith("-busy") and t["key"][:-5] in marked and t not in chosen]
        if not chosen:
            print("Останавливать нечего" + (": демоны заняты сборкой, снять их: mnrh ram clean -f." if skipped else "."))
        else:
            print("Будет остановлено:")
            show_plan(chosen)
            if skipped:
                print(f"  пропускаю занятые сборкой демоны: {sum(len(t['pids']) for t in skipped)} (снять и их: -f)")
            if not confirm("Остановить?", has_flag(args, "-y", "--yes")):
                print("Отменено.")
                return
        left = [t for t in targets if t["key"] == "emulator" and t not in chosen]
        if sum(t["mb"] for t in left) >= 512:
            print(f"Android-эмуляторы держат {fmt(sum(t['mb'] for t in left)).strip()}, их не трогал: "
                  f"{CLEAN_HINT} --emulator")
    if chosen:
        stop(chosen)

    left = [t for t in targets if t["key"] == "app" and t not in chosen][:3]
    if left and not interactive:
        print("Больше всего еще держат приложения: " + ", ".join(f"{t['name']} {fmt(t['mb']).strip()}" for t in left)
              + f". Закрыть их: {CLEAN_HINT} --apps")


GRADLE_PROPS = os.path.join(HOME, ".gradle", "gradle.properties")
IDLE_KEY = "org.gradle.daemon.idletimeout"
IDLE_WANTED = 30 * 60 * 1000
IDLE_DEFAULT = 3 * 3600 * 1000


def read_props(path):
    """Только ключи и значения gradle.properties; остальное в файле (пароли и т.п.) никуда не выводится."""
    props = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("#", "!")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip().replace("\\=", "=").replace("\\:", ":")
    return props


def xmx_mb(text):
    m = re.search(r"-Xmx(\d+)([kKmMgG]?)", text or "")
    if not m:
        return None
    return int(m.group(1)) * {"k": 1 / 1024, "m": 1, "g": 1024, "": 1 / 1048576}[m.group(2).lower()]


def daemon_heaps(props):
    """(Xmx Gradle-демона, Xmx Kotlin-демона, откуда Kotlin) в МБ; None, если не задано."""
    jvm = props.get("org.gradle.jvmargs", "")
    gradle = xmx_mb(jvm)
    kotlin = xmx_mb(props.get("kotlin.daemon.jvmargs"))
    if kotlin is not None:
        return gradle, kotlin, "kotlin.daemon.jvmargs"
    m = re.search(r'-Dkotlin\.daemon\.jvm\.options="?([^" ]+)', jvm)
    if m and xmx_mb(m.group(1)) is not None:
        return gradle, xmx_mb(m.group(1)), "kotlin.daemon.jvm.options"
    return gradle, None, "как у Gradle"


def gb(mb):
    return f"{mb / 1024:g} ГБ" if mb >= 1024 else f"{mb:g} МБ"


def tune():
    total_mb = int(run(["sysctl", "-n", "hw.memsize"]) or 0) / 1048576
    cap = max(2048, min(4096, total_mb // 4))
    home = read_props(GRADLE_PROPS) or {}
    print(f"Демоны сборки на Mac с {total_mb / 1024:.0f} ГБ памяти. Разумный потолок одного демона {gb(cap)}.\n")

    idle = home.get(IDLE_KEY)
    idle_ms = int(idle) if idle and idle.isdigit() else None
    if idle_ms is None:
        print(f"Простой: {IDLE_KEY} не задан, демон Gradle держит память 3 ч после последней сборки.")
    else:
        print(f"Простой: {IDLE_KEY}={idle_ms}, демон уходит через {idle_ms // 60000} мин после последней сборки.")
    need_idle = (idle_ms or IDLE_DEFAULT) > 2 * IDLE_WANTED
    if need_idle:
        print(f"  совет: {IDLE_KEY}={IDLE_WANTED} (30 мин) в {tilde(GRADLE_PROPS)}, записать: mnrh ram tune --apply")
    if "org.gradle.jvmargs" in home:
        print(f"  org.gradle.jvmargs задан в {tilde(GRADLE_PROPS)} и перекрывает jvmargs всех проектов целиком")
    print()

    rows = []
    for proj in projects():
        props = read_props(os.path.join(proj, "gradle.properties"))
        if props is None:
            continue
        merged = dict(props, **home)
        gradle, kotlin, source = daemon_heaps(merged)
        if gradle is None and kotlin is None:
            continue
        rows.append((os.path.basename(proj), gradle, kotlin, source))
    if rows:
        print(f"{'GRADLE':>8}  {'KOTLIN':>8}  ПРОЕКТ")
        for name, gradle, kotlin, source in sorted(rows, key=lambda r: -((r[1] or 512) + (r[2] or r[1] or 512))):
            g = gb(gradle) if gradle else "512 МБ"
            k = gb(kotlin) if kotlin else f"={g}"
            over = [what for what, mb in (("Gradle", gradle), ("Kotlin", kotlin)) if mb and mb > cap]
            note = f"  больше {gb(cap)}: {', '.join(over)}" if over else ""
            print(f"{g:>8}  {k:>8}  {name}{note}")
        worst = max((r[1] or 512) + (r[2] or r[1] or 512) for r in rows)
        print("=X у Kotlin: отдельно не задано, Kotlin-демон берет столько же, сколько Gradle.")
        print(f"\nGradle и Kotlin-демон одного проекта вместе могут взять до {gb(worst)}.")
        if any((r[1] or 0) > cap or (r[2] or 0) > cap for r in rows):
            print(f"Снизить: -Xmx{int(cap // 1024)}g в org.gradle.jvmargs и kotlin.daemon.jvmargs в gradle.properties "
                  "этих проектов. Если сборка начнет падать с OutOfMemoryError, верни прежнее значение.")
        print("Еще вариант: kotlin.compiler.execution.strategy=in-process. Тогда Kotlin компилирует внутри "
              "демона Gradle,\nотдельного Kotlin-демона нет, но Gradle-демону нужно больше памяти.")
        print(f"\n-Xmx в {tilde(GRADLE_PROPS)} mnrh не пишет: глобальный org.gradle.jvmargs заменил бы jvmargs "
              "каждого проекта целиком.")

    if not has_flag(args, "--apply"):
        return
    if not need_idle:
        print("\nЗаписывать нечего: простой демона уже короткий.")
        return
    line = f"{IDLE_KEY}={IDLE_WANTED}"
    if not confirm(f"Записать {line} в {tilde(GRADLE_PROPS)}?", has_flag(args, "-y", "--yes")):
        print("Не записано.")
        return
    write_prop(GRADLE_PROPS, IDLE_KEY, str(IDLE_WANTED))
    print(f"Записано. Уже запущенные демоны живут по-старому, новые уйдут через 30 минут простоя "
          f"(снять сразу: {CLEAN_HINT} --gradle).")


def write_prop(path, key, value):
    """Меняет строку key=... на месте или дописывает в конец; остальные строки и права файла не трогает."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    rx = re.compile(r"^[ \t]*" + re.escape(key) + r"[ \t]*[=:].*$", re.M)
    if rx.search(text):
        text = rx.sub(lambda _: f"{key}={value}", text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"# mnrh ram tune: демон Gradle уходит через 30 минут простоя\n{key}={value}\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".mnrh-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    else:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


if __name__ == "__main__":
    if args and args[0] == "clean":
        clean()
    elif args and args[0] == "watch":
        watch()
    elif args and args[0] == "tune":
        tune()
    else:
        show()
