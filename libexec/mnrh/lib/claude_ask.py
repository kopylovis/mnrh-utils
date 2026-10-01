import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import find_project, tilde

CLAUDE_BIN = os.environ.get("MNRH_CLAUDE_BIN", "claude")
TIMEOUT = 300
DEFAULT_MODEL = "sonnet"


def ask(project, question, model=None):
    question = " ".join((question or "").split())
    if not project or not question:
        return "Нужны проект и вопрос.", True
    root, err = find_project(project)
    if err:
        return err, True
    prompt = (f"Тебя спрашивают из другого проекта, ответ уйдёт в чужую сессию Claude. Работай только на чтение: "
              f"ничего не меняй и не запускай. Найди ответ в коде проекта {root} и ответь кратко и конкретно: "
              f"факты, пути к файлам с номерами строк, нужные куски кода. Если не нашёл — так и скажи.\n\n"
              f"Вопрос: {question}")
    env = dict(os.environ, MNRH_CHILD="1")
    cmd = [CLAUDE_BIN, "-p", "--no-session-persistence", "--output-format", "json",
           "--model", model or DEFAULT_MODEL, "--tools", "Read,Grep,Glob", "--allowedTools", "Read", "Grep", "Glob",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--", prompt]
    try:
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=TIMEOUT, env=env,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"Claude в {os.path.basename(root)} не ответил за {TIMEOUT // 60} мин.", True
    except OSError as e:
        return f"Не запустился claude: {e}", True
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return f"Claude в {os.path.basename(root)} ответил не JSON: {(r.stderr or r.stdout).strip()[-800:]}", True
    answer = (data.get("result") or "").strip()
    if data.get("is_error") or not answer:
        return f"Claude в {os.path.basename(root)} не справился: {answer or data.get('subtype') or 'пустой ответ'}", True
    u = data.get("usage") or {}
    tokens = sum(u.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return f"Ответ из {os.path.basename(root)} ({tilde(root)}, {model or DEFAULT_MODEL}, ~{tokens // 1000}k токенов):\n\n{answer}", False


TOOL = {
    "name": "ask_project",
    "description": ("Спросить про другой проект: в его папке запускается отдельный Claude только с чтением (Read, Grep, "
                    "Glob) и возвращает короткий ответ с путями и кусками кода. Контекст этой сессии не засоряется "
                    "чтением чужих файлов. Например: формат ответа эндпоинта в backend, как в kcalm устроена "
                    "авторизация, какие поля у модели в другом репозитории. Занимает до пары минут и тратит лимит; "
                    "для своего проекта не нужен — читай файлы сам."),
    "inputSchema": {"type": "object", "properties": {
        "project": {"type": "string", "description": "имя папки проекта или путь"},
        "question": {"type": "string", "description": "вопрос, понятный без контекста этой сессии"},
        "model": {"type": "string", "description": "sonnet (по умолчанию), opus или haiku"}},
        "required": ["project", "question"]},
}


def main():
    args = sys.argv[2:] if sys.argv[1:2] == ["ask"] else sys.argv[1:]
    if len(args) < 2 or args[0] in ("-h", "--help"):
        print("mnrh claude ask <проект> <вопрос>   спросить отдельный Claude про другой проект (только чтение)")
        print("mnrh claude ask <проект> <вопрос> --model opus")
        print()
        print("В Claude Code: /ask <проект> <вопрос> или инструмент ask_project.")
        return 0 if args[:1] in (["-h"], ["--help"]) else 2
    model = None
    if "--model" in args:
        i = args.index("--model")
        model = args[i + 1] if i + 1 < len(args) else None
        args = args[:i] + args[i + 2:]
    text, bad = ask(args[0], " ".join(args[1:]), model)
    print(text)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
