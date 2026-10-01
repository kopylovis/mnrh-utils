import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mnrhlib import HOME, tilde

SHOTS = os.path.join(HOME, ".cache", "mnrh", "shots")
MAX_SIDE = 1600
KEEP_DAYS = 3
SECRET = re.compile(r"(github_pat_|ghp_|gho_|glpat-|sk-|xox[bap]-|AKIA|AIza)[A-Za-z0-9_\-]{4,}|[A-Za-z0-9+/_\-]{32,}")

LIST_JS = r'''
ObjC.import('CoreGraphics');
function run() {
  var ref = $.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements, $.kCGNullWindowID);
  var list = ObjC.deepUnwrap(ObjC.castRefToObject(ref)) || [];
  var out = [];
  for (var i = 0; i < list.length; i++) {
    var w = list[i];
    if (w.kCGWindowLayer !== 0 || !w.kCGWindowBounds || w.kCGWindowBounds.Width < 80) continue;
    out.push({id: w.kCGWindowNumber, app: w.kCGWindowOwnerName || "", title: w.kCGWindowName || "",
              w: w.kCGWindowBounds.Width, h: w.kCGWindowBounds.Height});
  }
  return JSON.stringify(out);
}
'''


def redact(text):
    return SECRET.sub(lambda m: m.group(0)[:6] + "•••", text or "")


def windows():
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", LIST_JS], capture_output=True, text=True, timeout=15)
    try:
        found = json.loads(r.stdout or "[]")
    except ValueError:
        return []
    for w in found:
        w["title"] = redact(w["title"])
    return found


def describe(ws):
    lines = [f"  {w['app']}: {w['title'] or '(без заголовка)'}  [{int(w['w'])}×{int(w['h'])}]" for w in ws]
    return "\n".join(lines) or "  открытых окон не видно"


def choose(app=None, title=None):
    ws = windows()
    if not ws:
        return None, ("Не вижу ни одного окна. Нужно разрешение «Запись экрана» для терминала, в котором идёт "
                      "Claude: Системные настройки → Конфиденциальность и безопасность → Запись экрана и аудио.")
    hits = ws
    if app:
        q = app.lower()
        hits = [w for w in hits if q in w["app"].lower()] or [w for w in hits if q in w["title"].lower()]
    if title:
        hits = [w for w in hits if title.lower() in w["title"].lower()]
    if not hits:
        wanted = " ".join(x for x in (app, title) if x)
        return None, f"Нет окна «{wanted}». Видны:\n{describe(ws)}"
    return hits[0], None


def prune():
    cutoff = time.time() - KEEP_DAYS * 86400
    for f in os.listdir(SHOTS) if os.path.isdir(SHOTS) else []:
        p = os.path.join(SHOTS, f)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def capture(w):
    os.makedirs(SHOTS, mode=0o700, exist_ok=True)
    prune()
    name = re.sub(r"[^\w.-]+", "-", w["app"]).strip("-") or "window"
    path = os.path.join(SHOTS, f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.png")
    r = subprocess.run(["screencapture", "-x", "-o", "-l", str(w["id"]), path], capture_output=True, text=True,
                       timeout=20)
    if r.returncode or not os.path.exists(path) or os.path.getsize(path) == 0:
        return None, (f"Снимок не получился ({(r.stderr or '').strip() or 'пустой файл'}). Нужно разрешение «Запись экрана» "
                      "для терминала, в котором идёт Claude.")
    subprocess.run(["sips", "-Z", str(MAX_SIDE), path], capture_output=True, timeout=20)
    return path, None


def shot(app=None, title=None):
    w, err = choose(app, title)
    if err:
        return None, None, err
    path, err = capture(w)
    return path, w, err


TOOL = {
    "name": "window_screenshot",
    "description": ("Снимок окна приложения на этом Mac: Chrome, Safari, Android Studio, Xcode, Figma, симулятор, "
                    "VS Code и т. п. Чтобы посмотреть, как сейчас выглядит страница, превью или экран. Без app — список "
                    "открытых окон (заголовки, токены в них скрыты). app — имя приложения или кусок заголовка, "
                    "title — уточнение по заголовку окна. Снимок приходит картинкой. Снимай только то, о чём просит "
                    "пользователь: на экране могут быть чужие переписки и пароли."),
    "inputSchema": {"type": "object", "properties": {
        "app": {"type": "string", "description": "приложение: Chrome, Safari, Simulator, Android Studio…"},
        "title": {"type": "string", "description": "кусок заголовка окна"}}},
}


def handle(a):
    if not a.get("app") and not a.get("title"):
        ws = windows()
        return {"content": [{"type": "text", "text": "Открытые окна (сверху — переднее):\n" + describe(ws)}],
                "isError": False}
    path, w, err = shot(a.get("app"), a.get("title"))
    if err:
        return {"content": [{"type": "text", "text": err}], "isError": True}
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return {"content": [{"type": "text", "text": f"{w['app']}: {w['title'] or '(без заголовка)'} — {tilde(path)}"},
                        {"type": "image", "data": data, "mimeType": "image/png"}], "isError": False}


def main():
    args = sys.argv[1:]
    if args[:1] in (["-h"], ["--help"]):
        print("mnrh shot                   список открытых окон")
        print("mnrh shot <приложение> [заголовок]   снимок переднего окна приложения в ~/.cache/mnrh/shots")
        print()
        print(f"Снимки уменьшаются до {MAX_SIDE} px по большей стороне и удаляются через {KEEP_DAYS} дня.")
        print("Нужно разрешение «Запись экрана» для терминала. В Claude Code: /shot или инструмент window_screenshot.")
        return 0
    if not args:
        print(describe(windows()))
        return 0
    path, w, err = shot(args[0], " ".join(args[1:]) or None)
    if err:
        print(err)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
