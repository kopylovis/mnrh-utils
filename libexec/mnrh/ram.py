import re
import subprocess
import sys
from collections import defaultdict

args = sys.argv[1:]
if "-h" in args or "--help" in args:
    print("mnrh ram           кто ест оперативную память, сгруппировано по приложениям")
    print("mnrh ram -p        то же по отдельным процессам")
    print("mnrh ram -n 30     показать больше строк (по умолчанию 15)")
    sys.exit(0)
per_process = "-p" in args
limit = 15
if "-n" in args:
    try:
        limit = int(args[args.index("-n") + 1])
    except (IndexError, ValueError):
        sys.exit("mnrh ram: после -n нужно число")

DAEMON_HINT = "mnrh killdaemons"
UNITS = {"B": 1 / 1048576, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1048576}


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def to_mb(value):
    m = re.match(r"([\d.]+)([BKMGT]?)", value.strip())
    if not m:
        return 0.0
    return float(m.group(1)) * UNITS.get(m.group(2) or "B", 0)


def fmt(mb):
    return f"{mb / 1024:5.2f} ГБ" if mb >= 1024 else f"{mb:5.0f} МБ"


def classify(cmd):
    if "GradleDaemon" in cmd:
        return "Gradle-демоны", True
    if "KotlinCompileDaemon" in cmd or "kotlin-daemon" in cmd:
        return "Kotlin-демоны", True
    if "GradleWorkerMain" in cmd:
        return "Gradle worker", True
    if "GradleWrapperMain" in cmd:
        return "Gradle wrapper", True
    if "/CoreSimulator/" in cmd or "CoreSimulator.framework" in cmd:
        return "iOS-симулятор", False
    app = re.search(r"/([^/]+)\.app/", cmd)
    if app:
        return app.group(1), False
    first = cmd.split(" ")[0]
    base = first.rsplit("/", 1)[-1]
    if base == "claude" or "/claude/versions/" in first:
        return "Claude Code (CLI)", False
    if base == "java":
        return "Java (прочее)", False
    return base or "неизвестно", False


def summary():
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
    used = app + wired + compressed
    swap = re.search(r"used = ([\d.]+)M", run(["sysctl", "-n", "vm.swapusage"]))
    swap_gb = float(swap.group(1)) / 1024 if swap else 0
    print(f"Занято {used:.1f} из {total:.0f} ГБ · приложения {app:.1f} · wired {wired:.1f} · "
          f"сжато {compressed:.1f} · свободно {max(total - used, 0):.1f} · swap {swap_gb:.1f}")


commands = {}
for line in run(["ps", "-axo", "pid=,command="]).splitlines():
    parts = line.strip().split(" ", 1)
    if len(parts) == 2:
        commands[parts[0]] = parts[1]

rows = []
top_out = run(["top", "-l", "1", "-stats", "pid,mem,cmprs"]).splitlines()
started = False
for line in top_out:
    if line.startswith("PID"):
        started = True
        continue
    if not started:
        continue
    cols = line.split()
    if len(cols) < 3:
        continue
    pid, mem, cmprs = cols[0], to_mb(cols[1]), to_mb(cols[2])
    label, daemon = classify(commands.get(pid, ""))
    rows.append((pid, mem, cmprs, label, daemon))

summary()
print()

daemon_mb = sum(r[1] for r in rows if r[4])
sim_mb = sum(r[1] for r in rows if r[3] == "iOS-симулятор")

if per_process:
    rows.sort(key=lambda r: r[1], reverse=True)
    print(f"{'ПАМЯТЬ':>9}  {'СЖАТО':>9}  {'PID':>6}  ПРОЦЕСС")
    for pid, mem, cmprs, label, daemon in rows[:limit]:
        mark = f"   <- {DAEMON_HINT}" if daemon else ""
        print(f"{fmt(mem):>9}  {fmt(cmprs):>9}  {pid:>6}  {label}{mark}")
else:
    groups = defaultdict(lambda: [0.0, 0.0, 0, False])
    for pid, mem, cmprs, label, daemon in rows:
        g = groups[label]
        g[0] += mem
        g[1] += cmprs
        g[2] += 1
        g[3] = g[3] or daemon
    ordered = sorted(groups.items(), key=lambda kv: kv[1][0], reverse=True)
    print(f"{'ПАМЯТЬ':>9}  {'СЖАТО':>9}  {'ПРОЦ':>4}  ПРИЛОЖЕНИЕ")
    for label, (mem, cmprs, count, daemon) in ordered[:limit]:
        mark = f"   <- {DAEMON_HINT}" if daemon else ""
        print(f"{fmt(mem):>9}  {fmt(cmprs):>9}  {count:>4}  {label}{mark}")

vm = run(["vm_stat"])
stored = re.search(r"Pages stored in compressor:\s+(\d+)", vm)
occupied = re.search(r"Pages occupied by compressor:\s+(\d+)", vm)
ratio = int(stored.group(1)) / int(occupied.group(1)) if stored and occupied and int(occupied.group(1)) else 0

print()
print("ПАМЯТЬ — полный объём процесса, сжатая часть в нём учтена в несжатом размере.")
if ratio:
    print(f"Сейчас macOS сжимает в {ratio:.1f} раза, поэтому сумма колонки больше, чем «Занято».")
print("В Stats всё сжатое — одна полоса Compressed без разбивки по процессам.")
if daemon_mb >= 512:
    print(f"\nДемоны сборки держат {fmt(daemon_mb).strip()}. Освободить: {DAEMON_HINT}")
if sim_mb >= 512:
    print(f"iOS-симулятор держит {fmt(sim_mb).strip()}. Закрыть: xcrun simctl shutdown all")
