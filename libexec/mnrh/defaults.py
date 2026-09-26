import os
import plistlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import CONFIG, HOME, OK, WARN, confirm, has_flag, paint, run

args = sys.argv[1:]
ACTIONS = ("apply", "undo", "snap", "diff")
if has_flag(args, "-h", "--help") or (args and args[0] not in ACTIONS):
    print("mnrh defaults               удобные настройки macOS для разработки: что уже включено, что нет")
    print("mnrh defaults apply [имя…]  включить все (или выбранные); старые значения запоминаются")
    print("mnrh defaults undo          вернуть всё, как было до apply")
    print("mnrh defaults snap          запомнить все настройки, потом поменять что-то в Настройках…")
    print("mnrh defaults diff          …и получить готовые команды defaults write для этой перемены")
    print("   -y                       не спрашивать")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)
assume_yes = has_flag(args, "-y", "--yes")

STATE_DIR = os.path.dirname(CONFIG)
UNDO = os.path.join(STATE_DIR, "defaults-undo.plist")
SNAP = os.path.join(STATE_DIR, "defaults-snapshot.plist")
G = "NSGlobalDomain"

# имя, что даёт, домен, ключ, значение, кого перезапустить
PRESET = [
    ("extensions", "Finder показывает расширения всех файлов", G, "AppleShowAllExtensions", True, "Finder"),
    ("hidden", "Finder показывает скрытые файлы (.gradle, .idea, .env)", "com.apple.finder", "AppleShowAllFiles", True, "Finder"),
    ("pathbar", "строка пути внизу окна Finder", "com.apple.finder", "ShowPathbar", True, "Finder"),
    ("statusbar", "строка состояния в Finder: сколько файлов и места", "com.apple.finder", "ShowStatusBar", True, "Finder"),
    ("search-here", "поиск в Finder ищет в текущей папке, а не по всему Mac", "com.apple.finder", "FXDefaultSearchScope", "SCcf", "Finder"),
    ("no-ext-warning", "без вопроса при смене расширения файла", "com.apple.finder", "FXEnableExtensionChangeWarning", False, "Finder"),
    ("no-ds-store-net", "не оставлять .DS_Store на сетевых дисках", "com.apple.desktopservices", "DSDontWriteNetworkStores", True, None),
    ("no-ds-store-usb", "не оставлять .DS_Store на флешках", "com.apple.desktopservices", "DSDontWriteUSBStores", True, None),
    ("key-repeat", "быстрый повтор клавиш (нужен выход из системы)", G, "KeyRepeat", 2, "logout"),
    ("key-delay", "короткая задержка перед повтором (нужен выход из системы)", G, "InitialKeyRepeat", 15, "logout"),
    ("no-press-hold", "зажатая клавиша повторяется, а не показывает меню с «ё», «é»", G, "ApplePressAndHoldEnabled", False, "logout"),
    ("no-smart-quotes", "без «умных» кавычек: код из заметок и чатов не ломается", G, "NSAutomaticQuoteSubstitutionEnabled", False, None),
    ("no-smart-dashes", "без автозамены -- на тире", G, "NSAutomaticDashSubstitutionEnabled", False, None),
    ("no-autocorrect", "без автоисправления слов", G, "NSAutomaticSpellingCorrectionEnabled", False, None),
    ("save-expanded", "окно «Сохранить» сразу развёрнуто", G, "NSNavPanelExpandedStateForSaveMode", True, None),
    ("dock-fast", "Dock появляется без задержки (если он скрывается)", "com.apple.dock", "autohide-delay", 0.0, "Dock"),
    ("spaces-fixed", "рабочие столы не переставляются сами по последнему использованию", "com.apple.dock", "mru-spaces", False, "Dock"),
    ("screenshots-dir", "скриншоты в ~/Pictures/Screenshots, а не на рабочий стол", "com.apple.screencapture", "location",
     os.path.join(HOME, "Pictures", "Screenshots"), "SystemUIServer"),
    ("xcode-build-time", "Xcode показывает, сколько шла сборка", "com.apple.dt.Xcode", "ShowBuildOperationDuration", True, None),
]
ABSENT = "__mnrh_absent__"


def export(domain):
    try:
        out = subprocess.run(["defaults", "export", domain, "-"], capture_output=True, timeout=20).stdout
        return plistlib.loads(out) if out else {}
    except Exception:
        return {}


def write(domain, key, value):
    if isinstance(value, bool):
        typed = ["-bool", "true" if value else "false"]
    elif isinstance(value, int):
        typed = ["-int", str(value)]
    elif isinstance(value, float):
        typed = ["-float", str(value)]
    else:
        typed = ["-string", str(value)]
    subprocess.run(["defaults", "write", domain, key, *typed])


def delete(domain, key):
    subprocess.run(["defaults", "delete", domain, key], capture_output=True)


def load_plist(path):
    try:
        with open(path, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return {}


def save_plist(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(data, f)


def restart(targets):
    for t in sorted(targets - {"logout"}):
        run(["killall", t])
    if "logout" in targets:
        print(f"{WARN} Клавиатура поменяется после выхода из системы и входа обратно.")


def same(a, b):
    return a == b or (isinstance(b, bool) and a in (0, 1) and bool(a) == b)


def status():
    cache = {}
    for name, desc, domain, key, value, _ in PRESET:
        cur = cache.setdefault(domain, export(domain)).get(key, ABSENT)
        mark = OK if same(cur, value) else paint("·", "2")
        print(f"  {mark} {name:<17} {desc}")
    undo = load_plist(UNDO)
    print(paint("\n✓ — уже так. Включить всё: mnrh defaults apply, выборочно: mnrh defaults apply hidden pathbar", "2"))
    if undo:
        print(paint("Вернуть как было до apply: mnrh defaults undo", "2"))


def apply():
    names = [a for a in args[1:] if not a.startswith("-")]
    unknown = [n for n in names if n not in {p[0] for p in PRESET}]
    if unknown:
        sys.exit(f"Нет таких настроек: {', '.join(unknown)}. Список: mnrh defaults")
    chosen = [p for p in PRESET if not names or p[0] in names]
    cache, todo = {}, []
    for p in chosen:
        _, _, domain, key, value, _ = p
        cur = cache.setdefault(domain, export(domain)).get(key, ABSENT)
        if not same(cur, value):
            todo.append((p, cur))
    if not todo:
        print("Всё выбранное уже включено.")
        return
    for (name, desc, *_), _ in todo:
        print(f"  {name:<17} {desc}")
    if not confirm(f"Включить {len(todo)}?", assume_yes):
        print("Отменено.")
        return
    undo = load_plist(UNDO)
    restarts = set()
    for (name, _, domain, key, value, who), cur in todo:
        undo.setdefault(f"{domain}\t{key}", cur)  # запоминаем только самое первое значение
        if key == "location":
            os.makedirs(value, exist_ok=True)
        write(domain, key, value)
        if who:
            restarts.add(who)
    save_plist(UNDO, undo)
    restart(restarts)
    print(f"{OK} Включено: {len(todo)}. Вернуть: mnrh defaults undo")


def undo():
    saved = load_plist(UNDO)
    if not saved:
        print("Откатывать нечего: mnrh defaults apply ещё не запускался.")
        return
    restarts = set()
    who_by_key = {(p[2], p[3]): p[5] for p in PRESET}
    for k, value in saved.items():
        domain, key = k.split("\t", 1)
        if value == ABSENT:
            delete(domain, key)
        else:
            write(domain, key, value)
        if who_by_key.get((domain, key)):
            restarts.add(who_by_key[(domain, key)])
    os.unlink(UNDO)
    restart(restarts)
    print(f"{OK} Вернул как было: {len(saved)}")


def all_domains():
    out = run(["defaults", "domains"], timeout=30)
    return [G] + [d.strip() for d in out.split(",") if d.strip()]


def snapshot():
    domains = all_domains()
    with ThreadPoolExecutor(max_workers=8) as pool:
        data = dict(zip(domains, pool.map(export, domains)))
    return data


def snap():
    start = time.time()
    data = snapshot()
    save_plist(SNAP, {"taken": time.time(), "domains": data})
    print(f"{OK} Запомнил {len(data)} доменов за {time.time() - start:.0f} с. Поменяй что нужно в Настройках"
          " или приложении и запусти mnrh defaults diff")


def literal(value):
    if isinstance(value, bool):
        return f"-bool {'true' if value else 'false'}"
    if isinstance(value, int):
        return f"-int {value}"
    if isinstance(value, float):
        return f"-float {value}"
    if isinstance(value, str):
        return "-string '" + value.replace("'", "'\\''") + "'"
    return None


def diff():
    old = load_plist(SNAP)
    if not old:
        sys.exit("Сначала mnrh defaults snap")
    before, after = old.get("domains", {}), snapshot()
    lines, complex_ = [], []
    for domain in sorted(set(before) | set(after)):
        a, b = before.get(domain, {}), after.get(domain, {})
        for key in sorted(set(a) | set(b)):
            if key in a and key in b and a[key] == b[key]:
                continue
            if key not in b:
                lines.append(f"defaults delete {domain} '{key}'")
            elif literal(b[key]):
                lines.append(f"defaults write {domain} '{key}' {literal(b[key])}")
            else:
                complex_.append(f"{domain} {key}")
    # Счётчики и отметки времени меняются сами — прячем очевидный шум.
    noise = ("LastUpdate", "Timestamp", "timestamp", "Date", "Count", "WindowFrame", "NSWindow Frame",
             "NSStatusItem", "Recent", "Last", "lastUsed", "SUEnableAutomaticChecks", "SULastCheckTime")
    shown = [l for l in lines if not any(n in l for n in noise)]
    if not shown and not complex_:
        print("Ничего не поменялось (кроме служебных счётчиков).")
        return
    print("\n".join(shown))
    if complex_:
        print(paint("\nИзменились сложные значения (словари, массивы), их проще посмотреть через defaults read:", "2"))
        for c in complex_[:15]:
            print(paint(f"  {c}", "2"))
    hidden = len(lines) - len(shown)
    if hidden:
        print(paint(f"\nСкрыто служебных изменений: {hidden}", "2"))


{"status": status, "apply": apply, "undo": undo, "snap": snap, "diff": diff}[args[0] if args else "status"]()
