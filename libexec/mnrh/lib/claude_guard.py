import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import HOME, read_config, tilde, write_config

LOG = os.path.join(HOME, "Library", "Logs", "mnrh-guard.log")
PROTECTED = re.compile(r"^(main|master|develop|dev|release([/-].*)?|prod(uction)?)$")
SAFE_ROOTS = ["/tmp", "/private/tmp", "/var/folders", "/private/var/folders"]
TAIL = 4 << 20

RULES = {
    "force-push": "git push --force в защищённую ветку (main, master, develop, release/*)",
    "discard": "git reset --hard, git clean, git checkout/restore файлов, git stash drop/clear, "
               "git branch -D — когда есть что потерять",
    "rm": "rm -r за пределами проекта, временных папок и scratchpad, а также сам проект, ~ и /",
    "release": "выкладка: лейны fastlane с release/beta/upload/deploy/…, gradle publish, "
               "gh release create, firebase deploy/appdistribution, скрипты distribute/deploy/release/publish",
    "secrets": "чтение и правка секретов: .env, *.p8, *.p12, *.jks, *.keystore, приватные ключи SSH, "
               "service-account/credentials *.json, keystore.properties, .netrc, пароли из связки ключей",
    "sql": "DROP TABLE/DATABASE/SCHEMA, TRUNCATE, DELETE без WHERE",
    "remote": "ssh на сервер с reboot, shutdown, systemctl stop/restart/disable, docker rm, "
              "docker compose down, rm -r",
    "pipe-shell": "curl или wget, отданные прямо в sh, bash, zsh или python",
}

SECRET_NAMES = [".env", ".env.*", "*.p8", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.pem", "*.key",
                "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "*service-account*.json", "*service_account*.json",
                "*credentials*.json", "credentials", "keystore.properties", "signing.properties", ".netrc",
                ".npmrc", ".pypirc", "*.mobileprovision"]
NOT_SECRET = [".env.example", ".env.sample", ".env.template", ".env.dist", "*.pub"]
READERS = {"cat", "less", "more", "head", "tail", "bat", "cp", "scp", "rsync", "base64", "xxd", "hexdump",
           "strings", "grep", "rg", "open", "pbcopy", "sed", "awk", "source", ".", "keytool", "openssl"}
RELEASE_LANE = re.compile(r"release|beta|upload|deploy|distribut|publish|submit|store|play|prod|supply|pilot|"
                          r"deliver|testflight|rustore|firebase|appgallery|promote", re.I)


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


def git_out(cwd, *args):
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def check_git(ws, cwd):
    args = [w for w in ws[1:]]
    while args and args[0] in ("-C", "-c"):
        if args[0] == "-C" and len(args) > 1:
            cwd = os.path.join(cwd, os.path.expanduser(args[1]))
        args = args[2:]
    if not args:
        return None
    sub, rest = args[0], args[1:]
    if sub == "push":
        force = any(a in ("-f", "--force", "--force-with-lease", "--force-if-includes") or
                    a.startswith("--force-with-lease=") for a in rest)
        refs = [a for a in rest if not a.startswith("-")]
        plus = [r for r in refs[1:] if r.startswith("+")]
        if not force and not plus:
            return None
        targets = [r.lstrip("+").split(":")[-1] for r in (plus or refs[1:])]
        if not targets:
            targets = [git_out(cwd, "branch", "--show-current")]
        bad = [t.replace("refs/heads/", "") for t in targets if PROTECTED.match(t.replace("refs/heads/", ""))]
        if bad:
            return "force-push", f"git push --force в {', '.join(bad)} перепишет историю на сервере"
        return None
    dirty = lambda: bool(git_out(cwd, "status", "--porcelain"))
    if sub == "reset" and "--hard" in rest and dirty():
        return "discard", "git reset --hard выбросит незакоммиченные изменения"
    if sub == "clean" and any(re.match(r"^-[a-zA-Z]*f", a) or a == "--force" for a in rest):
        if git_out(cwd, "clean", "-n", "-d"):
            return "discard", "git clean удалит неотслеживаемые файлы"
    if sub in ("checkout", "restore") and ("--" in rest or "." in rest) and "--staged" not in rest \
            and "-b" not in rest and "-B" not in rest and dirty():
        return "discard", f"git {sub} откатит незакоммиченные изменения в файлах"
    if sub == "stash" and rest[:1] in (["drop"], ["clear"]):
        return "discard", f"git stash {rest[0]} удалит отложенные изменения"
    if sub == "branch" and ("-D" in rest or "--delete" in rest and ("--force" in rest or "-f" in rest)):
        return "discard", "git branch -D удалит ветку вместе с неслитыми коммитами"
    return None


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


def check_rm(ws, cwd, env, project, scratch):
    flags = [w for w in ws[1:] if w.startswith("-") and w != "--"]
    if not any(re.match(r"^-\w*[rR]", f) or f == "--recursive" for f in flags):
        return None
    targets = [w for w in ws[1:] if not w.startswith("-") or w == "--"]
    targets = [t for t in targets if t != "--"]
    roots = [project] + SAFE_ROOTS + ([scratch] if scratch else [])
    for t in targets:
        p = resolve(t, cwd, env)
        if p is None:
            return "rm", f"rm -r {t}: путь не раскрыть заранее, проверь, что там"
        if p in ("/", HOME, project) or p == os.path.dirname(project):
            return "rm", f"rm -r {tilde(p)} — это {'проект целиком' if p == project else 'корень или домашний каталог'}"
        if not any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots):
            return "rm", f"rm -r {tilde(p)} — за пределами проекта и временных папок"
    return None


def check_release(ws, segment):
    if not ws:
        return None
    exe = os.path.basename(ws[0])
    rest = ws[1:]
    if exe == "bundle" and rest[:1] == ["exec"]:
        ws, rest = rest[1:], rest[2:]
        exe = os.path.basename(ws[0]) if ws else ""
    if exe == "fastlane":
        lanes = [w for w in rest if not w.startswith("-") and ":" not in w]
        if lanes and lanes[0] in ("android", "ios", "mac"):
            lanes = lanes[1:]
        if lanes and (lanes[0] == "run" and len(lanes) > 1 and RELEASE_LANE.search(lanes[1]) or RELEASE_LANE.search(lanes[0])):
            return "release", f"fastlane {' '.join(lanes[:2])} выкладывает сборку"
    if exe in ("gradle", "gradlew") or exe.endswith("gradlew"):
        tasks = [w for w in rest if re.search(r"(^|:)publish", w) and "MavenLocal" not in w]
        if tasks:
            return "release", f"gradle {tasks[0]} публикует артефакты"
    if exe == "gh" and rest[:2] == ["release", "create"]:
        return "release", "gh release create выпускает релиз на GitHub"
    if exe == "firebase" and rest[:1] and (rest[0] == "deploy" or rest[0].startswith("appdistribution")):
        return "release", f"firebase {rest[0]} выкладывает"
    if re.search(r"(^|/)[\w.-]*(distribut|deploy|release|publish)[\w.-]*\.(sh|rb|py)$", ws[0]):
        return "release", f"{os.path.basename(ws[0])} похоже на выкладку"
    return None


def check_secrets_cmd(ws, segment):
    if not ws:
        return None
    exe = os.path.basename(ws[0])
    if exe == "security" and len(ws) > 1 and (ws[1] == "dump-keychain" or
                                               (ws[1].startswith("find-") and ("-w" in ws or "-g" in ws))):
        return "secrets", "пароль из связки ключей попадёт в переписку"
    if exe in READERS:
        for w in ws[1:]:
            if not w.startswith("-") and is_secret(w):
                return "secrets", f"{exe} {tilde(os.path.expanduser(w))}: содержимое секрета попадёт в переписку"
    for m in re.finditer(r"<\s*([^\s<>|;&]+)", segment):
        if is_secret(m.group(1)):
            return "secrets", f"чтение {m.group(1)}: содержимое секрета попадёт в переписку"
    return None


def check_bash(cmd, cwd, project, scratch):
    if re.search(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh|python3?)\b", cmd):
        return "pipe-shell", "скачанный скрипт выполнится сразу, не глядя"
    if re.search(r"\bdrop\s+(table|database|schema)\b|\btruncate\s+(table\s+)?\w|\bdelete\s+from\s+[\w.\"]+\s*(;|$|\"|')",
                 cmd, re.I | re.M):
        return "sql", "SQL-запрос удалит данные"
    m = re.search(r"\bssh\b\s+(?:-\S+\s+(?:\S+\s+)?)*([\w.@-]+)\s+(.+)", cmd, re.S)
    if m and re.search(r"\b(reboot|shutdown|poweroff|halt)\b|systemctl\s+(stop|restart|disable|mask)\b|"
                       r"docker(\s+compose|-compose)?\s+(rm|down|kill|prune)\b|\brm\s+-\w*[rR]", m.group(2)):
        return "remote", f"команда на сервере {m.group(1)} что-то остановит или удалит"
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
        exe = os.path.basename(ws[0])
        found = (check_git(ws, cwd) if exe == "git" else None) or \
            (check_rm(ws, cwd, env, project, scratch) if exe == "rm" else None) or \
            check_release(ws, seg) or check_secrets_cmd(ws, seg)
        if found:
            return found
    return None


def check(tool, inp, cwd, scratch=None):
    project = os.path.normpath(cwd or os.getcwd())
    if tool == "Bash":
        return check_bash(str(inp.get("command") or ""), project, project, scratch)
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    if tool in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit") and path and is_secret(path):
        verb = "чтение" if tool == "Read" else "правка"
        return "secrets", f"{verb} {tilde(path)}: это секрет, его содержимое попадёт в переписку"
    return None


def code_for(sid, tool, inp):
    raw = json.dumps([sid, tool, inp.get("command") or inp.get("file_path") or ""], sort_keys=True)
    return str(int(hashlib.sha256(raw.encode()).hexdigest(), 16) % 9000 + 1000)


def last_user_text(path):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL))
            chunk = f.read()
    except (OSError, TypeError):
        return ""
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
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            texts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
            if texts:
                return "\n".join(texts)
    return ""


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
        log(f"ask   {rule:<10} {what}")
        return {"permissionDecision": "ask", "permissionDecisionReason": f"mnrh guard: {why}"}
    code = code_for(payload.get("session_id"), tool, inp)
    if re.search(rf"(?<!\d){code}(?!\d)", last_user_text(payload.get("transcript_path"))):
        log(f"allow {rule:<10} код {code} {what}")
        return {"permissionDecision": "allow", "permissionDecisionReason": f"mnrh guard: подтверждено кодом {code}"}
    log(f"deny  {rule:<10} код {code} {what}")
    return {"permissionDecision": "deny", "permissionDecisionReason": (
        f"mnrh guard остановил: {why}. Не обходи это другой командой. Объясни пользователю, что и зачем "
        f"собираешься сделать, и спроси разрешения. Если он согласен, он сам напишет в чате код {code}, "
        f"после этого повтори ровно этот же вызов. Отключить правило: mnrh claude guard skip {rule}.")}


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
    print("Хук PreToolUse останавливает опасные команды и чтение секретов. В обычном режиме Claude Code")
    print("просто спросит разрешение. В auto и --dangerously-skip-permissions вызов запрещается, и Claude")
    print("должен спросить тебя: чтобы разрешить, ты сам пишешь в чате четырёхзначный код из его вопроса.")


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
