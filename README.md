# mnrh-utils

Набор утилит для обслуживания Mac. Одна команда `mnrh`, подкоманды — отдельные файлы.

```
mnrh ram              кто ест оперативную память, сгруппировано по приложениям
mnrh ram -p           то же по отдельным процессам
mnrh ram -n 30        больше строк
mnrh killdaemons      остановить демоны Gradle и Kotlin любых версий
mnrh killdaemons -l   только показать
mnrh killdaemons -f   снять и тех, кто занят сборкой
```

Короткие псевдонимы: `mnrh mem`, `mnrh kd`.

## Установка

```bash
git clone https://github.com/kopylovis/mnrh-utils ~/Developer/mnrh-utils
cd ~/Developer/mnrh-utils
make install
```

`make install` кладёт в `~/.local/bin` символическую ссылку на проект, поэтому правки
работают без переустановки. Другой каталог: `make install PREFIX=/usr/local`.

Удаление: `make uninstall`. Проверки: `make test`.

## Требования

macOS, bash и `/usr/bin/python3` из Xcode Command Line Tools
(`xcode-select --install`). От Homebrew утилита не зависит.

## Почему `mnrh ram`, а не Stats или Activity Monitor

Stats показывает сжатую память одной полосой Compressed, без разбивки по процессам.
`ps` и RSS сжатую память не видят вовсе: простаивающий Gradle-демон с 5 ГБ
в RSS выглядит как 120 МБ. `mnrh ram` берёт у `top` полный объём процесса (MEM)
и его сжатую часть (CMPRS) и группирует вспомогательные процессы по приложению —
Chrome, Claude, iOS-симулятор идут одной строкой.

Сжатая часть учитывается в несжатом размере, поэтому сумма колонки больше,
чем «Занято»; утилита выводит текущий коэффициент сжатия.

## Новая команда

Положить файл в `libexec/mnrh/`: `имя.sh` запускается через bash, `имя.py` через
`/usr/bin/python3`, остальное — как исполняемый файл. Команда сразу доступна как
`mnrh имя`. Описание для `mnrh help` добавляется в функцию `describe` в `bin/mnrh`.
