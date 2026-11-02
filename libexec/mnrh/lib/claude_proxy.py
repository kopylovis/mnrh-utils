"""Прокси через свой VPS только для одной сессии Claude Code.

mihomo слушает HTTP/SOCKS на 127.0.0.1 и отправляет всё через подписку 3x-ui (VLESS, Hysteria2: какой жив).
К серверу он ходит через физический интерфейс, мимо TUN-туннелей VPN вроде Happ.
Системный прокси и TUN не трогаем: через VPS идёт только процесс, которому дали HTTPS_PROXY, и его потомки.
mihomo запускается с первой такой сессией и останавливается после последней.
"""
import fcntl
import getpass
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import BAD, HOME, OK, WARN, paint, tilde  # noqa: E402

CONF_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "mnrh", "proxy")
CONFIG = os.path.join(CONF_DIR, "config.yaml")
STATE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"), "mnrh", "proxy")
HOLDERS = os.path.join(STATE, "holders")
PIDFILE = os.path.join(STATE, "mihomo.pid")
LOCK = os.path.join(STATE, "lock")
LOG = os.path.join(STATE, "mihomo.log")
LAST = os.path.join(STATE, "last")
EXIT = os.path.join(STATE, "exit.json")
SUB_URL = os.path.join(CONF_DIR, "subscription.url")
SUB_FILE = os.path.join(CONF_DIR, "subscription.txt")
DEFAULT_PORT = 17890
CHECK_URL = "https://1.1.1.1/cdn-cgi/trace"
PROBE_URL = "https://www.gstatic.com/generate_204"
NO_PROXY = "localhost,127.0.0.1,::1,*.local"

TEMPLATE = """# mnrh claude proxy: через VPS идут только сессии mnrh claude -x. Файл создаёт mnrh claude proxy setup.
# mnrh-country: {country}
mixed-port: {port}
bind-address: 127.0.0.1
allow-lan: false
mode: rule
log-level: warning
interface-name: en0
ipv6: false
proxy-providers:
  vps:
    type: file
    path: ./subscription.txt{filter}
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
proxy-groups:
  - name: VPS
    type: fallback
    use: [vps]
    proxies: [REJECT]
    url: https://www.gstatic.com/generate_204
    interval: 300
rules:
  - MATCH,VPS
"""


class ProxyError(Exception):
    pass


def mihomo_bin():
    found = shutil.which("mihomo")
    if found:
        return found
    for path in ("/opt/homebrew/bin/mihomo", "/usr/local/bin/mihomo"):
        if os.access(path, os.X_OK):
            return path
    return None


def configured():
    return os.path.isfile(CONFIG)


def setting(name):
    try:
        with open(CONFIG) as f:
            m = re.search(r"^# mnrh-" + name + r":[ \t]*(\S*)", f.read(), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


def port():
    try:
        with open(CONFIG) as f:
            m = re.search(r"^mixed-port:\s*(\d+)", f.read(), re.M)
        return int(m.group(1)) if m else DEFAULT_PORT
    except OSError:
        return DEFAULT_PORT


def listening(p):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", p)) == 0


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_pid():
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    cmd = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return pid if os.path.basename(cmd) == "mihomo" else None


def holders():
    try:
        names = os.listdir(HOLDERS)
    except OSError:
        return []
    live = []
    for name in names:
        if name.isdigit() and alive(int(name)):
            live.append(int(name))
        else:
            try:
                os.remove(os.path.join(HOLDERS, name))
            except OSError:
                pass
    return live


class locked:
    def __enter__(self):
        os.makedirs(STATE, exist_ok=True)
        self.f = open(LOCK, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def physical_interface():
    """Интерфейс маршрута по умолчанию. VPN вроде Happ в режиме TUN ставят свои маршруты 0/1 и 128.0/1,
    а default оставляют на Wi-Fi или Ethernet: его и берём, чтобы ходить к серверу мимо их туннеля."""
    out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True).stdout
    m = re.search(r"interface:\s*(\S+)", out)
    return m.group(1) if m and not m.group(1).startswith("utun") else None


def pin_interface():
    iface = physical_interface()
    with open(CONFIG) as f:
        text = f.read()
    line = f"interface-name: {iface}" if iface else ""
    new = re.sub(r"^interface-name:.*$", line, text, flags=re.M) if re.search(r"^interface-name:", text, re.M) \
        else text.replace("log-level: warning\n", f"log-level: warning\n{line}\n" if line else "log-level: warning\n")
    if new != text:
        fd = os.open(CONFIG + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(new)
        os.replace(CONFIG + ".tmp", CONFIG)


def write_private(path, data):
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


def refresh_subscription():
    """Скачать подписку мимо VPN (сами, а не mihomo). Не вышло: остаётся прошлая копия, если она есть."""
    try:
        with open(SUB_URL) as f:
            url = f.read().strip()
    except OSError:
        url = ""
    if url:
        iface = physical_interface()
        cmd = ["curl", "-sf", "--max-time", "15", "-A", "mihomo"] + (["--interface", iface] if iface else []) + [url]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and r.stdout.strip():
            write_private(SUB_FILE, r.stdout)
            return True
    if not os.path.exists(SUB_FILE):
        raise ProxyError("не скачал подписку и прошлой копии нет. Проверь ссылку: mnrh claude proxy setup")
    print("mnrh claude proxy: подписка не скачалась, беру прошлую копию", file=sys.stderr)
    return False


def start():
    """mihomo на нашем порту: уже запущенный или новый. Вызывать под locked()."""
    p = port()
    if read_pid() and listening(p):
        return
    pin_interface()
    refresh_subscription()
    if listening(p):
        raise ProxyError(f"порт {p} занят другой программой. Поменяй mixed-port в {tilde(CONFIG)}")
    exe = mihomo_bin()
    if not exe:
        raise ProxyError("нет mihomo: brew install mihomo")
    os.makedirs(STATE, exist_ok=True)
    with open(LOG, "a") as log:
        proc = subprocess.Popen([exe, "-d", CONF_DIR, "-f", CONFIG], stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    with open(PIDFILE, "w") as f:
        f.write(str(proc.pid))
    for _ in range(100):
        if listening(p) or proc.poll() is not None:
            break
        time.sleep(0.1)
    if not listening(p):
        raise ProxyError(f"mihomo не поднялся на порту {p}, смотри {tilde(LOG)}")
    # порт открывается раньше, чем mihomo прочитает подписку: ждём первого настоящего ответа через VPS
    deadline = time.time() + 15
    while time.time() < deadline:
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "4",
                            "-x", f"http://127.0.0.1:{p}", PROBE_URL], capture_output=True, text=True)
        if r.stdout.strip() == "204":
            break
        time.sleep(0.5)
    else:
        print(f"mnrh claude proxy: VPS пока не отвечает, запускаю всё равно (лог {tilde(LOG)})", file=sys.stderr)
    got = trace(proxy_env(p))
    try:
        with open(EXIT, "w") as f:
            json.dump({"ip": got[0], "loc": got[1]} if got else {}, f)
    except OSError:
        pass
    want = setting("country").upper()
    if want:
        if not got or (got[1] or "").upper() != want:
            stop()
            raise ProxyError(f"выход через {fmt(got)}, а нужен {want}. Сессию через прокси не запускаю, "
                             f"проверь сервер: mnrh claude proxy test")


def stop():
    pid = read_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(30):
            if not alive(pid):
                break
            time.sleep(0.1)
    for path in (PIDFILE, EXIT):
        try:
            os.remove(path)
        except OSError:
            pass
    return pid is not None


def proxy_env(p):
    url = f"http://127.0.0.1:{p}"
    env = dict(os.environ)
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        env[name] = url
    env["ALL_PROXY"] = env["all_proxy"] = f"socks5h://127.0.0.1:{p}"
    env["NO_PROXY"] = env["no_proxy"] = NO_PROXY
    env["MNRH_PROXY"] = str(p)
    # промптсилл (строка состояния) покажет «proxy KZ»: страна выхода, которую проверили при старте mihomo
    try:
        with open(EXIT) as f:
            loc = json.load(f).get("loc")
    except (OSError, ValueError):
        loc = None
    if isinstance(loc, str) and re.match(r"^[A-Za-z]{2}$", loc):
        env["PROMPTSILL_PROXY_LABEL"] = loc.upper()
    return env


def run(cmd):
    if not cmd:
        print("mnrh claude proxy run -- <команда>", file=sys.stderr)
        return 2
    if not configured():
        print(f"mnrh claude proxy: прокси не настроен, сначала mnrh claude proxy setup", file=sys.stderr)
        return 2
    with locked():
        try:
            start()
        except ProxyError as e:
            print(f"mnrh claude proxy: {e}", file=sys.stderr)
            return 1
        os.makedirs(HOLDERS, exist_ok=True)
        mark = os.path.join(HOLDERS, str(os.getpid()))
        open(mark, "w").close()
    # Ctrl+C и Ctrl+\ достаются Claude Code, а не нам: мы только ждём и убираем за собой.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGQUIT, signal.SIG_IGN)
    try:
        child = subprocess.Popen(cmd, env=proxy_env(port()))
    except OSError as e:
        print(f"mnrh claude proxy: не запустил {cmd[0]}: {e}", file=sys.stderr)
        child = None

    def forward(sig, _frame):
        if child and child.poll() is None:
            child.send_signal(sig)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, forward)
    code = 127
    if child:
        while True:
            try:
                code = child.wait()
                break
            except InterruptedError:
                continue
    with locked():
        try:
            os.remove(mark)
        except OSError:
            pass
        if not holders():
            stop()
    return code if code >= 0 else 128 - code


def trace(env=None):
    proxy = env.get("HTTPS_PROXY") if env else None
    cmd = ["curl", "-s", "--max-time", "12"] + (["-x", proxy] if proxy else []) + [CHECK_URL]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return (info.get("ip"), info.get("loc")) if info.get("ip") else None


def check():
    """Запустить mihomo, если надо, и сравнить выход напрямую и через VPS."""
    with locked():
        start()
        was_held = bool(holders())
    try:
        direct, via = trace(), None
        for _ in range(3):  # подписку mihomo качает при старте, первая попытка может не успеть
            via = trace(proxy_env(port()))
            if via:
                break
            time.sleep(2)
        return direct, via
    finally:
        with locked():
            if not was_held and not holders():
                stop()


def write_config(url, p, country="", only=""):
    os.makedirs(CONF_DIR, mode=0o700, exist_ok=True)
    os.chmod(CONF_DIR, 0o700)
    tmp = CONFIG + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(TEMPLATE.format(port=p, country=country.upper(),
                                filter=f"\n    filter: {json.dumps(only)}" if only else ""))
    os.replace(tmp, CONFIG)
    write_private(SUB_URL, url.encode())
    for old in (SUB_FILE, os.path.join(CONF_DIR, "vps.yaml")):
        if os.path.exists(old):
            os.remove(old)


def free_port():
    p = DEFAULT_PORT
    while listening(p) and p < DEFAULT_PORT + 50:
        p += 1
    return p


def install_mihomo():
    brew = shutil.which("brew") or next((b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
                                         if os.access(b, os.X_OK)), None)
    if not brew:
        print(f"{BAD} нет mihomo и нет Homebrew. Поставь mihomo: https://wiki.metacubex.one")
        return False
    if sys.stdin.isatty():
        answer = input("Нужен mihomo. Поставить через brew install mihomo? [Y/n] ").strip().lower()
        if answer not in ("", "y", "yes", "д", "да"):
            return False
    print("brew install mihomo …")
    subprocess.run([brew, "install", "mihomo"])
    return bool(mihomo_bin())


def setup(args):
    if not mihomo_bin() and not install_mihomo():
        print(f"{BAD} без mihomo прокси не заработает: brew install mihomo")
        return 1
    opts, rest, it = {}, [], iter(args)
    for a in it:
        if a in ("--country", "--only"):
            opts[a[2:]] = next(it, "")
        else:
            rest.append(a)
    country, only = opts.get("country", "").strip(), opts.get("only", "").strip()
    if country and not re.match(r"^[A-Za-z]{2}$", country):
        print(f"{BAD} --country ждёт код страны из двух букв, например KZ")
        return 2
    if only:
        try:
            re.compile(only)
        except re.error as e:
            print(f"{BAD} --only: неправильное регулярное выражение ({e})")
            return 2
    url = rest[0] if rest else ""
    if not url:
        if not sys.stdin.isatty():
            print("mnrh claude proxy setup <ссылка на подписку>", file=sys.stderr)
            return 2
        print("Ссылка на подписку из 3x-ui (та же, что в Happ). На экране её видно не будет.")
        url = getpass.getpass("Подписка: ").strip()
    if url.startswith("happ://"):
        print(f"{BAD} это зашифрованная ссылка Happ (happ://crypt…), её читает только сам Happ. "
              "Нужна обычная https://… ссылка на подписку от того, кто её выдал")
        return 2
    if not re.match(r"^https?://\S+$", url):
        print(f"{BAD} это не ссылка на подписку: нужна https://…, а не vless:// или hysteria2://")
        return 2
    with locked():
        if holders():
            print(f"{WARN} сейчас идут сессии через VPS ({len(holders())}). Закрой их и повтори setup.")
            return 1
        stop()
        old = port() if configured() else None
        p = old if old and not listening(old) else free_port()
        write_config(url, p, country, only)
    print(f"{OK} настройка в {tilde(CONFIG)} (права 600), порт {p}"
          + (f", только серверы «{only}»" if only else "") + (f", выход должен быть в {country.upper()}" if country else ""))
    try:
        direct, via = check()
    except ProxyError as e:
        print(f"{BAD} {e}")
        return 1
    if not via:
        print(f"{BAD} через VPS ничего не открылось. Проверь подписку, лог: {tilde(LOG)}")
        return 1
    print(f"{OK} напрямую {fmt(direct)}, через VPS {fmt(via)}")
    print("Запуск: mnrh claude <проект> -x, или выбери «через VPS» в меню mnrh claude.")
    return 0


def fmt(t):
    return f"{t[0]} ({t[1]})" if t else "не открылось"


def status(test=False):
    if not configured():
        print(f"{WARN} прокси не настроен: mnrh claude proxy setup")
        return 1
    p, pid, live = port(), read_pid(), holders()
    want = setting("country")
    print(f"{OK} настроен: {tilde(CONFIG)}, порт {p}" + (f", выход должен быть в {want}" if want else ""))
    if not mihomo_bin():
        print(f"{BAD} нет mihomo: brew install mihomo")
    if pid and listening(p):
        print(f"{OK} mihomo работает (pid {pid}), сессий через VPS {len(live)}")
    else:
        print(paint("  mihomo не запущен: он поднимется вместе с первой сессией через VPS", "2"))
    if os.environ.get("MNRH_PROXY"):
        print(f"{OK} эта сессия идёт через VPS")
    if test:
        try:
            direct, via = check()
        except ProxyError as e:
            print(f"{BAD} {e}")
            return 1
        print(f"{OK if via else BAD} напрямую {fmt(direct)}, через VPS {fmt(via)}")
        return 0 if via else 1
    return 0


def remove():
    with locked():
        if holders():
            print(f"{WARN} сейчас идут сессии через VPS. Закрой их и повтори.")
            return 1
        stop()
    if os.path.isdir(CONF_DIR):
        shutil.rmtree(CONF_DIR)
    print(f"{OK} прокси удалён: {tilde(CONF_DIR)}")
    return 0


def remember(choice):
    if choice not in ("vps", "direct"):
        return 2
    os.makedirs(STATE, exist_ok=True)
    with open(LAST, "w") as f:
        f.write(choice)
    return 0


def last():
    try:
        with open(LAST) as f:
            return f.read().strip()
    except OSError:
        return "direct"


USAGE = """mnrh claude proxy                 статус: настроен ли, запущен ли mihomo, сколько сессий через VPS
mnrh claude proxy test            сравнить выход напрямую и через VPS (IP и страна)
mnrh claude proxy setup [ссылка] [--country KZ] [--only KZ]
                                  настроить по ссылке на подписку (https://…, как в Happ).
                                  --country: если выход окажется в другой стране, сессия не запустится;
                                  --only: брать из подписки только серверы, чьё имя подходит под это выражение
mnrh claude proxy remove          удалить настройку
mnrh claude proxy run -- <cmd>    запустить команду через VPS (так делает mnrh claude -x)

Через VPS идёт только сессия, запущенная с -x (или «через VPS» в меню), и всё, что она запускает:
git, npm, curl, MCP-серверы. Остальные программы ходят напрямую. Нужен mihomo: brew install mihomo.
Он слушает 127.0.0.1:{port}, стартует с первой такой сессией и останавливается после последней.
К серверу mihomo ходит через Wi-Fi или Ethernet в обход TUN Happ и других VPN, поэтому Happ можно
включать и выключать во время сессии. Если живых серверов нет, соединения отклоняются, а не идут напрямую."""


def main(argv):
    cmd = argv[0] if argv else "status"
    rest = argv[1:]
    if cmd in ("-h", "--help", "help"):
        print(USAGE.format(port=port()))
        return 0
    if cmd == "status":
        return status()
    if cmd == "test":
        return status(test=True)
    if cmd == "setup":
        return setup(rest)
    if cmd == "remove":
        return remove()
    if cmd == "run":
        return run(rest[1:] if rest[:1] == ["--"] else rest)
    if cmd == "configured":
        return 0 if configured() and mihomo_bin() else 1
    if cmd == "remember":
        return remember(rest[0] if rest else "")
    if cmd == "last":
        print(last())
        return 0
    print(f"mnrh claude proxy: не знаю «{cmd}»\n\n{USAGE.format(port=port())}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
