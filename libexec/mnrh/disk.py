import glob
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import (DEV, HOME, du_bytes, gradle_versions_in_use, has_flag, human, paint,
                     projects, run, tilde, version_key)

args = sys.argv[1:]
if has_flag(args, "-h", "--help"):
    print("mnrh disk              что можно почистить: безопасное и то, что стоит посмотреть")
    print("mnrh disk --apply      удалить безопасное (спросит подтверждение)")
    print("mnrh disk --apply -y   удалить без вопроса — для запуска не из терминала")
    sys.exit(0)
apply = has_flag(args, "--apply")
assume_yes = has_flag(args, "-y", "--yes")

L = os.path.join(HOME, "Library")
SAFE, REVIEW = "safe", "review"


class Item:
    def __init__(self, level, label, paths=(), hint=None, action=None, size=None):
        self.level = level
        self.label = label
        self.paths = [p for p in paths if os.path.lexists(p)]
        self.hint = hint
        self.action = action
        self.size = size


items = []


def add(level, label, paths=(), hint=None, action=None, size=None):
    it = Item(level, label, paths, hint, action, size)
    if it.paths or it.size:
        items.append(it)


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
    builds = []
    for proj in projects():
        for root, dirs, files in os.walk(proj):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".idea", "Pods")]
            if "build" in dirs and ({"build.gradle", "build.gradle.kts"} & set(files)):
                builds.append(os.path.join(root, "build"))
            dirs[:] = [d for d in dirs if d not in ("build", ".gradle")]
        pg = os.path.join(proj, ".gradle")
        if os.path.isdir(pg):
            builds.append(pg)
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
    add(SAFE, "Gradle: build cache", [os.path.join(g, "caches", "build-cache-1")])


def scan_simple_caches():
    add(SAFE, "Xcode DerivedData", glob.glob(f"{L}/Developer/Xcode/DerivedData/*"),
        hint="Xcode пересоберёт индексы")
    add(SAFE, "кеш npm", [os.path.join(HOME, ".npm", "_cacache"), os.path.join(HOME, ".npm", "_npx")])
    add(SAFE, "кеши pip, SwiftPM, CocoaPods",
        [f"{L}/Caches/pip", f"{L}/Caches/org.swift.swiftpm", f"{L}/Caches/CocoaPods"])
    add(REVIEW, "кеш Chrome", [f"{L}/Caches/Google/Chrome"], hint="безопасно, но лучше при закрытом Chrome")
    add(REVIEW, "Корзина", glob.glob(os.path.join(HOME, ".Trash", "*")) + glob.glob(os.path.join(HOME, ".Trash", ".*")),
        hint="очистить в Finder")


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


for scan in (scan_ides, scan_build_outputs, scan_gradle, scan_simple_caches, scan_brew, scan_simulators):
    scan()

with ThreadPoolExecutor(max_workers=8) as pool:
    pending = {it: [pool.submit(du_bytes, p) for p in it.paths] for it in items if it.size is None}
    for it, futs in pending.items():
        it.size = sum(f.result() for f in futs)

items = [it for it in items if it.size and it.size >= 1048576]
safe = sorted((i for i in items if i.level == SAFE), key=lambda i: -i.size)
review = sorted((i for i in items if i.level == REVIEW), key=lambda i: -i.size)


def show(title, group):
    if not group:
        return
    print(paint(title, "1"))
    for it in group:
        line = f"  {human(it.size):>9}  {it.label}"
        if it.hint:
            line += paint(f"   -> {it.hint}", "2")
        print(line)
    print(f"  {human(sum(i.size for i in group)):>9}  итого\n")


total, used_b, free_before = shutil.disk_usage("/System/Volumes/Data")
print(f"Свободно {human(free_before)} из {human(total)}\n")
show("Безопасно, пересоздаётся само:", safe)
show("Стоит посмотреть, удалять осознанно:", review)

if not safe:
    print("Безопасно удалять нечего.")
    sys.exit(0)
if not apply:
    print(paint(f"Удалить безопасное ({human(sum(i.size for i in safe))}): mnrh disk --apply", "2"))
    sys.exit(0)

if not assume_yes:
    if not sys.stdin.isatty():
        print("Не терминал, подтвердить нельзя. Запусти с -y: mnrh disk --apply -y")
        sys.exit(2)
    if input(f"Удалить безопасное, {human(sum(i.size for i in safe))}? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
        print("Отменено.")
        sys.exit(0)


def remove(path):
    if not path.startswith(HOME + os.sep) or path.rstrip("/") == HOME:
        return
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    else:
        shutil.rmtree(path, ignore_errors=True)


for it in safe:
    if it.action:
        it.action()
    else:
        for p in it.paths:
            remove(p)
    print(f"  удалено: {it.label}")

free_after = shutil.disk_usage("/System/Volumes/Data")[2]
print(f"\nСвободно: {human(free_before)} -> {human(free_after)} (+{human(max(free_after - free_before, 0))})")
