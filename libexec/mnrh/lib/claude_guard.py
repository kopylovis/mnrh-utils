import calendar
import fnmatch
import hashlib
import json
import os
import re
import shlex
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import HOME, read_config, tilde, write_config

LOG = os.path.join(HOME, "Library", "Logs", "mnrh-guard.log")
INSIDE_ONLY = [os.path.join(HOME, ".cache")]
TAIL = 4 << 20

RULES = {
    "secrets-read": "чтение и правка секретов: .env, *.p8, *.p12, *.jks, *.keystore, приватные ключи SSH, "
                    "service-account/credentials *.json, keystore.properties, .netrc, пароли из связки ключей",
    "secrets-delete": "удаление секретов: rm файла-ключа или папки, где они лежат (~/.ssh, fastlane/ с ключами…), "
                      "security delete-* из связки ключей",
}
GUARD_STATE = os.path.join(HOME, ".cache", "mnrh", "guard")
YES = re.compile(r"^\W*(да|ага|угу|ок|окей|ok|okay|yes|y|давай|выполняй|можно|разрешаю|разреши|делай|go|sure)\b", re.I)
SENSITIVE_DIRS = ["~/.ssh", "~/.gnupg", "~/.aws", "~/Library/Keychains", "~/.config/gh"]
DELETERS = {"rm", "rmdir", "unlink", "shred", "srm", "trash"}
WALK_LIMIT = 3000

SECRET_NAMES = [".env", ".env.*", "*.p8", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.pem", "*.key",
                "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "*service-account*.json", "*service_account*.json",
                "*credentials*.json", "credentials", "keystore.properties", "signing.properties", ".netrc",
                ".npmrc", ".pypirc", "*.mobileprovision"]
NOT_SECRET = [".env.example", ".env.sample", ".env.template", ".env.dist", "*.pub"]
READERS = {"cat", "less", "more", "head", "tail", "bat", "cp", "scp", "rsync", "base64", "xxd", "hexdump",
           "strings", "grep", "rg", "open", "pbcopy", "sed", "awk", "source", ".", "keytool", "openssl"}


def enabled():
    return read_config().get("guard", "on") != "off"


def skipped():
    return {r.strip() for r in read_config().get("guard_skip", "").split(",") if r.strip()}


def log(line):
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass


def is_secret(path):
    name = os.path.basename(path.rstrip("/"))
    if not name or any(fnmatch.fnmatch(name, p) for p in NOT_SECRET):
        return False
    if any(fnmatch.fnmatch(name, p) for p in SECRET_NAMES):
        return True
    full = os.path.expanduser(path)
    for d in ("~/.ssh/", "~/.aws/", "~/.gnupg/", "~/.config/gh/hosts", "~/.claude/.credentials"):
        if full.startswith(os.path.expanduser(d)) and not name.endswith((".pub", "known_hosts", "config")):
            return True
    return False


def split_segments(cmd):
    parts = re.split(r"\|\||&&|;|\n|(?<![|>])\|(?!\|)", cmd)
    return [p.strip() for p in parts if p.strip()]


def words(segment):
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return segment.split()


def strip_env(ws):
    i = 0
    while i < len(ws) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", ws[i]):
        i += 1
    ws = ws[i:]
    while ws and ws[0] in ("sudo", "command", "exec", "nohup", "time", "env", "caffeinate", "xargs"):
        ws = ws[1:]
        while ws and ws[0].startswith("-"):
            ws = ws[1:]
    return ws


def resolve(path, cwd, env):
    def sub(m):
        name = m.group(1) or m.group(2)
        return env.get(name, m.group(0))
    path = re.sub(r"\$\{([A-Za-z_]\w*)(?::[?-][^}]*)?\}|\$([A-Za-z_]\w*)", sub, path)
    if "$" in path or "`" in path:
        return None
    path = os.path.expanduser(path)
    return os.path.normpath(os.path.join(cwd, path))


def assigned(value, cwd, env):
    if value.startswith(("/", "~", "$", ".")):
        return resolve(value, cwd, env) or value
    return value


def check_secrets_cmd(ws, segment):
    if not ws:
        return None
    exe = os.path.basename(ws[0])
    if exe == "security" and len(ws) > 1 and (ws[1] == "dump-keychain" or
                                               (ws[1].startswith("find-") and ("-w" in ws or "-g" in ws))):
        return "secrets-read", "пароль из связки ключей попадёт в переписку"
    if exe in READERS:
        for w in ws[1:]:
            if not w.startswith("-") and is_secret(w):
                return "secrets-read", f"{exe} {tilde(os.path.expanduser(w))}: содержимое секрета попадёт в переписку"
    for m in re.finditer(r"<\s*([^\s<>|;&]+)", segment):
        if is_secret(m.group(1)):
            return "secrets-read", f"чтение {m.group(1)}: содержимое секрета попадёт в переписку"
    return None


def holds_secrets(path):
    full = os.path.realpath(path)
    for d in SENSITIVE_DIRS:
        d = os.path.realpath(os.path.expanduser(d))
        if full == d or d.startswith(full.rstrip("/") + "/"):
            return tilde(d) + "/"
        if full.startswith(d + "/"):
            return tilde(path)
    if os.path.isfile(path):
        return tilde(path) if is_secret(path) else None
    if not os.path.isdir(path):
        return tilde(path) if is_secret(path) else None
    seen = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [x for x in dirs if x not in ("node_modules", ".git", "build", ".gradle", "Pods", "DerivedData")]
        for f in files:
            seen += 1
            if is_secret(os.path.join(root, f)):
                return tilde(os.path.join(root, f))
        if seen > WALK_LIMIT or root.count(os.sep) - path.count(os.sep) >= 4:
            dirs[:] = []
    return None


def check_secret_delete(ws, cwd, env):
    exe = os.path.basename(ws[0])
    if exe == "security" and len(ws) > 1 and ws[1].startswith("delete-"):
        return "secrets-delete", f"security {ws[1]} удалит запись из связки ключей"
    if exe not in DELETERS:
        return None
    for t in ws[1:]:
        if t.startswith("-"):
            continue
        p = resolve(t, cwd, env)
        if p is None:
            continue
        found = holds_secrets(p)
        if found:
            what = (f"папка с ключами {found}" if found.endswith("/") else "это секрет" if found == tilde(p)
                    else f"там секрет: {found}")
            return "secrets-delete", f"{exe} {tilde(p)} — {what}, восстановить не получится"
    return None


def check_bash(cmd, cwd):
    env = {"HOME": HOME, "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "PWD": cwd}
    for seg in split_segments(cmd):
        raw = words(seg)
        for w in raw:
            m2 = re.match(r"^([A-Za-z_]\w*)=(.*)$", w)
            if not m2:
                break
            env[m2.group(1)] = assigned(m2.group(2), cwd, env)
        ws = strip_env(raw)
        if not ws:
            continue
        if ws[0] == "export":
            for w in ws[1:]:
                m2 = re.match(r"^([A-Za-z_]\w*)=(.*)$", w)
                if m2:
                    env[m2.group(1)] = assigned(m2.group(2), cwd, env)
            continue
        if ws[0] == "cd" and len(ws) > 1:
            nxt = resolve(ws[1], cwd, env)
            if nxt:
                cwd = nxt
            continue
        found = check_secret_delete(ws, cwd, env) or check_secrets_cmd(ws, seg)
        if found:
            return found
    return None


def check(tool, inp, cwd, scratch=None):
    cwd = os.path.normpath(cwd or os.getcwd())
    if tool == "Bash":
        return check_bash(str(inp.get("command") or ""), cwd)
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    if tool in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit") and path and is_secret(path):
        verb = "чтение" if tool == "Read" else "правка"
        return "secrets-read", f"{verb} {tilde(path)}: это секрет, его содержимое попадёт в переписку"
    return None


def call_key(sid, tool, inp):
    raw = json.dumps([sid, tool, inp.get("command") or inp.get("file_path") or ""], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def pending_path(sid):
    return os.path.join(GUARD_STATE, re.sub(r"[^\w-]", "_", sid or "none") + ".json")


def load_pending(sid):
    try:
        with open(pending_path(sid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_pending(sid, data):
    os.makedirs(GUARD_STATE, exist_ok=True)
    data = {k: v for k, v in data.items() if time.time() - v < 3600}
    with open(pending_path(sid), "w") as f:
        json.dump(data, f)


def last_user(path):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL))
            chunk = f.read()
    except (OSError, TypeError):
        return "", 0
    for raw in reversed(chunk.split(b"\n")):
        if not re.search(rb'"type":\s*"user"', raw):
            continue
        try:
            o = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if o.get("isMeta") or o.get("isCompactSummary"):
            continue
        c = (o.get("message") or {}).get("content")
        text = c if isinstance(c, str) else "\n".join(
            b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text") if isinstance(c, list) else ""
        if not text:
            continue
        try:
            ts = calendar.timegm(time.strptime(o.get("timestamp", "")[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            ts = 0
        return text, ts
    return "", 0


def decide(payload):
    tool = payload.get("tool_name") or ""
    inp = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    found = check(tool, inp, payload.get("cwd"), payload.get("scratchpad_dir"))
    if not found or found[0] in skipped():
        return None
    rule, why = found
    mode = payload.get("permission_mode") or "default"
    what = str(inp.get("command") or inp.get("file_path") or "")[:200].replace("\n", " ")
    if mode not in ("bypassPermissions", "auto", "dontAsk"):
        log(f"ask   {rule:<14} {what}")
        return {"permissionDecision": "ask", "permissionDecisionReason": f"mnrh guard: {why}"}
    sid = payload.get("session_id")
    key = call_key(sid, tool, inp)
    pending = load_pending(sid)
    text, ts = last_user(payload.get("transcript_path"))
    if key in pending and ts > pending[key] and YES.search(text.strip()):
        pending.pop(key)
        save_pending(sid, pending)
        log(f"allow {rule:<14} {what}")
        return {"permissionDecision": "allow", "permissionDecisionReason": "mnrh guard: пользователь разрешил"}
    pending[key] = time.time()
    save_pending(sid, pending)
    log(f"hold  {rule:<14} {what}")
    return {"permissionDecision": "deny", "permissionDecisionReason": (
        f"mnrh guard: {why}. Это не отказ: спроси пользователя одним коротким вопросом, выполнить ли это, и объясни "
        f"зачем. Если он ответит «да» — повтори ровно этот же вызов, и он пройдёт. Если нет — не делай и не ищи "
        f"обходной путь.")}


def hook():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict) or not enabled():
        return 0
    try:
        out = decide(payload)
    except Exception as e:
        log(f"error {e!r}")
        return 0
    if out:
        print(json.dumps({"hookSpecificOutput": dict({"hookEventName": "PreToolUse"}, **out)}, ensure_ascii=False))
    return 0


def usage():
    print("mnrh claude guard                   состояние и правила")
    print("mnrh claude guard on | off          включить или выключить совсем")
    print("mnrh claude guard skip <правило>    не проверять правило; unskip <правило> — вернуть")
    print("mnrh claude guard test '<команда>'  что сделает guard с этой командой (в папке, где запущен)")
    print("mnrh claude guard log               последние решения")
    print()
    print("Хук PreToolUse следит только за чтением и удалением секретов и ключей. В обычном режиме Claude Code")
    print("спросит разрешение сам. В auto Claude спросит тебя в чате: ответишь «да» — этот вызов пройдёт.")


def status():
    skip = skipped()
    print(f"mnrh guard: {'включён' if enabled() else 'выключен'}. Лог: {tilde(LOG)}")
    for rule, text in RULES.items():
        mark = "  выкл" if rule in skip else "  вкл "
        print(f"{mark} {rule:<11} {text}")


def main():
    args = sys.argv[1:]
    if args[:1] == ["guard-hook"]:
        return hook()
    rest = args[1:]
    if not rest:
        status()
        return 0
    cmd = rest[0]
    if cmd in ("-h", "--help"):
        usage()
        return 0
    if cmd in ("on", "off"):
        write_config(guard=cmd)
        print(f"mnrh guard {'включён' if cmd == 'on' else 'выключен'}")
        return 0
    if cmd in ("skip", "unskip") and len(rest) == 2:
        if rest[1] not in RULES:
            print(f"Нет правила «{rest[1]}». Есть: {', '.join(RULES)}", file=sys.stderr)
            return 2
        skip = skipped()
        skip = skip | {rest[1]} if cmd == "skip" else skip - {rest[1]}
        write_config(guard_skip=",".join(sorted(skip)))
        print(f"Правило {rest[1]} {'выключено' if cmd == 'skip' else 'включено'}")
        return 0
    if cmd == "test" and len(rest) >= 2:
        text = " ".join(rest[1:])
        tool, inp = ("Read", {"file_path": text}) if not re.search(r"\s", text) and is_secret(text) else \
            ("Bash", {"command": text})
        found = check(tool, inp, os.getcwd())
        if not found:
            print("пропущу")
        else:
            print(f"остановлю ({found[0]}{', правило выключено' if found[0] in skipped() else ''}): {found[1]}")
        return 0
    if cmd == "log":
        try:
            with open(LOG) as f:
                print("".join(f.readlines()[-30:]), end="")
        except OSError:
            print("Лог пуст.")
        return 0
    usage()
    return 2


if __name__ == "__main__":
    sys.exit(main())
