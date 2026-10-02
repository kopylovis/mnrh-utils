import base64
import getpass
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import HOME, OK, WARN, BAD, has_flag, paint, tilde
from swiftagent import SwiftAgent

args = sys.argv[1:]
ACTIONS = ("status", "setup", "pair", "invite", "join", "rename", "on", "off", "pause", "resume", "send", "images", "log",
           "remove")
if has_flag(args, "-h", "--help") or (args and args[0] not in ACTIONS):
    print("mnrh tossy                    меню с действиями (без терминала — то же, что status)")
    print("mnrh tossy status             состояние: работает ли, связь с сервером, устройства, что передано")
    print("mnrh tossy setup [<сервер>]   подключить свой ntfy-сервер и телефон с приложением Tossy")
    print("mnrh tossy pair [--new]       показать QR для телефона; --new — новый ключ, все устройства подключать заново")
    print("mnrh tossy invite             код для второго Mac (10 минут, один раз)")
    print("mnrh tossy join <код>         подключить этот Mac к комнате другого: mnrh tossy join ntfy.example.com/ABCD-EFGH")
    print("mnrh tossy rename             переименовать устройство в комнате (выбор из списка)")
    print("mnrh tossy rename <имя>       новое имя этого Mac — его увидят все в комнате")
    print("mnrh tossy rename <кто> <имя> как называть другое устройство на этом Mac (пустое имя — вернуть его собственное)")
    print("mnrh tossy send <файл|текст>  отправить текст, картинку или любой файл до 24 МБ на все устройства")
    print("  … | mnrh tossy send         отправить то, что пришло в пайп (текст или файл)")
    print("  --to <кто>                  только этому устройству (имя из status; можно несколько)")
    print("mnrh tossy pause [мин]        не отправлять буфер этого Mac (по умолчанию 30 мин)")
    print("mnrh tossy resume             снова отправлять")
    print("mnrh tossy images on|off      передавать ли картинки")
    print("mnrh tossy log                что передано и почему что-то пропущено")
    print("mnrh tossy on | off           включить или выключить помощника")
    print("mnrh tossy remove             выключить и удалить помощника и настройки")
    print()
    print("Общий буфер обмена твоих Mac и Android-телефона через свой ntfy-сервер: скопировал на одном —")
    print("вставляешь на остальных. Всё шифруется на устройствах, пароли из менеджеров паролей не передаются.")
    print("Прежнее имя команды, mnrh clip, тоже работает.")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)
cmd = args[0] if args else ("menu" if sys.stdin.isatty() and sys.stdout.isatty() else "status")

LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
REGISTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "share", "mnrh",
                        "commands.tsv")
CONFIG = os.path.join(HOME, ".config", "mnrh", "clip.json")
NOTIFY_APP = os.path.join(HOME, "Library", "Application Support", "mnrh", "mnrh Notify.app")
QR = os.path.join(HOME, ".cache", "mnrh", "clip-pair.png")
TOKEN_RE = re.compile(r"^tk_[A-Za-z0-9]{20,}$")
agent = SwiftAgent("clip", "mnrh Clip", "com.mnrh.clip", service_args=["--config", CONFIG])


def read_conf():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_conf(conf):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(conf, f)
    os.replace(tmp, CONFIG)


def write_conf(**values):
    conf = read_conf()
    conf.update(values)
    save_conf(conf)


def drop_conf(key):
    conf = read_conf()
    conf.pop(key, None)
    save_conf(conf)


def configured():
    c = read_conf()
    return all(c.get(k) for k in ("server", "token", "key")) and bool(c.get("room") or c.get("to_mac"))


def migrate():
    c = read_conf()
    if c.get("room") or not c.get("to_mac"):
        if c.get("room") and not c.get("device_id"):
            write_conf(device_id=secrets.token_hex(8))
        return False
    conf = {k: v for k, v in c.items() if k not in ("to_mac", "to_phone")}
    conf.update(room=f"tossy-{secrets.token_hex(12)}", legacy_to_mac=c["to_mac"], legacy_to_phone=c["to_phone"],
                device_id=c.get("device_id") or secrets.token_hex(8))
    save_conf(conf)
    return True


def http(method, url, token, body=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Authorization": f"Bearer {token}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        return 0, str(getattr(e, "reason", e)).encode()


def error_text(raw):
    try:
        return json.loads(raw).get("error") or raw.decode(errors="replace")
    except ValueError:
        return raw.decode(errors="replace").strip()


def read_token():
    if sys.stdin.isatty():
        return getpass.getpass("Токен ntfy (tk_…, при вводе не виден): ").strip()
    clip = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
    if TOKEN_RE.match(clip):
        subprocess.run(["pbcopy"], input="", text=True)
        print("Взял токен из буфера обмена и очистил буфер.")
        return clip
    return ""


def normalize_server(value):
    value = value.strip().rstrip("/")
    if value and not re.match(r"^https?://", value):
        value = "https://" + value
    return value


def new_channel():
    return {"room": f"tossy-{secrets.token_hex(12)}", "key": base64.b64encode(secrets.token_bytes(32)).decode(),
            "device_id": read_conf().get("device_id") or secrets.token_hex(8), "legacy_to_mac": "", "legacy_to_phone": ""}


CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_code():
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def ensure_notify_app():
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
        import claude_notify
        if claude_notify.ensure_app():
            return claude_notify.AGENT.app
    except Exception:
        pass
    return NOTIFY_APP if os.path.isdir(NOTIFY_APP) else ""


def setup():
    conf = read_conf()
    server = normalize_server(args[1] if len(args) > 1 else "")
    if not server:
        hint = f" [{conf['server']}]" if conf.get("server") else ""
        server = normalize_server(input(f"Адрес ntfy-сервера, например https://ntfy.example.com{hint}: ") or conf.get("server", ""))
    if not server:
        sys.exit("Нужен адрес сервера.")
    print("Токен: на сервере `ntfy token add <пользователь>`. В файлы mnrh он попадёт только в ~/.config/mnrh/clip.json (права 600).")
    token = read_token() or (conf.get("token", "") if conf.get("server") == server else "")
    if not token:
        sys.exit("Нет токена. Скопируй его (tk_…) и запусти команду ещё раз.")
    code, raw = http("GET", f"{server}/v1/account", token)
    if code != 200:
        sys.exit(f"{BAD} сервер не принял токен: HTTP {code} {error_text(raw)}")
    user = json.loads(raw).get("username", "?")
    print(f"{OK} {server}, пользователь {user}")
    probe = f"tossy-probe-{secrets.token_hex(6)}"
    code, raw = http("PUT", f"{server}/{probe}", token, body=b"probe", headers={"X-Filename": "probe.bin"})
    if code != 200:
        print(f"{WARN} не могу публиковать: HTTP {code} {error_text(raw)} — проверь права пользователя (ntfy access)")
    elif "attachment" not in json.loads(raw):
        print(f"{WARN} на сервере выключены вложения: картинки и длинные тексты не пройдут "
              f"(attachment-cache-dir в server.yml)")
    else:
        print(f"{OK} вложения работают, картинки пройдут")
    values = {"server": server, "token": token, "notify_app": ensure_notify_app()}
    if not configured() or "--new" in args:
        values.update(new_channel())
    values.setdefault("images", conf.get("images", True))
    write_conf(**values)
    start()
    pair(fresh=False)


def start():
    if migrate():
        print(f"{OK} настройки переведены на комнату: телефон переедет сам, как только получит сообщение.")
    write_conf(paused_until=0)
    if agent.source_hash() != (open(agent.stamp).read().strip() if os.path.exists(agent.stamp) else ""):
        agent.stop()
    agent.build()
    if agent.installed() and agent.state():
        agent.restart()
        if agent.wait_state():
            return
    started = agent.start()
    if started == "requiresApproval":
        if not agent.approve_login_item():
            sys.exit(1)
        started = agent.start()
    if not started:
        sys.exit(f"Помощник не запустился. Лог: {tilde(agent.log)}")


def paired_at():
    s = agent.state() or {}
    return s.get("phone_at") or 0, s.get("phone", "")


def pair(fresh=None):
    if not configured():
        sys.exit("Сначала mnrh tossy setup <сервер>")
    if fresh is None and "--new" in args:
        write_conf(**new_channel())
        start()
        print(f"{OK} новый ключ и каналы; телефон, подключённый раньше, больше ничего не получит.")
    agent.build()
    os.makedirs(os.path.dirname(QR), exist_ok=True)
    r = subprocess.run([agent.bin, "--config", CONFIG, "--qr", QR], capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not os.path.exists(QR):
        sys.exit(f"Не сделал QR: {r.stdout.strip() or r.stderr.strip()}")
    asked = time.time()
    print("Открой Tossy на телефоне → «Подключить» и наведи камеру на QR (жду до 3 минут).")
    print(paint("  В QR — ключ шифрования и токен: не показывай его никому и не делай скриншот.", "2"))
    viewer = subprocess.Popen(["qlmanage", "-p", QR], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(180):
            at, name = paired_at()
            if at >= asked:
                print(f"{OK} подключён {name}")
                break
            time.sleep(1)
        else:
            print("Не дождался ответа телефона. Повторить: mnrh tossy pair")
    except KeyboardInterrupt:
        print()
    finally:
        viewer.terminate()
        try:
            os.remove(QR)
        except OSError:
            pass


def invite():
    if not configured():
        sys.exit("Сначала mnrh tossy setup <сервер>")
    migrate()
    agent.build()
    code = new_code()
    r = subprocess.run([agent.bin, "--config", CONFIG, "--invite", code], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        sys.exit(f"Не создал приглашение: {r.stdout.strip() or r.stderr.strip()}")
    host = read_conf()["server"].split("://", 1)[-1]
    print(f"{OK} приглашение на 10 минут. На втором Mac выполни:")
    print()
    print(f"    mnrh tossy join {host}/{code}")
    print()
    print(paint("  Код одноразовый по смыслу: кто его знает, тот войдёт в комнату. Не публикуй его.", "2"))


def join():
    if len(args) < 2:
        sys.exit("mnrh tossy join <сервер>/<КОД> — команду показывает mnrh tossy invite на первом Mac")
    target = args[1].strip()
    server, _, code = target.rpartition("/")
    server = normalize_server(server or read_conf().get("server", ""))
    if not server or not re.match(r"^[0-9A-Za-z]{4}-?[0-9A-Za-z]{4}$", code):
        sys.exit("Нужно вида: mnrh tossy join ntfy.example.com/ABCD-EFGH")
    agent.build()
    out = os.path.join(os.path.dirname(CONFIG), "clip-join.tmp")
    try:
        r = subprocess.run([agent.bin, "--config", "/dev/null", "--join", server, code, "--out", out],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not os.path.exists(out):
            sys.exit(f"{BAD} {r.stdout.strip() or r.stderr.strip() or 'не получилось'}")
        with open(out) as f:
            invite = json.load(f)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    old = read_conf()
    conf = {"server": normalize_server(invite["s"]), "token": invite["t"], "room": invite["r"], "key": invite["k"],
            "device_id": old.get("device_id") or secrets.token_hex(8), "images": old.get("images", True),
            "notify_app": ensure_notify_app()}
    conf.update({k: old[k] for k in ("name", "aliases") if old.get(k)})
    save_conf(conf)
    start()
    print(f"{OK} этот Mac в одной комнате с {r.stdout.strip()}: скопированное здесь появится там и на телефоне.")


def on():
    if not configured():
        sys.exit("Сначала mnrh tossy setup <сервер>")
    start()
    print(f"{OK} mnrh tossy включён: буфер обмена общий с телефоном.")


def off():
    was = agent.installed()
    agent.uninstall()
    print("mnrh tossy выключен." if was else "mnrh tossy и так выключен.")


def remove():
    off()
    shutil.rmtree(agent.app, ignore_errors=True)
    try:
        os.remove(CONFIG)
    except OSError:
        pass
    print("Помощник и настройки удалены (ключ тоже — телефон придётся подключать заново).")


def pause():
    minutes = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
    until = time.time() + minutes * 60
    write_conf(paused_until=until)
    print(f"{OK} не отправляю буфер на телефон до {datetime.fromtimestamp(until):%H:%M}. "
          f"С телефона на Mac — по-прежнему. Раньше: mnrh tossy resume")


def resume():
    write_conf(paused_until=0)
    print(f"{OK} снова отправляю буфер на телефон.")


def images():
    if len(args) < 2 or args[1] not in ("on", "off"):
        sys.exit("mnrh tossy images on|off")
    write_conf(images=args[1] == "on")
    print(f"{OK} картинки {'передаю' if args[1] == 'on' else 'не передаю, только текст'}.")


def send():
    if not configured():
        sys.exit("Сначала mnrh tossy setup <сервер>")
    rest, targets = [], []
    items = iter(args[1:])
    for a in items:
        if a in ("--to", "-t"):
            targets.append(next(items, ""))
        elif a.startswith("--to="):
            targets.append(a[5:])
        else:
            rest.append(a)
    ids = []
    if targets:
        conf = read_conf()
        devices = room_devices(conf, agent.state() or {})
        for t in targets:
            low = t.strip().lower()
            found = [d for d in devices if low in (d["title"].lower(), d["name"].lower(), d["id"].lower())]
            if not found:
                names = ", ".join(d["title"] for d in devices) or "в комнате пока никого"
                sys.exit(f"Нет устройства «{t}». Есть: {names}")
            ids.append(found[0]["id"])
    piped = None
    if not rest and not sys.stdin.isatty():
        piped = sys.stdin.buffer.read()
    if not rest and not piped:
        sys.exit("mnrh tossy send [--to <кто>] <файл|текст>…  или  … | mnrh tossy send [--to <кто>]")
    agent.build()
    cmd_args = [a for i in ids for a in ("--to", i)]
    tmp = None
    if piped is not None:
        os.makedirs(os.path.dirname(QR), exist_ok=True)
        try:
            piped.decode("utf-8")
            tmp = os.path.join(os.path.dirname(QR), f"tossy-send-{os.getpid()}.txt")
            cmd_args += ["--text", tmp]
        except UnicodeDecodeError:
            tmp = os.path.join(os.path.dirname(QR), f"tossy-send-{os.getpid()}", "stdin.bin")
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            cmd_args += ["--send", tmp]
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(piped)
    for a in rest:
        cmd_args += ["--send", a]
    try:
        code = subprocess.run([agent.bin, "--config", CONFIG, *cmd_args]).returncode
    finally:
        if tmp:
            try:
                os.remove(tmp)
                if tmp.endswith("stdin.bin"):
                    os.rmdir(os.path.dirname(tmp))
            except OSError:
                pass
    sys.exit(code)


def show_log():
    try:
        lines = open(agent.log, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        sys.exit("Лога пока нет.")
    for line in lines[-25:]:
        stamp, _, text = line.partition(" ")
        try:
            stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%d.%m %H:%M:%S")
        except ValueError:
            pass
        print(f"{paint(stamp, '2')}  {text}")


def ago(ts):
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "—"


def status():
    conf = read_conf()
    if not configured():
        print("mnrh tossy не настроен." + paint("   -> mnrh tossy setup <адрес ntfy-сервера>", "2"))
        return
    if not agent.installed():
        print("mnrh tossy выключен." + paint("   -> mnrh tossy on", "2"))
        return
    s = agent.state()
    if not s:
        print(f"{BAD} mnrh tossy включён, но помощник не работает." + paint("   -> mnrh tossy on", "2"))
        print(f"  лог: {tilde(agent.log)}")
        return
    link = f"{OK} на связи с {conf['server']}" if s.get("connected") else f"{WARN} нет связи с {conf['server']}"
    print(f"{link} (pid {s['pid']}, с {ago(s['started'])})")
    print(f"  этот Mac: {my_name(conf)}")
    others = room_devices(conf, s)
    if others:
        names = [f"{d['title']} ({'Mac' if d['mac'] else 'телефон'}, {ago(d['seen'])})" for d in others]
        print(f"  устройства в комнате: {', '.join(names)}")
    else:
        print(f"  телефон: {s.get('phone') or 'ещё не подключался — mnrh tossy pair'}")
    if not conf.get("room"):
        print(f"{WARN} старая схема без комнаты" + paint("   -> mnrh tossy on", "2"))
    print(f"  отправлено: {s.get('sent', 0)} (последнее {ago(s.get('last_sent'))}) · "
          f"получено: {s.get('received', 0)} (последнее {ago(s.get('last_received'))})")
    until = conf.get("paused_until") or 0
    if until > time.time():
        print(f"{WARN} на паузе до {datetime.fromtimestamp(until):%H:%M}" + paint("   -> mnrh tossy resume", "2"))
    if not conf.get("images", True):
        print("  картинки не передаю (mnrh tossy images on)")


def computer_name():
    r = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True)
    return r.stdout.strip() or "Mac"


def my_name(conf):
    return conf.get("name") or computer_name()


def room_devices(conf, state):
    aliases = conf.get("aliases") or {}
    me = conf.get("device_id", "")
    out = []
    for key, m in (state.get("members") or {}).items():
        if key == me:
            continue
        name = m.get("name", "?")
        out.append({"id": key, "name": name, "title": aliases.get(key) or name, "mac": m.get("src") == "mac",
                    "seen": m.get("seen") or 0})
    return sorted(out, key=lambda d: -d["seen"])


def apply_names():
    if agent.installed() and agent.state():
        agent.restart()
        agent.wait_state()


def rename_self(name):
    if name:
        write_conf(name=name)
    else:
        drop_conf("name")
    apply_names()
    print(f"{OK} этот Mac теперь «{my_name(read_conf())}» — имя увидят все устройства в комнате.")


def rename_other(device, name):
    aliases = dict(read_conf().get("aliases") or {})
    if name and name != device["name"]:
        aliases[device["id"]] = name
    else:
        aliases.pop(device["id"], None)
    if aliases:
        write_conf(aliases=aliases)
    else:
        drop_conf("aliases")
    apply_names()
    if device["id"] in aliases:
        print(f"{OK} «{device['name']}» на этом Mac называется «{aliases[device['id']]}». На других устройствах имя не меняется.")
    else:
        print(f"{OK} «{device['name']}» снова называется своим именем.")


def rename():
    if not configured():
        sys.exit("Сначала mnrh tossy setup <сервер>")
    conf = read_conf()
    others = room_devices(conf, agent.state() or {})
    if len(args) == 2:
        rename_self(args[1].strip())
        return
    if len(args) >= 3:
        wanted = args[1].strip().lower()
        found = [d for d in others if wanted in (d["title"].lower(), d["name"].lower(), d["id"].lower())]
        if not found:
            sys.exit(f"Нет устройства «{args[1]}» в комнате. Список: mnrh tossy status")
        rename_other(found[0], " ".join(args[2:]).strip())
        return
    if not sys.stdin.isatty():
        sys.exit("mnrh tossy rename <имя> | mnrh tossy rename <кто> <имя>")
    sys.path.insert(0, LIB)
    from menu import pick
    rows = [(f"{my_name(conf)} этот mac", f"{my_name(conf)}  {paint('этот Mac · имя видят все', '2')}")]
    for d in others:
        note = "Mac" if d["mac"] else "телефон"
        if d["title"] != d["name"]:
            note += f" · сам называет себя «{d['name']}»"
        rows.append((f"{d['title']} {d['name']}", f"{d['title']}  {paint(note + ' · имя только на этом Mac', '2')}"))
    i = pick(rows, title="\x1b[1;35mКого переименовать?\x1b[0m", esc="назад")
    if i is None:
        return
    try:
        name = input("Новое имя (пусто — прежнее): ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return
    if i == 0:
        rename_self(name)
    else:
        rename_other(others[i - 1], name)


def menu():
    launcher = os.path.join(LIB, "launcher.py")
    while True:
        r = subprocess.run([sys.executable, launcher, "actions", REGISTRY, "tossy"], stdout=subprocess.PIPE, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            return
        picked = shlex.split(r.stdout)
        print(paint(f"$ mnrh tossy {' '.join(picked)}", "2"))
        rc = subprocess.run([sys.executable, os.path.abspath(__file__), *picked]).returncode
        if subprocess.run([sys.executable, launcher, "after", str(rc)]).returncode != 0:
            return


{"menu": menu, "status": status, "rename": rename, "setup": setup, "pair": pair, "invite": invite, "join": join, "on": on, "off": off, "pause": pause, "resume": resume,
 "send": send, "images": images, "log": show_log, "remove": remove}[cmd]()
