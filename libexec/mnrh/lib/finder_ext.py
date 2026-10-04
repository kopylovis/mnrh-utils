import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import time

from mnrhlib import HOME

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SRC = os.path.join(ROOT, "share", "mnrh", "tossy-finder")
APP = os.path.join(HOME, "Library", "Application Support", "mnrh", "mnrh Tossy Finder.app")
APPEX = os.path.join(APP, "Contents", "PlugIns", "TossyFinder.appex")
STAMP = os.path.join(APP, "Contents", "Resources", "source.sha256")
HOST_ID = "com.mnrh.tossy.finder"
EXT_ID = f"{HOST_ID}.send"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/"
              "Support/lsregister")


def identity():
    out = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"], capture_output=True, text=True).stdout
    m = re.search(r'"(Apple Development: [^"]+)"', out)
    return m.group(1) if m else "-"


def source_hash(sign):
    h = hashlib.sha256(sign.encode())
    for name in sorted(os.listdir(SRC)):
        with open(os.path.join(SRC, name), "rb") as f:
            h.update(name.encode() + f.read())
    return h.hexdigest()


def swiftc(*args):
    r = subprocess.run(["swiftc", "-O", "-swift-version", "5", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-2000:])


def info(identifier, executable, package, extra):
    version = "1." + time.strftime("%Y%m%d")
    return plistlib.dumps({"CFBundleIdentifier": identifier, "CFBundleName": "mnrh Tossy Finder",
                           "CFBundleDisplayName": "mnrh Tossy Finder", "CFBundleExecutable": executable,
                           "CFBundlePackageType": package, "CFBundleVersion": str(int(time.time())),
                           "CFBundleShortVersionString": version, "LSMinimumSystemVersion": "13.0", **extra})


def build(icon=None):
    sign = identity()
    want = source_hash(sign)
    try:
        if open(STAMP).read().strip() == want and os.path.isdir(APPEX):
            return False
    except OSError:
        pass
    with tempfile.TemporaryDirectory() as tmp:
        app = os.path.join(tmp, os.path.basename(APP))
        contents = os.path.join(app, "Contents")
        appex = os.path.join(contents, "PlugIns", "TossyFinder.appex")
        for d in (os.path.join(contents, "MacOS"), os.path.join(contents, "Resources"), os.path.join(appex, "Contents", "MacOS")):
            os.makedirs(d)
        swiftc(os.path.join(SRC, "main.swift"), "-o", os.path.join(contents, "MacOS", "mnrh-tossy-finder"))
        swiftc("-parse-as-library", "-application-extension", "-module-name", "TossyFinder",
               "-Xlinker", "-e", "-Xlinker", "_NSExtensionMain", "-framework", "FinderSync",
               os.path.join(SRC, "FinderSync.swift"), "-o", os.path.join(appex, "Contents", "MacOS", "TossyFinder"))
        with open(os.path.join(appex, "Contents", "Info.plist"), "wb") as f:
            f.write(info(EXT_ID, "TossyFinder", "XPC!", {"NSExtension": {
                "NSExtensionAttributes": {}, "NSExtensionPointIdentifier": "com.apple.FinderSync",
                "NSExtensionPrincipalClass": "TossyFinder.FinderSync"}}))
        extra = {"LSUIElement": True}
        if icon and os.path.exists(icon):
            shutil.copy(icon, os.path.join(contents, "Resources", "AppIcon.icns"))
            extra["CFBundleIconFile"] = "AppIcon"
        with open(os.path.join(contents, "Info.plist"), "wb") as f:
            f.write(info(HOST_ID, "mnrh-tossy-finder", "APPL", extra))
        with open(os.path.join(contents, "Resources", "source.sha256"), "w") as f:
            f.write(want + "\n")
        entitlements = os.path.join(tmp, "sandbox.plist")
        with open(entitlements, "wb") as f:
            f.write(plistlib.dumps({"com.apple.security.app-sandbox": True}))
        for target, ent in ((appex, entitlements), (app, None)):
            cmd = ["codesign", "--force", "--sign", sign] + (["--entitlements", ent] if ent else []) + [target]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip())
        subprocess.run(["pluginkit", "-r", APPEX], capture_output=True)
        shutil.rmtree(APP, ignore_errors=True)
        os.makedirs(os.path.dirname(APP), exist_ok=True)
        r = subprocess.run(["ditto", app, APP], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
    return True


def install(icon=None):
    try:
        build(icon)
    except (RuntimeError, OSError) as e:
        return str(e)
    if os.path.exists(LSREGISTER):
        subprocess.run([LSREGISTER, "-f", APP], capture_output=True)
    subprocess.run(["pluginkit", "-a", APPEX], capture_output=True)
    subprocess.run(["pluginkit", "-e", "use", "-i", EXT_ID], capture_output=True)
    return None


def enabled():
    out = subprocess.run(["pluginkit", "-m", "-i", EXT_ID], capture_output=True, text=True).stdout
    return out.strip().startswith("+")


def remove():
    subprocess.run(["pluginkit", "-e", "ignore", "-i", EXT_ID], capture_output=True)
    subprocess.run(["pluginkit", "-r", APPEX], capture_output=True)
    shutil.rmtree(APP, ignore_errors=True)
