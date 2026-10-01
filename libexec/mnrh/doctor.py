import glob
import json
import os
import plistlib
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import BAD, HOME, OK, WARN, git_versions, human, has_flag, paint, run, settings_path, tilde, \
    version_key

args = sys.argv[1:]
if has_flag(args, "-h", "--help"):
    print("mnrh doctor          проверка системы: диск, память, оболочка, ссылки, автозапуск, безопасность")
    print("mnrh doctor --full   ещё обновления macOS и целостность Homebrew (дольше, нужна сеть)")
    sys.exit(0)
full = has_flag(args, "--full")

report = []
counts = {"bad": 0, "warn": 0}


def section(title):
    report.append(("section", title, None))


def item(level, text, hint=None):
    if level == "bad":
        counts["bad"] += 1
    elif level == "warn":
        counts["warn"] += 1
    report.append((level, text, hint))


def check_disk():
    section("Диск")
    total, used, free = shutil.disk_usage("/System/Volumes/Data")
    pct = free / total * 100
    text = f"свободно {human(free)} из {human(total)} ({pct:.0f}%)"
    if free < 10 * 1073741824 or pct < 5:
        item("bad", text, "mnrh disk")
    elif free < 30 * 1073741824 or pct < 15:
        item("warn", text, "mnrh disk")
    else:
        item("ok", text)


def top_mem():
    out = run(["top", "-l", "1", "-stats", "pid,mem"])
    units = {"B": 1, "K": 1024, "M": 1048576, "G": 1073741824}
    mem = {}
    started = False
    for line in out.splitlines():
        if line.startswith("PID"):
            started = True
            continue
        cols = line.split()
        if started and len(cols) >= 2:
            m = re.match(r"([\d.]+)([BKMG])", cols[1])
            if m:
                mem[cols[0]] = float(m.group(1)) * units[m.group(2)]
    return mem


def check_memory():
    section("Память")
    swap = re.search(r"used = ([\d.]+)M", run(["sysctl", "-n", "vm.swapusage"]))
    swap_b = float(swap.group(1)) * 1048576 if swap else 0
    if swap_b > 6 * 1073741824:
        item("bad", f"swap занят на {human(swap_b)}", "mnrh ram")
    elif swap_b > 3 * 1073741824:
        item("warn", f"swap занят на {human(swap_b)}", "mnrh ram")
    else:
        item("ok", f"swap {human(swap_b)}")

    daemons = [l.split()[0] for l in run(["ps", "-axo", "pid=,command="]).splitlines()
               if re.search(r"GradleDaemon|KotlinCompileDaemon|GradleWorkerMain", l)]
    if daemons:
        mem = top_mem()
        total = sum(mem.get(p, 0) for p in daemons)
        level = "warn" if total > 1073741824 else "ok"
        item(level, f"демоны сборки: {len(daemons)}, держат {human(total)}", "mnrh killdaemons" if level == "warn" else None)
    else:
        item("ok", "демонов сборки нет")

    booted = [l.strip() for l in run(["xcrun", "simctl", "list", "devices", "booted"]).splitlines() if "(Booted)" in l]
    if booted:
        names = ", ".join(re.sub(r"\s*\(.*", "", b) for b in booted)
        item("warn", f"запущен iOS-симулятор: {names}", "xcrun simctl shutdown all")


def check_shell():
    section("Оболочка")
    times = []
    for _ in range(3):
        start = time.time()
        run(["/bin/zsh", "-i", "-c", "exit"], timeout=20)
        times.append(time.time() - start)
    avg = sorted(times)[1]
    text = f"запуск zsh {avg:.2f} с"
    if avg > 1.5:
        item("bad", text, "профилируй ~/.zshrc: nvm, rbenv, completion")
    elif avg > 0.5:
        item("warn", text, "профилируй ~/.zshrc: nvm, rbenv, completion")
    else:
        item("ok", text)

    env = {"HOME": HOME, "TERM": "xterm", "SHELL": "/bin/zsh"}
    path = run(["/bin/zsh", "-l", "-i", "-c", "echo $PATH"], timeout=20, env=env).strip().splitlines()
    entries = path[-1].split(":") if path else []
    dead = [p for p in entries if p and not os.path.isdir(p) and "cryptexd" not in p]
    dups = sorted({p for p in entries if entries.count(p) > 1})
    if dead:
        item("warn", f"в PATH несуществующие каталоги: {', '.join(tilde(p) for p in dead)}")
    if dups:
        item("warn", f"в PATH дубликаты: {', '.join(tilde(p) for p in dups)}")
    if not dead and not dups:
        item("ok", f"PATH чистый, {len(entries)} элементов")
    gits = git_versions()
    if gits:
        first_path, first = gits[0]
        system = next((v for p, v in gits if p == "/usr/bin/git"), None)
        if system and first_path != "/usr/bin/git" and version_key(first) < version_key(system):
            hint = "sudo /usr/local/git/uninstall.sh" if os.path.exists("/usr/local/git/uninstall.sh") else "убери его из PATH"
            item("warn", f"в PATH первым старый git {first} ({tilde(first_path)}), системный — {system}", hint)


def check_links():
    section("Ссылки")
    found = []
    for base in (os.path.join(HOME, ".local/bin"), os.path.join(HOME, ".claude/skills"), "/opt/homebrew/bin"):
        for p in glob.glob(os.path.join(base, "*")) + glob.glob(os.path.join(base, ".*")):
            if os.path.islink(p) and not os.path.exists(p):
                found.append(p)
    for p in found:
        item("warn", f"битая ссылка {tilde(p)} -> {tilde(os.readlink(p))}", f"rm {tilde(p)}")
    if not found:
        item("ok", "битых ссылок в ~/.local/bin, ~/.claude/skills и Homebrew нет")


def check_claude():
    section("Claude Code")
    cfg_path = os.path.join(HOME, ".claude.json")
    if not os.path.exists(cfg_path):
        item("ok", "~/.claude.json нет")
        return
    mode = os.stat(cfg_path).st_mode & 0o777
    try:
        cfg = json.load(open(cfg_path, encoding="utf-8"))
    except (OSError, ValueError):
        item("bad", "~/.claude.json не читается как JSON")
        return
    has_secret = "Bearer" in json.dumps(cfg.get("mcpServers", {}))
    if mode & 0o077 and has_secret:
        item("bad", f"~/.claude.json с токенами читается другими пользователями ({oct(mode)})", "chmod 600 ~/.claude.json")
    broken = []
    for name, srv in cfg.get("mcpServers", {}).items():
        cmd = srv.get("command")
        if cmd and cmd.startswith("/") and not os.path.exists(cmd):
            broken.append(name)
    for name in broken:
        item("warn", f"MCP-сервер {name}: команда не существует", f"claude mcp remove {name} --scope user")
    dead_projects = [k for k in cfg.get("projects", {}) if not os.path.isdir(k)]
    if dead_projects:
        item("warn", f"в реестре проектов {len(dead_projects)} записей на несуществующие каталоги")
    dead_repos = sum(1 for v in cfg.get("githubRepoPaths", {}).values() for p in v if not os.path.isdir(p))
    if dead_repos:
        item("warn", f"в карте репозиториев {dead_repos} мёртвых путей")
    if not broken and not dead_projects and not dead_repos and not (mode & 0o077 and has_secret):
        item("ok", f"конфиг в порядке, MCP-серверов: {len(cfg.get('mcpServers', {}))}")


def program_of(plist):
    try:
        with open(plist, "rb") as f:
            data = plistlib.load(f)
    except Exception:
        return None
    prog = data.get("Program")
    if not prog and data.get("ProgramArguments"):
        prog = data["ProgramArguments"][0]
    return prog


def check_launchd():
    section("Автозапуск")
    orphans = []
    dirs = [os.path.join(HOME, "Library/LaunchAgents"), "/Library/LaunchAgents", "/Library/LaunchDaemons"]
    total = 0
    for d in dirs:
        for plist in glob.glob(os.path.join(d, "*.plist")):
            total += 1
            prog = program_of(plist)
            if prog and prog.startswith("/") and not os.path.exists(prog):
                orphans.append((plist, prog))
    for plist, prog in orphans:
        item("warn", f"служба-сирота {os.path.basename(plist)}: нет {tilde(prog)}")
    if not orphans:
        item("ok", f"служб: {total}, у всех программа на месте")


def check_security():
    section("Безопасность")
    fw = run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    if "disabled" in fw:
        item("warn", "файрвол выключен", settings_path("settings", "network", "firewall"))
    elif fw:
        item("ok", "файрвол включён")
    fv = run(["fdesetup", "status"])
    if "Off" in fv:
        item("warn", "FileVault выключен, диск не зашифрован", settings_path("settings", "privacy"))
    elif "On" in fv:
        item("ok", "FileVault включён")
    sip = run(["csrutil", "status"])
    if "enabled" in sip:
        item("ok", "SIP включён")
    elif sip:
        item("bad", "SIP выключен")
    tm = run(["tmutil", "destinationinfo"])
    if "No destinations" in tm or not tm.strip():
        item("warn", "Time Machine не настроен, бэкапов нет")
    else:
        item("ok", "Time Machine настроен")
    if "NOPASSWD: ALL" in run(["sudo", "-n", "-l"], timeout=5):
        item("warn", "sudo работает без пароля для всех команд")
    if "assessments enabled" in run(["spctl", "--status"]):
        item("ok", "Gatekeeper включён")
    else:
        item("bad", "Gatekeeper выключен: запускается любая неподписанная программа", "sudo spctl --master-enable")
    listening = {l.rsplit(".", 1)[-1] for l in (line.split()[3] for line in run(["netstat", "-anp", "tcp"]).splitlines()
                                                 if line.endswith("LISTEN") and len(line.split()) > 3)}
    shared = [name for port, name in (("22", "удалённый вход по SSH"), ("5900", "общий экран"),
                                      ("3283", "удалённое управление"), ("445", "общие файлы (SMB)"),
                                      ("548", "общие файлы (AFP)")) if port in listening]
    if shared:
        item("warn", "открыт доступ из сети: " + ", ".join(shared), "Настройки -> Основные -> Общий доступ")
    su = run(["defaults", "read", "/Library/Preferences/com.apple.SoftwareUpdate"])
    if re.search(r"CriticalUpdateInstall = 0", su) or re.search(r"ConfigDataInstall = 0", su):
        item("warn", "обновления безопасности не ставятся сами",
             "Настройки -> Основные -> Обновление ПО -> Автоматические обновления")
    exts = re.findall(r"^\s*\*?\s*\*\s+\S+\s+\S+ \([^)]*\)\s+(.+?)\s+\[([^\]]+)\]", run(["systemextensionsctl", "list"]), re.M)
    if exts:
        item("ok", "системные расширения: " + ", ".join(f"{n}" + (" (ждёт разрешения)" if "waiting" in st else "")
                                                     for n, st in exts))
    if "MDM enrollment: Yes" in run(["profiles", "status", "-type", "enrollment"]):
        item("warn", "Mac под управлением MDM: организация может менять настройки и ставить программы")


def check_battery():
    out = run(["system_profiler", "SPPowerDataType"], timeout=20)
    cycles = re.search(r"Cycle Count:\s*(\d+)", out)
    capacity = re.search(r"Maximum Capacity:\s*(\d+)", out)
    if not cycles:
        return
    section("Аккумулятор")
    c, cap = int(cycles.group(1)), int(capacity.group(1)) if capacity else None
    text = f"{c} циклов" + (f", ёмкость {cap}%" if cap is not None else "")
    if (cap is not None and cap < 80) or c > 1000:
        item("warn", text, "пора планировать замену")
    else:
        item("ok", text)


def check_full():
    section("Обновления")
    upd = run(["softwareupdate", "--list"], timeout=180)
    titles = re.findall(r"Title: ([^,]+), Version: ([^,]+)", upd)
    if titles:
        for t, v in titles:
            item("warn", f"доступно: {t} {v}")
    else:
        item("ok", "macOS обновлён")
    if shutil.which("brew"):
        missing = run(["brew", "missing"], timeout=120).strip()
        if missing:
            item("warn", f"brew: не хватает зависимостей: {missing}", "brew install <пакет>")
        else:
            item("ok", "brew: зависимости на месте")


for fn in (check_disk, check_memory, check_shell, check_links, check_claude, check_launchd, check_security, check_battery):
    fn()
if full:
    check_full()

marks = {"ok": OK, "warn": WARN, "bad": BAD}
for level, text, hint in report:
    if level == "section":
        print(f"\n{paint(text, '1')}")
        continue
    line = f"  {marks[level]} {text}"
    if hint:
        line += paint(f"   -> {hint}", "2")
    print(line)

print()
summary = f"Проблем: {counts['bad']}, предупреждений: {counts['warn']}"
print(paint(summary, "31" if counts["bad"] else ("33" if counts["warn"] else "32")))
if not full:
    print(paint("Обновления macOS и Homebrew: mnrh doctor --full", "2"))
sys.exit(1 if counts["bad"] else 0)
