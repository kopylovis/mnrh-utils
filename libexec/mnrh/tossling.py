import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import tossling_path

path = tossling_path()
if path:
    os.execv(path, [path, *sys.argv[1:]])
print("Tossling (бывший Tossy) — общий буфер обмена своих Mac и Android-телефона — отдельная программа tossling.")
print("mnrh tossling … передаёт команды ей, но она не установлена.")
print("Поставить: brew install --cask tossling/tap/tossling или Tossling.dmg с https://github.com/tossling/tossling-desktop/releases")
sys.exit(0 if any(a in ("-h", "--help") for a in sys.argv[1:]) else 1)
