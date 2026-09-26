import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import has_flag, process_commands, run

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or not args or args[0] != "scroll":
    print("mnrh fix scroll   прокрутка тачпада перевернулась после сна: перезапустить Scroll Reverser")
    print("                  (и похожие: Mos, LinearMouse, UnnaturalScrollWheels), без перезагрузки Mac")
    sys.exit(0 if has_flag(args, "-h", "--help") or not args else 2)

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
    running = [(name, found) for name in SCROLL_APPS if (found := app_pids(name))]
    if not running:
        print("Scroll Reverser и похожие программы не запущены — переворачивает не они.")
        print("Попробуй выключить и включить «Естественную прокрутку»: Настройки → Трекпад → Прокрутка и масштаб.")
        return 1
    ok = True
    for name, found in running:
        if restart(name, found):
            print(f"{name} перезапущен. Прокрутка тачпада должна вернуться в норму.")
        else:
            ok = False
            print(f"{name} остановлен, но не запустился снова. Открой его вручную: open -a \"{name}\"")
    return 0 if ok else 1


sys.exit(scroll())
