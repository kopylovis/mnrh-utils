import glob
import os
import plistlib
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import BAD, HOME, OK, WARN, has_flag, paint, run, tilde

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or (args and args[0] != "off"):
    print("mnrh login              что запускается при входе и работает в фоне: чьё, подписано ли, запущено ли")
    print("mnrh login off <имя>    убрать из автозапуска (пункт входа или служба launchd; файл — в Корзину)")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)

UID = os.getuid()
PLACES = [
    ("Ваши службы (~/Library/LaunchAgents)", os.path.join(HOME, "Library", "LaunchAgents"), f"gui/{UID}", False),
    ("Службы для всех пользователей (/Library/LaunchAgents)", "/Library/LaunchAgents", f"gui/{UID}", True),
    ("Системные службы от root (/Library/LaunchDaemons)", "/Library/LaunchDaemons", "system", True),
]


def load(path):
    try:
        with open(path, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return None


def program(p):
    prog = p.get("Program") or (p.get("ProgramArguments") or [None])[0]
    if prog and not prog.startswith("/"):
        # launchd ищет относительное имя в своём PATH.
        prog = shutil.which(prog, path="/usr/bin:/bin:/usr/sbin:/sbin") or prog
    if prog in ("/bin/sh", "/bin/bash", "/bin/zsh"):
        rest = [a for a in p.get("ProgramArguments", [])[1:] if a != "-c"]
        return prog, re.sub(r"\s+", " ", " ".join(rest)).strip()[:50]
    return prog, ""


def owner(prog):
    if not prog:
        return "?"
    app = re.search(r"/([^/]+)\.app/", prog)
    return app.group(1) if app else os.path.basename(prog)


def signature(prog):
    if not prog or not os.path.exists(prog):
        return None
    target = re.match(r"(.*?\.app)/", prog)
    out = subprocess.run(["codesign", "-dv", "--verbose=2", target.group(1) if target else prog],
                         capture_output=True, text=True).stderr
    if "not signed at all" in out:
        return "не подписано"
    if "Signature=adhoc" in out:
        return "adhoc"
    auth = re.search(r"Authority=([^\n]+)", out)
    if auth:
        who = auth.group(1)
        m = re.match(r"Developer ID Application: (.+?) \(", who)
        return m.group(1) if m else ("Apple" if "Software Signing" in who else who)
    return "?"


def running_labels():
    pids = {}
    for line in run(["launchctl", "list"]).splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) > 0:
            pids[parts[2]] = int(parts[0])
    for m in re.finditer(r"^\s+(\d+)\s+\S+\s+(\S+)$", run(["launchctl", "print", "system"]), re.M):
        if int(m.group(1)) > 0:
            pids.setdefault(m.group(2), int(m.group(1)))
    return pids


def login_items():
    out = run(["osascript", "-e", 'tell application "System Events" to get {name, path} of every login item'],
              timeout=15).strip()
    if not out:
        return []
    parts = [p.strip() for p in out.split(", ")]
    half = len(parts) // 2
    return list(zip(parts[:half], parts[half:]))


def status():
    pids = running_labels()
    items = login_items()
    if items:
        print(paint("Открываются при входе (Настройки → Основные → Объекты входа):", "1;34"))
        for name, path in items:
            mark = OK if os.path.exists(path) else BAD
            print(f"  {mark} {name:<28} {paint(tilde(path), '2')}" + ("" if os.path.exists(path) else "  файла нет"))
    for title, folder, _, _ in PLACES:
        plists = sorted(glob.glob(os.path.join(folder, "*.plist")))
        if not plists:
            continue
        rows = []
        for path in plists:
            p = load(path)
            if p is None:
                rows.append((BAD, os.path.basename(path)[:-6], "не читается", "", "", ""))
                continue
            label = p.get("Label", os.path.basename(path)[:-6])
            prog, extra = program(p)
            rows.append([label, prog, extra, p])
        with ThreadPoolExecutor(max_workers=8) as pool:
            sigs = list(pool.map(lambda r: signature(r[1]) if len(r) == 4 else None, rows))
        print(f"\n{paint(title + ':', '1')}")
        for r, sig in zip(rows, sigs):
            if len(r) != 4:
                print(f"  {r[0]} {r[1]}  {r[2]}")
                continue
            label, prog, extra, p = r
            missing = prog and not os.path.exists(prog)
            if label.startswith("com.mnrh."):
                sig_note, level = "mnrh", OK
            elif missing:
                sig_note, level = "программы нет", BAD
            elif sig in ("не подписано", "adhoc"):
                sig_note, level = sig, WARN
            else:
                sig_note, level = sig or "?", OK
            state = "работает" if label in pids else ("выключено" if p.get("Disabled") else "ждёт")
            when = "при входе" if p.get("RunAtLoad") else "по событию"
            what = owner(prog)
            print(f"  {level} {label:<44} {what:<24} {paint(f'{sig_note}, {state}, {when}', '2')}")
            if extra:
                print(paint(f"      {os.path.basename(prog)}: {extra}", "2"))
    print(paint("\n✗ — программы уже нет: пункт мусорный. ! — не подписано: стоит понять, откуда оно."
                "\nУбрать: mnrh login off <имя>", "2"))


def off():
    target = " ".join(a for a in args[1:] if not a.startswith("-"))
    if not target:
        sys.exit("mnrh login off <имя или label>")
    low = target.lower()
    for name, _ in login_items():
        if name.lower() == low:
            run(["osascript", "-e", f'tell application "System Events" to delete login item "{name}"'])
            print(f"{OK} «{name}» больше не открывается при входе.")
            return
    for title, folder, domain, needs_root in PLACES:
        for path in glob.glob(os.path.join(folder, "*.plist")):
            p = load(path) or {}
            label = p.get("Label", os.path.basename(path)[:-6])
            if low not in (label.lower(), os.path.basename(path)[:-6].lower()):
                continue
            if needs_root:
                print(f"{WARN} {label} — служба для всей системы, нужен sudo. Выполни в Терминале:")
                print(f"  sudo launchctl bootout {domain}/{label}; sudo mv '{path}' ~/.Trash/")
                return
            run(["launchctl", "bootout", f"{domain}/{label}"])
            dest = os.path.join(HOME, ".Trash", os.path.basename(path))
            shutil.move(path, dest)
            print(f"{OK} {label} остановлен и убран из автозапуска (файл в Корзине).")
            return
    sys.exit(f"Не нашёл «{target}» ни среди объектов входа, ни среди служб: mnrh login")


off() if args else status()
