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
if has_flag(args, "-h", "--help") or (args and args[0].isalpha() and args[0] not in ("clean", "status")):
    print("mnrh ram                 меню с действиями (без терминала — то же, что status)")
    print("mnrh ram status          кто ест оперативную память, сгруппировано по приложениям")
    print("mnrh ram -p              то же по отдельным процессам")
    print("mnrh ram -n 30           показать больше строк (по умолчанию 15)")
    print("mnrh ram clean           отметить галочками, что остановить: демоны Gradle и Kotlin, iOS-симуляторы,")
    print("                         эмуляторы Android; потом показать, сколько вернулось")
    print("mnrh ram clean -y        без меню: демоны и симуляторы (без терминала -y обязателен)")
    print("  --daemons, --gradle, --kotlin, --sim  только это; --emulator ещё эмулятор Android")
    print("  -f                     снять и занятые сборкой демоны")
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
    if kind in ("daemon", "emulator") or (kind == "sim" and sims_booted):
        return f"   <- {CLEAN_HINT}"
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


def avd_name(adb, serial):
    lines = run([adb, "-s", serial, "emu", "avd", "name"], timeout=10).splitlines()
    return lines[0].strip() if lines and lines[0].strip() else serial


def find_targets(rows):
    """Что можно остановить, по строке на пункт меню."""
    daemons = [r for r in rows if r["kind"] == "daemon" and r["cmd"].split(" ")[0].rsplit("/", 1)[-1] == "java"]
    targets = []
    for key, title in (("gradle", "Gradle-демоны"), ("kotlin", "Kotlin-демоны")):
        own = [r for r in daemons if (r["label"].startswith("Kotlin")) == (key == "kotlin")]
        idle = [r for r in own if r["cpu"] <= BUSY_CPU]
        busy = [r for r in own if r["cpu"] > BUSY_CPU]
        for part, group, note in ((key, idle, ""), (key + "-busy", busy, "заняты сборкой, она упадёт")):
            if group:
                targets.append({"key": part, "label": f"{title}: {len(group)}", "note": note,
                                "mb": sum(r["mem"] for r in group), "pids": [r["pid"] for r in group]})
    mb = defaultdict(float)
    for r in rows:
        mb[r["kind"]] += r["mem"]
    sims = booted_simulators()
    if sims:
        targets.append({"key": "sim", "label": f"iOS-симуляторы: {', '.join(sims)}", "mb": mb["sim"],
                        "note": "данные и входы в аккаунты сохранятся"})
    adb = adb_path()
    emus = running_emulators(adb)
    if emus:
        targets.append({"key": "emulator", "label": "Android-эмуляторы: " + ", ".join(avd_name(adb, e) for e in emus),
                        "mb": mb["emulator"], "note": "состояние сохранится в Quick Boot", "adb": adb, "serials": emus})
    return targets


def wanted_keys():
    picked = set()
    for flag, keys in (("--daemons", ("gradle", "kotlin")), ("--gradle", ("gradle",)), ("--kotlin", ("kotlin",)),
                       ("--sim", ("sim",)), ("--emulator", ("emulator",))):
        if flag in args:
            picked.update(keys)
    if not picked or picked == {"emulator"}:
        picked |= {"gradle", "kotlin", "sim"}
    if has_flag(args, "-f", "--force"):
        picked |= {k + "-busy" for k in ("gradle", "kotlin") if k in picked}
    return picked


def choose(targets, marked):
    from menu import pick_many
    rows = [(t["key"], f"{fmt(t['mb']):>9}  {t['label']}" + (f"  \x1b[2m{t['note']}\x1b[0m" if t["note"] else ""))
            for t in targets]

    def summary(marks):
        return f"отмечено {len(marks)} · {fmt(sum(targets[i]['mb'] for i in marks)).strip()}"

    picked = pick_many(rows, title="\x1b[1;35mmnrh ram\x1b[0m  \x1b[2mчто остановить?\x1b[0m",
                       marked={i for i, t in enumerate(targets) if t["key"] in marked}, summary=summary)
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
    if any(t["key"] == "sim" for t in chosen):
        run(["xcrun", "simctl", "shutdown", "all"], timeout=120)
    emu = next((t for t in chosen if t["key"] == "emulator"), None)
    if emu:
        for serial in emu["serials"]:
            run([emu["adb"], "-s", serial, "emu", "kill"], timeout=60)
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


def clean():
    rows = collect()
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
            marked = {t["key"] for t in chosen}
    else:
        chosen = [t for t in targets if t["key"] in marked]
        skipped = [t for t in targets if t["key"].endswith("-busy") and t not in chosen]
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
        emu = next((t for t in targets if t["key"] == "emulator" and t not in chosen), None)
        if emu and emu["mb"] >= 512:
            print(f"Android-эмулятор держит {fmt(emu['mb']).strip()}, его не трогал: {CLEAN_HINT} --emulator")
    if chosen:
        stop(chosen)

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
