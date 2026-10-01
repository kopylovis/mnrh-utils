import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import HOME, OK, WARN, BAD, has_flag, paint, process_commands, run, tilde
from swiftagent import SwiftAgent

args = sys.argv[1:]
ACTIONS = ("on", "off", "pause", "resume", "log", "remove")
if has_flag(args, "-h", "--help") or (args and args[0] not in ACTIONS):
    print("mnrh notunes                 состояние: работает ли, сколько раз закрыл Music")
    print("mnrh notunes on              не давать запускаться Apple Music (вместо noTunes)")
    print("   --open <приложение>|none  что открыть вместо Music, например Spotify (по умолчанию ничего)")
    print("mnrh notunes pause [мин]     пустить Music на время (по умолчанию 30 мин)")
    print("mnrh notunes resume          снова не пускать")
    print("mnrh notunes log             последние записи: когда и после чего закрыт Music")
    print("mnrh notunes off             выключить и убрать из автозапуска")
    print("mnrh notunes remove          выключить и удалить помощника с диска")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)
cmd = args[0] if args else "status"

CONFIG = os.path.join(HOME, ".config", "mnrh", "notunes.json")
agent = SwiftAgent("notunes", "mnrh noTunes", "com.mnrh.notunes", service_args=["--config", CONFIG])
ORIGINAL = "noTunes"
ORIGINAL_DOMAIN = "digital.twisted.noTunes"
APP_DIRS = ("/Applications", os.path.join(HOME, "Applications"), "/System/Applications",
            "/Applications/Utilities")


def read_conf():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_conf(**values):
    conf = read_conf()
    conf.update(values)
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(conf, f)
    os.replace(tmp, CONFIG)


def find_app(name):
    if name.endswith(".app") and os.path.isdir(os.path.expanduser(name)):
        return os.path.abspath(os.path.expanduser(name))
    base = name[:-4] if name.endswith(".app") else name
    for d in APP_DIRS:
        p = os.path.join(d, base + ".app")
        if os.path.isdir(p):
            return p
    for query in (f'kMDItemCFBundleIdentifier == "{base}"', f'kMDItemDisplayName == "{base}"cd'):
        for p in run(["mdfind", f'kMDItemContentType == "com.apple.application-bundle" && {query}'], timeout=10).splitlines():
            if p.endswith(".app"):
                return p
    return None


def original_pids():
    return [int(pid) for pid, c in process_commands().items() if f"/{ORIGINAL}.app/Contents/MacOS/" in c]


def original_replacement():
    r = subprocess.run(["defaults", "read", ORIGINAL_DOMAIN, "replacement"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def quit_original():
    if not original_pids():
        return False
    run(["osascript", "-e", f'quit app "{ORIGINAL}"'], timeout=10)
    time.sleep(1)
    for pid in original_pids():
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    return True


def original_hint():
    cask = shutil.which("brew") and "notunes" in run(["brew", "list", "--cask"], timeout=30).split()
    if cask:
        print(paint(f"  {ORIGINAL} больше не нужен: brew uninstall --cask notunes", "2"))
    elif os.path.isdir(f"/Applications/{ORIGINAL}.app"):
        print(paint(f"  {ORIGINAL} больше не нужен: перенеси /Applications/{ORIGINAL}.app в Корзину", "2"))


def replacement_label(conf):
    path = conf.get("replacement") or ""
    return os.path.basename(path)[:-4] if path else "ничего"


def on():
    conf = read_conf()
    if "replacement" not in conf:
        old = original_replacement()
        conf["replacement"] = (find_app(old) or "") if old else ""
    if "--open" in args:
        i = args.index("--open")
        value = args[i + 1] if i + 1 < len(args) else ""
        if not value:
            sys.exit("mnrh notunes: после --open нужно приложение или none")
        if value.lower() in ("none", "off", "-"):
            conf["replacement"] = ""
        else:
            path = find_app(value)
            if not path:
                sys.exit(f"mnrh notunes: не нашёл приложение «{value}»")
            conf["replacement"] = path
    write_conf(replacement=conf["replacement"], paused_until=0)
    if agent.source_hash() != (open(agent.stamp).read().strip() if os.path.exists(agent.stamp) else ""):
        agent.stop()
    agent.build()
    had_original = quit_original()
    started = agent.start()
    if started == "requiresApproval":
        if not agent.approve_login_item():
            return
        started = agent.start()
    if not started:
        sys.exit(f"Помощник не запустился. Лог: {tilde(agent.log)}")
    print(f"{OK} mnrh notunes включён: Apple Music не запустится, помощник стартует при входе в систему.")
    print(f"  вместо Music открывать: {replacement_label(read_conf())}")
    if had_original:
        print(f"{ORIGINAL} закрыт, теперь Music закрывает mnrh.")
    if had_original or os.path.isdir(f"/Applications/{ORIGINAL}.app"):
        original_hint()


def off():
    was = agent.installed()
    agent.uninstall()
    print("mnrh notunes выключен, Music снова запускается." if was else "mnrh notunes и так выключен.")


def remove():
    off()
    shutil.rmtree(agent.app, ignore_errors=True)
    print("Помощник удалён с диска.")


def pause():
    minutes = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
    until = time.time() + minutes * 60
    write_conf(paused_until=until)
    print(f"{OK} Music можно запускать до {datetime.fromtimestamp(until):%H:%M}. Раньше: mnrh notunes resume")
    if not agent.state():
        print(f"{WARN} помощник и так не работает: mnrh notunes on")


def resume():
    write_conf(paused_until=0)
    print(f"{OK} Music снова не запускается." if agent.state() else f"{WARN} помощник не работает: mnrh notunes on")


def show_log():
    try:
        lines = open(agent.log, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        sys.exit("Лога пока нет.")
    for line in lines[-20:]:
        stamp, _, text = line.partition(" ")
        try:
            stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%d.%m %H:%M:%S")
        except ValueError:
            pass
        print(f"{paint(stamp, '2')}  {text}")


def status():
    conf = read_conf()
    if not agent.installed():
        print("mnrh notunes выключен." + paint("   -> mnrh notunes on", "2"))
        if original_pids():
            print(f"Сейчас Music не пускает {ORIGINAL}.")
        return
    s = agent.state()
    if not s:
        print(f"{BAD} mnrh notunes включён, но помощник не работает." + paint("   -> mnrh notunes on", "2"))
        print(f"  лог: {tilde(agent.log)}")
        return
    since = datetime.fromtimestamp(s["started"]).strftime("%d.%m %H:%M")
    until = conf.get("paused_until") or 0
    if until > time.time():
        print(f"{WARN} mnrh notunes на паузе до {datetime.fromtimestamp(until):%H:%M}"
              + paint("   -> mnrh notunes resume", "2"))
    else:
        print(f"{OK} mnrh notunes работает (pid {s['pid']}, с {since}): Apple Music не запустится")
    last = s.get("last_blocked") or 0
    print(f"  закрыл Music: {s.get('blocked', 0)} раз с запуска"
          + (f", последний — {datetime.fromtimestamp(last):%d.%m %H:%M}" if last else "")
          + f" · вместо Music: {replacement_label(conf)}")
    if original_pids():
        print(f"{WARN} Запущен и {ORIGINAL} — он больше не нужен." + paint(f"   -> закрой {ORIGINAL}", "2"))
        original_hint()


{"status": status, "on": on, "off": off, "pause": pause, "resume": resume, "log": show_log,
 "remove": remove}[cmd]()
