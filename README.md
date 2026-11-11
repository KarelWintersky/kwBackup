# kwbackup

Бэкап баз данных, файлового storage и файловых архивов в S3-совместимое
облако (или любой remote `rclone`). Один исполняемый файл, Python 3.10+,
**без внешних зависимостей**.

Замена bash-версии `kwbackup.sh` 0.9.0: те же сценарии и та же схема
ротации, но с валидируемым YAML-конфигом, честными кодами возврата,
блокировками через `flock`, проверкой копий и восстановлением.

## Возможности

* **Три сценария**: дамп БД → сжатие → заливка; `rclone sync/copy` каталогов;
  `tar` + компрессор → заливка.
* **GFS-ротация**: DAILY каждый запуск, WEEKLY дополнительно по понедельникам,
  MONTHLY дополнительно 1-го числа. Глубина — `keep: N` или `max_age: 7d`,
  на каждый класс своё правило.
* **Проверка, а не надежда**: `--list` показывает, что реально лежит в облаке
  и где дыры; `--verify` скачивает копию и проверяет, что она разжимается
  и похожа на дамп. Ненулевой код возврата годится для watchdog.
* **Восстановление**: `--restore --to DIR` для БД, архива и storage;
  заливка в живую БД — только явным `--into-db ... --yes`.
* **Безопасно к параллельным запускам**: `flock` на `(проект, задача)`,
  блокировка снимается ядром даже при `kill -9`.
* **Миграция**: `kwbackup migrate old.conf` конвертирует старый bash-конфиг
  в YAML, сохраняя глубину хранения.
* `--dry-run`, `--check-config`, `--selftest`.

## Требования

| | |
|---|---|
| ОС | Linux (нужны `flock`, `tar`) |
| Python | 3.10+ (только стандартная библиотека) |
| Обязательно | `rclone` |
| По типу БД | `mysqldump` / `mariadb-dump` / `pg_dump` / `sqlite3` |
| По компрессору | `zstd` / `gzip` / `pigz` / `rar` |
| Для архивов | `tar` (+ компрессор) |

## Установка

```bash
git clone https://github.com/KarelWintersky/kwBackup.git
cd kwBackup
python3 build.py --test                       # собрать dist/kwbackup и прогнать тесты
sudo install -m 755 dist/kwbackup /usr/local/bin/kwbackup
kwbackup --version
```

`dist/kwbackup` — один файл с шебангом. Если у вас уже есть готовая сборка,
достаточно `sudo install -m 755 dist/kwbackup /usr/local/bin/kwbackup`.

## Быстрый старт

```bash
# 1. remote для rclone (один раз)
rclone config

# 2. секреты отдельным файлом, не в YAML
sudo install -m 600 /dev/null /etc/kwbackup/secrets.env
sudo tee /etc/kwbackup/secrets.env >/dev/null <<'EOF'
RCLONE_KEY_ID=...
RCLONE_SECRET=...
MYSQL_PWD=...
EOF

# 3. конфиг проекта
sudo cp examples/database-only.yaml /etc/kwbackup/shop.yaml

# 4. проверка конфига и пробный прогон (от root, с загруженными секретами)
sudo -s
set -a; . /etc/kwbackup/secrets.env; set +a
kwbackup -c /etc/kwbackup/shop.yaml --check-config
kwbackup -c /etc/kwbackup/shop.yaml --dry-run

# 5. боевой запуск и проверка результата
kwbackup -c /etc/kwbackup/shop.yaml
kwbackup -c /etc/kwbackup/shop.yaml --list
```

Дальше — расписание: [`examples/systemd/`](examples/systemd) или
[`examples/cron/kwbackup`](examples/cron/kwbackup).

## Команды

| Команда | Что делает |
|---|---|
| `kwbackup -c FILE -d` | бэкап баз данных из конфига |
| `kwbackup -c FILE -s` | синхронизация storage |
| `kwbackup -c FILE -a` | упаковка и заливка архива |
| `kwbackup -c FILE` | все действия, включённые в конфиге |
| `kwbackup -c FILE --dry-run ...` | показать план, ничего не менять |
| `kwbackup -c FILE --check-config` | проверить конфиг и наличие бинарников |
| `kwbackup -c FILE --list` | что лежит в облаке, где пропуски |
| `kwbackup -c FILE --verify [--all]` | скачать и проверить копию |
| `kwbackup -c FILE --restore -d --to DIR [--at 2026-11-09]` | достать дамп |
| `kwbackup -c FILE --restore -a --to DIR` | распаковать архив |
| `kwbackup -c FILE --restore -s --to DIR` | скачать storage |
| `kwbackup -c FILE --restore -d --into-db db --yes` | залить в живую БД |
| `kwbackup migrate old.conf -o new.yaml` | конвертировать bash-конфиг |
| `kwbackup --selftest` | встроенные тесты |

Полная справка: [`docs/usage.md`](docs/usage.md).
Справочник по всем ключам конфига: [`docs/configuration.md`](docs/configuration.md).
Миграция со старого скрипта: [`docs/migration.md`](docs/migration.md).

## Конфиг

Минимальный пример (полный — [`examples/full.yaml`](examples/full.yaml)):

```yaml
version: 1
project: shop

rclone:
  config: /etc/kwbackup/rclone.conf
  remote: beget
  env:
    AWS_ACCESS_KEY_ID: ${RCLONE_KEY_ID}
    AWS_SECRET_ACCESS_KEY: ${RCLONE_SECRET}

database:
  enabled: true
  type: mysql
  host: localhost
  user: backup
  password_env: MYSQL_PWD
  compressor: zstd
  target: "beget:/shop/DB"
  databases: [shop]
  retention:
    daily:   {keep: 8}
    weekly:  {keep: 6}
    monthly: {keep: 12}
```

Готовые варианты: [`examples/database-only.yaml`](examples/database-only.yaml),
[`examples/storage-only.yaml`](examples/storage-only.yaml),
[`examples/archive-only.yaml`](examples/archive-only.yaml).

Секреты в конфиг не пишутся: только `${ENV_VAR}` или `rclone.env`,
значения — в `/etc/kwbackup/secrets.env` (0600).

## Как это работает

```
dump  →  zstd/gzip/rar  →  файл в temp_dir  →  rclone copyto  →  облако
```

Один дамп на запуск; при необходимости тот же файл уходит в 1-3 каталога:

```
<target>/DAILY/<db>_2026-11-12-03-30-00.sql.zst      # каждый запуск
<target>/WEEKLY/...                                   # + по понедельникам
<target>/MONTHLY/...                                  # + 1-го числа
```

Если баз несколько, добавляется уровень с именем БД:
`<target>/<db>/DAILY/...`. Архивы: `<target>/<name>_<timestamp>.tar.zst`.
Storage: `<target>/<dest>/...`, где `dest` — `basename` источника или явное
значение из конфига.

## Коды возврата

| Код | Значение |
|---|---|
| 0 | успех |
| 1 | ошибка выполнения (дамп, сжатие, rclone, preflight) |
| 2 | ошибка конфига |
| 3 | ошибка аргументов |
| 4 | задача уже выполняется (lock) |
| 5 | `--verify`/`--list` нашли проблему: копий нет, они стары или повреждены |

`--list` и `--verify` можно ставить в cron/systemd: код 5 означает «пора
смотреть руками».

## Документация

* [docs/usage.md](docs/usage.md) — сценарии, расписание, мониторинг,
  восстановление, разбор ошибок.
* [docs/configuration.md](docs/configuration.md) — все ключи, типы, значения
  по умолчанию.
* [docs/migration.md](docs/migration.md) — из `kwbackup.sh` в YAML.
* [examples/](examples) — конфиги под каждый сценарий, systemd-юниты, cron.
* [CHANGELOG.md](CHANGELOG.md) — что изменилось между версиями.

## Разработка

```
src/00_header.py     импорты, константы          src/50_runner.py    пайплайны
src/10_yaml.py       YAML-подмножество            src/60_rclone.py    rclone
src/20_config.py     схема и валидация            src/70_retention.py keep/max_age
src/30_logging.py    логи                        src/80_database.py  дампы
src/40_lock.py       flock                       src/81_storage.py   sync/copy
src/82_archive.py    tar + компрессор            src/83_restore.py   восстановление
src/85_report.py     --list и --verify            src/90_preflight.py проверки
src/92_migrate.py    конвертер конфигов           src/95_cli.py       CLI
src/99_selftest.py   --selftest                   src/zz_main.py      вход
```

```bash
python3 build.py --test     # сборка + py_compile + selftest
python3 tests/e2e.py        # сквозные тесты на фейковом rclone
```

Сборка — простая конкатенация `src/*.py` в порядке числовых префиксов.
Единственное правило: **все `import` живут в `src/00_header.py`**, сборщик
падает на импорте в другом файле. Благодаря этому нет бандлера, нет рерайта
импортов, и `dist/kwbackup` ровно соответствует `src/`.

## Legacy

`kwbackup.sh`, `_config.conf` и `kwbackup-config.md` — старая bash-версия,
оставлена для справки и для миграции. Новые установки используют
`dist/kwbackup` и YAML. Обе версии нельзя запускать одновременно на один
и тот же проект: у них разные механизмы блокировок.
