import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import (BAD, OK, confirm, du_bytes, has_flag, human, konan_prebuilts, konan_unused, kotlin_version_of,
                     kotlin_versions_in_use, paint, projects, remove_under_home, version_key)

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or (args and args[0] not in ("status", "clean", "--check", "update")):
    print("mnrh kotlin                    меню с действиями (без терминала — то же, что status)")
    print("mnrh kotlin status             версии Kotlin: какие проекты на чём, скачанные Kotlin/Native")
    print("mnrh kotlin --check            ещё сверить с последней версией Kotlin (нужна сеть)")
    print("mnrh kotlin update             обновить Kotlin до последней версии: выбрать проекты в списке")
    print("mnrh kotlin update <проект>… -y   без вопросов; --all — все отстающие; --version 2.4.21 — своя версия")
    print("mnrh kotlin clean              удалить Kotlin/Native версий, которые не использует ни один проект")
    print("                               без терминала нужен -y")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)
assume_yes = has_flag(args, "-y", "--yes")
METADATA = "https://repo1.maven.org/maven2/org/jetbrains/kotlin/kotlin-gradle-plugin/maven-metadata.xml"


def sizes(paths):
    with ThreadPoolExecutor(max_workers=8) as pool:
        return sum(pool.map(du_bytes, paths))


def latest():
    try:
        with urllib.request.urlopen(METADATA, timeout=10) as r:
            text = r.read().decode()
    except Exception:
        return None
    stable = [v for v in re.findall(r"<version>([^<]+)</version>", text) if re.fullmatch(r"\d+\.\d+\.\d+", v)]
    return max(stable, key=version_key) if stable else None


def status():
    used = kotlin_versions_in_use()
    native = konan_prebuilts()
    newest = latest() if has_flag(args, "--check") else None
    if not used and not native:
        print("Ни в одном проекте не нашёл версию Kotlin, Kotlin/Native не скачан.")
        return
    print(paint("Версии Kotlin:", "1;34"))
    for v in sorted(set(used) | set(native), key=version_key, reverse=True):
        who = ", ".join(sorted(used.get(v, [])))
        print(f"  {v:<8} " + (f"проекты: {who}" if who else paint("не используется", "33")))
        size = sizes(native.get(v, []))
        if size:
            hint = paint("   -> mnrh kotlin clean", "2") if not who else ""
            print(f"           Kotlin/Native {human(size)}{hint}")
    if used:
        old, deps = konan_unused(used)
        if deps:
            print(f"\nLLVM и инструменты от старых Kotlin/Native: {human(sizes(deps))}"
                  + paint("   -> mnrh kotlin clean", "2"))
    if newest:
        behind = [v for v in used if version_key(v) < version_key(newest)]
        print(f"\nПоследняя версия Kotlin: {newest}"
              + (f". Отстают: {', '.join(sorted(behind, key=version_key))}" if behind else ", все проекты на ней."))
        if behind:
            print(paint("Обновить: mnrh kotlin update", "2"))


def clean():
    used = kotlin_versions_in_use()
    if not used:
        print("Ни в одном проекте не нашёл версию Kotlin — не трогаю ничего, чтобы не удалить нужное.")
        return
    old, deps = konan_unused(used)
    targets = old + deps
    if not targets:
        print(f"Удалять нечего: в ~/.konan только версии из проектов ({', '.join(sorted(used))}).")
        return
    total = sizes(targets)
    names = [os.path.basename(p).rsplit("-", 1)[-1] for p in old]
    what = []
    if names:
        what.append(f"Kotlin/Native {', '.join(sorted(names, key=version_key))}")
    if deps:
        what.append(f"LLVM и инструменты старых версий ({len(deps)})")
    print(f"Будет удалено {human(total)}: {'; '.join(what)}")
    print(paint(f"Остаются версии из проектов: {', '.join(sorted(used, key=version_key))}", "2"))
    if not confirm("Удалить?", assume_yes):
        print("Отменено.")
        return
    for p in targets:
        remove_under_home(p)
    print(f"Готово, освобождено около {human(total)}.")


def ksp_tied(proj, current):
    """KSP1 вида 2.0.21-1.0.28 собран под одну версию Kotlin, KSP2 (2.x.y) от неё не зависит."""
    try:
        text = open(os.path.join(proj, "gradle", "libs.versions.toml"), encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r'^\s*ksp\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m and m.group(1).startswith(current + "-") else None


def update_one(proj, target):
    current, path, rx = kotlin_version_of(proj)
    rel = os.path.relpath(path, proj)
    dirty = subprocess.run(["git", "-C", proj, "status", "--porcelain", "--", rel],
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        return False, f"в {rel} есть незакоммиченные правки, пропускаю"
    ksp = ksp_tied(proj, current)
    if ksp:
        return False, f"KSP {ksp} собран под Kotlin {current}: сначала обнови KSP (KSP2, версии 2.x, от Kotlin не зависит)"
    saved = open(path, encoding="utf-8").read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(rx.sub(lambda m: m.group(1) + target + m.group(3), saved, count=1))
    if not os.access(os.path.join(proj, "gradlew"), os.X_OK):
        return True, f"изменён {rel}; gradlew нет, сборку не проверял"
    started = time.time()
    try:
        r = subprocess.run(["./gradlew", "help", "--no-daemon", "-q"], cwd=proj, capture_output=True, text=True,
                           timeout=1800, stdin=subprocess.DEVNULL)
        ok, out = r.returncode == 0, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        ok, out = False, "не уложился в 30 минут"
    if not ok:
        with open(path, "w", encoding="utf-8") as f:
            f.write(saved)
        m = re.search(r"\* What went wrong:\s*(.*?)(?:\n\s*\* Try:|\Z)", out, re.S)
        tail = "\n".join("      " + l.strip() for l in (m.group(1) if m else out).splitlines() if l.strip())[-1200:]
        return False, f"проект на Kotlin {target} не настроился, {rel} вернул как был:\n{tail}"
    return True, f"за {int(time.time() - started)} с, изменён {rel}"


def update():
    target = None
    if "--version" in args:
        i = args.index("--version")
        target = args[i + 1] if i + 1 < len(args) else None
    target = target or latest()
    if not target:
        sys.exit("Не узнал последнюю версию Kotlin: нет сети? Укажи сам: mnrh kotlin update --version 2.4.21")
    names = [a for a in args[1:] if not a.startswith("-") and a != target]
    behind = []
    for proj in projects():
        info = kotlin_version_of(proj)
        if info and version_key(info[0]) < version_key(target):
            behind.append((proj, info[0]))
    if names:
        behind = [b for b in behind if any(os.path.basename(b[0]).startswith(n) for n in names)]
    if not behind:
        print(f"Все проекты уже на Kotlin {target} или новее." if not names else "Среди названных отстающих нет.")
        return
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not assume_yes
    if interactive:
        from menu import choice, pick_many
        rows = [(os.path.basename(p), f"{os.path.basename(p):<22} {cur} → {target}") for p, cur in behind]
        marked = set(range(len(rows)))
        while True:
            picked = pick_many(rows, title=f"\x1b[1;35mmnrh kotlin update\x1b[0m  \x1b[2mобновить до {target}\x1b[0m",
                               marked=marked, summary=lambda m: f"отмечено {len(m)}")
            if not picked:
                print("Ничего не менял.")
                return
            marked = picked
            names_line = ", ".join(os.path.basename(behind[i][0]) for i in sorted(picked))
            answer = choice(f"Обновить {names_line} до Kotlin {target}?",
                            [("go", "Обновить", "yд"), ("back", "← К списку", "nн"), ("quit", "Выйти", "qй")],
                            default=1, back="back")
            if answer == "go":
                break
            if answer == "quit":
                print("Ничего не менял.")
                return
        behind = [behind[i] for i in sorted(picked)]
    elif not assume_yes and not has_flag(args, "--all") and not names:
        print("Не терминал, выбрать нельзя. Без вопросов: mnrh kotlin update --all -y или mnrh kotlin update <проект> -y")
        sys.exit(2)
    print(paint(f"Обновляю до Kotlin {target}: {', '.join(os.path.basename(p) for p, _ in behind)}", "1;34"))
    print(paint("Каждый проект настраивается на новой версии (./gradlew help) — это и есть проверка. "
                "Первый раз скачаются плагины Kotlin.", "2"))
    done = []
    for proj, cur in behind:
        print(f"  {os.path.basename(proj)}: {cur} → {target} …", flush=True)
        ok, msg = update_one(proj, target)
        print(f"    {OK if ok else BAD} {msg}")
        if ok:
            done.append(os.path.basename(proj))
    if done:
        print(paint(f"\nГотово: {', '.join(done)}. Изменения не закоммичены — проверь сборку и закоммить. "
                    f"Старый Kotlin/Native убрать потом: mnrh kotlin clean", "2"))


if args and args[0] == "clean":
    clean()
elif args and args[0] == "update":
    update()
else:
    status()
