import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import HOME, OK, WARN, BAD, has_flag, paint, process_commands, read_config, run, write_config
from swiftagent import SwiftAgent

args = sys.argv[1:]
ACTIONS = ("on", "off", "test", "restart", "remove")
if has_flag(args, "-h", "--help") or (args and args[0] not in ACTIONS):
    print("mnrh scroll                 состояние: работает ли, есть ли доступ, что переворачивается")
    print("mnrh scroll on              включить: мышь переворачивается, трекпад нет (вместо Scroll Reverser)")
    print("   --mouse vh|v|h|on|off    какие оси мыши переворачивать (on = vh) (по умолчанию из Scroll Reverser, иначе v)")
    print("   --trackpad vh|v|h|on|off то же для трекпада (по умолчанию off)")
    print("   --step N                 строк за щелчок колеса мыши, 1 — как в macOS (по умолчанию 3)")
    print("mnrh scroll off             выключить и убрать из автозапуска")
    print("mnrh scroll test            20 секунд показывать, что пришло: мышь или трекпад и что с ним сделано")
    print("mnrh scroll restart         перезапустить помощника")
    print("mnrh scroll remove          выключить и удалить помощника с диска")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)
cmd = args[0] if args else "status"

agent = SwiftAgent("scroll", "mnrh Scroll", "com.mnrh.scroll")
SRC, APP, BIN, PLIST, STATE, LOG = agent.src, agent.app, agent.bin, agent.plist, agent.state_file, agent.log
SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
SR = "Scroll Reverser"
SR_DOMAIN = "com.pilotmoon.scroll-reverser"
AXES = {"vh": "vh", "hv": "vh", "on": "vh", "v": "v", "h": "h", "off": "", "none": "", "": ""}
state, build, stop_agent, start_agent = agent.state, agent.build, agent.stop, agent.start


def sr_pids():
    return [int(pid) for pid, c in process_commands().items() if f"/{SR}.app/Contents/MacOS/" in c]


def describe(spec):
    return {"vh": "переворачивается по обеим осям", "v": "переворачивается по вертикали",
            "h": "переворачивается по горизонтали"}.get(spec, "не трогается")


def settings():
    conf = read_config()
    if "scroll_mouse" in conf:
        return conf["scroll_mouse"], conf.get("scroll_trackpad", ""), int(conf.get("scroll_step", "3") or 3)
    # Первый запуск: берём настройки Scroll Reverser, с его значениями по умолчанию.
    try:
        prefs = plistlib.loads(subprocess.run(["defaults", "export", SR_DOMAIN, "-"],
                                              capture_output=True).stdout or b"")
    except Exception:
        prefs = {}
    if not prefs.get("InvertScrollingOn"):
        return "v", "", 3
    y, x = prefs.get("ReverseY", True), prefs.get("ReverseX", False)

    def spec(on):
        return ("v" if on and y else "") + ("h" if on and x else "")
    return spec(prefs.get("ReverseMouse", True)), spec(prefs.get("ReverseTrackpad", True)), \
        int(prefs.get("DiscreteScrollStepSize", 3))


def option(name, current):
    if name not in args:
        return current
    try:
        value = args[args.index(name) + 1]
    except IndexError:
        sys.exit(f"mnrh scroll: после {name} нужно значение")
    if name == "--step":
        if not value.isdigit() or not 1 <= int(value) <= 20:
            sys.exit("mnrh scroll: --step от 1 до 20")
        return int(value)
    if value not in AXES:
        sys.exit(f"mnrh scroll: {name} принимает vh, v, h, on или off")
    return AXES[value]




def write_plist(mouse, trackpad, step):
    agent.write_plist(["--mouse", mouse or "off", "--trackpad", trackpad or "off", "--step", str(step)])


def quit_scroll_reverser():
    pids = sr_pids()
    if not pids:
        return False
    run(["osascript", "-e", f'quit app "{SR}"'], timeout=10)
    time.sleep(1)
    for pid in sr_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    return True


def wait_for_access():
    s = state()
    if s and s.get("trusted"):
        return True
    print(paint("Нужен доступ.", "1") + " Открываю настройки: Конфиденциальность и безопасность → Универсальный доступ.")
    print("Включи там «mnrh Scroll» (если его нет в списке — «+» и выбери")
    print(f"  {APP.replace(HOME, '~', 1)}).")
    run(["open", SETTINGS_URL])
    if not sys.stdin.isatty():
        print("Как включишь, прокрутка заработает сама, перезапускать ничего не нужно.")
        return False
    print("Жду до двух минут...", end="", flush=True)
    for _ in range(120):
        time.sleep(1)
        s = state()
        if s and s.get("trusted") and s.get("active"):
            print(" есть.")
            return True
    print("\nНе дождался. Как включишь, прокрутка заработает сама: mnrh scroll покажет состояние.")
    return False


def on():
    if not os.path.exists(SRC):
        sys.exit(f"Нет исходника помощника: {SRC}")
    mouse, trackpad, step = settings()
    mouse, trackpad, step = option("--mouse", mouse), option("--trackpad", trackpad), option("--step", step)
    write_config(scroll_mouse=mouse, scroll_trackpad=trackpad, scroll_step=str(step))

    had_binary = os.path.exists(BIN)
    stop_agent()
    rebuilt = build()
    if rebuilt and had_binary:
        subprocess.run(["tccutil", "reset", "Accessibility", agent.label], capture_output=True)
    if quit_scroll_reverser():
        print(f"{SR} закрыт, чтобы прокрутка не переворачивалась дважды.")
    write_plist(mouse, trackpad, step)
    if not start_agent():
        sys.exit(f"Помощник не запустился. Лог: {LOG.replace(HOME, '~', 1)}")
    print(f"{OK} mnrh scroll включён и будет запускаться при входе в систему.")
    print(f"  мышь: {describe(mouse)} · трекпад: {describe(trackpad)} · колесо: {step} стр. за щелчок")
    s = state()
    if rebuilt and had_binary and not (s and s.get("trusted")):
        print(f"{WARN} Помощник пересобран, и macOS считает его новым приложением. В списке Универсального")
        print("  доступа включи «mnrh Scroll» (если его нет — «+» и выбери приложение).")
    if not wait_for_access():
        return
    if os.path.isdir(f"/Applications/{SR}.app"):
        print(f"\n{SR} больше не нужен. Чтобы он не запускался при входе: в его настройках сними")
        print(f"«Start at login» или перенеси /Applications/{SR}.app в Корзину.")


def off():
    stop_agent()
    removed = os.path.exists(PLIST)
    if removed:
        os.unlink(PLIST)
    try:
        os.unlink(STATE)
    except OSError:
        pass
    print("mnrh scroll выключен и убран из автозапуска." if removed else "mnrh scroll и так выключен.")


def remove():
    off()
    shutil.rmtree(APP, ignore_errors=True)
    r = subprocess.run(["tccutil", "reset", "Accessibility", agent.label], capture_output=True, text=True)
    print("Помощник удалён с диска" + (", доступ в настройках сброшен." if r.returncode == 0 else
                                       ". Строку «mnrh Scroll» в Универсальном доступе можно удалить «−»."))


def restart():
    if not os.path.exists(PLIST):
        sys.exit("mnrh scroll не включён: mnrh scroll on")
    s = agent.restart()
    print(f"{OK} Помощник перезапущен." if s else f"{BAD} Помощник не поднялся. Лог: {LOG.replace(HOME, '~', 1)}")


def test():
    s = state()
    if not s:
        sys.exit("Помощник не запущен: mnrh scroll on")
    if not s.get("active"):
        sys.exit("Помощник ждёт доступа в Универсальном доступе — событий он пока не видит.")
    print(run([BIN, "--check"]).strip())
    start = os.path.getsize(LOG) if os.path.exists(LOG) else 0
    os.kill(s["pid"], signal.SIGUSR1)
    print("Покрути мышью и трекпадом, 20 секунд...\n")
    shown, end = start, time.time() + 21
    while time.time() < end:
        time.sleep(0.5)
        try:
            with open(LOG, "rb") as f:
                f.seek(shown)
                chunk = f.read()
        except OSError:
            continue
        shown += len(chunk)
        seen = set()
        for line in chunk.decode("utf-8", "replace").splitlines():
            text = line.split(" ", 1)[-1]
            if text.startswith("scroll ") and text not in seen:
                seen.add(text)
                print("  " + text[len("scroll "):])
            elif not text.startswith("scroll ") and "подробный лог" not in text:
                print("  " + text)


def status():
    if not os.path.exists(PLIST):
        print("mnrh scroll выключен." + paint("   -> mnrh scroll on", "2"))
        if sr_pids():
            print(f"Сейчас прокрутку переворачивает {SR}.")
        return
    conf = read_config()
    s = state()
    if not s:
        print(f"{BAD} mnrh scroll включён, но помощник не работает." + paint("   -> mnrh scroll restart", "2"))
        print(f"  лог: {LOG.replace(HOME, '~', 1)}")
    else:
        since = datetime.fromtimestamp(s["started"]).strftime("%d.%m %H:%M")
        if s.get("trusted") and s.get("active"):
            print(f"{OK} mnrh scroll работает (pid {s['pid']}, с {since})")
        elif s.get("trusted"):
            print(f"{WARN} mnrh scroll: доступ есть, но перехват выключен" + paint("   -> mnrh scroll restart", "2"))
        else:
            print(f"{BAD} mnrh scroll ждёт доступа: Универсальный доступ → «mnrh Scroll»"
                  + paint("   -> mnrh scroll on", "2"))
        print(f"  мышь: {describe(conf.get('scroll_mouse', 'v'))} · трекпад: {describe(conf.get('scroll_trackpad', ''))}"
              f" · колесо: {conf.get('scroll_step', '3')} стр. за щелчок")
        print(f"  событий с запуска: мышь {s.get('mouse_events', 0)}, трекпад {s.get('trackpad_events', 0)} · "
              f"пробуждений после сна: {s.get('wakes', 0)} · перехват восстанавливался: {s.get('reenabled', 0)}")
    if sr_pids():
        print(f"{WARN} Запущен и {SR}: мышь переворачивается дважды, то есть не переворачивается."
              + paint(f"   -> закрой {SR} и убери его из автозапуска", "2"))


{"status": status, "on": on, "off": off, "test": test, "restart": restart, "remove": remove}[cmd]()
