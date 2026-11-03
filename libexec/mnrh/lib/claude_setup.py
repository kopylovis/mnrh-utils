"""Шаги mnrh claude setup и проверка перед запуском mnrh claude.

setup записывает в ~/.config/mnrh/claude-setup.json, какие шаги сделаны и от каких отказались.
mnrh claude перед запуском сверяет запись со STEPS. Если setup на этом Mac не запускали или после
обновления в STEPS появился шаг, которого нет в записи, предлагает настроить.
Новый шаг в setup: добавь его id и описание в STEPS, и при первом запуске после обновления его предложат.
Шаг с вопросом еще и в ASKS, а setup записывает его результат сам, как proxy_step.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import HOME, OK, WARN, tilde  # noqa: E402

STATE = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "mnrh", "claude-setup.json")
CLAUDE = os.environ.get("MNRH_CLAUDE_HOME", os.path.join(HOME, ".claude"))
STEPS = [
    ("commands", "команды /restart, /forget, /fresh, /slim, /todo и другие"),
    ("hooks", "хуки Claude Code: сообщения после /restart, guard, уведомления, humanization, строка состояния"),
    ("zsh", "хук перезапуска в ~/.zshrc"),
    ("mcp", "MCP-сервер mnrh"),
    ("notify-app", "приложение уведомлений"),
    ("proxy", "прокси через VPS для mnrh claude -x"),
]
IDS = [s for s, _ in STEPS]
# Шаги с вопросами, у каждого своя функция. Остальные setup делает сам и отмечает сделанными все разом.
ASKS = {"proxy"}
AUTO = set(IDS) - ASKS
# Эти шаги setup делал и до того, как начал вести запись. Если записи нет, а /restart стоит, они сделаны.
BEFORE_STATE = {"commands", "hooks", "zsh", "mcp", "notify-app"}
YES = ("", "y", "yes", "д", "да")


def read():
    try:
        with open(STATE) as f:
            data = json.load(f)
        return set(data.get("done", [])), set(data.get("skipped", []))
    except (OSError, ValueError, AttributeError):
        return None


def write(done, skipped):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"done": sorted(done), "skipped": sorted(skipped - done)}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, STATE)


def current():
    """(сделано, отказались). Без записи, но с /restart считаем сделанным то, что setup ставил до записи."""
    state = read()
    if state is not None:
        return state
    if os.path.exists(os.path.join(CLAUDE, "commands", "restart.md")):
        return set(BEFORE_STATE), set()
    return set(), set()


def record(done=(), skipped=()):
    old = current()
    write(old[0] | set(done), (old[1] - set(done)) | set(skipped))


def proxy_configured():
    import claude_proxy
    return claude_proxy.configured()


def pending():
    """(шаги, которых нет ни в сделанных, ни в отказах; True, если setup на этом Mac не запускали)."""
    if read() is None and not os.path.exists(os.path.join(CLAUDE, "commands", "restart.md")):
        return list(IDS), True
    done, skipped = current()
    if "proxy" not in done and proxy_configured():
        done = done | {"proxy"}
    return [s for s in IDS if s not in done and s not in skipped], False


def ask(question, options, default=0, back=None):
    """options: [(значение, подпись, клавиши)]; back: что вернуть на Esc. Без меню спрашивает строкой."""
    back = back or options[-1][0]
    try:
        from menu import choice
        return choice(question, options, default=default, back=back,
                      esc=next(label for value, label, _ in options if value == back).lower())
    except OSError:
        hint = "/".join(o[1] for o in options)
        answer = input(f"{question} [{hint}] ").strip().lower()
        for value, label, keys in options:
            if answer == label.lower() or (answer and answer in keys):
                return value
        return options[default][0]


def proxy_step(country="", only=""):
    """Шаг setup про прокси. Возвращает "done", "skipped" или None (не настроен и не спрашивали)."""
    import claude_proxy
    if claude_proxy.configured():
        want = claude_proxy.setting("country")
        print(f"{OK} прокси через VPS уже настроен" + (f", выход должен быть в {want}" if want else "")
              + " (проверить: mnrh claude proxy test)")
        if country and country.upper() != want.upper():
            print(f"{WARN} --country {country.upper()} не применил, прокси уже настроен. "
                  f"Поменять: mnrh claude proxy setup --country {country.upper()}")
        return "done"
    if not sys.stdin.isatty():
        print(f"{WARN} прокси через VPS не настроен, не терминал. Настроить: mnrh claude proxy setup --country KZ")
        return None
    print("Прокси через VPS: сессии mnrh claude -x ходят через твой сервер (подписка 3x-ui), остальное напрямую.")
    if ask("Настроить прокси сейчас?", [("yes", "Да", "yд"), ("no", "Пропустить", "nн")]) != "yes":
        print(f"{WARN} прокси пропущен. Настроить потом: mnrh claude proxy setup")
        return "skipped"
    if not country:
        country = input("Страна выхода, которую проверять при запуске, например KZ (Enter, не проверять): ").strip()
    claude_proxy.setup((["--country", country] if country else []) + (["--only", only] if only else []))
    return "done" if claude_proxy.configured() else None


def check():
    """Перед запуском mnrh claude: предложить setup, если его не делали или появились новые шаги."""
    if os.environ.get("MNRH_NO_SETUP_CHECK") or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return 0
    steps, fresh = pending()
    if not steps:
        return 0
    about = dict(STEPS)
    if fresh:
        print("mnrh claude setup на этом Mac еще не запускали. Он ставит:")
    else:
        print("После обновления в mnrh claude setup есть шаги, которые здесь не настроены:")
    for s in steps:
        print(f"  {about[s]}")
    answer = ask("Настроить сейчас?", [("yes", "Да", "yд"), ("later", "Позже", "lп"), ("never", "Не предлагать", "nн")],
                 back="later")
    print()
    if answer == "never":
        record(skipped=steps)
        print("Больше не спрошу. Настроить можно в любой момент: mnrh claude setup\n")
    elif answer == "yes" and steps == ["proxy"]:
        result = proxy_step()
        if result:
            record(**{"done" if result == "done" else "skipped": ["proxy"]})
        print()
    elif answer == "yes":
        subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_restart.py"),
                        "setup"])
        print()
    else:
        print("Спрошу при следующем запуске.\n")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "check":
        sys.exit(check())
    if cmd == "pending":
        steps, fresh = pending()
        print(("fresh " if fresh else "") + " ".join(steps))
        sys.exit(0)
    print("claude_setup.py check | pending", file=sys.stderr)
    sys.exit(2)
