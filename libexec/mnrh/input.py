import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import BAD, CONFIG, OK, WARN, bundle_info, find_apps, has_flag, paint, run
from swiftagent import SwiftAgent

args = sys.argv[1:]
ACTIONS = ("on", "off", "set", "unset", "remember")
if has_flag(args, "-h", "--help") or (args and args[0] not in ACTIONS):
    print("mnrh input                        раскладка под приложение: правила, включено ли")
    print("mnrh input on                     включить; при первом включении среда разработки получит en")
    print("mnrh input set <Приложение> en    всегда эта раскладка в приложении (en, ru или id раскладки)")
    print("mnrh input unset <Приложение>     убрать правило")
    print("mnrh input remember on|off        остальным приложениям возвращать раскладку, с которой их оставили")
    print("mnrh input off                    выключить")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)

agent = SwiftAgent("input", "mnrh input", "com.mnrh.input")
CONF = os.path.join(os.path.dirname(CONFIG), "input.json")
# Среда разработки и терминалы: в них почти всегда нужна английская раскладка.
DEV_APPS = ["com.google.android.studio", "com.google.android.studio-EAP", "com.apple.dt.Xcode", "com.apple.Terminal",
            "com.googlecode.iterm2", "com.microsoft.VSCode", "com.jetbrains.intellij", "com.jetbrains.intellij.ce",
            "com.jetbrains.fleet", "dev.warp.Warp-Stable", "com.mitchellh.ghostty", "net.kovidgoyal.kitty",
            "com.torusknot.SourceTreeNotMAS", "com.termius-dmg.mac", "com.postmanlabs.mac", "com.sublimetext.4",
            "dev.zed.Zed", "com.todesktop.230313mzl4w4u92"]


def layouts():
    """[(id, название, [языки])], текущая. Список раскладок знает только Swift-помощник."""
    agent.build()
    out = run([agent.bin, "--list"])
    items, current = [], None
    for line in out.splitlines():
        parts = line.split("\t")
        if parts[0] == "current":
            current = parts[1]
        elif len(parts) >= 3:
            items.append((parts[0], parts[1], parts[2].split(",")))
    return items, current


def resolve_layout(word, items):
    for lid, name, langs in items:
        if word in (lid, name) or word.lower() == name.lower():
            return lid
    lang = {"en": "en", "eng": "en", "английская": "en", "ru": "ru", "rus": "ru", "русская": "ru"}.get(word.lower(), word)
    for lid, _, langs in items:
        if langs and langs[0] == lang:
            return lid
    return None


def load():
    try:
        with open(CONF) as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    c.setdefault("rules", {})
    c.setdefault("remember", True)
    return c


def save(c):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    with open(CONF, "w") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)


def app_names():
    """bundle id -> имя установленного приложения, для вывода."""
    names = {}
    out = run(["mdfind", "-attr", "kMDItemCFBundleIdentifier", "kMDItemContentType == 'com.apple.application-bundle'"],
              timeout=30)
    for line in out.splitlines():
        path, _, rest = line.partition("   kMDItemCFBundleIdentifier = ")
        if rest and rest != "(null)":
            names.setdefault(rest.strip(), os.path.basename(path.strip())[:-4])
    return names


def pick_app(query):
    apps = find_apps(query)
    if len(apps) != 1:
        if apps:
            print("Подходит несколько, уточни: " + ", ".join(os.path.basename(a)[:-4] for a in apps))
        else:
            print(f"Не нашёл приложение «{query}»")
        sys.exit(2)
    bundle, name = bundle_info(apps[0])
    if not bundle:
        sys.exit(f"У «{name}» нет идентификатора")
    return bundle, os.path.basename(apps[0])[:-4]


def apply_changes(c):
    save(c)
    if agent.installed() and not agent.state():
        agent.start()


def status():
    c = load()
    items, current = layouts()
    names = {lid: name for lid, name, _ in items}
    s = agent.state()
    if not agent.installed():
        print("mnrh input выключен." + paint("   -> mnrh input on", "2"))
    elif s:
        print(f"{OK} mnrh input работает, переключений раскладки: {s.get('switches', 0)}")
    else:
        print(f"{BAD} mnrh input включён, но помощник не работает" + paint("   -> mnrh input on", "2"))
    print(f"Раскладки: {', '.join(n for _, n, _ in items)} (сейчас {names.get(current, current)})")
    apps = app_names() if c["rules"] else {}
    if c["rules"]:
        print(paint("\nВсегда:", "1"))
        for bundle, lid in sorted(c["rules"].items(), key=lambda kv: apps.get(kv[0], kv[0]).lower()):
            print(f"  {apps.get(bundle, bundle):<28} {names.get(lid, lid)}")
    print(f"\nОстальные приложения: " + ("раскладка, с которой их оставили" if c["remember"]
                                        else "не трогаю") + paint("   -> mnrh input remember on|off", "2"))


def on():
    c = load()
    items, _ = layouts()
    if not os.path.exists(CONF) or not load()["rules"]:
        en = resolve_layout("en", items)
        installed = app_names()
        if en:
            c["rules"] = {b: en for b in DEV_APPS if b in installed}
            print(f"Для начала английская в: {', '.join(installed[b] for b in c['rules'])}")
    save(c)
    agent.build()
    agent.write_plist(["--config", CONF])
    if agent.start():
        print(f"{OK} mnrh input включён и будет запускаться при входе. Правила: mnrh input")
    else:
        print(f"{BAD} Помощник не запустился. Лог: {agent.log}")


def off():
    was = agent.installed()
    agent.uninstall()
    print("mnrh input выключен." if was else "mnrh input и так выключен.")


def set_rule():
    words = [a for a in args[1:] if not a.startswith("-")]
    if len(words) < 2:
        sys.exit("mnrh input set <Приложение> en|ru|<раскладка>")
    items, _ = layouts()
    lid = resolve_layout(words[-1], items)
    if not lid:
        sys.exit(f"Нет раскладки «{words[-1]}». Есть: {', '.join(n for _, n, _ in items)}")
    bundle, name = pick_app(" ".join(words[:-1]))
    c = load()
    c["rules"][bundle] = lid
    apply_changes(c)
    print(f"{OK} {name}: всегда {dict((i, n) for i, n, _ in items)[lid]}")


def unset_rule():
    query = " ".join(a for a in args[1:] if not a.startswith("-"))
    if not query:
        sys.exit("mnrh input unset <Приложение>")
    bundle, name = pick_app(query)
    c = load()
    if c["rules"].pop(bundle, None) is None:
        print(f"Для {name} правила и не было.")
        return
    apply_changes(c)
    print(f"{OK} {name}: правило убрано")


def remember():
    if len(args) < 2 or args[1] not in ("on", "off"):
        sys.exit("mnrh input remember on|off")
    c = load()
    c["remember"] = args[1] == "on"
    apply_changes(c)
    print(f"{OK} Остальным приложениям " + ("возвращаю их раскладку." if c["remember"] else "раскладку не трогаю."))


{"status": status, "on": on, "off": off, "set": set_rule, "unset": unset_rule,
 "remember": remember}[args[0] if args else "status"]()
