import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import PROMPTSILL_HINT, promptsill_path

path = promptsill_path()
if path:
    os.execv(path, [path, *sys.argv[1:]])
if not sys.stdin.isatty():
    print(f"строка состояния теперь отдельная программа promptsill: {PROMPTSILL_HINT}")
    sys.exit(0)
print("Строка состояния Claude Code теперь отдельная программа promptsill, mnrh claude statusline передаёт ей команды.")
print(f"Поставить: {PROMPTSILL_HINT}, потом mnrh claude setup. Подробнее: https://github.com/kopylovis/promptsill")
sys.exit(0 if any(a in ("-h", "--help") for a in sys.argv[1:]) else 1)
