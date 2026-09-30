import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mnrhlib import has_flag
import gradle_deps

args = sys.argv[1:]
if has_flag(args, "-h", "--help"):
    print("mnrh deps               устаревшие зависимости в gradle/libs.versions.toml: этот проект или все")
    print("mnrh deps <проект>      в одном проекте (имя папки или путь)")
    print("mnrh deps --pre         показывать и alpha, beta, rc")
    print()
    print("Версии сверяются с Google Maven, Maven Central и Gradle Plugin Portal (кеш 6 ч в ~/.cache/mnrh/maven).")
    print("Ничего не меняет. То же самое Claude видит через MCP-инструмент deps_outdated.")
    sys.exit(0)
names = [a for a in args if not a.startswith("-")]
text, err = gradle_deps.report_text(names[0] if names else None, os.getcwd(), has_flag(args, "--pre"))
print(text)
sys.exit(1 if err else 0)
