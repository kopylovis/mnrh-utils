import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import adb_path, confirm, has_flag

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or (args and args[0].isalpha() and args[0] != "clean"):
    print("mnrh ram                 кто ест оперативную память, сгруппировано по приложениям")
    print("mnrh ram -p              то же по отдельным процессам")
    print("mnrh ram -n 30           показать больше строк (по умолчанию 15)")
    print("mnrh ram clean           остановить демоны Gradle и Kotlin и iOS-симуляторы, показать, сколько вернулось")
    print("mnrh ram clean --daemons только демоны; --sim только симуляторы; --emulator ещё эмулятор Android")
    print("                         -f снять и занятые сборкой демоны, -y без вопроса (без терминала обязателен)")
    print("Приложения clean не закрывает: у них могут быть несохранённые данные. Подскажет, какие закрыть вручную.")
    sys.exit(0)
limit = 15
if "-n" in args:
    try:
        limit = int(args[args.index("-n") + 1])
    except (IndexError, ValueError):
        sys.exit("mnrh ram: после -n нужно число")

CLEAN_HINT = "mnrh ram clean"
UNITS = {"B": 1 / 1048576, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1048576}
BUSY_CPU = 5.0


def run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
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
    if base == "java":
        return "Java (прочее)", None
    return base or "неизвестно", None


def collect():
    commands, cpu = {}, {}
    for line in run(["ps", "-axo", "pid=,pcpu=,command="]).splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3:
            commands[parts[0]] = parts[2]
            cpu[parts[0]] = float(parts[1])
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
        rows.append({"pid": pid, "mem": to_mb(cols[1]), "cmprs": to_mb(cols[2]), "label": label,
                     "kind": kind, "cpu": cpu.get(pid, 0.0), "cmd": commands.get(pid, "")})
    return rows


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
    return {"total": total, "wired": wired, "compressed": compressed, "app": app,
            "used": app + wired + compressed, "swap": float(swap.group(1)) / 1024 if swap else 0,
            "ratio": stored / occupied if occupied else 0}


def mark(kind, sims_booted):
    if kind == "daemon" or (kind == "sim" and sims_booted):
        return f"   <- {CLEAN_HINT}"
    if kind == "emulator":
        return f"   <- {CLEAN_HINT} --emulator"
    return ""


def show():
    rows = collect()
    s = memory_state()
    sims_booted = bool(booted_simulators())
    print(f"Занято {s['used']:.1f} из {s['total']:.0f} ГБ · приложения {s['app']:.1f} · wired {s['wired']:.1f} · "
          f"сжато {s['compressed']:.1f} · свободно {max(s['total'] - s['used'], 0):.1f} · swap {s['swap']:.1f}")
    print()

    if "-p" in args:
        rows.sort(key=lambda r: r["mem"], reverse=True)
        print(f"{'ПАМЯТЬ':>9}  {'СЖАТО':>9}  {'PID':>6}  ПРОЦЕСС")
        for r in rows[:limit]:
            print(f"{fmt(r['mem']):>9}  {fmt(r['cmprs']):>9}  {r['pid']:>6}  {r['label']}{mark(r['kind'], sims_booted)}")
    else:
        groups = defaultdict(lambda: [0.0, 0.0, 0, None])
        for r in rows:
            g = groups[r["label"]]
            g[0] += r["mem"]
            g[1] += r["cmprs"]
            g[2] += 1
            g[3] = g[3] or r["kind"]
        ordered = sorted(groups.items(), key=lambda kv: kv[1][0], reverse=True)
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
    if held:
        print(f"\nМожно вернуть: {', '.join(held)}. Освободить: {CLEAN_HINT}")


def booted_simulators():
    data = json.loads(run(["xcrun", "simctl", "list", "devices", "booted", "-j"]) or "{}")
    return [d["name"] for lst in data.get("devices", {}).values() for d in lst if d.get("state") == "Booted"]


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


def clean():
    picked = [k for k, flag in (("daemon", "--daemons"), ("sim", "--sim"), ("emulator", "--emulator"))
              if flag in args]
    wanted = set(picked) if picked and picked != ["emulator"] else {"daemon", "sim"} | set(picked)
    force = has_flag(args, "-f", "--force")
    rows = collect()
    by_kind = defaultdict(float)
    for r in rows:
        by_kind[r["kind"]] += r["mem"]

    plan = []
    daemons = [r for r in rows if r["kind"] == "daemon" and r["cmd"].split(" ")[0].rsplit("/", 1)[-1] == "java"]
    busy = [r for r in daemons if r["cpu"] > BUSY_CPU and not force] if "daemon" in wanted else []
    idle = [r for r in daemons if r not in busy]
    if "daemon" in wanted and idle:
        plan.append(f"демоны сборки: {len(idle)}, {fmt(sum(r['mem'] for r in idle)).strip()}")
    sims = booted_simulators() if "sim" in wanted else []
    if sims:
        plan.append(f"iOS-симуляторы: {', '.join(sims)}, {fmt(by_kind['sim']).strip()}"
                    " (данные и входы в аккаунты сохранятся)")
    adb = adb_path()
    emus = running_emulators(adb) if "emulator" in wanted else []
    if emus:
        plan.append(f"Android-эмуляторы: {', '.join(emus)}, {fmt(by_kind['emulator']).strip()}"
                    " (состояние сохранится в Quick Boot)")

    if not plan:
        print("Останавливать нечего" + (": демоны заняты сборкой, снять их: mnrh ram clean -f." if busy else "."))
    else:
        print("Будет остановлено:")
        for line in plan:
            print(f"  {line}")
        if busy:
            print(f"  пропускаю занятые сборкой демоны: {len(busy)} (снять и их: -f)")
        if not confirm("Остановить?", has_flag(args, "-y", "--yes")):
            print("Отменено.")
            return
        before = memory_state()
        if "daemon" in wanted and idle:
            stop_daemons([r["pid"] for r in idle])
        if sims:
            run(["xcrun", "simctl", "shutdown", "all"], timeout=120)
        for serial in emus:
            run([adb, "-s", serial, "emu", "kill"], timeout=60)
        if emus:
            for _ in range(15):
                if not any(r["kind"] == "emulator" for r in collect()):
                    break
                time.sleep(1)
        time.sleep(3)
        after = memory_state()
        print(f"Свободно: {max(before['total'] - before['used'], 0):.1f} -> "
              f"{max(after['total'] - after['used'], 0):.1f} ГБ   |   swap: {before['swap']:.1f} -> {after['swap']:.1f} ГБ")
        if after["swap"] > 0.5 and after["swap"] >= before["swap"] - 0.1:
            print("Swap сам уменьшается не сразу: macOS подтягивает страницы обратно по мере обращения к ним.")

    if "emulator" not in wanted and by_kind["emulator"] >= 512:
        print(f"Android-эмулятор держит {fmt(by_kind['emulator']).strip()}, его не трогал: {CLEAN_HINT} --emulator")
    apps = defaultdict(float)
    for r in rows:
        if r["kind"] == "app":
            apps[r["label"]] += r["mem"]
    heavy = [(label, mb) for label, mb in sorted(apps.items(), key=lambda kv: -kv[1]) if mb >= 1024][:3]
    if heavy:
        print("Больше всего ещё держат приложения, их закрыть вручную: "
              + ", ".join(f"{label} {fmt(mb).strip()}" for label, mb in heavy))


if args and args[0] == "clean":
    clean()
else:
    show()
