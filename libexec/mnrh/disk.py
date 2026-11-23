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
from mnrhlib import (DEV, HOME, RECENT_DAYS, android_sdk, du_bytes, gradle_build_dirs, gradle_versions_in_use, has_flag, human,
                     konan_prebuilts, konan_unused, kotlin_versions_in_use, last_change, orphan_paths, paint, projects,
                     remove_under_home, run, tilde, version_key)

args = sys.argv[1:]
if has_flag(args, "-h", "--help"):
    print("mnrh disk              меню с действиями (без терминала — то же, что status)")
    print("mnrh disk status       что можно почистить: безопасное и то, что удалять осознанно")
    print("mnrh disk clean        отметить в списке, что удалить (безопасное уже отмечено), и удалить")
    print("mnrh disk clean -y     без вопросов удалить безопасное, что в меню отмечено заранее, для скриптов")
    print("mnrh disk top          куда ушло место на всем диске, по папкам")
    print("mnrh disk top <папка>  что внутри папки, по убыванию; -n 30 показать больше строк (по умолчанию 15)")
    print()
    print("Крупные пункты разбиты на части: build/ по проектам, DerivedData, версии Gradle и Kotlin/Native.")
    print("build/ и DerivedData заранее отмечены только у проектов, где давно ничего не менялось.")
    print()
    print("Кеши приложений, которые сейчас открыты (Chrome, Android Studio…), пропускаются: закрой и повтори.")
    sys.exit(0)
apply = has_flag(args, "--apply") or (args[:1] == ["clean"])
assume_yes = has_flag(args, "-y", "--yes")

L = os.path.join(HOME, "Library")
SAFE, REVIEW = "safe", "review"
MIN_SHOW = 1048576
BIG = 300 * 1048576


IDLE_DAYS = 30
SHOW_PARTS = 5


class Item:
    def __init__(self, level, label, paths=(), hint=None, action=None, size=None, app=None, group=None, marked=None):
        self.level = level
        self.label = label
        self.paths = [p for p in paths if os.path.lexists(p)]
        self.hint = hint
        self.action = action
        self.size = size
        self.app = app
        self.group = group
        self.marked = level == SAFE if marked is None else marked


items = []
covered = set()
group_hints = {}


def add(level, label, paths=(), hint=None, action=None, size=None, app=None, group=None, marked=None):
    it = Item(level, label, paths, hint, action, size, app, group, marked)
    covered.update(os.path.realpath(p) for p in it.paths)
    if it.paths or it.size:
        items.append(it)


def add_group(level, title, parts, hint=None, app=None):
    """Крупный пункт по частям: каждая часть отмечается в меню отдельно. parts: (подпись, пути, отметить ли)."""
    group_hints[title] = hint
    for label, paths, marked in parts:
        add(level, label, paths, app=app, group=title, marked=marked)


def days_since(ts):
    return max(int((time.time() - ts) // 86400), 0)


def ago(days):
    return "сегодня" if days == 0 else "вчера" if days == 1 else f"{days} дн. назад"


def project_idle_days(proj):
    """Сколько дней в проекте ничего не происходило: последний коммит или изменение индекса git."""
    times = []
    ct = run(["git", "-C", proj, "log", "-1", "--format=%ct"], timeout=10).strip()
    if ct.isdigit():
        times.append(int(ct))
    for name in ("index", "HEAD"):
        try:
            times.append(os.path.getmtime(os.path.join(proj, ".git", name)))
        except OSError:
            pass
    return days_since(max(times)) if times else None


def app_running(app):
    if not app:
        return False
    if app == "gradle":
        return bool(run(["pgrep", "-f", "GradleDaemon"]).strip())
    if app.startswith("avd:"):
        return bool(run(["pgrep", "-f", f"-avd {app[4:]}( |$)"]).strip())
    if app == "instruments":
        return bool(run(["pgrep", "-f", "/Instruments.app/Contents/MacOS/|/usr/bin/xctrace "]).strip())
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
    by_version = {}
    for p in caches + logs:
        by_version.setdefault(os.path.basename(p), []).append(p)
    add_group(SAFE, "кеши и логи старых версий Android Studio и JetBrains",
              [(name, paths, True) for name, paths in sorted(by_version.items())])
    add_group(REVIEW, "настройки старых версий Android Studio и JetBrains",
              [(os.path.basename(p), [p], False) for p in sorted(settings)],
              hint="плагины и настройки прошлых версий, текущая их уже перенесла")


def scan_build_outputs():
    builds, modules = [], []
    for proj in projects():
        name = os.path.basename(proj)
        idle = project_idle_days(proj)
        when = f", последний коммит {ago(idle)}" if idle is not None else ""
        stale = idle is None or idle >= IDLE_DAYS
        dirs = gradle_build_dirs(proj)
        if dirs:
            builds.append((name + when, dirs, stale))
        found = []
        for root, subdirs, files in os.walk(proj):
            if "node_modules" in subdirs:
                found.append(os.path.join(root, "node_modules"))
            subdirs[:] = [d for d in subdirs if d not in ("node_modules", ".git", "build")]
        if found:
            modules.append((name + when, found, False))
    add_group(SAFE, f"build/ и .gradle/ в проектах {tilde(DEV)}", builds,
              hint=f"пересоберутся при следующей сборке, заранее отмечены проекты без коммитов {IDLE_DAYS}+ дн.")
    add_group(REVIEW, "node_modules в проектах", modules, hint="вернутся через npm install")


def scan_gradle():
    used = gradle_versions_in_use()
    g = os.path.join(HOME, ".gradle")
    if not used or not os.path.isdir(g):
        return
    stale = {}
    for p in glob.glob(os.path.join(g, "caches", "*")):
        v = os.path.basename(p)
        if re.fullmatch(r"\d+\.\d+(\.\d+)?", v) and v not in used:
            stale.setdefault(v, []).append(p)
    for p in glob.glob(os.path.join(g, "wrapper", "dists", "gradle-*")):
        v = re.sub(r"gradle-([\d.]+)-(bin|all)", r"\1", os.path.basename(p))
        if v not in used:
            stale.setdefault(v, []).append(p)
    add_group(SAFE, f"Gradle: версии, которые не использует ни один проект (в работе: {', '.join(sorted(used))})",
              [(f"Gradle {v}", paths, True) for v, paths in sorted(stale.items(), key=lambda kv: version_key(kv[0]))])
    add(SAFE, "Gradle: build cache", [os.path.join(g, "caches", "build-cache-1")], app="gradle",
        hint="при запущенных демонах Gradle пропускается")
    current = [os.path.join(g, "caches", v) for v in sorted(used) if os.path.isdir(os.path.join(g, "caches", v))]
    add(REVIEW, f"Gradle {', '.join(sorted(used))}: трансформации и служебные кеши", current, app="gradle",
        hint="пересоздадутся, первая сборка будет долгой")
    add(REVIEW, "Gradle: скачанные зависимости", [os.path.join(g, "caches", "modules-2")], app="gradle",
        hint="скачаются заново при сборке, нужен интернет")


def scan_konan():
    used = kotlin_versions_in_use()
    if not used or not konan_prebuilts():
        return
    old, deps = konan_unused(used)
    add_group(SAFE, f"Kotlin/Native старых версий (в проектах: {', '.join(sorted(used))})",
              [(os.path.basename(p).replace("kotlin-native-prebuilt-", "Kotlin/Native "), [p], True)
               for p in sorted(old, key=lambda p: version_key(os.path.basename(p)))],
              hint="скачается снова, если какой-то проект вернется на эту версию")
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


AVD_IDLE_DAYS = 60


def avd_last_run(avd):
    """Эмулятор переписывает файлы в своей папке при каждом запуске: берем самый свежий из верхнего уровня."""
    times = [os.path.getmtime(avd)]
    for f in os.listdir(avd):
        try:
            times.append(os.path.getmtime(os.path.join(avd, f)))
        except OSError:
            pass
    return max(times)


def scan_avds():
    avd_dir = os.path.join(HOME, ".android", "avd")
    images, idle = set(), []
    for avd in sorted(glob.glob(os.path.join(avd_dir, "*.avd"))):
        name = os.path.basename(avd)[:-4]
        try:
            config = open(os.path.join(avd, "config.ini"), encoding="utf-8", errors="replace").read()
        except OSError:
            config = ""
        images |= {m.strip().rstrip("/") for m in re.findall(r"^image\.sysdir\.\d+\s*=\s*(.+)$", config, re.M)}
        days = days_since(avd_last_run(avd))
        if days >= AVD_IDLE_DAYS:
            idle.append((f"{name}, не запускался {days} дн.", [avd, os.path.join(avd_dir, name + ".ini")], name))
        data = glob.glob(os.path.join(avd, "userdata-qemu.img*")) + glob.glob(os.path.join(avd, "cache.img*")) + \
            glob.glob(os.path.join(avd, "snapshots")) + glob.glob(os.path.join(avd, "*.qcow2"))
        size = sum(du_bytes(p) for p in data)
        if size < BIG:
            continue
        add(REVIEW, f"эмулятор {name}: данные и снимки Quick Boot, не запускался {days} дн." if days
            else f"эмулятор {name}: данные и снимки Quick Boot", data, size=size, app=f"avd:{name}",
            hint="как Wipe Data: приложения и вход в аккаунты сбросятся")
    title = f"эмуляторы Android, которые не запускались {AVD_IDLE_DAYS}+ дн."
    group_hints[title] = "удалятся целиком, создать заново можно в Device Manager"
    for label, paths, name in idle:
        add(REVIEW, label, paths, group=title, marked=False, app=f"avd:{name}")

    sdk = android_sdk()
    if not sdk or not os.path.isdir(os.path.join(avd_dir)):
        return
    unused = []
    for p in sorted(glob.glob(os.path.join(sdk, "system-images", "*", "*", "*"))):
        rel = os.path.relpath(p, sdk)
        if os.path.isdir(p) and rel not in images:
            unused.append((rel.replace("system-images/", "", 1), [p], False))
    add_group(REVIEW, "образы Android, которые не нужны ни одному эмулятору", unused,
              hint="скачаются снова, если создать эмулятор на этом образе")


def scan_archives():
    found, names = {}, {}
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
        names[key] = info.get("Name") or os.path.basename(arc).rsplit(" ", 2)[0]
    parts = []
    for (bundle, version), arcs in sorted(found.items(), key=lambda kv: str(kv[0])):
        arcs.sort()
        old = [a for _, _, a in arcs[:-1]]
        if old:
            parts.append((f"{names[(bundle, version)]} {version or ''}".strip() + f": старых сборок {len(old)}",
                          old, False))
    add_group(REVIEW, "архивы Xcode: старые сборки тех же версий", parts,
              hint="у каждой версии остается последняя сборка с dSYM для расшифровки падений")


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
    add(REVIEW, "резервные копии claudemeister sessions slim", glob.glob(os.path.join(mnrh, "slim-backup", "*")),
        hint="после удаления сжатие уже не вернуть через --undo")



def scan_simple_caches():
    derived = []
    for p in sorted(glob.glob(f"{L}/Developer/Xcode/DerivedData/*")):
        name = re.sub(r"-[a-z]{28}$", "", os.path.basename(p))
        try:
            idle = days_since(os.path.getmtime(p))
        except OSError:
            idle = None
        derived.append((f"{name}, менялся {ago(idle)}" if idle is not None else name, [p],
                        idle is None or idle >= 7))
    add_group(SAFE, "Xcode DerivedData", derived,
              hint="Xcode пересоберет и переиндексирует, заранее отмечено то, что не менялось 7+ дн.")
    add(SAFE, "кеш npm", [os.path.join(HOME, ".npm", "_cacache"), os.path.join(HOME, ".npm", "_npx")])
    add(SAFE, "кеши pip, SwiftPM, CocoaPods",
        [f"{L}/Caches/pip", f"{L}/Caches/org.swift.swiftpm", f"{L}/Caches/CocoaPods"])
    add(REVIEW, "кеш Chrome", [f"{L}/Caches/Google/Chrome"], app="Google Chrome", hint="вход на сайты сохранится")
    add(REVIEW, "Корзина", glob.glob(os.path.join(HOME, ".Trash", "*")) + glob.glob(os.path.join(HOME, ".Trash", ".*")),
        hint="удалится насовсем")


def brew_frees(out):
    m = re.search(r"free approximately ([\d.]+)\s*([KMG]B)", out)
    return float(m.group(1)) * {"KB": 1024, "MB": 1048576, "GB": 1073741824}[m.group(2)] if m else 0


def scan_brew():
    if not shutil.which("brew"):
        return
    env = dict(os.environ, HOMEBREW_NO_AUTOREMOVE="1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(run, ["brew", "cleanup", "-n"], 120, env)
        every = pool.submit(run, ["brew", "cleanup", "-n", "--prune=all"], 120, env)
        size, whole = brew_frees(old.result()), brew_frees(every.result())
    if size:
        add(SAFE, "Homebrew: старые версии и кеш загрузок", size=size,
            action=lambda: subprocess.run(["brew", "cleanup"], capture_output=True, env=env))
    if whole - size >= MIN_SHOW:
        add(REVIEW, "Homebrew: загрузки текущих версий", size=whole - size,
            hint="нужны, только чтобы переустановить без интернета",
            action=lambda: subprocess.run(["brew", "cleanup", "--prune=all"], capture_output=True, env=env))


USER_CACHE = os.path.realpath(run(["getconf", "DARWIN_USER_CACHE_DIR"], timeout=5).strip() or "/nonexistent")
USER_TEMP = os.path.realpath(run(["getconf", "DARWIN_USER_TEMP_DIR"], timeout=5).strip() or "/nonexistent")
TRACE_SPOTS = [(DEV, 5), (os.path.join(HOME, "Desktop"), 2), (os.path.join(HOME, "Downloads"), 2),
               (os.path.join(HOME, "Documents"), 2), ("/private/tmp", 2)]


def find_traces():
    """Записи Instruments: пакеты .trace и файлы .ktrace там, куда их обычно сохраняют."""
    found = []
    for base, depth in TRACE_SPOTS:
        base_depth = base.rstrip("/").count("/")
        for root, dirs, files in os.walk(base):
            found += [os.path.join(root, d) for d in dirs if d.endswith(".trace")]
            found += [os.path.join(root, f) for f in files if f.endswith(".ktrace")]
            dirs[:] = [d for d in dirs if not d.endswith(".trace") and not d.startswith(".")
                       and d not in ("node_modules", "build", "Pods", "DerivedData")
                       and root.count("/") - base_depth < depth]
    return found


INSTALLERS = (".dmg", ".pkg", ".xip", ".iso")
TO_TRASH = set()
FILE_DAYS = 14
FILE_MIN = 100 * 1048576


def last_used(path):
    """Когда файл открывали в последний раз или меняли, что позже."""
    when = os.path.getmtime(path)
    used = run(["mdls", "-name", "kMDItemLastUsedDate", "-raw", path], timeout=10).strip()
    try:
        when = max(when, datetime.strptime(used[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        pass
    return when


def to_trash(path):
    trash = os.path.join(HOME, ".Trash")
    name = os.path.basename(path)
    target = os.path.join(trash, name)
    n = 1
    while os.path.lexists(target):
        stem, ext = os.path.splitext(name)
        target = os.path.join(trash, f"{stem} {n}{ext}")
        n += 1
    os.makedirs(trash, exist_ok=True)
    os.rename(path, target)


def scan_downloads():
    installers, other = [], []
    for base in (os.path.join(HOME, "Downloads"), os.path.join(HOME, "Desktop")):
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".") and not d.endswith((".app", ".photoslibrary"))
                       and root == base]
            for f in files:
                p = os.path.join(root, f)
                try:
                    if os.path.islink(p) or os.path.getsize(p) < FILE_MIN:
                        continue
                    days = days_since(last_used(p))
                except OSError:
                    continue
                if days < FILE_DAYS:
                    continue
                (installers if f.lower().endswith(INSTALLERS) else other).append((p, days))
    for title, found, marked in (("установщики в Загрузках и на Рабочем столе", installers, True),
                                 ("крупные файлы в Загрузках и на Рабочем столе", other, False)):
        group_hints[title] = f"не открывались {FILE_DAYS}+ дн., уйдут в Корзину"
        TO_TRASH.add(title)
        for p, days in found:
            add(REVIEW, f"{tilde(p)}, открывался {ago(days)}", [p], group=title, marked=marked,
                action=lambda path=p: to_trash(path))


def scan_orphans():
    """Данные в ~/Library приложений, которых уже нет. Свежие (менялись за неделю) чем-то используются, их не трогаем."""
    now = time.time()
    found = [p for p in orphan_paths() if now - last_change(p) >= RECENT_DAYS * 86400]
    title = "остатки удаленных приложений в ~/Library"
    group_hints[title] = "приложений с такими bundle id нет, уйдут в Корзину"
    TO_TRASH.add(title)
    for p in found:
        add(REVIEW, tilde(p), [p], group=title, marked=False, action=lambda path=p: to_trash(path))


def scan_dev_caches():
    add(SAFE, "кеш модулей clang", [os.path.join(USER_CACHE, "clang", "ModuleCache")],
        hint="Xcode и clang соберут модули заново при следующей сборке")
    add(SAFE, "временные файлы Instruments", glob.glob(os.path.join(USER_TEMP, "instruments*.ktrace")),
        app="instruments", hint="остаются после профилирования, одна запись бывает на 5-20 ГБ")
    traces = find_traces()
    add_group(REVIEW, "записи Instruments (.trace, .ktrace)",
              [(f"{tilde(p)}, {ago(days_since(os.path.getmtime(p)))}", [p], False) for p in traces],
              hint="удаляются насовсем, нужные цифры перенеси до этого", app="instruments")
    go = shutil.which("go")
    if go:
        gocache, modcache = (run([go, "env", "GOCACHE", "GOMODCACHE"], timeout=20).splitlines() + ["", ""])[:2]
        if gocache:
            add(SAFE, "Go: кеш сборки", [gocache],
                action=lambda: subprocess.run([go, "clean", "-cache"], capture_output=True).returncode == 0)
        if modcache:
            add(REVIEW, "Go: скачанные модули", [modcache], hint="скачаются заново при сборке, нужен интернет",
                action=lambda: subprocess.run([go, "clean", "-modcache"], capture_output=True).returncode == 0)
    pub = os.environ.get("PUB_CACHE") or os.path.join(HOME, ".pub-cache")
    add(REVIEW, "Dart и Flutter: скачанные пакеты", [pub], hint="вернутся через flutter pub get, нужен интернет")


PLATFORMS = {"iphonesimulator": "iOS", "watchsimulator": "watchOS", "appletvsimulator": "tvOS", "xrsimulator": "visionOS"}


def scan_simulators():
    devs = json.loads(run(["xcrun", "simctl", "list", "devices", "-j"]) or "{}").get("devices", {})
    unavailable = [d for lst in devs.values() for d in lst if not d.get("isAvailable", True)]
    paths = [d["dataPath"].rsplit("/data", 1)[0] for d in unavailable if d.get("dataPath")]
    add(SAFE, f"iOS-симуляторы без runtime ({len(unavailable)})", paths,
        action=lambda: subprocess.run(["xcrun", "simctl", "delete", "unavailable"], capture_output=True))

    runtimes = json.loads(run(["xcrun", "simctl", "runtime", "list", "-j"]) or "{}")
    count = {rt: len(lst) for rt, lst in devs.items() if lst}
    by_platform = {}
    for rid, r in runtimes.items():
        by_platform.setdefault(r.get("platformIdentifier", ""), []).append((rid, r))
    now = datetime.now(timezone.utc)
    for platform, rts in by_platform.items():
        name = next((n for key, n in PLATFORMS.items() if key in platform), "")
        # Оставляем самый новый runtime. Сборок одной версии бывает несколько (iOS-27-2 и iOS-27-2-iPhone19-4
        # под одно устройство), тогда ту, на которой больше симуляторов.
        newest = max(rts, key=lambda x: (version_key(x[1].get("version", "0")),
                                         count.get(x[1].get("runtimeIdentifier"), 0),
                                         version_key(x[1].get("build", ""))))[0]
        for rid, r in rts:
            if rid == newest or r.get("deletable") is False:
                continue
            last = r.get("lastUsedAt")
            days = (now - datetime.fromisoformat(last.replace("Z", "+00:00"))).days if last else None
            if days is None or days > 60:
                when = f"не запускался {days} дн." if days is not None else "ни разу не запускался"
                n = count.get(r.get("runtimeIdentifier"), 0)
                when += f", симуляторов на нем {n}" if n else ", симуляторов на нем нет"
                add(REVIEW, f"runtime {name} {r.get('version')} ({r.get('build')}), {when}".replace("  ", " "),
                    size=r.get("sizeBytes", 0),
                    hint="симуляторы этой версии перестанут запускаться, скачать заново можно в Xcode",
                    action=lambda r_id=rid: subprocess.run(["xcrun", "simctl", "runtime", "delete", r_id],
                                                           capture_output=True, timeout=600).returncode == 0)

    ds = glob.glob(f"{L}/Developer/Xcode/iOS DeviceSupport/*")
    if len(ds) > 1:
        keep = newest_keep(ds)
        add(REVIEW, "символы старых версий iOS для отладки на устройстве", [p for p in ds if p not in keep],
            hint="скачаются снова при подключении устройства с этой версией")


def size_col(n):
    return paint(f"{human(n):>9}", "36")


def sizes_of(paths):
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(paths, pool.map(du_bytes, paths)))


def children(path):
    try:
        return [os.path.join(path, n) for n in os.listdir(path) if not os.path.islink(os.path.join(path, n))]
    except OSError:
        return []


def top_lines(sizes, limit, indent="  ", min_size=MIN_SHOW):
    ordered = sorted(sizes.items(), key=lambda kv: -kv[1])
    shown = [(p, n) for p, n in ordered[:limit] if n >= min_size]
    for p, n in shown:
        print(f"{indent}{size_col(n)}  {os.path.basename(p)}{'/' if os.path.isdir(p) else ''}")
    rest = sum(n for _, n in ordered[len(shown):])
    if rest >= min_size:
        print(paint(f"{indent}{human(rest):>9}  остальное ({len(ordered) - len(shown)})", "2"))


def top_limit():
    if "-n" in args:
        try:
            return int(args[args.index("-n") + 1])
        except (IndexError, ValueError):
            sys.exit("mnrh disk top: после -n нужно число")
    return 15


def disk_top():
    rest = [a for a in args[1:] if not a.startswith("-") and not a.isdigit()]
    limit = top_limit()
    if rest:
        path = os.path.abspath(os.path.expanduser(rest[0]))
        if not os.path.isdir(path):
            sys.exit(f"mnrh disk top: нет такой папки {rest[0]}")
        print(paint(f"{tilde(path)}, считаю размеры…", "2"))
        found = sizes_of(children(path))
        top_lines(found, limit)
        print(paint(f"  {human(sum(found.values())):>9}  всего", "1"))
        print(paint("Глубже: mnrh disk top <папка>. Что из этого можно почистить: mnrh disk", "2"))
        return
    total, used, free = shutil.disk_usage("/System/Volumes/Data")
    print(f"Занято {human(used)} из {human(total)}, свободно {human(free)}")
    print(paint("Считаю размеры, это займет около минуты…", "2") + "\n")
    lib = os.path.join(HOME, "Library")
    # Папки с разбивкой считаем по детям за один проход: итог папки = сумма детей.
    split = {HOME: [c for c in children(HOME) if c != lib], lib: children(lib),
             "/Applications": children("/Applications"), "/Library": children("/Library")}
    whole = [("/opt/homebrew", "Homebrew /opt/homebrew"), ("/usr/local", "/usr/local"),
             ("/private/var/folders", "временные файлы /private/var/folders"),
             ("/private/var/vm", "swap и образ сна /private/var/vm"), ("/Users/Shared", "/Users/Shared")]
    whole = [(p, label) for p, label in whole if os.path.isdir(p)]
    found = sizes_of([c for kids in split.values() for c in kids] + [p for p, _ in whole])
    part = {d: {c: found[c] for c in kids} for d, kids in split.items()}
    part[HOME][lib] = sum(part[lib].values())
    big = {d: sum(v.values()) for d, v in part.items() if d != lib}
    big.update({p: found[p] for p, _ in whole})
    labels = {HOME: "домашняя папка ~", "/Applications": "/Applications", "/Library": "/Library",
              **{p: label for p, label in whole}}
    for p in sorted(big, key=lambda d: -big[d]):
        if big[p] < MIN_SHOW:
            continue
        print(f"{size_col(big[p])}  {paint(labels[p], '1')}")
        if p in part:
            top_lines(part[p], limit if p == HOME else 8, min_size=BIG)
        if p == HOME:
            print(paint(f"  {'':>9}  в ~/Library:", "2"))
            top_lines(part[lib], 6, indent="    ", min_size=BIG)
    rest_size = max(used - sum(big.values()), 0)
    print(f"{size_col(rest_size)}  {paint('macOS, снимки Time Machine и то, что нельзя прочитать без прав', '1')}")
    print()
    print(paint("Часть папок закрыта правами macOS (Корзина, почта, данные других пользователей), "
                "их размер попал в последнюю строку.", "2"))
    print(paint("Глубже: mnrh disk top ~/Library. Что можно почистить: mnrh disk", "2"))


if args[:1] == ["top"]:
    disk_top()
    sys.exit(0)

for scan in (scan_ides, scan_build_outputs, scan_gradle, scan_konan, scan_simple_caches, scan_brew, scan_dev_caches,
             scan_downloads, scan_orphans, scan_simulators, scan_devices, scan_avds, scan_archives, scan_app_caches):
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
        return "демоны Gradle (mnrh killdaemons stop)"
    if it.app.startswith("avd:"):
        return f"эмулятор {it.app[4:]}"
    return it.app


def blocked(it):
    return busy.get(id(it), False)


def entries(group):
    """Пункты и группы по убыванию размера: (размер, пункт или None, заголовок группы, части группы)."""
    out, groups = [], {}
    for it in group:
        if it.group:
            groups.setdefault(it.group, []).append(it)
        else:
            out.append((it.size, it, None, None))
    for title, parts in groups.items():
        if len(parts) == 1:
            out.append((parts[0].size, parts[0], None, None))
        else:
            out.append((sum(p.size for p in parts), None, title, sorted(parts, key=lambda p: -p.size)))
    return sorted(out, key=lambda e: -e[0])


def label_of(it):
    """Подпись пункта вне группы: у одиночной части группы к ней добавляется заголовок группы."""
    return f"{it.group} ({it.label})" if it.group else it.label


def hint_of(it):
    return group_hints.get(it.group) if it.group else it.hint


def note_of(it):
    hint = hint_of(it)
    return paint(f"   закрой {app_name(it)}", "33") if blocked(it) else paint(f"   {hint}", "2") if hint else ""


def show(title, group, parts_shown=SHOW_PARTS):
    if not group:
        return
    print(paint(title, "1;34"))
    for size, it, gtitle, parts in entries(group):
        if it:
            print(f"  {size_col(size)}  {label_of(it)}{note_of(it)}")
            continue
        hint = group_hints.get(gtitle)
        print(f"  {size_col(size)}  {gtitle}" + (paint(f"   {hint}", "2") if hint else ""))
        for p in parts[:parts_shown]:
            print(f"    {paint(f'{human(p.size):>9}', '2')}  {p.label}{note_of(p) if blocked(p) else ''}")
        rest = parts[parts_shown:]
        if rest:
            print(paint(f"    {'':>9}  и еще {len(rest)} на {human(sum(p.size for p in rest))}", "2"))
    print(paint(f"  {human(sum(i.size for i in group)):>9}  итого", "1") + "\n")


def remove_path(path):
    """Удаляет в домашней папке, во временных папках пользователя и в /private/tmp, больше нигде."""
    real = os.path.realpath(path)
    if any(real.startswith(root + os.sep) for root in (USER_CACHE, USER_TEMP, "/private/tmp")):
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
        else:
            shutil.rmtree(path, ignore_errors=True)
    else:
        remove_under_home(path)


def remove(it):
    if it.action:
        return it.action() is not False
    for p in it.paths:
        remove_path(p)
    return True


def run_cleanup(chosen):
    _, _, before = shutil.disk_usage("/System/Volumes/Data")
    done, header = 0, None
    counts = {}
    for it in chosen:
        counts[it.group] = counts.get(it.group, 0) + 1
    for it in chosen:
        grouped = it.group and counts[it.group] > 1
        if grouped and it.group != header:
            print(f"  {it.group}")
        header = it.group if grouped else None
        pad = "    " if grouped else "  "
        name = it.label if grouped else label_of(it)
        if blocked(it) and app_running(it.app):
            print(f"{pad}{paint('пропущено', '33')}: {name}, закрой {app_name(it)} и повтори")
            continue
        ok = remove(it)
        done += ok
        print(f"{pad}{paint('✓', '32') if ok else paint('✗ не вышло:', '31')} {name}")
    after = shutil.disk_usage("/System/Volumes/Data")[2]
    print(f"\nСвободно: {human(before)} → " + paint(human(after), "1;32") +
          paint(f" (+{human(max(after - before, 0))})", "32"))
    trashed = sum(it.size for it in chosen if it.group in TO_TRASH and not os.path.lexists(it.paths[0]))
    if trashed:
        print(paint(f"В Корзину ушло {human(trashed)}, это место освободится, когда очистишь Корзину.", "2"))


def ordered(group):
    """Пункты в том порядке, в каком они в меню и в сводке: группы целиком, части по убыванию."""
    return [x for _, it, _, parts in entries(group) for x in ([it] if it else parts)]


def choose(previous=None):
    from menu import pick_many
    rows, order = [], []
    for title, group in (("Безопасно, пересоздается само", safe), ("Осознанно, прочитай пояснение", review)):
        if not group:
            continue
        rows.append((None, title))
        order.append(None)
        for size, it, gtitle, parts in entries(group):
            if gtitle:
                hint = group_hints.get(gtitle)
                rows.append((None, f"  {gtitle} · {human(size)}" + (f"  \x1b[2m{hint}\x1b[0m" if hint else "")))
                order.append(None)
            for x in [it] if it else parts:
                note = f"закрой {app_name(x)}" if blocked(x) else ((x.hint or "") if gtitle else (hint_of(x) or ""))
                indent = "    " if gtitle else ""
                label = x.label if gtitle else label_of(x)
                rows.append((label, f"{human(x.size):>8}  {indent}{label}" +
                             (f"  \x1b[2m{note}\x1b[0m" if note else "")))
                order.append(x)
    marked = {i for i, it in enumerate(order) if it is not None and it.marked and not blocked(it)}
    if previous is not None:
        marked = {i for i, it in enumerate(order) if it is not None and it in previous}

    def summary(marks):
        total = sum(order[i].size for i in marks if order[i] is not None)
        return f"отмечено {len(marks)} · {human(total)}"

    picked = pick_many(rows, title="\x1b[1;35mmnrh disk\x1b[0m  \x1b[2mчто удалить?\x1b[0m", marked=marked, summary=summary)
    if picked is None:
        return None
    return [order[i] for i in sorted(picked) if order[i] is not None]


total, used_b, free_before = shutil.disk_usage("/System/Volumes/Data")
snapshots = [l for l in run(["tmutil", "listlocalsnapshots", "/"], timeout=20).splitlines() if "TimeMachine" in l]
print(f"Свободно {human(free_before)} из {human(total)}")
if snapshots:
    print(paint(f"Локальных снимков Time Machine {len(snapshots)}. Их место macOS освободит сама, когда диск "
                "заполнится, удалить сразу можно командой tmutil thinlocalsnapshots / 999999999999 4", "2"))
print()
if not apply:
    show("Безопасно, пересоздается само:", safe)
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
    preset = [it for it in safe if it.marked]
    if not preset:
        print("Безопасно удалять нечего.")
        sys.exit(0)
    show("Удаляю безопасное:", preset)
    run_cleanup(ordered(preset))
    sys.exit(0)

from menu import choice

chosen = None
while True:
    chosen = choose(chosen)
    if not chosen:
        print("Ничего не удалено.")
        sys.exit(0)
    size = sum(i.size for i in chosen)
    print(paint(f"\nУдалю {len(chosen)}, ", "1;34") + paint(human(size), "1;36") + paint(":", "1;34"))
    for size_, it, gtitle, parts in entries(chosen):
        if it:
            warn = paint(f"  {hint_of(it)}", "2") if it.level == REVIEW and hint_of(it) else ""
            print(f"  {size_col(size_)}  {label_of(it)}{warn}")
            continue
        hint = group_hints.get(gtitle)
        warn = paint(f"  {hint}", "2") if parts[0].level == REVIEW and hint else ""
        print(f"  {size_col(size_)}  {gtitle}{warn}")
        for p in parts:
            print(f"    {paint(f'{human(p.size):>9}', '2')}  {p.label}")
    print()
    answer = choice("Удалить?", [("go", "Удалить", "yд"), ("back", "← К списку", "nн"), ("quit", "Выйти", "qй")],
                    default=1, back="back")
    if answer == "go":
        break
    if answer == "quit":
        print("Ничего не удалено.")
        sys.exit(0)
run_cleanup(ordered(chosen))
