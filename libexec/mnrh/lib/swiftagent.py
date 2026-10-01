"""Помощник на Swift, который живёт в фоне как LaunchAgent: сборка в .app, автозапуск, состояние.

Общий код для mnrh scroll и приложения уведомлений mnrh Notify. Исходник — share/mnrh/<name>/main.swift. Собирается
в ~/Library/Application Support/mnrh/<display>.app только при изменении исходника: macOS
выдаёт разрешения (Универсальный доступ и т. п.) конкретной сборке, и лишняя пересборка их сбросит.
"""
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time

from mnrhlib import HOME

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class SwiftAgent:
    def __init__(self, name, display, label):
        self.name, self.display, self.label = name, display, label
        self.src = os.path.join(ROOT, "share", "mnrh", name, "main.swift")
        self.app = os.path.join(HOME, "Library", "Application Support", "mnrh", f"{display}.app")
        self.bin = os.path.join(self.app, "Contents", "MacOS", f"mnrh-{name}")
        self.stamp = os.path.join(self.app, "Contents", "Resources", "source.sha256")
        self.plist = os.path.join(HOME, "Library", "LaunchAgents", f"{label}.plist")
        self.state_file = os.path.join(HOME, ".cache", "mnrh", f"{name}.json")
        self.log = os.path.join(HOME, "Library", "Logs", f"mnrh-{name}.log")
        self.domain = f"gui/{os.getuid()}"

    def launchctl(self, *a):
        return subprocess.run(["launchctl", *a], capture_output=True, text=True)

    def loaded(self):
        return self.launchctl("print", f"{self.domain}/{self.label}").returncode == 0

    def installed(self):
        return os.path.exists(self.plist)

    def state(self):
        try:
            with open(self.state_file) as f:
                s = json.load(f)
            os.kill(s["pid"], 0)
            return s
        except (OSError, ValueError, KeyError):
            return None

    def source_hash(self):
        with open(self.src, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def build(self):
        """Собирает, если исходник поменялся. True — собран заново."""
        want = self.source_hash()
        try:
            if open(self.stamp).read().strip() == want and os.access(self.bin, os.X_OK):
                return False
        except OSError:
            pass
        if not shutil.which("swiftc"):
            sys.exit("Нужен компилятор Swift из Xcode Command Line Tools: xcode-select --install")
        print(f"Собираю {self.display}...")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "bin")
            r = subprocess.run(["swiftc", "-O", "-swift-version", "5", self.src, "-o", out],
                               capture_output=True, text=True)
            if r.returncode != 0:
                sys.exit("Сборка не удалась:\n" + r.stderr[-2000:])
            shutil.rmtree(self.app, ignore_errors=True)
            os.makedirs(os.path.dirname(self.bin))
            os.makedirs(os.path.dirname(self.stamp))
            shutil.copy2(out, self.bin)
        with open(os.path.join(self.app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": self.label, "CFBundleName": self.display,
                           "CFBundleDisplayName": self.display, "CFBundleExecutable": os.path.basename(self.bin),
                           "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "LSUIElement": True}, f)
        with open(self.stamp, "w") as f:
            f.write(want + "\n")
        r = subprocess.run(["codesign", "--force", "--sign", "-", "--identifier", self.label, self.app],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit("Не удалось подписать помощника:\n" + r.stderr)
        return True

    def write_plist(self, extra_args):
        os.makedirs(os.path.dirname(self.plist), exist_ok=True)
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.plist, "wb") as f:
            plistlib.dump({
                "Label": self.label,
                "ProgramArguments": [self.bin, *extra_args, "--state", self.state_file],
                "RunAtLoad": True,
                "KeepAlive": True,
                "ProcessType": "Interactive",
                "StandardErrorPath": self.log,
                "StandardOutPath": self.log,
            }, f)

    def stop(self):
        if self.loaded():
            self.launchctl("bootout", f"{self.domain}/{self.label}")
            for _ in range(20):
                if not self.loaded():
                    break
                time.sleep(0.25)

    def start(self):
        """Перезапускает с текущим plist. True, если помощник поднялся и записал состояние."""
        self.stop()
        try:
            if os.path.getsize(self.log) > 1048576:
                os.truncate(self.log, 0)
        except OSError:
            pass
        r = self.launchctl("bootstrap", self.domain, self.plist)
        if r.returncode != 0:
            sys.exit(f"launchctl не запустил помощника: {r.stderr.strip()}")
        for _ in range(20):
            if self.state():
                return True
            time.sleep(0.25)
        return False

    def restart(self):
        self.launchctl("kickstart", "-k", f"{self.domain}/{self.label}")
        time.sleep(1)
        return self.state()

    def uninstall(self):
        self.stop()
        for p in (self.plist, self.state_file):
            try:
                os.unlink(p)
            except OSError:
                pass
