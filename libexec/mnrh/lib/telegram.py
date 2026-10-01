import getpass
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import read_config, write_config

SERVICE = "mnrh-telegram"
ACCOUNT = "bot"
API = "https://api.telegram.org/bot{}/{}"
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
DEFAULT_AWAY = 120


class TelegramError(Exception):
    pass


def token():
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def save_token(value):
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", ACCOUNT,
                        "-l", "mnrh Telegram bot", "-w", value], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise TelegramError("не сохранил токен в Связку ключей")


def forget_token():
    subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", ACCOUNT],
                   capture_output=True, timeout=10)


def call(method, tok=None, wait=15, **params):
    tok = tok or token()
    if not tok:
        raise TelegramError("бот не подключён: mnrh claude notify telegram")
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(API.format(tok, method), data=data),
                                    timeout=wait) as r:
            reply = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            reply = json.load(e)
        except ValueError:
            raise TelegramError(f"Telegram ответил HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise TelegramError(f"нет связи с api.telegram.org ({getattr(e, 'reason', e)})") from None
    if not reply.get("ok"):
        raise TelegramError(reply.get("description") or "Telegram вернул ошибку")
    return reply["result"]


def chat():
    return read_config().get("telegram_chat", "")


def configured():
    return bool(chat()) and bool(token())


def mode():
    return "always" if read_config().get("telegram_when") == "always" else "away"


def away_after():
    try:
        return max(0, int(read_config().get("telegram_away", DEFAULT_AWAY)))
    except ValueError:
        return DEFAULT_AWAY


def idle_seconds():
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0
    m = re.search(r'"HIDIdleTime" = (\d+)', out)
    return int(m.group(1)) / 1e9 if m else 0


def screen_locked():
    try:
        r = subprocess.run("ioreg -n Root -d1 -a | plutil -extract IOConsoleLocked raw -o - -", shell=True,
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.stdout.strip() == "true"


def away():
    return screen_locked() or idle_seconds() >= away_after()


def wanted():
    return configured() and (mode() == "always" or away())


def send(title, subtitle, body):
    parts = [f"<b>{html.escape(title)}</b>"]
    if subtitle:
        parts.append(f"<i>{html.escape(subtitle)}</i>")
    if body:
        parts.append(html.escape(body))
    return call("sendMessage", chat_id=chat(), text="\n".join(parts), parse_mode="HTML",
                disable_web_page_preview="true")


def read_token():
    if sys.stdin.isatty():
        return getpass.getpass("Токен бота (при вводе не виден): ").strip()
    clip = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
    if TOKEN_RE.match(clip):
        subprocess.run(["pbcopy"], input="", text=True)
        print("Взял токен из буфера обмена и очистил буфер.")
        return clip
    return ""


def wait_for_start(tok, seconds=180):
    offset = None
    deadline = time.time() + seconds
    while time.time() < deadline:
        updates = call("getUpdates", tok, wait=40, offset=offset, timeout=25, allowed_updates='["message"]')
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            ch = msg.get("chat") or {}
            if ch.get("type") == "private" and (msg.get("text") or "").startswith("/start"):
                call("getUpdates", tok, offset=offset, timeout=0)
                return ch
    return None


def setup():
    print("Подключаю уведомления в Telegram.")
    print("  1. В Telegram открой @BotFather, отправь /newbot, придумай имя и адрес бота.")
    print("  2. BotFather пришлёт токен вида 123456789:AA… — скопируй его.")
    print("Токен хранится в Связке ключей macOS, в файлы и в вывод не попадает.")
    tok = read_token()
    if not TOKEN_RE.match(tok or ""):
        print("Нет токена. Скопируй его из сообщения BotFather и запусти команду ещё раз.")
        return 1
    try:
        bot = call("getMe", tok)
    except TelegramError as e:
        print(f"✗ Telegram не принял токен: {e}")
        return 1
    save_token(tok)
    name = bot.get("username", "")
    print(f"✓ бот @{name}")
    print(f"  3. Открываю t.me/{name} — нажми там «Start» (до 3 минут).")
    subprocess.run(["open", f"https://t.me/{name}"], capture_output=True)
    try:
        ch = wait_for_start(tok)
    except TelegramError as e:
        print(f"✗ {e}")
        return 1
    if not ch:
        print("Не дождался /start. Запусти mnrh claude notify telegram ещё раз.")
        return 1
    write_config(telegram_chat=str(ch["id"]), telegram_bot=name)
    who = " ".join(x for x in (ch.get("first_name"), ch.get("last_name")) if x) or ch.get("username") or ch["id"]
    try:
        call("sendMessage", chat_id=ch["id"], text="mnrh подключён ✓ Сюда будут приходить уведомления Claude, "
                                                   "когда ты не за Mac.")
    except TelegramError:
        pass
    print(f"✓ подключено к чату с {who}. Если это не ты — mnrh claude notify telegram off")
    print(f"Уведомления идут в Telegram, когда ты не за Mac: экран заблокирован или {away_after() // 60} мин "
          f"без мыши и клавиатуры.")
    return 0


def describe():
    if not configured():
        return "Telegram: не подключён — mnrh claude notify telegram"
    bot = read_config().get("telegram_bot", "")
    when = "всегда" if mode() == "always" else f"когда ты не за Mac (блокировка или {away_after()} с без ввода)"
    return f"Telegram: @{bot}, {when}"


def main(args):
    cmd = args[0] if args else ""
    if not cmd:
        if configured():
            print(describe())
            print("  test | off | always | away [сек]")
            return 0
        return setup()
    if cmd == "setup":
        return setup()
    if cmd == "off":
        forget_token()
        write_config(telegram_chat="", telegram_bot="")
        print("Telegram отключён, токен удалён из Связки ключей")
        return 0
    if cmd == "always":
        write_config(telegram_when="always")
        print("Telegram: присылаю всегда, даже если ты за Mac")
        return 0
    if cmd == "away":
        if len(args) > 1:
            if not args[1].isdigit():
                print("mnrh claude notify telegram away <секунд без ввода>")
                return 2
            write_config(telegram_away=args[1])
        write_config(telegram_when="away")
        print(describe())
        return 0
    if cmd == "test":
        try:
            send("mnrh: пробное уведомление", "Claude Code", "Так будут выглядеть уведомления, когда ты не за Mac.")
        except TelegramError as e:
            print(f"✗ {e}")
            return 1
        print("✓ отправлено в Telegram")
        return 0
    print("mnrh claude notify telegram [test | off | always | away [сек]]")
    return 2
