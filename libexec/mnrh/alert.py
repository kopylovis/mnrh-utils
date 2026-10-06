import collections
import os
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import BAD, OK, WARN, has_flag, paint, tilde
import ntfy_alert

args = sys.argv[1:]


def usage():
    print("mnrh alert                       меню с действиями (без терминала — то же, что status)")
    print("mnrh alert status                куда уходят уведомления")
    print("mnrh alert <текст>               уведомление на телефон, в Tossling → Уведомления")
    print("   -t <заголовок>  -p <1–5>  --to <канал>  --click <ссылка>")
    print("mnrh alert run <команда…>        запустить команду и прислать итог: успех или ошибка, время, конец вывода")
    print("mnrh alert test                  пробное уведомление")
    print("echo текст | mnrh alert          текст из pipe")
    print()
    print(f"Канал по умолчанию — «{ntfy_alert.DEFAULT_TOPIC}». На телефоне: Tossling → Уведомления → + → канал "
          f"{ntfy_alert.DEFAULT_TOPIC}.")
    print("Сервер и токен берутся из Tossling (tossling config --json).")


def option(name, rest):
    if name in rest:
        i = rest.index(name)
        if i + 1 < len(rest):
            value = rest[i + 1]
            del rest[i:i + 2]
            return value
        del rest[i]
    return None


def send(text, title="", priority=3, topic=None, click=None, tags=None, markdown=False):
    try:
        ntfy_alert.publish(topic or ntfy_alert.DEFAULT_TOPIC, text, title=title, priority=priority, tags=tags,
                           click=click, markdown=markdown)
    except ntfy_alert.AlertError as e:
        print(f"{BAD} не отправил: {e}", file=sys.stderr)
        return 1
    return 0


def status():
    target = ntfy_alert.endpoint()
    if not target:
        print(f"{WARN} Tossling не настроен — уведомлениям некуда уходить: tossling setup")
        return 1
    print(f"{OK} уведомления уходят на {target[0]}, канал по умолчанию «{ntfy_alert.DEFAULT_TOPIC}»")
    mode = ntfy_alert.phone_mode()
    claude = {"off": "выключены (mnrh claude notify phone away — включить)",
              "away": "когда ты не за Mac", "always": "всегда"}[mode]
    print(f"  Claude Code в канал «{ntfy_alert.CLAUDE_TOPIC}»: {claude}")
    return 0


def duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин {seconds % 60} с"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"


def run_command(command, topic):
    if not command:
        print("Нужна команда: mnrh alert run ./gradlew assembleRelease", file=sys.stderr)
        return 2
    shown = " ".join(shlex.quote(a) for a in command)
    tail = collections.deque(maxlen=12)
    started = time.time()
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                bufsize=1)
    except OSError as e:
        print(f"{BAD} не запустил: {e}", file=sys.stderr)
        return 127
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            tail.append(line.rstrip("\n"))
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        return 130
    took = duration(time.time() - started)
    where = tilde(os.getcwd())
    short = shown if len(shown) <= 60 else shown[:57] + "…"
    if code == 0:
        title, tags, priority = f"Готово: {short}", ["white_check_mark"], 3
    else:
        title, tags, priority = f"Ошибка: {short}", ["x"], 4
    lines = "\n".join(tail).strip()
    body = f"{where} · {took}" + ("" if code == 0 else f" · код {code}")
    if lines:
        body += "\n\n```\n" + lines[-1500:] + "\n```"
    send(body, title=title, priority=priority, topic=topic, tags=tags, markdown=True)
    return code


def main():
    if has_flag(args, "-h", "--help"):
        usage()
        return 0
    rest = list(args)
    if rest and rest[0] == "run":
        topic = None
        command = rest[1:]
        if command[:1] == ["--to"] and len(command) > 1:
            topic, command = command[1], command[2:]
        if command[:1] == ["--"]:
            command = command[1:]
        return run_command(command, topic)
    if rest == ["status"] or (not rest and sys.stdin.isatty()):
        return status()
    if rest == ["test"]:
        code = send("Если это видно на телефоне, mnrh alert работает.", title="Проверка mnrh alert", tags=["wave"])
        if code == 0:
            print(f"{OK} отправил. Не пришло — добавь в Tossling канал «{ntfy_alert.DEFAULT_TOPIC}».")
        return code
    title = option("-t", rest) or option("--title", rest) or ""
    priority = option("-p", rest) or option("--priority", rest) or "3"
    topic = option("--to", rest)
    click = option("--click", rest)
    if not priority.isdigit() or not 1 <= int(priority) <= 5:
        print("Приоритет — число от 1 до 5", file=sys.stderr)
        return 2
    text = " ".join(rest).strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text and not title:
        if not sys.stdin.isatty():
            return status()
        usage()
        return 2
    code = send(text, title=title, priority=int(priority), topic=topic, click=click)
    if code == 0 and sys.stdout.isatty():
        print(f"{OK} отправил {paint('в «' + (topic or ntfy_alert.DEFAULT_TOPIC) + '»', '2')}")
    return code


sys.exit(main())
