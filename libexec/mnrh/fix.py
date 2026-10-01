import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import OK, BAD, WARN, adb_path, has_flag, process_commands, run, settings_name, settings_path
from swiftagent import SwiftAgent

args = sys.argv[1:]

SCROLL_APPS = ("Scroll Reverser", "Mos", "LinearMouse", "UnnaturalScrollWheels")


def app_pids(name):
    marker = f"/{name}.app/Contents/MacOS/"
    return [(int(pid), cmd.split(marker)[0] + f"/{name}.app")
            for pid, cmd in process_commands().items() if marker in cmd]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def restart(name, found):
    pids = [pid for pid, _ in found]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    for _ in range(20):
        if not any(alive(p) for p in pids):
            break
        time.sleep(0.25)
    else:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.5)
    run(["open", "-a", found[0][1]])
    for _ in range(20):
        if app_pids(name):
            return True
        time.sleep(0.25)
    return False


def scroll():
    helper = SwiftAgent("scroll", "mnrh Scroll", "com.mnrh.scroll", service_args=[])
    agent = helper.loaded()
    if agent:
        helper.restart()
        print("mnrh scroll перезапущен. Состояние: mnrh scroll")
    running = [(name, found) for name in SCROLL_APPS if (found := app_pids(name))]
    if agent and not running:
        return 0
    if not running:
        print("Scroll Reverser и похожие программы не запущены — переворачивает не они.")
        print(f"Попробуй выключить и включить «{settings_name('natural')}»: "
              f"{settings_path('settings', 'trackpad', 'scroll')}.")
        return 1
    ok = True
    for name, found in running:
        if restart(name, found):
            print(f"{name} перезапущен. Прокрутка тачпада должна вернуться в норму.")
        else:
            ok = False
            print(f"{name} остановлен, но не запустился снова. Открой его вручную: open -a \"{name}\"")
    return 0 if ok else 1


def sudo(cmd):
    """Команда от root: в терминале sudo спросит пароль, без терминала — только подсказка."""
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0 or sys.stdin.isatty():
        return subprocess.run(["sudo", *cmd]).returncode == 0
    print(f"{WARN} Нужен пароль администратора. Запусти в Терминале: sudo {' '.join(cmd)}")
    return False


def restart_process(*names):
    running = [n for n in names if run(["pgrep", "-x", n]).strip()]
    for n in running:
        run(["killall", n])
    return running


def dock():
    restart_process("Dock")
    print(f"{OK} Dock перезапущен (вместе с ним Mission Control и Launchpad).")


def finder():
    restart_process("Finder")
    print(f"{OK} Finder перезапущен.")


def menubar():
    done = restart_process("SystemUIServer", "ControlCenter")
    print(f"{OK} Строка меню перезапущена: {', '.join(done) or 'нечего было перезапускать'}.")


def wifi():
    ports = run(["networksetup", "-listallhardwareports"])
    m = re.search(r"Hardware Port: Wi-Fi\nDevice: (\S+)", ports)
    if not m:
        print(f"{BAD} Wi-Fi не найден.")
        return 1
    dev = m.group(1)
    run(["networksetup", "-setairportpower", dev, "off"])
    time.sleep(2)
    run(["networksetup", "-setairportpower", dev, "on"])
    for _ in range(20):
        time.sleep(1)
        if re.search(r"inet \d", run(["ifconfig", dev])):
            print(f"{OK} Wi-Fi ({dev}) выключен и включён, адрес получен.")
            return 0
    print(f"{WARN} Wi-Fi ({dev}) включён, но адреса пока нет — подожди или проверь сеть и VPN")
    return 1


def sim():
    run(["xcrun", "simctl", "shutdown", "all"], timeout=120)
    run(["killall", "-9", "com.apple.CoreSimulator.CoreSimulatorService"])
    time.sleep(2)
    ok = run(["xcrun", "simctl", "list", "devices", "-j"], timeout=60).strip().startswith("{")
    print(f"{OK} Служба симуляторов перезапущена, simctl отвечает." if ok
          else f"{BAD} simctl не отвечает. Помогает перезапуск Xcode или Mac.")
    return 0 if ok else 1


def adb():
    path = adb_path()
    if not path:
        print(f"{BAD} adb не найден (Android SDK: ~/Library/Android/sdk).")
        return 1
    run([path, "kill-server"])
    run([path, "start-server"], timeout=60)
    devices = [l for l in run([path, "devices"]).splitlines()[1:] if l.strip()]
    print(f"{OK} adb перезапущен. Устройств: {len(devices)}")
    for line in devices:
        serial, state = line.split()[:2]
        note = {"unauthorized": " — подтверди отладку на телефоне", "offline": " — переподключи кабель"}.get(state, "")
        print(f"  {serial}  {state}{note}")
    return 0


def dns():
    if sudo(["dscacheutil", "-flushcache"]) and sudo(["killall", "-HUP", "mDNSResponder"]):
        print(f"{OK} Кеш DNS сброшен.")
        return 0
    return 1


def bluetooth():
    if sudo(["pkill", "bluetoothd"]):
        print(f"{OK} Bluetooth перезапущен: устройства переподключатся через несколько секунд.")
        return 0
    return 1


def audio():
    if sudo(["killall", "coreaudiod"]):
        print(f"{OK} Звук перезапущен (coreaudiod). Если идёт звонок — он на секунду прервётся.")
        return 0
    return 1


FIXES = {
    "scroll": ("прокрутка трекпада перевернулась после сна: перезапустить mnrh scroll или Scroll Reverser", scroll),
    "dock": ("Dock, Mission Control или Launchpad зависли", dock),
    "finder": ("Finder завис или не обновляет файлы", finder),
    "menubar": ("иконки в строке меню пропали или не реагируют", menubar),
    "wifi": ("Wi-Fi подключён, но интернета нет: выключить и включить", wifi),
    "sim": ("симулятор не запускается, simctl висит: перезапустить CoreSimulator", sim),
    "adb": ("adb не видит телефон или эмулятор: перезапустить сервер adb", adb),
    "dns": ("сайты не открываются после VPN или смены сети: сбросить кеш DNS (sudo)", dns),
    "bt": ("Bluetooth-наушники или мышь не подключаются: перезапустить Bluetooth (sudo)", bluetooth),
    "audio": ("пропал звук или не переключается выход: перезапустить coreaudiod (sudo)", audio),
}

if not args or has_flag(args, "-h", "--help") or args[0] not in FIXES:
    for name, (desc, _) in FIXES.items():
        print(f"mnrh fix {name:<8} {desc}")
    sys.exit(0 if not args or has_flag(args, "-h", "--help") else 2)
sys.exit(FIXES[args[0]][1]() or 0)
