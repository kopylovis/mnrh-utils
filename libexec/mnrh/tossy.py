import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import tossy_path

path = tossy_path()
if path:
    os.execv(path, [path, *sys.argv[1:]])
print("Tossy — общий буфер обмена своих Mac и Android-телефона — теперь отдельная программа tossy.")
print("mnrh tossy … передаёт команды ей, но она не установлена.")
print("Поставить: git clone git@github.com:kopylovis/tossy.git ~/Developer/tossy && make -C ~/Developer/tossy install")
sys.exit(0 if any(a in ("-h", "--help") for a in sys.argv[1:]) else 1)
