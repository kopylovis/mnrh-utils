import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import OK, confirm, has_flag, paint, run

args = sys.argv[1:]
if has_flag(args, "-h", "--help") or (args and args[0] != "install"):
    print("mnrh kit                        подборка удобных программ для Mac: что уже стоит, что нет")
    print("mnrh kit install maccy alt-tab  поставить выбранные через brew")
    print("mnrh kit install                спросить, что ставить (в терминале)")
    sys.exit(0 if has_flag(args, "-h", "--help") else 2)

# имя, пакет brew, cask ли, приложение в /Applications, раздел, зачем
KIT = [
    ("rectangle", "rectangle", True, "Rectangle", "Окна", "окна по половинам и углам экрана горячими клавишами"),
    ("aerospace", "nikitabobko/tap/aerospace", True, "AeroSpace", "Окна",
     "плиточный менеджер: свой рабочий стол для Studio, Xcode, симулятора"),
    ("alt-tab", "alt-tab", True, "AltTab", "Окна", "⌘Tab по окнам, а не по приложениям, с превью"),
    ("karabiner", "karabiner-elements", True, "Karabiner-Elements", "Клавиатура",
     "переназначение клавиш: Caps Lock → Esc, одна клавиша для RU/EN"),
    ("maccy", "maccy", True, "Maccy", "Клавиатура", "история буфера обмена: стектрейсы, id, команды"),
    ("ice", "jordanbaird-ice", True, "Ice", "Строка меню", "прячет лишние иконки, важно на MacBook с вырезом"),
    ("hiddenbar", "hiddenbar", True, "Hidden Bar", "Строка меню", "то же попроще, если Ice не заработает"),
    ("monitorcontrol", "monitorcontrol", True, "MonitorControl", "Экран", "яркость и звук внешнего монитора с клавиатуры"),
    ("betterdisplay", "betterdisplay", True, "BetterDisplay", "Экран", "HiDPI и любые разрешения на внешнем мониторе"),
    ("shottr", "shottr", True, "Shottr", "Скриншоты", "скриншоты с пометками, линейкой и распознаванием текста"),
    ("middleclick", "middleclick", True, "MiddleClick", "Мышь", "средний клик тремя пальцами на трекпаде"),
    ("latest", "latest", True, "Latest", "Разное", "какие приложения пора обновить, включая не из App Store"),
    ("keka", "keka", True, "Keka", "Разное", "архиватор: 7z, rar и всё остальное"),
    ("iina", "iina", True, "IINA", "Разное", "видеоплеер, который открывает всё"),
    ("itsycal", "itsycal", True, "Itsycal", "Разное", "календарь в строке меню"),
    ("stats", "stats", True, "Stats", "Разное", "процессор, память, сеть и батарея в строке меню"),
    ("scrcpy", "scrcpy", False, None, "Разработка", "экран Android-телефона на Mac с управлением мышью"),
    ("xcodes", "xcodes", False, None, "Разработка", "ставить и переключать версии Xcode из терминала"),
    ("mas", "mas", False, None, "Разработка", "App Store из терминала: mas outdated, mas upgrade"),
]


def installed():
    env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1")
    casks = set(subprocess.run(["brew", "list", "--cask", "-1"], capture_output=True, text=True, env=env).stdout.split())
    formulae = set(subprocess.run(["brew", "list", "--formula", "-1"], capture_output=True, text=True, env=env).stdout.split())
    have = set()
    for name, pkg, cask, app, *_ in KIT:
        token = pkg.rsplit("/", 1)[-1]
        if (cask and token in casks) or (not cask and token in formulae) or \
                (app and (os.path.isdir(f"/Applications/{app}.app") or
                          os.path.isdir(os.path.expanduser(f"~/Applications/{app}.app")))):
            have.add(name)
    return have


def status(have):
    section = None
    for i, (name, _, _, _, sec, why) in enumerate(KIT, 1):
        if sec != section:
            section = sec
            print(f"\n{paint(sec, '1')}")
        mark = OK if name in have else paint("·", "2")
        print(f"  {mark} {i:>2} {name:<15} {why}")
    print(paint("\n✓ — уже стоит. Поставить: mnrh kit install <имя или номер…>", "2"))


def install(have):
    wanted = [a for a in args[1:] if not a.startswith("-")]
    if not wanted:
        if not sys.stdin.isatty():
            sys.exit("Укажи, что ставить: mnrh kit install maccy alt-tab")
        status(have)
        wanted = input("\nНомера или имена через пробел: ").split()
    by_num = {str(i): k[0] for i, k in enumerate(KIT, 1)}
    names = [by_num.get(w, w) for w in wanted]
    unknown = [n for n in names if n not in {k[0] for k in KIT}]
    if unknown:
        sys.exit(f"Нет в подборке: {', '.join(unknown)}")
    todo = [k for k in KIT if k[0] in names and k[0] not in have]
    for k in KIT:
        if k[0] in names and k[0] in have:
            print(f"{OK} {k[0]} уже стоит")
    if not todo:
        return
    if not confirm(f"Поставить через brew: {', '.join(k[0] for k in todo)}?", has_flag(args, "-y", "--yes")):
        print("Отменено.")
        return
    for name, pkg, cask, *_ in todo:
        cmd = ["brew", "install"] + (["--cask"] if cask else []) + [pkg]
        print(paint("$ " + " ".join(cmd), "2"))
        r = subprocess.run(cmd)
        print(f"{OK} {name}" if r.returncode == 0 else f"✗ {name}: brew завершился с ошибкой")
    if any(k[0] == "karabiner" for k in todo):
        print(paint("Karabiner попросит разрешить драйвер: Настройки → Конфиденциальность и безопасность.", "2"))


if not shutil.which("brew"):
    sys.exit("Нужен Homebrew: https://brew.sh")
have = installed()
install(have) if args else status(have)
