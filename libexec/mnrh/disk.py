import glob
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import (DEV, HOME, du_bytes, gradle_build_dirs, gradle_versions_in_use, has_flag, human, paint,
                     projects, remove_under_home, run, tilde, version_key)

args = sys.argv[1:]
if has_flag(args, "-h", "--help"):
    print("mnrh disk              что можно почистить: безопасное и то, что удалять осознанно")
    print("mnrh disk clean        отметить в списке, что удалить (безопасное уже отмечено), и удалить")
    print("mnrh disk clean -y     удалить только безопасное без вопросов — для скриптов")
    print()
    print("Кеши приложений, которые сейчас открыты (Chrome, Android Studio…), пропускаются: закрой и повтори.")
    sys.exit(0)
apply = has_flag(args, "--apply") or (args[:1] == ["clean"])
assume_yes = has_flag(args, "-y", "--yes")

L = os.path.join(HOME, "Library")
SAFE, REVIEW = "safe", "review"
MIN_SHOW = 1048576
BIG = 300 * 1048576


class Item:
    def __init__(self, level, label, paths=(), hint=None, action=None, size=None, app=None):
        self.level = level
        self.label = label
        self.paths = [p for p in paths if os.path.lexists(p)]
        self.hint = hint
        self.action = action
        self.size = size
        self.app = app


items = []
covered = set()


def add(level, label, paths=(), hint=None, action=None, size=None, app=None):
    it = Item(level, label, paths, hint, action, size, app)
    covered.update(os.path.realpath(p) for p in it.paths)
    if it.paths or it.size:
        items.append(it)


def app_running(app):
    if not app:
        return False
    if app == "gradle":
        return bool(run(["pgrep", "-f", "GradleDaemon"]).strip())
    if app.startswith("avd:"):
        return bool(run(["pgrep", "-f", f"-avd {app[4:]}( |$)"]).strip())
    return bool(run(["pgrep", "-f", f"/{app}.app/Contents/MacOS/"]).strip())


def newest_keep(paths, recent_days=7):
    if not paths:
        return set()
    keep = {max(paths, key=lambda p: version_key(os.path.basename(p)))}
    cutoff = datetime.now().timestamp() - recent_days * 86400
    keep |= {p for p in paths if os.path.getmtime(p) > cutoff}
    return keep


def stale_ide_dirs(pattern_dirs, name_re):
    stale = []
    for base in pattern_dirs:
        groups = {}
        for p in glob.glob(os.path.join(base, "*")):
            m = re.match(name_re, os.path.basename(p))
            if m and os.path.isdir(p):
                groups.setdefault(m.group(1), []).append(p)
        for paths in groups.values():
            keep = newest_keep(paths)
            stale += [p for p in paths if p not in keep]
    return stale


def scan_ides():
    as_re = r"(AndroidStudio)[\d.]+$"
    jb_re = r"([A-Za-z]+?)(\d{4}\.\d+)$"
    caches = stale_ide_dirs([f"{L}/Caches/Google"], as_re) + stale_ide_dirs([f"{L}/Caches/JetBrains"], jb_re)
    logs = stale_ide_dirs([f"{L}/Logs/Google"], as_re) + stale_ide_dirs([f"{L}/Logs/JetBrains"], jb_re)
    settings = (stale_ide_dirs([f"{L}/Application Support/Google"], as_re)
                + stale_ide_dirs([f"{L}/Application Support/JetBrains"], jb_re))
    add(SAFE, "кеши старых версий Android Studio и JetBrains", caches)
    add(SAFE, "логи старых версий Android Studio и JetBrains", logs)
    add(REVIEW, "настройки старых версий Android Studio и JetBrains", settings,
        hint="плагины и настройки прошлых версий, текущая их уже перенесла")


def scan_build_outputs():
    builds = [d for proj in projects() for d in gradle_build_dirs(proj)]
    add(SAFE, f"build/ и .gradle/ в проектах {tilde(DEV)}", builds,
        hint="пересоберутся при следующей сборке")

    modules = []
    for proj in projects():
        for root, dirs, files in os.walk(proj):
            if "node_modules" in dirs:
                modules.append(os.path.join(root, "node_modules"))
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "build")]
    add(REVIEW, "node_modules в проектах", modules, hint="вернутся через npm install")


def scan_gradle():
    used = gradle_versions_in_use()
    g = os.path.join(HOME, ".gradle")
    if not used or not os.path.isdir(g):
        return
    stale = [p for p in glob.glob(os.path.join(g, "caches", "*"))
             if re.fullmatch(r"\d+\.\d+(\.\d+)?", os.path.basename(p)) and os.path.basename(p) not in used]
    stale += [p for p in glob.glob(os.path.join(g, "wrapper", "dists", "gradle-*"))
              if re.sub(r"gradle-([\d.]+)-(bin|all)", r"\1", os.path.basename(p)) not in used]
    add(SAFE, f"Gradle: версии, которые не использует ни один проект (в работе: {', '.join(sorted(used))})", stale)
    add(SAFE, "Gradle: build cache", [os.path.join(g, "caches", "build-cache-1")], app="gradle",
        hint="при запущенных демонах Gradle пропускается")
    current = [os.path.join(g, "caches", v) for v in sorted(used) if os.path.isdir(os.path.join(g, "caches", v))]
    add(REVIEW, f"Gradle {', '.join(sorted(used))}: трансформации и служебные кеши", current, app="gradle",
        hint="пересоздадутся, первая сборка будет долгой")
    add(REVIEW, "Gradle: скачанные зависимости", [os.path.join(g, "caches", "modules-2")], app="gradle",
        hint="скачаются заново при сборке, нужен интернет")


def kotlin_versions():
    found = set()
    for proj in projects():
        try:
            text = open(os.path.join(proj, "gradle", "libs.versions.toml"), encoding="utf-8").read()
        except OSError:
            continue
        m = re.search(r'^\s*kotlin\s*=\s*"([^"]+)"', text, re.M)
        if m:
            found.add(m.group(1))
    return found


def scan_konan():
    konan = os.path.join(HOME, ".konan")
    prebuilts = glob.glob(os.path.join(konan, "kotlin-native-prebuilt-*"))
    if not prebuilts:
        return
    used = kotlin_versions()
    if not used:
        return
    old = [p for p in prebuilts if not any(p.endswith("-" + v) for v in used)]
    keep = [p for p in prebuilts if p not in old]
    add(SAFE, f"Kotlin/Native старых версий (в проектах: {', '.join(sorted(used))})", old,
        hint="скачается снова, если какой-то проект вернётся на эту версию")
    props = " ".join(open(os.path.join(p, "konan", "konan.properties"), encoding="utf-8", errors="replace").read()
                     for p in keep if os.path.exists(os.path.join(p, "konan", "konan.properties")))
    if not props:
        return
    deps = [d for d in glob.glob(os.path.join(konan, "dependencies", "*"))
            if os.path.isdir(d) and os.path.basename(d) != "cache" and os.path.basename(d) not in props]
    add(SAFE, "Kotlin/Native: LLVM и инструменты от старых версий", deps,
        hint="текущим версиям Kotlin не нужны")


def scan_devices():
    devs = json.loads(run(["xcrun", "simctl", "list", "devices", "-j"]) or "{}").get("devices", {})
    for rt, lst in devs.items():
        runtime = rt.split(".")[-1].replace("-", " ", 1).replace("-", ".")
        for d in lst:
            size = d.get("dataPathSize", 0)
            if size < BIG or not d.get("isAvailable", True) or d.get("state") == "Booted":
                continue
            data = d.get("dataPath", "")
            try:
                days = int((time.time() - os.path.getmtime(data)) // 86400)
            except OSError:
                days = None
            when = f"не менялся {days} дн." if days else "менялся сегодня" if days == 0 else ""
            udid = d["udid"]
            add(REVIEW, f"симулятор {d['name']} ({runtime}): приложения и данные, {when}".rstrip(", "), size=size,
                hint="сбросится до чистого, само устройство останется",
                action=lambda u=udid: subprocess.run(["xcrun", "simctl", "erase", u], capture_output=True).returncode == 0)


def scan_avds():
    for avd in sorted(glob.glob(os.path.join(HOME, ".android", "avd", "*.avd"))):
        name = os.path.basename(avd)[:-4]
        data = glob.glob(os.path.join(avd, "userdata-qemu.img*")) + glob.glob(os.path.join(avd, "cache.img*")) + \
            glob.glob(os.path.join(avd, "snapshots")) + glob.glob(os.path.join(avd, "*.qcow2"))
        size = sum(du_bytes(p) for p in data)
        if size < BIG:
            continue
        days = int((time.time() - os.path.getmtime(avd)) // 86400)
        add(REVIEW, f"эмулятор {name}: данные и снимки Quick Boot, не запускался {days} дн." if days
            else f"эмулятор {name}: данные и снимки Quick Boot", data, size=size, app=f"avd:{name}",
            hint="как Wipe Data: приложения и вход в аккаунты сбросятся")


def scan_archives():
    found = {}
    for arc in glob.glob(os.path.join(L, "Developer", "Xcode", "Archives", "*", "*.xcarchive")):
        try:
            with open(os.path.join(arc, "Info.plist"), "rb") as f:
                info = plistlib.load(f)
        except (OSError, plistlib.InvalidFileException):
            continue
        ap = info.get("ApplicationProperties") or {}
        key = (ap.get("CFBundleIdentifier") or info.get("Name"), ap.get("CFBundleShortVersionString"))
        build = ap.get("CFBundleVersion") or "0"
        found.setdefault(key, []).append((version_key(build), info.get("CreationDate") or datetime.min, arc))
    old = []
    for key, arcs in found.items():
        arcs.sort()
        old += [a for _, _, a in arcs[:-1]]
    add(REVIEW, f"архивы Xcode: {len(old)} старых сборок тех же версий", old,
        hint="у каждой версии остаётся последняя сборка с dSYM для расшифровки падений")


FRIENDLY = {"com.openai.codex": ("Codex", "Codex"), "codex-runtimes": ("Codex: среды выполнения", "Codex"),
            "termius-updater": ("Termius: загруженные обновления", None), "com.spotify.client": ("Spotify", "Spotify"),
            "puppeteer": ("Puppeteer: браузеры для тестов", None), "mcp-devices": ("MCP mobile", None),
            "Yarn": ("Yarn", None), "ms-playwright": ("Playwright: браузеры для тестов", None),
            "com.tinyspeck.slackmacgap": ("Slack", "Slack"), "ru.keepcoder.Telegram": ("Telegram", "Telegram"),
            "com.microsoft.VSCode.ShipIt": ("VS Code: загруженные обновления", None)}
SKIP_CACHES = {"Homebrew", "pip", "org.swift.swiftpm", "CocoaPods", "JetBrains", "Google", "CloudKit"}


def scan_app_caches():
    for base in (os.path.join(L, "Caches"), os.path.join(HOME, ".cache")):
        for p in glob.glob(os.path.join(base, "*")):
            name = os.path.basename(p)
            if name in SKIP_CACHES or name.startswith("com.apple.") or name == "mnrh" or not os.path.isdir(p) \
                    or os.path.realpath(p) in covered:
                continue
            size = du_bytes(p)
            if size < BIG:
                continue
            label, app = FRIENDLY.get(name, (name, None))
            add(REVIEW, f"кеш {label}", [p], size=size, app=app, hint="приложение пересоздаст")
    google = os.path.join(L, "Caches", "Google")
    for p in glob.glob(os.path.join(google, "AndroidStudio*")):
        if os.path.realpath(p) not in covered:
            add(REVIEW, f"кеш {os.path.basename(p)}: индексы", [p], app="Android Studio",
                hint="переиндексирует проекты при следующем запуске")
    for p in glob.glob(os.path.join(L, "Caches", "JetBrains", "*")):
        if os.path.realpath(p) not in covered and du_bytes(p) >= 50 * 1048576:
            ide = re.match(r"([A-Za-z]+?)(\d{4}\.\d+)$", os.path.basename(p))
            add(REVIEW, f"кеш JetBrains {os.path.basename(p)}" + (": индексы" if ide else ""), [p],
                hint="переиндексирует при запуске" if ide else "приложение пересоздаст")
    mnrh = os.path.join(HOME, ".cache", "mnrh")
    add(SAFE, "снимки окон mnrh shot", [os.path.join(mnrh, "shots")])
    add(REVIEW, "резервные копии mnrh claude sessions slim", glob.glob(os.path.join(mnrh, "slim-backup", "*")),
        hint="после удаления сжатие уже не вернуть через --undo")



def scan_simple_caches():
    add(SAFE, "Xcode DerivedData", glob.glob(f"{L}/Developer/Xcode/DerivedData/*"),
        hint="Xcode пересоберёт индексы")
    add(SAFE, "кеш npm", [os.path.join(HOME, ".npm", "_cacache"), os.path.join(HOME, ".npm", "_npx")])
    add(SAFE, "кеши pip, SwiftPM, CocoaPods",
        [f"{L}/Caches/pip", f"{L}/Caches/org.swift.swiftpm", f"{L}/Caches/CocoaPods"])
    add(REVIEW, "кеш Chrome", [f"{L}/Caches/Google/Chrome"], app="Google Chrome", hint="вход на сайты сохранится")
    add(REVIEW, "Корзина", glob.glob(os.path.join(HOME, ".Trash", "*")) + glob.glob(os.path.join(HOME, ".Trash", ".*")),
        hint="удалится насовсем")


def scan_brew():
    if not shutil.which("brew"):
        return
    out = run(["brew", "cleanup", "-n"], timeout=120)
    m = re.search(r"free approximately ([\d.]+)\s*([KMG]B)", out)
    if m:
        size = float(m.group(1)) * {"KB": 1024, "MB": 1048576, "GB": 1073741824}[m.group(2)]
        add(SAFE, "Homebrew: старые версии и кеш загрузок", size=size,
            action=lambda: subprocess.run(["brew", "cleanup"], capture_output=True))


def scan_simulators():
    devs = json.loads(run(["xcrun", "simctl", "list", "devices", "-j"]) or "{}").get("devices", {})
    unavailable = [d for lst in devs.values() for d in lst if not d.get("isAvailable", True)]
    paths = [d["dataPath"].rsplit("/data", 1)[0] for d in unavailable if d.get("dataPath")]
    add(SAFE, f"iOS-симуляторы без runtime ({len(unavailable)})", paths,
        action=lambda: subprocess.run(["xcrun", "simctl", "delete", "unavailable"], capture_output=True))

    runtimes = json.loads(run(["xcrun", "simctl", "runtime", "list", "-j"]) or "{}")
    by_platform = {}
    for rid, r in runtimes.items():
        by_platform.setdefault(r.get("platformIdentifier", ""), []).append((rid, r))
    now = datetime.now(timezone.utc)
    for rts in by_platform.values():
        newest = max(rts, key=lambda x: version_key(x[1].get("version", "0")))[0]
        for rid, r in rts:
            if rid == newest:
                continue
            last = r.get("lastUsedAt")
            days = (now - datetime.fromisoformat(last.replace("Z", "+00:00"))).days if last else None
            if days is None or days > 60:
                when = f"не запускался {days} дн." if days is not None else "ни разу не запускался"
                add(REVIEW, f"runtime iOS {r.get('version')}, {when}", size=r.get("sizeBytes", 0),
                    hint=f"xcrun simctl runtime delete {rid}")

    ds = glob.glob(f"{L}/Developer/Xcode/iOS DeviceSupport/*")
    if len(ds) > 1:
        keep = newest_keep(ds)
        add(REVIEW, "символы старых версий iOS для отладки на устройстве", [p for p in ds if p not in keep],
            hint="скачаются снова при подключении устройства с этой версией")


for scan in (scan_ides, scan_build_outputs, scan_gradle, scan_konan, scan_simple_caches, scan_brew, scan_simulators,
             scan_devices, scan_avds, scan_archives, scan_app_caches):
    scan()

with ThreadPoolExecutor(max_workers=8) as pool:
    pending = {it: [pool.submit(du_bytes, p) for p in it.paths] for it in items if it.size is None}
    for it, futs in pending.items():
        it.size = sum(f.result() for f in futs)

items = [it for it in items if it.size and it.size >= MIN_SHOW]
safe = sorted((i for i in items if i.level == SAFE), key=lambda i: -i.size)
review = sorted((i for i in items if i.level == REVIEW), key=lambda i: -i.size)
busy = {id(it): app_running(it.app) for it in items if it.app}


def app_name(it):
    if it.app == "gradle":
        return "демоны Gradle (mnrh killdaemons)"
    if it.app.startswith("avd:"):
        return f"эмулятор {it.app[4:]}"
    return it.app


def blocked(it):
    return busy.get(id(it), False)


def show(title, group):
    if not group:
        return
    print(paint(title, "1"))
    for it in group:
        line = f"  {human(it.size):>9}  {it.label}"
        if blocked(it):
            line += paint(f"   закрой {app_name(it)}", "33")
        elif it.hint:
            line += paint(f"   {it.hint}", "2")
        print(line)
    print(f"  {human(sum(i.size for i in group)):>9}  итого\n")


def remove(it):
    if it.action:
        return it.action() is not False
    for p in it.paths:
        remove_under_home(p)
    return True


def run_cleanup(chosen):
    _, _, before = shutil.disk_usage("/System/Volumes/Data")
    done = 0
    for it in chosen:
        if blocked(it) and app_running(it.app):
            print(f"  {paint('пропущено', '33')}: {it.label} — закрой {app_name(it)} и повтори")
            continue
        ok = remove(it)
        done += ok
        print(f"  {'удалено' if ok else paint('не вышло', '31')}: {it.label}")
    after = shutil.disk_usage("/System/Volumes/Data")[2]
    print(f"\nСвободно: {human(before)} → {human(after)} (+{human(max(after - before, 0))})")


def choose():
    from menu import pick_many
    rows, order = [], []
    for title, group in (("Безопасно — пересоздаётся само", safe), ("Осознанно — прочитай пояснение", review)):
        if not group:
            continue
        rows.append((None, title))
        order.append(None)
        for it in group:
            note = f"закрой {app_name(it)}" if blocked(it) else (it.hint or "")
            rows.append((it.label, f"{human(it.size):>8}  {it.label}" + (f"  \x1b[2m{note}\x1b[0m" if note else "")))
            order.append(it)
    marked = {i for i, it in enumerate(order) if it is not None and it.level == SAFE and not blocked(it)}

    def summary(marks):
        total = sum(order[i].size for i in marks if order[i] is not None)
        return f"отмечено {len(marks)} · {human(total)}"

    picked = pick_many(rows, title="\x1b[1mmnrh disk\x1b[0m  \x1b[2mчто удалить?\x1b[0m", marked=marked, summary=summary)
    if picked is None:
        return None
    return [order[i] for i in sorted(picked) if order[i] is not None]


total, used_b, free_before = shutil.disk_usage("/System/Volumes/Data")
print(f"Свободно {human(free_before)} из {human(total)}\n")
show("Безопасно, пересоздаётся само:", safe)
show("Осознанно, прочитай пояснение:", review)

if not safe and not review:
    print("Чистить нечего.")
    sys.exit(0)
if not apply:
    print(paint("Выбрать и удалить: mnrh disk clean", "2"))
    sys.exit(0)

interactive = sys.stdin.isatty() and sys.stdout.isatty() and not assume_yes
if not interactive:
    if not assume_yes:
        print("Не терминал, выбрать нельзя. Удалить только безопасное без вопросов: mnrh disk clean -y")
        sys.exit(2)
    if not safe:
        print("Безопасно удалять нечего.")
        sys.exit(0)
    run_cleanup(safe)
    sys.exit(0)

chosen = choose()
if not chosen:
    print("Ничего не удалено.")
    sys.exit(0)
size = sum(i.size for i in chosen)
print(paint(f"Удалю {len(chosen)}, {human(size)}:", "1"))
for it in chosen:
    print(f"  {human(it.size):>9}  {it.label}")
if input("Удалить? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
    print("Отменено.")
    sys.exit(0)
run_cleanup(chosen)
