"""Сон с закрытой крышкой, пока идут caffeinate-сессии Claude.

caffeinate -is держит Mac от простоя, а от сети и с закрытой крышкой. На батарее крышку
перебивает только pmset disablesleep 1: его держит сторож, пока жива хоть одна caffeinate-сессия
Claude, и снимает через GRACE секунд после последней или когда батарея садится до LOW_BATTERY.
pmset нужен root: mnrh sleep lid setup ставит правило sudoers ровно на две команды.
"""
import getpass
import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import HOME, run, tilde

STATE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"), "mnrh")
PIDFILE = os.path.join(STATE, "lid.pid")
LOG = os.path.join(STATE, "lid.log")
SUDOERS = "/etc/sudoers.d/mnrh-pmset"
PMSET = "/usr/bin/pmset"
ON = [PMSET, "-a", "disablesleep", "1"]
OFF = [PMSET, "-a", "disablesleep", "0"]
GRACE = 120  # перезапуск сессии с claude update бывает дольше минуты
POLL = 15
LOW_BATTERY = 15
SESSION = re.compile(r"^caffeinate(\s+-\S+)*\s+\S*/?claude(\s|$)")


def ready():
    """Правило sudoers на месте: pmset можно звать без пароля.

    Одного sudo -n мало: с закешированным паролем он тоже проходит, сторож включил бы
    disablesleep, а когда кеш истечёт, уже не смог бы его снять.
    """
    return os.path.exists(SUDOERS) and all(
        subprocess.run(["sudo", "-n", "-l", *c], capture_output=True).returncode == 0 for c in (ON, OFF))


def disabled():
    return re.search(r"^\s*SleepDisabled\s+1\b", run([PMSET, "-g"]), re.M) is not None


def set_disabled(on):
    return subprocess.run(["sudo", "-n", *(ON if on else OFF)], capture_output=True).returncode == 0


def sessions():
    """pid caffeinate, которые держат сессии Claude (mnrh claude -k, open_claude, перезапуск)."""
    out = run(["ps", "-axo", "pid=,args="])
    return [int(l.split(None, 1)[0]) for l in out.splitlines()
            if len(l.split(None, 1)) == 2 and SESSION.match(l.split(None, 1)[1])]


def battery():
    """(от сети ли, процент или None)"""
    out = run([PMSET, "-g", "batt"])
    pct = re.search(r"(\d+)%", out)
    return "AC Power" in out, int(pct.group(1)) if pct else None


def watcher_pid():
    try:
        pid = int(open(PIDFILE).read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def log(text):
    os.makedirs(STATE, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")


def notify(text):
    subprocess.run(["osascript", "-e", f'display notification "{text}" with title "mnrh sleep"'],
                   capture_output=True)


def watch():
    os.makedirs(STATE, exist_ok=True)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    def stop(*_):
        raise SystemExit(0)  # прерывает и time.sleep, finally снимет запрет
    signal.signal(signal.SIGTERM, stop)
    held, low, idle_since = False, False, None
    log("сторож запущен")
    try:
        while True:
            ac, pct = battery()
            if not ac and pct is not None and pct <= LOW_BATTERY:
                if not low:
                    low = True
                    notify(f"Батарея {pct}%: Mac снова уснёт, если закрыть крышку")
                    log(f"батарея {pct}%, отпускаю")
            elif low and (ac or (pct or 0) > LOW_BATTERY + 5):
                low = False
            alive = bool(sessions())
            idle_since = None if alive else (idle_since or time.time())
            if idle_since and time.time() - idle_since >= GRACE:
                break
            want = alive and not low
            if want != held or (want and not disabled()):
                if set_disabled(want):
                    held = want
                    log("держу: крышка не усыпляет" if want else "отпустил")
                else:
                    log("sudo -n pmset не сработал: mnrh sleep lid setup")
                    break
            time.sleep(POLL)
    finally:
        if held or disabled():
            set_disabled(False)
            log("отпустил при выходе")
        try:
            os.remove(PIDFILE)
        except OSError:
            pass


def keep():
    """Запустить сторожа, если правило есть и он ещё не идёт. Тихо ничего не делает без правила."""
    if watcher_pid() or not ready():
        return False
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "watch"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, cwd="/")
    return True


def release():
    pid = watcher_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not watcher_pid():
                break
            time.sleep(0.25)
    if disabled():
        set_disabled(False)


def setup():
    user = getpass.getuser()
    rule = f"{user} ALL=(root) NOPASSWD: {' '.join(ON)}, {' '.join(OFF)}\n"
    tmp = os.path.join(STATE, "mnrh-pmset.sudoers")
    os.makedirs(STATE, exist_ok=True)
    with open(tmp, "w") as f:
        f.write(rule)
    if subprocess.run(["/usr/sbin/visudo", "-cf", tmp], capture_output=True).returncode:
        sys.exit("visudo не принял правило, ничего не поставил")
    print(f"Ставлю {SUDOERS}, sudo спросит пароль:\n  {rule.strip()}")
    r = subprocess.run(["sudo", "install", "-m", "0440", "-o", "root", "-g", "wheel", tmp, SUDOERS])
    os.remove(tmp)
    if r.returncode:
        sys.exit("не поставил")
    print("Готово. Caffeinate-сессии Claude теперь не дают Mac уснуть и с закрытой крышкой на батарее.")


def remove():
    release()
    r = subprocess.run(["sudo", "rm", "-f", SUDOERS])
    print("Правило убрано, крышка снова усыпляет Mac." if r.returncode == 0 else "не убрал")


def status_lines():
    lines = []
    if not ready():
        lines.append("Крышка: закрытая крышка на батарее усыпит Mac и в caffeinate-сессии. "
                     "Чтобы не усыпляла: mnrh sleep lid setup")
        return lines
    n = len(sessions())
    if disabled():
        lines.append(f"Крышка: Mac не уснёт и с закрытой крышкой, caffeinate-сессий Claude {n}"
                     + (f", сторож pid {watcher_pid()}" if watcher_pid() else ", сторожа нет — mnrh sleep fix"))
    else:
        lines.append("Крышка: правило есть, сейчас не держу" + (f" (сессий {n}, сторож их подхватит)" if n else ""))
    return lines


def main(args):
    cmd = args[0] if args else "status"
    if cmd in ("-h", "--help"):
        print("mnrh sleep lid           держит ли сейчас крышка Mac от сна")
        print("mnrh sleep lid setup     правило sudoers для pmset: caffeinate-сессии Claude не уснут и с закрытой")
        print("                         крышкой на батарее (до 15% заряда)")
        print("mnrh sleep lid remove    убрать правило")
        print("mnrh sleep lid off       отпустить сейчас")
        return 0
    if cmd == "watch":
        watch()
    elif cmd == "keep":
        keep()
    elif cmd == "setup":
        setup()
    elif cmd == "remove":
        remove()
    elif cmd == "off":
        release()
        print("Отпустил: крышка снова усыпляет Mac.")
    elif cmd == "status":
        print("\n".join(status_lines()))
        if os.path.exists(LOG):
            print(f"Журнал сторожа: {tilde(LOG)}")
    else:
        print(f"mnrh sleep lid: не знаю «{cmd}», см. mnrh sleep lid -h", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
