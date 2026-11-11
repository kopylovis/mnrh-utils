import glob
import os
import re
import subprocess
import sys
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import (BAD, HOME, OK, agp_of, android_sdk, confirm, du_bytes, has_flag, human, paint, projects,
                     remove_under_home, tilde, version_key)

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or (args and args[0] not in ("status", "clean", "--check", "update")):
    print("mnrh build-tools               меню с действиями (без терминала — то же, что status)")
    print("mnrh build-tools status        Android build-tools: какие стоят, каким проектам нужны")
    print("mnrh build-tools --check       ещё сверить с последней версией (нужна сеть)")
    print("mnrh build-tools update        поднять buildToolsVersion в проектах, где он прописан: выбрать в списке")
    print("mnrh build-tools update <проект>… -y   без вопросов; --all — все отстающие; --version 37.0.0 — своя версия")
    print("mnrh build-tools clean         удалить build-tools, которые не нужны ни одному проекту")
    print("                               без терминала нужен -y")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)
assume_yes = has_flag(args, "-y", "--yes")
SDK = android_sdk()
REPOSITORY = "https://dl.google.com/android/repository/repository2-3.xml"
BUILDER = os.path.join(HOME, ".gradle", "caches", "modules-2", "files-2.1", "com.android.tools.build", "builder")
PIN = re.compile(r'(buildToolsVersion\s*=?\s*["\'])([\d.]+(?:-rc\d+)?)(["\'])')
TOML_PIN = re.compile(r'^(\s*build[-_]?[tT]ools(?:[-_]?[vV]ersion)?\s*=\s*")([\d.]+(?:-rc\d+)?)(")', re.M)
ANDROID = re.compile(r'com\.android\.|libs\.plugins\.android|androidLibrary\s*\{|^\s*android\s*\{', re.M)
SKIP_DIRS = {"build", ".gradle", ".git", "node_modules", ".idea", ".kotlin", "Pods"}


def sizes(paths):
    with ThreadPoolExecutor(max_workers=8) as pool:
        return sum(pool.map(du_bytes, paths))


def installed():
    if not SDK:
        return {}
    return {os.path.basename(p): p for p in glob.glob(os.path.join(SDK, "build-tools", "*")) if os.path.isdir(p)}


def scripts(proj, depth=4):
    for root, dirs, files in os.walk(proj):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS] if root.count(os.sep) - proj.count(os.sep) < depth else []
        for f in files:
            if f.endswith((".gradle.kts", ".gradle")):
                yield os.path.join(root, f)


_defaults = {}


def agp_default(agp):
    """Build-tools по умолчанию для AGP: строка из ToolsRevisionUtils в builder-<agp>.jar из кеша Gradle."""
    if agp not in _defaults:
        found = None
        for jar in glob.glob(os.path.join(BUILDER, agp, "*", f"builder-{agp}.jar")):
            try:
                with zipfile.ZipFile(jar) as z:
                    data = z.read("com/android/builder/core/ToolsRevisionUtils.class")
            except (OSError, KeyError, zipfile.BadZipFile):
                continue
            m = re.search(rb"(\d+\.\d+\.\d+)", data)
            if m:
                found = m.group(1).decode()
                break
        _defaults[agp] = found
    return _defaults[agp]


def usage(proj):
    """{"pins": [(версия, файл, регулярка)], "agp": версия AGP, "default": нужна ли версия AGP по умолчанию}."""
    agp = agp_of(proj)
    pins, unpinned_module = [], False
    toml = os.path.join(proj, "gradle", "libs.versions.toml")
    try:
        m = TOML_PIN.search(open(toml, encoding="utf-8").read())
        if m:
            pins.append((m.group(2), toml, TOML_PIN))
    except OSError:
        pass
    for path in scripts(proj):
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        m = PIN.search(text)
        if m:
            pins.append((m.group(2), path, PIN))
        elif os.path.dirname(path) != proj and ANDROID.search(text) and "buildToolsVersion" not in text:
            unpinned_module = True
        elif os.path.dirname(path) == proj and re.search(r"^\s*android\s*\{", text, re.M):
            unpinned_module = True
    if not agp and not pins:
        return None
    return {"pins": pins, "agp": agp, "default": bool(agp) and (unpinned_module or not pins)}


def in_use():
    """({версия: [кто]}, [проекты, где версию по умолчанию узнать не удалось])."""
    used, unknown = {}, []
    for proj in projects():
        u = usage(proj)
        if not u:
            continue
        name = os.path.basename(proj)
        for v in sorted({p[0] for p in u["pins"]}):
            used.setdefault(v, []).append(name)
        if u["default"]:
            d = agp_default(u["agp"])
            if d:
                used.setdefault(d, []).append(f"{name} (AGP {u['agp']} по умолчанию)")
            else:
                unknown.append(f"{name} (AGP {u['agp']})")
    return used, unknown


def latest():
    try:
        with urllib.request.urlopen(REPOSITORY, timeout=20) as r:
            text = r.read().decode()
    except Exception:
        return None
    stable = [v for v in re.findall(r'path="build-tools;([^"]+)"', text) if re.fullmatch(r"\d+\.\d+\.\d+", v)]
    return max(stable, key=version_key) if stable else None


def status():
    if not SDK:
        print("Android SDK не найден (ANDROID_HOME или ~/Library/Android/sdk).")
        return
    used, unknown = in_use()
    inst = installed()
    newest = latest() if has_flag(args, "--check") else None
    print(paint(f"Build-tools в {tilde(os.path.join(SDK, 'build-tools'))}:", "1;34"))
    if not used and not inst:
        print("  не установлены, и проекты их не просят")
    for v in sorted(set(used) | set(inst), key=version_key, reverse=True):
        who = ", ".join(used.get(v, []))
        line = f"  {v:<10} " + (f"проекты: {who}" if who else paint("не используется", "33"))
        if v in inst:
            hint = paint("   -> mnrh build-tools clean", "2") if not who and not unknown else ""
            line += paint(f"   {human(sizes([inst[v]]))}", "2") + hint
        else:
            line += paint("   не установлена, AGP скачает при сборке", "2")
        print(line)
    if unknown:
        print(paint(f"\nНе узнал версию по умолчанию: {', '.join(unknown)}. AGP этой версии нет в кеше Gradle, "
                    f"собери проект один раз. Пока так, clean ничего не удаляет.", "33"))
    if newest:
        pinned = [v for v in used if any("по умолчанию" not in w for w in used[v])]
        behind = sorted({v for v in pinned if version_key(v) < version_key(newest)}, key=version_key)
        print(f"\nПоследняя версия build-tools: {newest}"
              + (f". Прописаны старее: {', '.join(behind)}" if behind else "."))
        if behind:
            print(paint("Обновить: mnrh build-tools update", "2"))
        print(paint("Где версия не прописана, ее выбирает AGP, она меняется вместе с AGP.", "2"))


def clean():
    if not SDK:
        print("Android SDK не найден.")
        return
    used, unknown = in_use()
    if not used and not unknown:
        print("Ни один проект не похож на Android — не трогаю ничего, чтобы не удалить нужное.")
        return
    if unknown:
        print(f"Не узнал версию по умолчанию: {', '.join(unknown)}. Собери эти проекты один раз, "
              f"чтобы AGP попал в кеш Gradle, и повтори. Пока ничего не удаляю.")
        return
    targets = {v: p for v, p in installed().items() if v not in used}
    if not targets:
        print(f"Удалять нечего: стоят только нужные проектам ({', '.join(sorted(used, key=version_key))}).")
        return
    total = sizes(list(targets.values()))
    print(f"Будет удалено {human(total)}: build-tools {', '.join(sorted(targets, key=version_key))}")
    print(paint(f"Остаются: {', '.join(sorted(used, key=version_key))}. Удаленная версия скачается снова, "
                f"если какой-то проект ее попросит.", "2"))
    if not confirm("Удалить?", assume_yes):
        print("Отменено.")
        return
    for p in targets.values():
        remove_under_home(p)
    left = [v for v in targets if os.path.exists(targets[v])]
    if left:
        print(f"{BAD} Не удалил {', '.join(left)}: SDK не в домашней папке, удали сам.")
    print(f"Готово, освобождено около {human(total - sizes([targets[v] for v in left]))}.")


def update_one(proj, pins, target):
    files = sorted({path for _, path, _ in pins})
    rels = [os.path.relpath(f, proj) for f in files]
    dirty = subprocess.run(["git", "-C", proj, "status", "--porcelain", "--"] + rels,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        return False, f"есть незакоммиченные правки в {', '.join(rels)}, пропускаю"
    for path, rel in zip(files, rels):
        text = open(path, encoding="utf-8").read()
        for _, p, rx in pins:
            if p == path:
                text = rx.sub(lambda m: m.group(1) + target + m.group(3)
                              if version_key(m.group(2)) < version_key(target) else m.group(0), text)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return True, f"изменены: {', '.join(rels)}"


def update():
    target = None
    if "--version" in args:
        i = args.index("--version")
        target = args[i + 1] if i + 1 < len(args) else None
    target = target or latest()
    if not target:
        sys.exit("Не узнал последнюю версию build-tools: нет сети? Укажи сам: mnrh build-tools update --version 37.0.0")
    names = [a for a in args[1:] if not a.startswith("-") and a != target]
    behind = []
    for proj in projects():
        u = usage(proj)
        old = [p for p in (u or {}).get("pins", []) if version_key(p[0]) < version_key(target)]
        if old:
            behind.append((proj, old))
    if names:
        behind = [b for b in behind if any(os.path.basename(b[0]).startswith(n) for n in names)]
    if not behind:
        print(f"Нигде не прописана версия build-tools старее {target}." if not names else "Среди названных отстающих нет.")
        print(paint("Где версия не прописана, ее выбирает AGP: обнови AGP.", "2"))
        return
    def row(b):
        vs = ", ".join(sorted({p[0] for p in b[1]}, key=version_key))
        return f"{os.path.basename(b[0]):<22} {vs} → {target}"
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not assume_yes
    if interactive:
        from menu import choice, pick_many
        rows = [(os.path.basename(b[0]), row(b)) for b in behind]
        marked = set(range(len(rows)))
        while True:
            picked = pick_many(rows, title=f"\x1b[1;35mmnrh build-tools update\x1b[0m  \x1b[2mподнять до {target}\x1b[0m",
                               marked=marked, summary=lambda m: f"отмечено {len(m)}")
            if not picked:
                print("Ничего не менял.")
                return
            marked = picked
            names_line = ", ".join(os.path.basename(behind[i][0]) for i in sorted(picked))
            answer = choice(f"Поднять build-tools в {names_line} до {target}?",
                            [("go", "Обновить", "yд"), ("back", "← К списку", "nн"), ("quit", "Выйти", "qй")],
                            default=1, back="back")
            if answer == "go":
                break
            if answer == "quit":
                print("Ничего не менял.")
                return
        behind = [behind[i] for i in sorted(picked)]
    elif not assume_yes and not has_flag(args, "--all") and not names:
        print("Не терминал, выбрать нельзя. Без вопросов: mnrh build-tools update --all -y "
              "или mnrh build-tools update <проект> -y")
        sys.exit(2)
    print(paint(f"Поднимаю build-tools до {target}: {', '.join(os.path.basename(p) for p, _ in behind)}", "1;34"))
    done = []
    for proj, pins in behind:
        ok, msg = update_one(proj, pins, target)
        print(f"  {OK if ok else BAD} {os.path.basename(proj)}: {msg}")
        if ok:
            done.append(os.path.basename(proj))
    if done:
        missing = "" if target in installed() else f" Build-tools {target} еще не стоят, AGP скачает их при первой сборке."
        print(paint(f"\nГотово: {', '.join(done)}. Изменения не закоммичены — собери проект и закоммить.{missing} "
                    f"Старые версии убрать потом: mnrh build-tools clean", "2"))


if args and args[0] == "clean":
    clean()
elif args and args[0] == "update":
    update()
else:
    status()
