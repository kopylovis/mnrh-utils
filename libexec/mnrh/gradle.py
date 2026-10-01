import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import (BAD, HOME, OK, confirm, du_bytes, gradle_build_dirs, gradle_versions_in_use, has_flag, human,
                     paint, process_commands, process_memory, projects, remove_under_home, version_key)

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or (args and args[0] not in ("clean", "--check", "update")):
    print("mnrh gradle                    версии Gradle: какие проекты на чём, дистрибутивы, кеши, демоны, build/")
    print("mnrh gradle --check            ещё сверить с последней версией Gradle (нужна сеть)")
    print("mnrh gradle update             обновить Gradle wrapper до последней версии: выбрать проекты в списке")
    print("mnrh gradle update <проект>… -y   без вопросов; --all — все отстающие; --version 9.8.0 — своя версия")
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
              + (f". Отстают: {', '.join(sorted(behind))}"
                 if behind else ", все проекты на ней."))
        if behind:
            print(paint("Обновить: mnrh gradle update", "2"))

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


WRAPPER_FILES = ["gradle/wrapper/gradle-wrapper.properties", "gradle/wrapper/gradle-wrapper.jar", "gradlew", "gradlew.bat"]


def wrapper_info(proj):
    path = os.path.join(proj, "gradle", "wrapper", "gradle-wrapper.properties")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r"gradle-([\d.]+(?:-[\w.]+)?)-(bin|all)\.zip", text)
    return (m.group(1), m.group(2), text) if m else None


def fetch_text(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read().decode().strip()


def agp_of(proj):
    try:
        text = open(os.path.join(proj, "gradle", "libs.versions.toml"), encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r'^\s*agp\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else None


def update_one(proj, target, kind):
    name = os.path.basename(proj)
    dirty = subprocess.run(["git", "-C", proj, "status", "--porcelain", "--"] + WRAPPER_FILES,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        return False, "в файлах wrapper есть незакоммиченные правки, пропускаю"
    try:
        sha = fetch_text(f"https://services.gradle.org/distributions/gradle-{target}-{kind}.zip.sha256")
    except Exception as e:
        return False, f"не скачал контрольную сумму: {e}"
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return False, "странная контрольная сумма с services.gradle.org"
    saved = {}
    for f in WRAPPER_FILES:
        try:
            with open(os.path.join(proj, f), "rb") as fh:
                saved[f] = fh.read()
        except OSError:
            pass
    props = os.path.join(proj, WRAPPER_FILES[0])
    text = saved[WRAPPER_FILES[0]].decode("utf-8")
    text = re.sub(r"gradle-[\d.]+(?:-[\w.]+)?-(bin|all)\.zip", f"gradle-{target}-{kind}.zip", text)
    if "distributionSha256Sum=" in text:
        text = re.sub(r"distributionSha256Sum=\S*", f"distributionSha256Sum={sha}", text)
    with open(props, "w", encoding="utf-8") as fh:
        fh.write(text)
    started = time.time()
    try:
        r = subprocess.run(["./gradlew", "wrapper", "--gradle-version", target, "--distribution-type", kind,
                            "--gradle-distribution-sha256-sum", sha, "--no-daemon", "-q"],
                           cwd=proj, capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL)
        ok, out = r.returncode == 0, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        ok, out = False, "не уложился в 30 минут"
    if not ok:
        for f, data in saved.items():
            with open(os.path.join(proj, f), "wb") as fh:
                fh.write(data)
        m = re.search(r"\* What went wrong:\s*(.*?)(?:\n\s*\* Try:|\Z)", out, re.S)
        tail = "\n".join("      " + l.strip() for l in (m.group(1) if m else out).splitlines() if l.strip())[-1200:]
        return False, f"сборка на {target} не настроилась, файлы вернул как были:\n{tail}"
    os.chmod(os.path.join(proj, "gradlew"), 0o755)
    changed = subprocess.run(["git", "-C", proj, "status", "--porcelain", "--"] + WRAPPER_FILES,
                             capture_output=True, text=True).stdout.split("\n")
    files = [l[3:] for l in changed if l.strip()]
    return True, f"за {int(time.time() - started)} с, изменены: {', '.join(files) or 'только properties'}"


def update():
    target = None
    if "--version" in args:
        i = args.index("--version")
        target = args[i + 1] if i + 1 < len(args) else None
    target = target or latest()
    if not target:
        sys.exit("Не узнал последнюю версию Gradle: нет сети? Укажи сам: mnrh gradle update --version 9.8.0")
    names = [a for a in args[1:] if not a.startswith("-") and a != target]
    behind = []
    for proj in projects():
        info = wrapper_info(proj)
        if info and version_key(info[0]) < version_key(target):
            behind.append((proj, info[0], info[1]))
    if names:
        behind = [b for b in behind if os.path.basename(b[0]) in names or
                  any(os.path.basename(b[0]).startswith(n) for n in names)]
    if not behind:
        print(f"Все проекты уже на Gradle {target} или новее." if not names else "Среди названных отстающих нет.")
        return
    def row(b):
        agp = agp_of(b[0])
        return f"{os.path.basename(b[0]):<22} {b[1]} → {target}" + (f"   \x1b[2mAGP {agp}\x1b[0m" if agp else "")
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not assume_yes
    if interactive:
        from menu import choice, pick_many
        rows = [(os.path.basename(b[0]), row(b)) for b in behind]
        marked = set(range(len(rows)))
        while True:
            picked = pick_many(rows, title=f"\x1b[1mmnrh gradle update\x1b[0m  \x1b[2mобновить до {target}\x1b[0m",
                               marked=marked, summary=lambda m: f"отмечено {len(m)}")
            if not picked:
                print("Ничего не менял.")
                return
            marked = picked
            names_line = ", ".join(os.path.basename(behind[i][0]) for i in sorted(picked))
            answer = choice(f"Обновить {names_line} до {target}?",
                            [("go", "Обновить", "yд"), ("back", "← К списку", "nн"), ("quit", "Выйти", "qй")],
                            default=1, back="back")
            if answer == "go":
                break
            if answer == "quit":
                print("Ничего не менял.")
                return
        behind = [behind[i] for i in sorted(picked)]
    elif not assume_yes and not has_flag(args, "--all") and not names:
        print("Не терминал, выбрать нельзя. Без вопросов: mnrh gradle update --all -y или mnrh gradle update <проект> -y")
        sys.exit(2)
    print(paint(f"Обновляю до Gradle {target}: {', '.join(os.path.basename(b[0]) for b in behind)}", "1"))
    print(paint("Первый раз скачается дистрибутив (~150 МБ). Каждый проект настраивается на новой версии — "
                "это и есть проверка.", "2"))
    done = []
    for proj, cur, kind in behind:
        print(f"  {os.path.basename(proj)}: {cur} → {target} …", flush=True)
        ok, msg = update_one(proj, target, kind)
        print(f"    {OK if ok else BAD} {msg}")
        if ok:
            done.append(os.path.basename(proj))
    if done:
        print(paint(f"\nГотово: {', '.join(done)}. Изменения не закоммичены — проверь сборку и закоммить. "
                    f"Старую версию убрать потом: mnrh gradle clean", "2"))


if args and args[0] == "clean":
    clean()
elif args and args[0] == "update":
    update()
else:
    status()
