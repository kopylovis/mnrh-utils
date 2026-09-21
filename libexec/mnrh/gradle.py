import glob
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import (HOME, confirm, du_bytes, gradle_build_dirs, gradle_versions_in_use, has_flag, human,
                     paint, process_commands, process_memory, projects, remove_under_home, version_key)

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or (args and args[0] not in ("clean", "--check")):
    print("mnrh gradle                    версии Gradle: какие проекты на чём, дистрибутивы, кеши, демоны, build/")
    print("mnrh gradle --check            ещё сверить с последней версией Gradle (нужна сеть)")
    print("mnrh gradle clean              удалить дистрибутивы и кеши версий, которые не использует ни один проект")
    print("mnrh gradle clean --builds     ещё build/ и .gradle/ во всех проектах")
    print("                               без терминала нужен -y")
    sys.exit(0)
assume_yes = has_flag(args, "-y", "--yes")
G = os.path.join(HOME, ".gradle")


def installed():
    versions = {}
    for p in glob.glob(os.path.join(G, "wrapper", "dists", "gradle-*")):
        m = re.fullmatch(r"gradle-([\d.]+)-(bin|all)", os.path.basename(p))
        if m:
            versions.setdefault(m.group(1), {"dist": [], "cache": []})["dist"].append(p)
    for p in glob.glob(os.path.join(G, "caches", "*")):
        name = os.path.basename(p)
        if re.fullmatch(r"\d+\.\d+(\.\d+)?", name):
            versions.setdefault(name, {"dist": [], "cache": []})["cache"].append(p)
    return versions


def daemons():
    mem = process_memory()
    found = {}
    for pid, cmd in process_commands().items():
        m = re.search(r"GradleDaemon ([\d.]+)", cmd)
        if m:
            d = found.setdefault(m.group(1), [0, 0])
            d[0] += 1
            d[1] += mem.get(pid, 0)
    return found


def sizes(paths):
    with ThreadPoolExecutor(max_workers=8) as pool:
        return sum(pool.map(du_bytes, paths))


def latest():
    try:
        with urllib.request.urlopen("https://services.gradle.org/versions/current", timeout=10) as r:
            return json.load(r).get("version")
    except Exception:
        return None


def status():
    used = gradle_versions_in_use()
    inst = installed()
    running = daemons()
    newest = latest() if has_flag(args, "--check") else None

    print(paint("Версии Gradle:", "1"))
    for v in sorted(set(used) | set(inst) | set(running), key=version_key, reverse=True):
        parts = inst.get(v, {"dist": [], "cache": []})
        dist, cache = sizes(parts["dist"]), sizes(parts["cache"])
        who = ", ".join(sorted(used.get(v, [])))
        head = f"  {v:<8} " + (f"проекты: {who}" if who else paint("не используется", "33"))
        print(head)
        details = []
        if dist:
            details.append(f"дистрибутив {human(dist)}")
        if cache:
            details.append(f"кеш {human(cache)}")
        if v in running:
            n, m = running[v]
            details.append(f"демонов: {n} ({human(m)})")
        hint = ""
        if not who and (dist or cache):
            hint = paint("   -> mnrh gradle clean", "2")
        if not who and v in running:
            hint = paint("   -> mnrh killdaemons: демоны версии без проектов не нужны", "2")
        if details:
            print(f"           {' · '.join(details)}{hint}")

    if newest:
        behind = [v for v in used if version_key(v) < version_key(newest)]
        print(f"\nПоследняя версия Gradle: {newest}"
              + (f". Отстают: {', '.join(sorted(behind))} — обновление: ./gradlew wrapper --gradle-version {newest}"
                 if behind else ", все проекты на ней."))

    shared = [os.path.join(G, "caches", d) for d in ("modules-2", "build-cache-1")]
    shared += glob.glob(os.path.join(G, "caches", "transforms-*")) + glob.glob(os.path.join(G, "caches", "jars-*"))
    shared_size = sizes([p for p in shared if os.path.exists(p)])
    if shared_size:
        print(f"\nОбщие кеши (зависимости, трансформации, build cache): {human(shared_size)}")

    builds = [(os.path.basename(p), sizes(gradle_build_dirs(p))) for p in projects()]
    builds = [(n, s) for n, s in builds if s > 1048576]
    if builds:
        print(f"\n{paint('build/ и .gradle/ в проектах:', '1')}")
        for n, s in sorted(builds, key=lambda x: -x[1]):
            print(f"  {human(s):>9}  {n}")
        print(f"  {human(sum(s for _, s in builds)):>9}  итого" + paint("   -> mnrh gradle clean --builds", "2"))


def clean():
    used = gradle_versions_in_use()
    if not used:
        print("Ни в одном проекте не найден gradle-wrapper.properties — не трогаю ничего, чтобы не удалить нужное.")
        return
    targets = []
    for v, parts in installed().items():
        if v not in used:
            targets += [(f"Gradle {v}", p) for p in parts["dist"] + parts["cache"]]
    if has_flag(args, "--builds"):
        targets += [(os.path.basename(proj), d) for proj in projects() for d in gradle_build_dirs(proj)]
    if not targets:
        print(f"Удалять нечего: все версии в работе ({', '.join(sorted(used))}), build/ не запрошены.")
        return
    total = sizes([p for _, p in targets])
    labels = sorted({label for label, _ in targets})
    print(f"Будет удалено {human(total)}: {', '.join(labels)}")
    running = daemons()
    stale_running = [v for v in running if v not in used]
    if stale_running:
        print(f"Сначала стоит остановить демоны версий {', '.join(stale_running)}: mnrh killdaemons")
    if not confirm("Удалить?", assume_yes):
        print("Отменено.")
        return
    for _, p in targets:
        remove_under_home(p)
    print(f"Готово, освобождено около {human(total)}.")


if args and args[0] == "clean":
    clean()
else:
    status()
