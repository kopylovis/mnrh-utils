#!/usr/bin/env python3
"""Иконки меню: SVG из описаний ниже и PNG 64x64 через headless Chrome.

Иконки команд mnrh. Каждая иконка это цветная плитка со скругленными углами и белым значком линиями, сетка 32x32. Меню
раскладывает строки по emoji, а в iTerm2 и WezTerm подменяет emoji на эти картинки (menu.py, icons.json).
Запуск после правки: python3 tools/icons.py [папка], по умолчанию share/mnrh/icons.
Нужен только тому, кто перерисовывает иконки: готовые PNG лежат в проекте.
"""
import json
import os
import subprocess
import sys
import tempfile

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SIZE = 64

DEV, MEMORY, CHECK, MAC = "#3d7eea", "#23a26d", "#e0802c", "#7c5ce6"
GRAY, CLAUDE, KOTLIN, GRADLE = "#6b7685", "#d97757", "#7f52ff", "#1c8a8a"

# имя: (emoji, цвет плитки, значок). Значок: SVG внутри плитки, линии белые, толщина 2.2. emoji те же, что в
# ICONS в libexec/mnrh/lib/launcher.py.
ICONS = {
    "claudemeister": ("🤖", CLAUDE, '<path d="M16 8v16M8 16h16M10.3 10.3l11.4 11.4M21.7 10.3 10.3 21.7"/>'),
    "repos": ("📚", DEV, '<circle cx="11" cy="9.5" r="2"/><circle cx="11" cy="22.5" r="2"/><circle cx="21" cy="12.5" r="2"/>'
                         '<path d="M11 11.5v9M21 14.5c0 4-10 2.5-10 6"/>'),
    "deps": ("📦", DEV, '<path d="M16 8 23.5 12v8L16 24l-7.5-4v-8z"/><path d="M8.5 12 16 16l7.5-4M16 16v8"/>'),
    "ssh": ("🔑", DEV, '<circle cx="12" cy="16" r="4"/><path d="M16 16h8M21 16v3M24 16v2.5"/>'),
    "alert": ("📣", DEV, '<path d="M9 14v4h3l7 4V10l-7 4z"/><path d="M22.5 13.5a3.5 3.5 0 0 1 0 5"/>'),
    "init": ("🧰", DEV, '<path d="M9 11h14M9 16h14M9 21h14"/>'
                        '<circle cx="13" cy="11" r="2.2" fill="TILE"/><circle cx="19.5" cy="16" r="2.2" fill="TILE"/>'
                        '<circle cx="14.5" cy="21" r="2.2" fill="TILE"/>'),
    "ram": ("🧠", MEMORY, '<rect x="10" y="10" width="12" height="12" rx="1.5"/>'
                          '<path d="M13 7.5v2.5M16 7.5v2.5M19 7.5v2.5M13 22v2.5M16 22v2.5M19 22v2.5'
                          'M7.5 13H10M7.5 16H10M7.5 19H10M22 13h2.5M22 16h2.5M22 19h2.5"/>'),
    "disk": ("💽", MEMORY, '<rect x="8" y="10" width="16" height="12" rx="2"/><path d="M8 17.5h16"/>'
                           '<circle cx="20.5" cy="20" r=".9" fill="#fff" stroke="none"/>'),
    "gradle": ("🐘", GRADLE, '<text x="16" y="21" text-anchor="middle" font-family="Menlo, monospace" '
                             'font-weight="bold" font-size="14" fill="#fff" stroke="none">G</text>'),
    "kotlin": ("🟣", KOTLIN, '<path d="M9.5 9.5h13l-6.5 6.5 6.5 6.5h-13z" fill="#fff" stroke="none"/>'),
    "build-tools": ("🔨", MEMORY, '<path d="M14 13l-5 5a1.8 1.8 0 0 0 2.5 2.5l5-5"/><path d="M12.5 11.5 17 7l7.5 7.5-4.5 4.5z"/>'),
    "sim": ("📱", MEMORY, '<rect x="11" y="7.5" width="10" height="17" rx="2"/><path d="M15 21.5h2"/>'),
    "killdaemons": ("💀", MEMORY, '<circle cx="16" cy="16" r="8"/><path d="M13 13l6 6M19 13l-6 6"/>'),
    "doctor": ("🏥", CHECK, '<path d="M7 16.5h4l2.5-5.5 4 11 2.5-5.5h5"/>'),
    "fix": ("🔧", CHECK, '<path d="M19.5 8.5a4.5 4.5 0 0 0-4.6 6l-6.4 6.4a1.8 1.8 0 0 0 2.6 2.6l6.4-6.4a4.5 4.5 0 0 0 6-4.6l-2.8 2.8-3-.7-.7-3z"/>'),
    "path": ("🧭", CHECK, '<circle cx="16" cy="16" r="8"/><path d="M19.5 12.5 17.5 17.5 12.5 19.5 14.5 14.5z"/>'),
    "outdated": ("⏰", CHECK, '<circle cx="16" cy="16.5" r="7.5"/><path d="M16 12.5v4l2.8 2"/>'),
    "login": ("🚪", MAC, '<path d="M17 9h5v14h-5M9 16h9M14 12l4 4-4 4"/>'),
    "sleep": ("😴", MAC, '<path d="M21.5 18.5a7.5 7.5 0 1 1-8-9.5 6 6 0 0 0 8 9.5z"/>'),
    "scroll": ("🐭", MAC, '<rect x="11" y="8" width="10" height="16" rx="5"/><path d="M16 11v3"/>'),
    "nomusic": ("🔇", MAC, '<path d="M13 21V10l8-2v10"/><circle cx="11" cy="21" r="2"/><circle cx="19" cy="18" r="2"/>'
                           '<path d="M8 8l16 16"/>'),
    "tossling": ("📋", MAC, '<rect x="10" y="9" width="12" height="15" rx="2"/><path d="M13.5 9V7.5h5V9M13 14h6M13 18h4"/>'),
    "uninstall": ("🧹", MAC, '<path d="M9 11h14M13 11V9h6v2M11 11l1 12h8l1-12M14.5 14.5v5M17.5 14.5v5"/>'),
    "ports": ("🔌", GRAY, '<path d="M13 8v4M19 8v4M10.5 12h11v3a5.5 5.5 0 0 1-11 0zM16 20.5V24"/>'),
    "todo": ("📝", GRAY, '<path d="M9 10.5l1.5 1.5 2.5-2.5M15.5 11H23M9 17l1.5 1.5L13 16M15.5 17.5H23M15.5 23H23M10 23h1"/>'),
    "shot": ("📸", GRAY, '<path d="M8.5 12.5a2 2 0 0 1 2-2h2l1.5-2h4l1.5 2h2a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2z"/>'
                         '<circle cx="16" cy="16.5" r="3.2"/>'),
}


def svg(color, glyph):
    glyph = glyph.replace('"TILE"', f'"{color}"')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}" viewBox="0 0 32 32">'
            f'<rect x="1" y="1" width="30" height="30" rx="7.5" fill="{color}"/>'
            f'<g fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
            f'{glyph}</g></svg>\n')


def render(src, png):
    """SVG -> PNG того же размера, фон прозрачный."""
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={profile}",
                        "--default-background-color=00000000", f"--window-size={SIZE},{SIZE}",
                        f"--screenshot={png}", "file://" + src],
                       check=True, capture_output=True, timeout=60)


def main(out):
    os.makedirs(out, exist_ok=True)
    mapping = {}
    for name, (emoji, color, glyph) in ICONS.items():
        src = os.path.join(out, name + ".svg")
        with open(src, "w") as f:
            f.write(svg(color, glyph))
        render(os.path.abspath(src), os.path.join(out, name + ".png"))
        mapping[emoji] = name
        print(name)
    with open(os.path.join(out, "icons.json"), "w") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    return 0


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "share", "mnrh", "icons")))
