import base64
import getpass
import json
import os
import plistlib
import re
import secrets
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
ACTIONS = ("status", "setup", "pair", "invite", "join", "rename", "on", "off", "pause", "resume", "send", "images", "auto",
           "hotkey", "log", "remove")
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
    print("mnrh tossy send <файл|текст>  отправить текст, картинку или любой файл до 500 МБ на все устройства")
    print("  … | mnrh tossy send         отправить то, что пришло в пайп (текст или файл)")
    print("  --to <кто>                  только этому устройству (имя из status; можно несколько)")
    print("  --pick                      спросить в окне, кому отправить")
    print("mnrh tossy pause [мин]        не отправлять буфер этого Mac (по умолчанию 30 мин)")
    print("mnrh tossy resume             снова отправлять")
    print("mnrh tossy images on|off      передавать ли картинки")
    print("mnrh tossy auto on|off        отправлять ли буфер сам по ⌘C (off — только по ⌃⌥⌘C)")
    print("mnrh tossy hotkey on|off      ⌃⌥⌘C отправляет то, что в буфере, включая файлы из Finder")
    print("mnrh tossy log                что передано и почему что-то пропущено")
    print("mnrh tossy on | off           включить или выключить помощника")
    print("mnrh tossy remove             выключить и удалить помощника и настройки")
    print()
    print("Общий буфер обмена твоих Mac и Android-телефона через свой ntfy-сервер: скопировал на одном —")
    print("вставляешь на остальных. Всё шифруется на устройствах, пароли из менеджеров паролей не передаются.")
    print("Файлы из Finder уходят только явно: ⌃⌥⌘C или правый клик → Быстрые действия → «Отправить в Tossy».")
    print("Прежнее имя команды, mnrh clip, тоже работает.")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)
cmd = args[0] if args else "status"

LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
CONFIG = os.path.join(HOME, ".config", "mnrh", "clip.json")
NOTIFY_APP = os.path.join(HOME, "Library", "Application Support", "mnrh", "mnrh Notify.app")
QR = os.path.join(HOME, ".cache", "mnrh", "clip-pair.png")
TOKEN_RE = re.compile(r"^tk_[A-Za-z0-9]{20,}$")
agent = SwiftAgent("clip", "mnrh Clip", "com.mnrh.clip", service_args=["--config", CONFIG], url_schemes=["mnrh-tossy"])


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


def not_configured():
    c = read_conf()
    if c.get("server") and c.get("token"):
        return ("Этот Mac не в комнате: его отключили с другого устройства или настройки сброшены.\n"
                "Вернуться: mnrh tossy setup --new — новая комната и QR для телефона,\n"
                "или mnrh tossy join <код> — в комнату другого Mac (код даёт mnrh tossy invite на нём).")
    return "Сначала mnrh tossy setup <сервер>"


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
    device_id = read_conf().get("device_id") or secrets.token_hex(8)
    return {"room": f"tossy-{secrets.token_hex(12)}", "key": base64.b64encode(secrets.token_bytes(32)).decode(),
            "device_id": device_id, "owner": device_id, "legacy_to_mac": "", "legacy_to_phone": ""}


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
    positional = [a for a in args[1:] if not a.startswith("--")]
    server = normalize_server(positional[0] if positional else "")
    if not server:
        hint = f" [{conf['server']}]" if conf.get("server") else ""
        server = normalize_server(input(f"Адрес ntfy-сервера, например https://ntfy.example.com{hint}: ") or conf.get("server", ""))
    if not server:
        sys.exit("Нужен адрес сервера.")
    token = conf.get("token", "") if conf.get("server") == server and "--token" not in args else ""
    if token:
        print("Токен: прежний из ~/.config/mnrh/clip.json (новый — mnrh tossy setup --token).")
    else:
        print("Токен: на сервере `ntfy token add <пользователь>`. В файлы mnrh он попадёт только в ~/.config/mnrh/clip.json (права 600).")
        token = read_token()
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
    install_quick_actions()


def paired_at():
    s = agent.state() or {}
    return s.get("phone_at") or 0, s.get("phone", "")


def pair(fresh=None):
    if not configured():
        sys.exit(not_configured())
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
        sys.exit(not_configured())
    migrate()
    agent.build()
    code = new_code()
    host = read_conf()["server"].split("://", 1)[-1]
    proc = subprocess.Popen([agent.bin, "--config", CONFIG, "--invite", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        first = proc.stdout.readline().strip()
        if first != "ok":
            proc.wait()
            sys.exit(f"Не создал приглашение: {first or proc.stderr.read().strip()}")
        print("На втором Mac выполни:")
        print()
        print(f"    mnrh tossy join {host}/{code}")
        print()
        print(paint("  Приглашение не хранится на сервере: оно уходит, только пока эта команда ждёт, до 10 минут.", "2"))
        print(paint("  Кто знает код, тот войдёт в комнату. Не публикуй его. Ctrl+C — отменить.", "2"))
        result = proc.stdout.read().strip()
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print()
        sys.exit("Приглашение отменено.")
    if proc.returncode == 0:
        print(f"{OK} {result or 'Mac'} в комнате.")
    elif result == "timeout":
        sys.exit("За 10 минут никто не подключился. Новое приглашение: mnrh tossy invite")
    else:
        sys.exit(f"{BAD} приглашение прервалось: {result or proc.stderr.read().strip()}")


def join():
    if len(args) < 2:
        sys.exit("mnrh tossy join <сервер>/<КОД> — команду показывает mnrh tossy invite на первом Mac")
    target = args[1].strip()
    server, _, code = target.rpartition("/")
    server = normalize_server(server or read_conf().get("server", ""))
    if not server or not re.match(r"^[0-9A-Za-z]{4}-?[0-9A-Za-z]{4}$", code):
        sys.exit("Нужно вида: mnrh tossy join ntfy.example.com/ABCD-EFGH")
    agent.build()
    old = read_conf()
    device_id = old.get("device_id") or secrets.token_hex(8)
    out = os.path.join(os.path.dirname(CONFIG), "clip-join.tmp")
    try:
        r = subprocess.run([agent.bin, "--config", "/dev/null", "--join", server, code, "--out", out,
                            "--id", device_id, "--name", old.get("name", "")],
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
    conf = {"server": normalize_server(invite["s"]), "token": invite["t"], "room": invite["r"], "key": invite["k"],
            "device_id": device_id, "owner": invite.get("o", ""), "images": old.get("images", True),
            "notify_app": ensure_notify_app()}
    conf.update({k: old[k] for k in ("name", "aliases", "identity") if old.get(k)})
    save_conf(conf)
    start()
    print(f"{OK} этот Mac в одной комнате с {r.stdout.strip()}: скопированное здесь появится там и на телефоне.")


def on():
    if not configured():
        sys.exit(not_configured())
    start()
    print(f"{OK} mnrh tossy включён: буфер обмена общий с телефоном.")


def off():
    was = agent.installed()
    agent.uninstall()
    print("mnrh tossy выключен." if was else "mnrh tossy и так выключен.")


def remove():
    off()
    remove_quick_actions()
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


def auto():
    if len(args) < 2 or args[1] not in ("on", "off"):
        sys.exit("mnrh tossy auto on|off")
    write_conf(auto=args[1] == "on")
    if args[1] == "on":
        print(f"{OK} скопированные текст и картинки уходят сами. Файлы — только по ⌃⌥⌘C или «Отправить в Tossy».")
    else:
        print(f"{OK} по ⌘C ничего не уходит. Отправить буфер: ⌃⌥⌘C или «Отправить в Tossy» в Finder.")


def hotkey():
    if len(args) < 2 or args[1] not in ("on", "off"):
        sys.exit("mnrh tossy hotkey on|off")
    write_conf(hotkey=args[1] == "on")
    apply_names()
    print(f"{OK} ⌃⌥⌘C {'отправляет буфер' if args[1] == 'on' else 'больше не занят Tossy'}.")


SERVICES = os.path.join(HOME, "Library", "Services")
QUICK_ACTIONS = (("Отправить в Tossy", "--notify"), ("Отправить в Tossy…", "--notify --pick"))


def quick_action_files(title, command):
    info = {"NSServices": [{
        "NSBackgroundColorName": "background", "NSIconName": "NSActionTemplate",
        "NSMenuItem": {"default": title}, "NSMessage": "runWorkflowAsService",
        "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"}, "NSSendFileTypes": ["public.item"],
    }]}
    arg = lambda i, name, default: {"default value": default, "name": name, "required": "0", "type": "0", "uuid": str(i)}
    action = {
        "AMAccepts": {"Container": "List", "Optional": True, "Types": ["com.apple.cocoa.path"]},
        "AMActionVersion": "2.0.3", "AMApplication": ["Automator"],
        "AMParameterProperties": {k: {} for k in ("COMMAND_STRING", "CheckedForUserDefaultShell", "inputMethod", "shell", "source")},
        "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.string"]},
        "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action", "ActionName": "Run Shell Script",
        "ActionParameters": {"COMMAND_STRING": command, "CheckedForUserDefaultShell": True, "inputMethod": 1,
                             "shell": "/bin/zsh", "source": ""},
        "BundleIdentifier": "com.apple.RunShellScript", "CFBundleVersion": "2.0.3",
        "CanShowSelectedItemsWhenRun": False, "CanShowWhenRun": True, "Category": ["AMCategoryUtilities"],
        "Class Name": "RunShellScriptAction",
        "InputUUID": "6E0A3C9D-3D2B-4C1E-9F0A-1A2B3C4D5E61", "OutputUUID": "6E0A3C9D-3D2B-4C1E-9F0A-1A2B3C4D5E62",
        "UUID": "6E0A3C9D-3D2B-4C1E-9F0A-1A2B3C4D5E63", "UnlocalizedApplications": ["Automator"],
        "arguments": {"0": arg(0, "inputMethod", 0), "1": arg(1, "CheckedForUserDefaultShell", False),
                      "2": arg(2, "source", ""), "3": arg(3, "COMMAND_STRING", ""), "4": arg(4, "shell", "/bin/sh")},
        "conversionLabel": 0, "isViewVisible": 1, "location": "309.000000:305.000000",
        "nibPath": "/System/Library/Automator/Run Shell Script.action/Contents/Resources/Base.lproj/main.nib",
    }
    document = {
        "AMApplicationBuild": "534", "AMApplicationVersion": "2.10", "AMDocumentVersion": "2",
        "actions": [{"action": action, "isViewVisible": 1}], "connectors": {},
        "workflowMetaData": {
            "applicationBundleIDsByPath": {}, "applicationPaths": [],
            "inputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "outputTypeIdentifier": "com.apple.Automator.nothing", "presentationMode": 15, "processesInput": False,
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing", "serviceProcessesInput": False,
            "systemImageName": "NSActionTemplate", "useAutomaticInputType": False,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }
    return plistlib.dumps(info), plistlib.dumps(document)


def install_quick_actions():
    import finder_ext
    icon = os.path.join(agent.app, "Contents", "Resources", "AppIcon.icns")
    error = finder_ext.install(icon=icon) if finder_ext.identity() != "-" else "нет сертификата Apple Development"
    if error is None:
        if any(os.path.exists(os.path.join(SERVICES, f"{title}.workflow")) for title, _ in QUICK_ACTIONS):
            remove_services()
        return
    print(f"{WARN} пункт в меню Finder не собрался, ставлю быстрые действия: {error.splitlines()[0] if error else ''}")
    mnrh = shutil.which("mnrh") or os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "mnrh"))
    changed = False
    for title, flags in QUICK_ACTIONS:
        contents = os.path.join(SERVICES, f"{title}.workflow", "Contents")
        files = dict(zip(("Info.plist", "document.wflow"), quick_action_files(title, f'"{mnrh}" tossy send {flags} "$@"')))
        for name, data in files.items():
            path = os.path.join(contents, name)
            try:
                with open(path, "rb") as f:
                    if f.read() == data:
                        continue
            except OSError:
                pass
            os.makedirs(contents, exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
            changed = True
    if enable_quick_actions() or changed:
        subprocess.run(["/System/Library/CoreServices/pbs", "-update"], capture_output=True)


def enable_quick_actions():
    r = subprocess.run(["defaults", "export", "pbs", "-"], capture_output=True)
    try:
        prefs = plistlib.loads(r.stdout) if r.returncode == 0 and r.stdout else {}
    except Exception:
        prefs = {}
    status = prefs.setdefault("NSServicesStatus", {})
    want = {"enabled_context_menu": True, "enabled_services_menu": True,
            "presentation_modes": {"ContextMenu": True, "ServicesMenu": True}}
    changed = False
    for title, _ in QUICK_ACTIONS:
        key = f"(null) - {title} - runWorkflowAsService"
        if status.get(key) != want:
            status[key] = want
            changed = True
    if changed:
        subprocess.run(["defaults", "import", "pbs", "-"], input=plistlib.dumps(prefs), capture_output=True)
    return changed


def remove_quick_actions():
    import finder_ext
    finder_ext.remove()
    remove_services()


def remove_services():
    for title, _ in QUICK_ACTIONS:
        shutil.rmtree(os.path.join(SERVICES, f"{title}.workflow"), ignore_errors=True)
    subprocess.run(["/System/Library/CoreServices/pbs", "-update"], capture_output=True)


def refresh_helper():
    """Помощник, запущенный до обновления mnrh, работает на старом коде, пока его не перезапустить.

    У каждой сборки свой идентификатор службы, поэтому старую службу надо снять отдельно: после
    пересборки она уже не совпадает с той, что записана в новом приложении.
    """
    if cmd in ("off", "remove", "setup", "on") or not configured():
        return
    s = agent.state()
    if not s and not agent.installed():
        return
    stamp = open(agent.stamp).read().strip() if os.path.exists(agent.stamp) else ""
    try:
        rebuilt = bool(s) and os.path.getmtime(agent.bin) > (s.get("started") or 0) + 1
    except OSError:
        rebuilt = False
    if agent.source_hash() == stamp and not rebuilt:
        return
    print(paint("обновляю помощника: он работал на прежней версии", "2"))
    listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout.splitlines()
    for line in listed:
        label = line.split("\t")[-1]
        if label.startswith(agent.base + "."):
            agent.launchctl("bootout", f"{agent.domain}/{label}")
    if s:
        try:
            os.kill(s["pid"], 15)
        except OSError:
            pass
    agent.stop()
    agent.build()
    if agent.start() is not True:
        print(f"{WARN} новый помощник не запустился" + paint("   -> mnrh tossy on", "2"))
    install_quick_actions()


def send():
    if not configured():
        sys.exit(not_configured())
    rest, targets, flags = [], [], []
    pick = False
    items = iter(args[1:])
    for a in items:
        if a == "--notify":
            flags.append(a)
        elif a == "--pick":
            pick = True
        elif a in ("--to", "-t"):
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
    if pick and not ids:
        chosen = pick_targets()
        if chosen is None:
            return
        ids = chosen
    piped = None
    if not rest and not sys.stdin.isatty():
        piped = sys.stdin.buffer.read()
    if not rest and not piped:
        sys.exit("mnrh tossy send [--to <кто>] <файл|текст>…  или  … | mnrh tossy send [--to <кто>]")
    agent.build()
    cmd_args = flags + [a for i in ids for a in ("--to", i)]
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


def pick_targets():
    devices = room_devices(read_conf(), agent.state() or {})
    if len(devices) < 2:
        return []
    everyone = "Все устройства"
    quote = lambda v: '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    titles = [d["title"] for d in devices]
    script = (f'choose from list {{{", ".join(quote(t) for t in [everyone] + titles)}}} with title "Tossy" '
              f'with prompt "Кому отправить?" default items {{{quote(everyone)}}} with multiple selections allowed')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    picked = [p.strip() for p in r.stdout.strip().split(", ")] if r.returncode == 0 else []
    if not picked or picked == ["false"]:
        return None
    if everyone in picked:
        return []
    return [d["id"] for d in devices if d["title"] in picked]


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
    if not conf.get("auto", True):
        print("  по ⌘C ничего не отправляю, только по ⌃⌥⌘C (mnrh tossy auto on)")
    if not conf.get("hotkey", True):
        print("  ⌃⌥⌘C выключена (mnrh tossy hotkey on)")


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
        sys.exit(not_configured())
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


refresh_helper()
{"status": status, "auto": auto, "hotkey": hotkey, "rename": rename, "setup": setup, "pair": pair, "invite": invite, "join": join, "on": on, "off": off, "pause": pause, "resume": resume,
 "send": send, "images": images, "log": show_log, "remove": remove}[cmd]()
