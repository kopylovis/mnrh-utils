import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import BAD, HOME, OK, WARN, has_flag, paint, run

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or (args and args[0] not in ("--speed",)):
    print("mnrh net           почему не качаются зависимости: сеть, DNS, VPN, прокси, доступность")
    print("                   Maven Central, Google Maven, Gradle, CocoaPods, GitHub, Apple")
    print("mnrh net --speed   ещё скорость и отклик (networkQuality, около 20 секунд)")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)

SERVICES = [
    ("Gradle", "https://services.gradle.org/versions/current"),
    ("Gradle плагины", "https://plugins.gradle.org/m2/"),
    ("Maven Central", "https://repo.maven.apache.org/maven2/"),
    ("Google Maven", "https://dl.google.com/dl/android/maven2/index.html"),
    ("JitPack", "https://jitpack.io/"),
    ("CocoaPods CDN", "https://cdn.cocoapods.org/CocoaPods-version.yml"),
    ("GitHub", "https://github.com/"),
    ("GitHub API", "https://api.github.com/"),
    ("Apple", "https://developer.apple.com/"),
    ("npm", "https://registry.npmjs.org/"),
]
problems = []


def section(title):
    print(f"\n{paint(title, '1')}")


def item(level, text):
    print(f"  {level} {text}")
    if level == BAD:
        problems.append(text)


def default_route():
    out = run(["route", "-n", "get", "default"])
    gw = re.search(r"gateway: (\S+)", out)
    iface = re.search(r"interface: (\S+)", out)
    return (gw.group(1) if gw else None), (iface.group(1) if iface else None)


def ping(host):
    out = run(["ping", "-c", "3", "-i", "0.3", "-t", "4", host], timeout=8)
    loss = re.search(r"([\d.]+)% packet loss", out)
    avg = re.search(r"= [\d.]+/([\d.]+)/", out)
    return (float(loss.group(1)) if loss else 100.0), (float(avg.group(1)) if avg else None)


def check_route():
    section("Сеть")
    gw, iface = default_route()
    if not gw:
        item(BAD, "нет маршрута в интернет: ни Wi-Fi, ни кабеля, или сеть не выдала адрес")
        return False
    kind = "VPN" if iface and iface.startswith(("utun", "ipsec", "ppp")) else iface
    loss, avg = ping(gw) if not iface.startswith("utun") else (0.0, None)
    if iface.startswith("utun"):
        item(OK, f"весь трафик идёт через VPN ({iface})")
    elif loss >= 100:
        item(WARN, f"шлюз {gw} ({kind}) не отвечает на ping — бывает, что роутер его просто запрещает")
    elif loss > 0 or (avg and avg > 50):
        item(WARN, f"шлюз {gw} ({kind}): потери {loss:.0f}%, отклик {avg or 0:.0f} мс — слабый Wi-Fi?")
    else:
        item(OK, f"шлюз {gw} через {kind}, отклик {avg:.0f} мс")
    return True


def check_vpn():
    connected = [l for l in run(["scutil", "--nc", "list"]).splitlines() if "(Connected)" in l]
    names = [re.search(r'"([^"]+)"', l).group(1) for l in connected if re.search(r'"([^"]+)"', l)]
    tunnels = [i for i in run(["ifconfig", "-l"]).split() if i.startswith("utun")]
    active = [t for t in tunnels if re.search(r"inet \d", run(["ifconfig", t]))]
    if names:
        item(WARN, f"подключён VPN: {', '.join(names)} — если Maven или Google не открываются, попробуй без него")
    elif active:
        item(WARN, f"есть туннель {', '.join(active)} с адресом — похоже на VPN из стороннего приложения")
    else:
        item(OK, "VPN не подключён")


def check_dns():
    section("DNS")
    servers = []
    for m in re.finditer(r"nameserver\[\d+\] : (\S+)", run(["scutil", "--dns"])):
        if m.group(1) not in servers:
            servers.append(m.group(1))
    if servers:
        item(OK, "серверы: " + ", ".join(servers[:4]))
    slow, failed = [], []
    for host in ("repo.maven.apache.org", "dl.google.com", "github.com"):
        start = time.time()
        try:
            socket.getaddrinfo(host, 443)
            took = (time.time() - start) * 1000
            if took > 300:
                slow.append(f"{host} {took:.0f} мс")
        except socket.gaierror:
            failed.append(host)
    if failed:
        item(BAD, f"не находятся адреса: {', '.join(failed)} — попробуй mnrh fix dns или другой DNS")
    elif slow:
        item(WARN, "DNS отвечает медленно: " + ", ".join(slow))
    else:
        item(OK, "имена находятся быстро")
    try:
        hosts = [l for l in open("/etc/hosts") if l.strip() and not l.startswith("#")
                 and any(h in l for h in ("maven", "google", "gradle", "github", "cocoapods", "apple"))]
        if hosts:
            item(WARN, "в /etc/hosts переопределены: " + "; ".join(h.split()[-1] for h in hosts))
    except OSError:
        pass


def gradle_proxy():
    props = {}
    for path in (os.path.join(HOME, ".gradle", "gradle.properties"),):
        try:
            for line in open(path):
                m = re.match(r"\s*systemProp\.(https?)\.proxyHost\s*=\s*(\S+)", line)
                if m:
                    props[m.group(1)] = m.group(2)
        except OSError:
            pass
    return props


def check_proxy():
    section("Прокси")
    sc = run(["scutil", "--proxy"])
    system = {}
    for kind in ("HTTP", "HTTPS", "SOCKS"):
        if re.search(rf"{kind}Enable : 1", sc):
            host = re.search(rf"{kind}Proxy : (\S+)", sc)
            port = re.search(rf"{kind}Port : (\d+)", sc)
            system[kind] = f"{host.group(1) if host else '?'}:{port.group(1) if port else '?'}"
    pac = re.search(r"ProxyAutoConfigURLString : (\S+)", sc) if "ProxyAutoConfigEnable : 1" in sc else None
    env = {k: v for k, v in os.environ.items() if k.lower() in ("http_proxy", "https_proxy", "all_proxy")}
    gradle = gradle_proxy()
    if not system and not pac and not env and not gradle:
        item(OK, "прокси нигде не задан")
        return
    global SYSTEM_PROXY
    SYSTEM_PROXY = ("http://" + system["HTTPS"]) if "HTTPS" in system else ("http://" + system["HTTP"]) if "HTTP" in system else None
    if system:
        item(WARN, "системный прокси: " + ", ".join(f"{k} {v}" for k, v in system.items()))
    if pac:
        item(WARN, f"автонастройка прокси (PAC): {pac.group(1)}")
    if env:
        item(WARN, "в окружении: " + ", ".join(f"{k}={v}" for k, v in env.items()))
    if gradle:
        item(OK, "у Gradle свой прокси: " + ", ".join(f"{k} {v}" for k, v in gradle.items()))
    elif system or pac:
        item(WARN, "Gradle системный прокси не видит: нужен systemProp.https.proxyHost/Port в ~/.gradle/gradle.properties")


SYSTEM_PROXY = None


def probe(url):
    """curl, а не urllib: системный Python 3.9 со старым LibreSSL зависает на части сайтов (jitpack)."""
    cmd = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code} %{time_total}", "--max-time", "10", url]
    if SYSTEM_PROXY:
        cmd[1:1] = ["-x", SYSTEM_PROXY]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return None, 15000, "timed out"
    parts = r.stdout.split()
    code = int(parts[0]) if parts and parts[0].isdigit() and parts[0] != "000" else None
    ms = float(parts[1]) * 1000 if len(parts) > 1 else 0
    return code, ms, None if code else (r.stderr.strip() or "нет ответа")


def check_services():
    section("Откуда качаются зависимости")
    with ThreadPoolExecutor(max_workers=len(SERVICES)) as pool:
        results = list(pool.map(lambda s: probe(s[1]), SERVICES))
    for (name, url), (code, ms, err) in zip(SERVICES, results):
        host = url.split("/")[2]
        if code and code < 500:
            level = WARN if ms > 2000 else OK
            item(level, f"{name:<15} {ms:>5.0f} мс  {paint(host, '2')}" + ("  медленно" if level == WARN else ""))
        elif code:
            item(WARN, f"{name:<15} сервер ответил {code}  {paint(host, '2')}")
        else:
            e = (err or "").lower()
            why = ("таймаут" if "timed out" in e or "timeout" in e else "сертификат" if "certificate" in e
                   else "имя не находится" if "resolve" in e else "соединение отклонено" if "refused" in e else err)
            item(BAD, f"{name:<15} недоступен: {why}  {paint(host, '2')}")
    if any("сертификат" in p for p in problems):
        print(paint("  Ошибка сертификата обычно значит, что трафик перехватывает прокси или антивирус,"
                    " либо на Mac сбились дата и время.", "2"))


def check_speed():
    section("Скорость")
    out = run(["networkQuality", "-s"], timeout=90)
    down = re.search(r"Downlink capacity: ([\d.]+ \S+)", out)
    up = re.search(r"Uplink capacity: ([\d.]+ \S+)", out)
    resp = re.search(r"Downlink Responsiveness: (.+)", out) or re.search(r"Responsiveness: (.+)", out)
    if down:
        item(OK, f"вниз {down.group(1)}, вверх {up.group(1) if up else '?'}"
                 + (f", отзывчивость {resp.group(1).strip()}" if resp else ""))
    else:
        item(WARN, "networkQuality не ответил")


if check_route():
    check_vpn()
    check_dns()
    check_proxy()
    check_services()
    if has_flag(args, "--speed"):
        check_speed()
print()
print(f"{BAD} Проблем: {len(problems)}" if problems else f"{OK} Всё доступно.")
sys.exit(1 if problems else 0)
