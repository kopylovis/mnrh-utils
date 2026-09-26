import glob
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import APP_DIRS, HOME, OK, BAD, WARN, bundle_info, confirm, du_bytes, find_apps, has_flag, human, paint, run, tilde

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or not args:
    print("mnrh uninstall <Приложение>   удалить приложение вместе с его данными в ~/Library (в Корзину)")
    print("mnrh uninstall --orphans      остатки приложений, которых уже нет: кеши, контейнеры, данные")
    print("   -y                         не спрашивать; без терминала обязателен")
    print("   -n                         только показать, ничего не трогать")
    sys.exit(0 if args else 2)
assume_yes = has_flag(args, "-y", "--yes")
dry = has_flag(args, "-n", "--dry-run")

LIB = os.path.join(HOME, "Library")
# Где приложения оставляют данные и как там называются папки и файлы.
BY_ID = ["Caches", "Preferences", "Containers", "Group Containers", "Saved Application State", "HTTPStorages",
         "WebKit", "Application Scripts", "Cookies", "Application Support", "Logs", "LaunchAgents",
         os.path.join("Preferences", "ByHost")]
BY_NAME = ["Application Support", "Caches", "Logs"]
SUFFIXES = (".plist", ".savedState", ".binarycookies")
BUNDLE_ID = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9_-]+){2,}$")
# Консольные инструменты и фоновые службы без своего .app: их данные — не остатки.
TOOLS = ("org.swift.", "com.firebase.", "com.google.Keystone", "com.google.GoogleUpdater", "com.google.SoftwareUpdate",
         "com.microsoft.autoupdate", "org.cocoapods", "com.github.", "org.python.", "org.nodejs.", "com.docker.",
         "com.jetbrains.toolbox", "io.flutter.", "com.crashlytics.", "org.gradle.", "org.jetbrains.kotlin")
RECENT_DAYS = 7


def last_change(path):
    """Самое свежее изменение внутри (не глубже 3 уровней, чтобы не тормозить)."""
    newest = os.path.getmtime(path) if os.path.exists(path) else 0
    base = path.rstrip("/").count("/")
    for root, dirs, files in os.walk(path):
        if root.count("/") - base >= 3:
            dirs[:] = []
        for f in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                pass
    return newest


def find_app(query):
    candidates = [a for a in find_apps(query) if not a.startswith("/System/")]
    if len(candidates) > 1:
        print("Подходит несколько, уточни:")
        for c in candidates:
            print(f"  {os.path.basename(c)[:-4]}")
        sys.exit(2)
    return candidates[0] if candidates else None


def strip(name):
    for s in SUFFIXES:
        if name.endswith(s):
            return name[: -len(s)]
    return name


def leftovers(bundle_id, app_name):
    found = set()
    for sub in BY_ID:
        d = os.path.join(LIB, sub)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            base = strip(n)
            if base == bundle_id or base.startswith(bundle_id + ".") or base.endswith("." + bundle_id) \
                    or (sub.endswith("ByHost") and n.startswith(bundle_id + ".")):
                found.add(os.path.join(d, n))
    for sub in BY_NAME:
        p = os.path.join(LIB, sub, app_name)
        if app_name and os.path.exists(p):
            found.add(p)
    return sorted(found)


def sizes(paths):
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(paths, pool.map(du_bytes, paths)))


def brew_cask(app):
    """Если приложение ставил brew, удалять лучше через него — иначе brew будет считать его установленным."""
    name = os.path.basename(app)
    for meta in glob.glob("/opt/homebrew/Caskroom/*/.metadata/*/*/Casks/*") + \
            glob.glob("/usr/local/Caskroom/*/.metadata/*/*/Casks/*"):
        try:
            if name in open(meta, errors="ignore").read():
                return meta.split("/Caskroom/")[1].split("/")[0]
        except OSError:
            continue
    return None


def to_trash(paths):
    """В Корзину через Finder, чтобы работало «Вернуть». Не вышло — переносим сами."""
    if not paths:
        return []
    items = ", ".join(f'POSIX file "{p}"' for p in paths)
    r = subprocess.run(["osascript", "-e", f"tell application \"Finder\" to delete {{{items}}}"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return paths
    trash = os.path.join(HOME, ".Trash")
    moved = []
    for p in paths:
        dest = os.path.join(trash, os.path.basename(p))
        if os.path.exists(dest):
            dest += f" {time.strftime('%H-%M-%S')}"
        try:
            shutil.move(p, dest)
            moved.append(p)
        except OSError as e:
            print(f"{BAD} {tilde(p)}: {e.strerror}")
    return moved


def uninstall(query):
    app = find_app(query)
    if not app:
        sys.exit(f"Не нашёл приложение «{query}» в /Applications и ~/Applications")
    bundle_id, name = bundle_info(app)
    if not bundle_id:
        sys.exit(f"У {app} нет Info.plist с идентификатором — не рискую.")
    if app.startswith("/System/") or bundle_id in ("com.apple.Safari",):
        sys.exit("Это системное приложение macOS, удалить его нельзя.")
    rest = leftovers(bundle_id, name)
    size = sizes([app] + rest)
    print(f"{paint(os.path.basename(app)[:-4], '1')} ({bundle_id}), {human(size[app])}")
    if rest:
        print("Данные в ~/Library:")
        for p in rest:
            print(f"  {human(size[p]):>9}  {tilde(p)}")
    else:
        print("Данных в ~/Library не нашёл.")
    total = sum(size.values())
    cask = brew_cask(app)
    if cask:
        print(paint(f"Поставлено через brew ({cask}): приложение удалю через brew uninstall --cask.", "2"))
    if dry:
        return
    if not confirm(f"Удалить в Корзину, всего {human(total)}?", assume_yes):
        print("Отменено.")
        return
    run(["osascript", "-e", f'if application id "{bundle_id}" is running then tell application id "{bundle_id}" to quit'],
        timeout=15)
    if cask:
        subprocess.run(["brew", "uninstall", "--cask", cask])
        targets = rest
    else:
        targets = [app] + rest
    moved = to_trash(targets)
    print(f"{OK} В Корзине: {len(moved)} из {len(targets)}" + ("" if len(moved) == len(targets)
                                                               else " — остальное без прав, см. выше"))


def installed_ids():
    ids = set()
    out = run(["mdfind", "-attr", "kMDItemCFBundleIdentifier", "kMDItemContentType == 'com.apple.application-bundle'"],
              timeout=60)
    for line in out.splitlines():
        m = re.search(r"kMDItemCFBundleIdentifier = (\S+)", line)
        if m and m.group(1) != "(null)":
            ids.add(m.group(1))
    # Без Spotlight тоже работаем: пробегаем папки приложений сами.
    for d in APP_DIRS + ["/System/Applications", "/System/Applications/Utilities"]:
        for app in glob.glob(os.path.join(d, "*.app")):
            bid, _ = bundle_info(app)
            if bid:
                ids.add(bid)
    return ids


def orphans():
    ids = installed_ids()
    lowered = {i.lower() for i in ids}

    def owned(base):
        b = base.lower()
        if b.startswith("com.apple.") or b.startswith("group.com.apple.") or \
                any(b.startswith(t.lower()) for t in TOOLS):
            return True
        if b.startswith("group."):
            b = b[len("group."):]
        b = re.sub(r"^[a-z0-9]{10}\.", "", b)  # префикс Team ID у Group Containers
        return any(b == i or b.startswith(i + ".") or i.startswith(b + ".") for i in lowered)

    found = []
    for sub in ("Caches", "Containers", "Group Containers", "Application Support", "HTTPStorages", "WebKit",
                "Saved Application State", "Application Scripts", "Logs"):
        d = os.path.join(LIB, sub)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            base = strip(n)
            if BUNDLE_ID.match(base) and not owned(base):
                found.append(os.path.join(d, n))
    size = sizes(found)
    found = [p for p in found if size[p] >= 1048576]
    found.sort(key=lambda p: -size[p])
    if not found:
        print("Остатков удалённых приложений больше 1 МБ нет.")
        return
    now = time.time()
    fresh = {p for p in found if now - last_change(p) < RECENT_DAYS * 86400}
    print(paint("Остатки приложений, которых нет в системе:", "1"))
    for p in found:
        note = paint(f"   менялось за {RECENT_DAYS} дней — чем-то используется, не трогаю", "2") if p in fresh else ""
        print(f"  {human(size[p]):>9}  {tilde(p)}{note}")
    found = [p for p in found if p not in fresh]
    total = sum(size[p] for p in found)
    print(f"  {human(total):>9}  можно убрать")
    print(paint("Проверь список: бывает, что программа живёт не в /Applications (CLI, плагин, утилита из архива).", "2"))
    if dry or not found:
        return
    if not confirm("Перенести в Корзину то, что можно убрать?", assume_yes):
        print("Отменено.")
        return
    moved = to_trash(found)
    print(f"{OK} В Корзине: {len(moved)}, около {human(sum(size[p] for p in moved))}")


if has_flag(args, "--orphans"):
    orphans()
else:
    query = next((a for a in args if not a.startswith("-")), None)
    if not query:
        sys.exit("mnrh uninstall <Приложение>")
    uninstall(query)
