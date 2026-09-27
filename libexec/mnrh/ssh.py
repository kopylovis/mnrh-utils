import glob
import os
import plistlib
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import BAD, HOME, OK, WARN, confirm, git_versions, has_flag, paint, projects, run, tilde, version_key

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or (args and args[0] != "github"):
    print("mnrh ssh                      ключи в ~/.ssh, какие загружены, кем они представляются GitHub")
    print("mnrh ssh github               пошагово: аккаунт, в который вошёл gh (или войти в другой),")
    print("                              ключ, Связка, ~/.ssh/config, GitHub, подпись, Sourcetree (если есть)")
    print("mnrh ssh github <логин>       то же для указанного аккаунта без вопроса")
    print("                              подробнее: выпустить ключ, загрузить в Связку, прописать")
    print("                              в ~/.ssh/config, добавить в GitHub, подписывать им коммиты,")
    print("                              перевести репозитории аккаунта с HTTPS на SSH (спросит)")
    print("   --alias github-work        имя хоста для второго аккаунта (первый получает github.com)")
    print("   --no-sign                  не подписывать коммиты этим ключом")
    print("   --ask-passphrase           задать пароль самому; по умолчанию он случайный и живёт только в Связке")
    print("   -y                         не спрашивать про перевод репозиториев")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)

SSH = os.path.join(HOME, ".ssh")
CONFIG = os.path.join(SSH, "config")
LOADER_LABEL = "com.mnrh.ssh-keychain"
LOADER = os.path.join(HOME, "Library", "LaunchAgents", f"{LOADER_LABEL}.plist")
ALLOWED = os.path.join(HOME, ".config", "git", "allowed_signers")
SCOPES = "admin:public_key,admin:ssh_signing_key"
assume_yes = has_flag(args, "-y", "--yes")


def sh(cmd, env=None, inp=None, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, input=inp, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, str(e)


def fingerprint(path):
    out = run(["ssh-keygen", "-lf", path]).split()
    return out[1] if len(out) > 1 else ""


def github_identity(host, key=None):
    cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        cmd += ["-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-i", key]
    _, out = sh(cmd + [f"git@{host}"], timeout=20)
    m = re.search(r"Hi ([^!]+)!", out)
    return m.group(1) if m else None


def mnrh_blocks():
    """{хост: (логин, ключ)} из блоков, которые записал mnrh."""
    try:
        text = open(CONFIG).read()
    except OSError:
        return {}
    found = {}
    for m in re.finditer(r"# mnrh ssh github (\S+)\nHost (\S+)\n(.*?)# /mnrh ssh", text, re.S):
        key = re.search(r"IdentityFile (\S+)", m.group(3))
        found[m.group(2)] = (m.group(1), key.group(1) if key else "")
    return found


# ---------- состояние ----------

def status():
    loaded = run(["ssh-add", "-l"])
    blocks = mnrh_blocks()
    keys = sorted(p[:-4] for p in glob.glob(os.path.join(SSH, "*.pub")) if os.path.exists(p[:-4]))
    if not keys:
        print("В ~/.ssh ключей нет." + paint("   -> mnrh ssh github", "2"))
    else:
        print(paint("Ключи в ~/.ssh:", "1"))
        for k in keys:
            info = run(["ssh-keygen", "-lf", k + ".pub"]).split()
            bits, fp, kind = (info[0], info[1], info[-1].strip("()")) if len(info) >= 3 else ("?", "", "?")
            year = time.strftime("%Y", time.localtime(os.path.getmtime(k)))
            in_agent = "в агенте" if fp and fp in loaded else "не загружен"
            weak = kind == "RSA" and bits.isdigit() and int(bits) < 3072 or kind in ("DSA", "ECDSA")
            print(f"  {WARN if weak else ' '} {os.path.basename(k):<32} {kind} {bits}, {year}, {in_agent}")
    if blocks:
        print(f"\n{paint('GitHub через mnrh:', '1')}")
        for host, (login, key) in blocks.items():
            who = github_identity(host)
            mark = OK if who == login else BAD
            print(f"  {mark} git@{host} -> {who or 'не пускает'}" + ("" if who == login else f" (ожидался {login})")
                  + paint(f"   {key}", "2"))
    else:
        who = github_identity("github.com")
        print(f"\ngit@github.com сейчас представляется как: {who or 'никто (ключа нет)'}"
              + paint("   -> mnrh ssh github", "2"))
    signing = run(["git", "config", "--global", "gpg.format"]).strip()
    if signing == "ssh":
        key = run(["git", "config", "--global", "user.signingkey"]).strip()
        on = run(["git", "config", "--global", "commit.gpgsign"]).strip() == "true"
        print(f"\nПодпись коммитов: {'включена' if on else 'настроена, но выключена'}, ключ {tilde(key)}")
    st = [a for a in sourcetree_accounts() if "github" in a["host"]] if sourcetree_app() else []
    if st:
        print("Sourcetree, аккаунты GitHub: " + ", ".join(
            f"{a['user']} ({'SSH' if a['ssh'] else 'HTTPS'})" for a in st))
    proto = run(["gh", "config", "get", "git_protocol", "-h", "github.com"]).strip()
    if proto:
        print(f"gh клонирует по: {proto}")


# ---------- выпуск ----------

def encrypted(key):
    return sh(["ssh-keygen", "-y", "-P", "", "-f", key])[0] != 0


def gh_env(login):
    token = run(["gh", "auth", "token", "-h", "github.com", "-u", login]).strip()
    if not token:
        return None
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    return env


def add_to_keychain(key, passphrase):
    if passphrase is None:
        # Пароль задавал человек: спросить его может только терминал.
        if sys.stdin.isatty():
            subprocess.run(["ssh-add", "--apple-use-keychain", key])
        return
    with tempfile.TemporaryDirectory() as tmp:
        askpass = os.path.join(tmp, "askpass")
        with open(askpass, "w") as f:
            f.write('#!/bin/sh\nprintf "%s\\n" "$MNRH_SSH_PASS"\n')
        os.chmod(askpass, stat.S_IRWXU)
        env = dict(os.environ, SSH_ASKPASS=askpass, SSH_ASKPASS_REQUIRE="force", MNRH_SSH_PASS=passphrase,
                   DISPLAY=os.environ.get("DISPLAY", ":0"))
        code, out = sh(["ssh-add", "--apple-use-keychain", key], env=env, inp="")
        if code != 0:
            sys.exit(f"Не удалось добавить ключ в Связку: {out}")
    # Случайный пароль живёт только в Связке. Проверяем, что он там правда есть: выгружаем
    # ключ из агента и просим macOS загрузить его обратно. Если не вышло — снимаем пароль,
    # иначе ключ станет кирпичом.
    sh(["ssh-add", "-d", key + ".pub"])
    sh(["ssh-add", "--apple-load-keychain", "-q"])
    if fingerprint(key + ".pub") in run(["ssh-add", "-l"]):
        return
    sh(["ssh-keygen", "-p", "-q", "-P", passphrase, "-N", "", "-f", key])
    sh(["ssh-add", key])
    print(f"{WARN} Связка ключей не приняла пароль (так бывает не из Терминала), ключ оставлен без пароля.")


def write_block(host, login, key):
    block = (f"# mnrh ssh github {login}\nHost {host}\n  HostName github.com\n  User git\n"
             f"  IdentityFile {tilde(key)}\n  IdentitiesOnly yes\n  AddKeysToAgent yes\n  UseKeychain yes\n"
             "# /mnrh ssh\n")
    try:
        text = open(CONFIG).read()
    except OSError:
        text = ""
    new = re.sub(r"# mnrh ssh github \S+\nHost " + re.escape(host) + r"\n.*?# /mnrh ssh\n\n?", "", text, flags=re.S)
    # В начало: у ssh побеждает первое найденное значение, так наш блок не перебьёт «Host *» ниже.
    new = block + "\n" + new
    if new == text:
        return False
    if text:
        shutil.copy2(CONFIG, CONFIG + ".mnrh-backup")
    os.makedirs(SSH, mode=0o700, exist_ok=True)
    with open(CONFIG, "w") as f:
        f.write(new)
    os.chmod(CONFIG, 0o600)
    return True


def install_loader():
    """Подгружать ключи из Связки в агент при входе: без этого подпись коммита до первого ssh не сработает."""
    if os.path.exists(LOADER):
        return
    os.makedirs(os.path.dirname(LOADER), exist_ok=True)
    with open(LOADER, "wb") as f:
        plistlib.dump({"Label": LOADER_LABEL, "ProgramArguments": ["/usr/bin/ssh-add", "--apple-load-keychain", "-q"],
                       "RunAtLoad": True}, f)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", LOADER], capture_output=True)


def upload(login, pub, title):
    env = gh_env(login)
    if not env:
        print(f"{BAD} gh не знает аккаунт {login}: gh auth login, потом повтори mnrh ssh github {login}")
        return False
    material = " ".join(open(pub).read().split()[:2])
    done = True
    for kind, endpoint, scope in (("authentication", "user/keys", "admin:public_key"),
                                  ("signing", "user/ssh_signing_keys", "admin:ssh_signing_key")):
        if kind == "signing" and has_flag(args, "--no-sign"):
            continue
        code, out = sh(["gh", "api", endpoint, "--paginate", "--jq", ".[].key"], env=env)
        if code != 0 and sys.stdin.isatty():
            print(f"У gh нет права добавлять ключи в {login}. Сейчас gh покажет код и откроет GitHub —")
            print("подтверди там доступ, дальше продолжу сам.")
            subprocess.run(["gh", "auth", "refresh", "-h", "github.com", "-u", login, "-s", SCOPES])
            env = gh_env(login) or env
            code, out = sh(["gh", "api", endpoint, "--paginate", "--jq", ".[].key"], env=env)
        if code != 0:
            print(f"{WARN} Нет права {scope} у gh для {login}. Запусти в Терминале: mnrh ssh github {login}")
            print(f"  (или вручную: gh auth refresh -h github.com -u {login} -s {SCOPES})")
            return False
        if material in out:
            print(f"{OK} В GitHub уже есть ключ ({'вход' if kind == 'authentication' else 'подпись'})")
            continue
        code, out = sh(["gh", "ssh-key", "add", pub, "--title", title, "--type", kind], env=env)
        if code != 0:
            print(f"{BAD} GitHub не принял ключ ({kind}): {out}")
            done = False
        else:
            print(f"{OK} Ключ добавлен в GitHub ({'вход' if kind == 'authentication' else 'подпись коммитов'})")
    return done


def old_gits():
    return [(p, v) for p, v in git_versions() if version_key(v) < [2, 34]]


def setup_signing(login, pub):
    old = old_gits()
    if old:
        # git до 2.34 не знает gpg.format ssh и из-за него перестаёт читать ~/.gitconfig целиком.
        print(f"{WARN} Подпись не включаю: git старше 2.34 не понимает SSH-подпись и сломается целиком.")
        for path, v in old:
            print(f"  {path}: git {v}")
        if os.path.exists("/usr/local/git/uninstall.sh"):
            print(paint("  Это старый установщик git-osx-installer. Убрать: sudo /usr/local/git/uninstall.sh,", "2"))
            print(paint("  после этого работает системный git, и mnrh ssh github включит подпись.", "2"))
        return
    email = run(["git", "config", "--global", "user.email"]).strip()
    for k, v in (("gpg.format", "ssh"), ("user.signingkey", pub), ("commit.gpgsign", "true"),
                 ("tag.gpgsign", "true"), ("gpg.ssh.allowedSignersFile", ALLOWED)):
        subprocess.run(["git", "config", "--global", k, v])
    os.makedirs(os.path.dirname(ALLOWED), exist_ok=True)
    line = f"{email} {' '.join(open(pub).read().split()[:2])}\n"
    try:
        existing = open(ALLOWED).read()
    except OSError:
        existing = ""
    if line not in existing:
        with open(ALLOWED, "a") as f:
            f.write(line)
    print(f"{OK} Коммиты и теги подписываются этим ключом (для {email or 'user.email не задан!'})")
    if email:
        print(paint(f"  «Verified» на GitHub будет, если {email} подтверждён в аккаунте {login}.", "2"))


def switch_remotes(login, host):
    todo = []
    for proj in projects():
        url = run(["git", "-C", proj, "remote", "get-url", "origin"]).strip()
        m = re.match(r"https://github\.com/([^/]+)/(.+?)(?:\.git)?/?$", url)
        if m and m.group(1).lower() == login.lower():
            todo.append((proj, f"git@{host}:{m.group(1)}/{m.group(2)}.git"))
    if not todo:
        print("Репозиториев аккаунта на HTTPS в папке проектов нет.")
        return
    print(f"\nРепозитории {login} на HTTPS:")
    for proj, url in todo:
        print(f"  {os.path.basename(proj):<28} -> {url}")
    if not assume_yes and not sys.stdin.isatty():
        print("Перевести: mnrh ssh github " + login + " -y")
        return
    if not confirm("Перевести их на SSH?", assume_yes):
        print("Оставил как есть.")
        return
    for proj, url in todo:
        subprocess.run(["git", "-C", proj, "remote", "set-url", "origin", url])
    print(f"{OK} Переведено: {len(todo)}")


ST_IDS = ("com.torusknot.SourceTreeNotMAS", "com.torusknot.SourceTree")
ST_ACCOUNTS = os.path.join(HOME, "Library", "Application Support", "SourceTree", "hostingservices_new.plist")


def sourcetree_app():
    for path in ("/Applications/Sourcetree.app", os.path.join(HOME, "Applications", "Sourcetree.app")):
        if os.path.isdir(path):
            return path
    for bundle_id in ST_IDS:
        found = run(["mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'"], timeout=10).splitlines()
        if found:
            return found[0]
    return None


def sourcetree_accounts():
    """Аккаунты Sourcetree только для чтения: [{host, user, ssh_key}]. Файл — его внутренний NSKeyedArchiver."""
    try:
        with open(ST_ACCOUNTS, "rb") as f:
            objs = plistlib.load(f)["$objects"]
    except Exception:
        return []

    def val(x):
        v = objs[x.data] if isinstance(x, plistlib.UID) else x
        if isinstance(v, dict) and "NS.relative" in v:
            return val(v["NS.relative"])
        if isinstance(v, dict) and "baseURL" in v:
            return val(v["baseURL"])
        return v

    found = []
    for o in objs:
        if isinstance(o, dict) and "protocol" in o and "credential" in o:
            cred = val(o["credential"])
            user = val(cred.get("username")) if isinstance(cred, dict) else ""
            proto = val(o["protocol"])
            found.append({"host": str(val(o.get("host"))), "user": str(user or ""),
                          # 0 — HTTPS, 1 — SSH (так Sourcetree пишет выбор из поля Protocol)
                          "ssh": proto == 1 or proto is True})
    return found


def sourcetree_step(login, app):
    # Sourcetree для Mac не выбирает ключ: в окне аккаунта всегда написано id_rsa.pub, а git он
    # запускает через системный OpenSSH, и тот берёт ключ из ~/.ssh/config (шаг 3). Остаётся только
    # переключить аккаунт на SSH, чтобы новые клоны шли по SSH.
    def mine():
        return next((a for a in sourcetree_accounts()
                     if "github" in a["host"] and a["user"].lower() == login.lower()), None)

    acc = mine()
    if acc and acc["ssh"]:
        print(f"{OK} Аккаунт {login} в Sourcetree работает по SSH")
    else:
        if not acc:
            print(f"В Sourcetree нет аккаунта {login}: Settings → Accounts → Add → GitHub, войди как {login}.")
        print(f"В Sourcetree: Settings → Accounts → {login} → Edit → Protocol: SSH → Save")
        if sys.stdin.isatty():
            run(["open", "-a", app])
            input("Нажми Enter, когда сохранишь... ")
            acc = mine()
            print(f"{OK} Аккаунт {login} в Sourcetree работает по SSH" if acc and acc["ssh"]
                  else f"{WARN} Пока не вижу SSH у {login}: Sourcetree пишет настройки при закрытии окна настроек")
    print(paint("  В окне аккаунта будет написано «SSH Key: id_rsa.pub» — это надпись, а не выбор: Sourcetree для Mac\n"
                "  всегда показывает id_rsa. Git в нём идёт через системный ssh и берёт ключ из ~/.ssh/config.", "2"))
    others = sorted({a["user"] for a in sourcetree_accounts()
                     if "github" in a["host"] and a["user"].lower() != login.lower()})
    if others:
        print(f"{WARN} Ещё аккаунты GitHub в Sourcetree: {', '.join(others)}. Если не нужны — Remove там же.")


STEPS = ["ключ", "Связка ключей и агент", "~/.ssh/config", "GitHub", "подпись коммитов",
         "проверка и репозитории", "Sourcetree"]


def step(n):
    print(f"\n{paint(f'Шаг {n}/{len(STEPS)}: {STEPS[n - 1]}', '1')}")


def ask_yes(question):
    return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes", "д", "да")


def gh_login_now():
    """Вход в GitHub через браузер: какой аккаунт выберешь там, тот и настраиваем."""
    print("Сейчас gh покажет код и откроет GitHub: войди в нужный аккаунт.")
    r = subprocess.run(["gh", "auth", "login", "-h", "github.com", "--web", "--git-protocol", "ssh",
                        "--skip-ssh-key", "-s", SCOPES])
    if r.returncode != 0:
        sys.exit("Вход в GitHub не завершён.")
    return run(["gh", "api", "user", "--jq", ".login"]).strip()


def choose_login(explicit):
    if explicit:
        return explicit
    if not shutil.which("gh"):
        sys.exit("Нужен GitHub CLI: brew install gh (или укажи логин: mnrh ssh github <логин>)")
    current = run(["gh", "api", "user", "--jq", ".login"]).strip()
    tty = sys.stdin.isatty() and not assume_yes
    if current and (not tty or ask_yes(f"gh вошёл в GitHub как {current}. Настраиваем его?")):
        return current
    if not tty:
        sys.exit("gh не вошёл в GitHub. Запусти в Терминале: mnrh ssh github")
    login = gh_login_now()
    if not login:
        sys.exit("Не удалось узнать, в какой аккаунт выполнен вход.")
    return login


def github():
    rest, explicit = args[1:], None
    for i, a in enumerate(rest):
        if not a.startswith("-") and (i == 0 or rest[i - 1] != "--alias"):
            explicit = a
            break
    login = choose_login(explicit)
    st_app = sourcetree_app()
    if not st_app:
        STEPS.remove("Sourcetree")
    blocks = mnrh_blocks()
    host = None
    if "--alias" in args:
        if args.index("--alias") + 1 >= len(args):
            sys.exit("mnrh ssh: после --alias нужно имя хоста")
        host = args[args.index("--alias") + 1]
    if not host:
        owner = blocks.get("github.com", (login,))[0]
        host = "github.com" if owner == login else f"github-{login.lower()}"
    key = os.path.join(SSH, f"github_{login}_ed25519")
    pub = key + ".pub"
    machine = socket.gethostname().split(".")[0]
    print(f"Настройка SSH для GitHub {login} (хост {host})")

    step(1)
    if os.path.exists(key):
        print(f"{OK} Ключ уже есть: {tilde(key)}")
        passphrase = ""
        if not encrypted(key) and not has_flag(args, "--ask-passphrase"):
            # Прошлый раз Связка не приняла пароль (например, запуск был не из терминала). Пробуем снова.
            passphrase = secrets.token_urlsafe(32)
            sh(["ssh-add", "-d", pub])
            sh(["ssh-keygen", "-p", "-q", "-P", "", "-N", passphrase, "-f", key])
    else:
        os.makedirs(SSH, mode=0o700, exist_ok=True)
        comment = f"{login}@github {machine} mnrh {time.strftime('%Y-%m-%d')}"
        if has_flag(args, "--ask-passphrase"):
            if not sys.stdin.isatty():
                sys.exit("--ask-passphrase работает только в терминале")
            subprocess.run(["ssh-keygen", "-t", "ed25519", "-C", comment, "-f", key], check=True)
            passphrase = None
        else:
            passphrase = secrets.token_urlsafe(32)
            code, out = sh(["ssh-keygen", "-q", "-t", "ed25519", "-C", comment, "-f", key, "-N", passphrase])
            if code != 0:
                sys.exit(f"ssh-keygen не справился: {out}")
        print(f"{OK} Выпущен ключ ed25519: {tilde(key)}")

    step(2)
    if fingerprint(pub) not in run(["ssh-add", "-l"]):
        if passphrase == "":
            # Ключ был раньше: пароль уже в Связке, просто подгружаем.
            sh(["ssh-add", "--apple-load-keychain", "-q"])
        else:
            add_to_keychain(key, passphrase)
    if fingerprint(pub) not in run(["ssh-add", "-l"]):
        print(f"{WARN} Ключ не загрузился в агент: ssh-add --apple-use-keychain {tilde(key)}")
    elif encrypted(key):
        print(f"{OK} Ключ в агенте, пароль в Связке ключей")
    else:
        print(f"{WARN} Ключ в агенте, но без пароля. Запусти mnrh ssh github {login} в Терминале — поставлю пароль в Связку")
    install_loader()

    step(3)
    if write_block(host, login, key):
        print(f"{OK} Host {host} -> {tilde(key)} (копия старого: config.mnrh-backup)")
    else:
        print(f"{OK} Host {host} уже смотрит на {tilde(key)}")

    step(4)
    uploaded = upload(login, pub, f"{machine} (mnrh)")

    step(5)
    if has_flag(args, "--no-sign"):
        print("Пропускаю: --no-sign")
    elif uploaded:
        setup_signing(login, pub)
    else:
        print("Пропускаю, пока ключа нет в GitHub: иначе коммиты были бы с пометкой Unverified.")

    step(6)
    who = github_identity(host) if uploaded else None
    if who == login:
        print(f"{OK} Проверка: git@{host} входит как {who}")
        if host == "github.com":
            subprocess.run(["gh", "config", "set", "git_protocol", "ssh", "-h", "github.com"], capture_output=True)
        switch_remotes(login, host)
    elif uploaded:
        print(f"{BAD} git@{host} входит как {who or 'никто'}, а не {login}. mnrh ssh покажет подробности.")
    else:
        print("Пропускаю, пока ключа нет в GitHub.")

    if st_app:
        step(7)
        sourcetree_step(login, st_app)
    if host != "github.com":
        print(f"Клонировать этим аккаунтом: git clone git@{host}:<владелец>/<репо>.git")


github() if args else status()
