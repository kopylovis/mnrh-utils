import os
import re
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import DEV, OK, WARN, BAD, has_flag, paint, run, tilde

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or (args and args[0] not in ("kill", "-a", "--all")):
    print("mnrh ports              кто слушает TCP-порты: процесс, проект, с какого времени")
    print("mnrh ports -a           вместе с системными (AirPlay, Handoff и т. п.)")
    print("mnrh ports kill 8080    остановить того, кто занял порт (сначала вежливо, через 3 с — -9)")
    print("mnrh ports kill 8080 5037 -f   и системные процессы тоже")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)

SYSTEM = {"ControlCenter": "приёмник AirPlay", "rapportd": "Handoff и Continuity",
          "sharingd": "AirDrop и общий доступ", "identityservicesd": "iMessage/FaceTime",
          "launchd": "launchd", "mDNSResponder": "Bonjour"}
KNOWN_PORTS = {5037: "сервер adb", 8081: "Metro (React Native)", 8097: "React DevTools",
               5554: "эмулятор Android (консоль)", 5555: "эмулятор Android (adb)", 9229: "отладчик Node",
               3000: "dev-сервер", 5173: "Vite", 4200: "Angular", 8080: "веб-сервер (Ktor, Spring и т. п.)",
               5432: "PostgreSQL", 6379: "Redis", 3306: "MySQL", 27017: "MongoDB", 11434: "Ollama"}


def describe(cmd, name):
    if "GradleDaemon" in cmd:
        m = re.search(r"GradleDaemon ([\d.]+)", cmd)
        return f"Gradle-демон {m.group(1) if m else ''}".strip()
    if "KotlinCompileDaemon" in cmd or "kotlin-daemon" in cmd:
        return "Kotlin-демон"
    if "GradleWorkerMain" in cmd:
        return "Gradle worker"
    parts = cmd.split()
    exe = os.path.basename(parts[0]) if parts else name
    if re.match(r"(python[\d.]*|node|ruby|bun|deno|php)$", exe, re.I):
        # Интерпретатор (системный python3 живёт внутри Xcode.app) — показываем, что он запустил.
        rest = [p for p in parts[1:] if not p.startswith("-")]
        target = parts[parts.index("-m") + 1] if "-m" in parts[:-1] else (os.path.basename(rest[0]) if rest else "")
        return f"{exe.lower()} {target}".strip()
    app = re.search(r"/([^/]+)\.app/", cmd)
    if app:
        return app.group(1)
    return name


def listeners():
    found = {}
    pid = cmd = None
    for line in run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pcn"], timeout=30).splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = int(value)
        elif tag == "c":
            cmd = value
        elif tag == "n" and pid:
            m = re.match(r"(.*):(\d+)$", value)
            if not m:
                continue
            port, addr = int(m.group(2)), m.group(1)
            entry = found.setdefault((port, pid), {"port": port, "pid": pid, "name": cmd, "addrs": set()})
            entry["addrs"].add(addr)
    return sorted(found.values(), key=lambda e: e["port"])


def proc_info(pid):
    out = run(["ps", "-o", "etime=,command=", "-p", str(pid)]).strip()
    etime, _, command = out.partition(" ")
    cwd = ""
    for line in run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-F", "n"]).splitlines():
        if line.startswith("n"):
            cwd = line[1:]
    return etime.strip(), command.strip(), cwd


def age(etime):
    # ps: [[dd-]hh:]mm:ss
    days, _, rest = etime.rpartition("-")
    parts = [int(p) for p in rest.split(":")] if rest else [0]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, _ = parts
    d = int(days) if days else 0
    if d:
        return f"{d} дн."
    if h:
        return f"{h} ч"
    return f"{m} мин"


def status():
    rows = listeners()
    shown = hidden = 0
    exposed = []
    if rows:
        print(f"{'ПОРТ':>6}  {'ДОСТУП':<10} {'PID':>6}  {'ИДЁТ':>7}  ПРОЦЕСС")
    for e in rows:
        if e["name"] in SYSTEM and not has_flag(args, "-a", "--all"):
            hidden += 1
            continue
        etime, command, cwd = proc_info(e["pid"])
        what = SYSTEM.get(e["name"]) or describe(command, e["name"])
        hint = KNOWN_PORTS.get(e["port"])
        if hint and hint.split(" ")[0].lower() not in what.lower():
            what += paint(f" ({hint})", "2")
        where = ""
        if cwd.startswith(DEV + os.sep):
            where = paint(f"  {os.path.relpath(cwd, DEV).split(os.sep)[0]}", "36")
        local = all(a in ("127.0.0.1", "[::1]", "localhost") for a in e["addrs"])
        access = "localhost" if local else "вся сеть"
        if not local and e["name"] not in SYSTEM:
            exposed.append(e["port"])
        print(f"{e['port']:>6}  {access:<10} {e['pid']:>6}  {age(etime) if etime else '?':>7}  {what}{where}")
        shown += 1
    if not shown:
        print("Своих слушающих портов нет.")
    if hidden:
        print(paint(f"\nСистемных скрыто: {hidden} (mnrh ports -a)", "2"))
    if exposed:
        print(f"\n{WARN} Доступны из сети, а не только с этого Mac: {', '.join(map(str, exposed))}."
              " Для разработки обычно хватает 127.0.0.1.")


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill():
    ports = [int(a) for a in args[1:] if a.isdigit()]
    if not ports:
        sys.exit("mnrh ports kill <порт> [порт...]")
    force = has_flag(args, "-f", "--force")
    rows = listeners()
    for port in ports:
        owners = [e for e in rows if e["port"] == port]
        if not owners:
            print(f"{port}: никто не слушает")
            continue
        for e in owners:
            if e["name"] in SYSTEM and not force:
                print(f"{BAD} {port}: это {SYSTEM[e['name']]} ({e['name']}), системный процесс. Если точно нужно: -f")
                continue
            name = describe(proc_info(e["pid"])[1], e["name"])
            try:
                os.kill(e["pid"], signal.SIGTERM)
            except PermissionError:
                print(f"{BAD} {port}: {name} (pid {e['pid']}) чужой, нужен sudo")
                continue
            except ProcessLookupError:
                pass
            for _ in range(30):
                if not alive(e["pid"]):
                    break
                time.sleep(0.1)
            else:
                os.kill(e["pid"], signal.SIGKILL)
                time.sleep(0.3)
            print(f"{OK} {port}: остановлен {name} (pid {e['pid']})" if not alive(e["pid"])
                  else f"{BAD} {port}: {name} (pid {e['pid']}) не остановился")


kill() if args and args[0] == "kill" else status()
