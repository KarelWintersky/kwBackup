# kwBackup

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
* **Слои конфигурации**: общий `/etc/kwbackup/defaults.yaml` (логи, rclone, глубина
  хранения, компрессоры) + короткий файл задачи на каждый проект.
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

Готовый пакет:

```bash
sudo dpkg -i kwbackup_<version>_all.deb
kwbackup --version
```

Пакет ставит `/usr/bin/kwbackup` (один файл с шебангом), conffile
`/etc/kwbackup/defaults.yaml` и документацию в `/usr/share/doc/kwbackup/`.

Из исходников (нужен `python3` 3.10+; для пакета — debhelper на Debian):

```bash
git clone https://github.com/KarelWintersky/kwBackup.git
cd kwBackup
make build                    # dist/kwbackup + ../kwbackup_<version>_all.deb
make test                     # встроенные и сквозные тесты
```

Без пакета достаточно артефакта:

```bash
make dist
sudo install -m 755 dist/kwbackup /usr/bin/kwbackup
```

## Быстрый старт

```bash
# 1. remote для rclone (один раз)
rclone config

# 2. общие настройки хоста: логи, rclone.config/remote, глубина хранения
sudo install -d -m 700 /etc/kwbackup /var/log/kwbackup
sudo install -m 600 examples/defaults.yaml /etc/kwbackup//etc/kwbackup/defaults.yaml

# 3. задача: project, target, базы и ключи бакета
#    (BEGET выдаёт access/secret на бакет — они уходят флагами rclone)
sudo install -m 600 examples/database-only.yaml /etc/kwbackup/shop.yaml
sudoedit /etc/kwbackup/shop.yaml

# 4. пароль БД и прочие секреты — в окружение
sudo install -m 600 examples/secrets.env.example /etc/kwbackup/secrets.env
sudoedit /etc/kwbackup/secrets.env

# 5. проверка конфига и пробный прогон (от root, с загруженными секретами)
sudo -s
set -a; . /etc/kwbackup/secrets.env; set +a
kwbackup --config /etc/kwbackup/shop.yaml --check-config
kwbackup --config /etc/kwbackup/shop.yaml --dry-run

# 6. боевой запуск и проверка результата
kwbackup --config /etc/kwbackup/shop.yaml --database
kwbackup --config /etc/kwbackup/shop.yaml --list
```

Дальше — расписание: [`examples/systemd/`](examples/systemd) или
[`examples/cron/kwbackup`](examples/cron/kwbackup).

## Команды

| Команда | Что делает |
|---|---|
| `kwbackup --config FILE --database` | бэкап баз данных из конфига |
| `kwbackup --config FILE --storage` | синхронизация storage |
| `kwbackup --config FILE --archive` | упаковка и заливка архива |
| `kwbackup --config FILE -d ... -m sync` | зеркало вместо copy (удаляет старое) |
| `kwbackup --config FILE` | показать доступные действия (ничего не запускает) |
| `kwbackup --config FILE --dry-run ...` | показать план, ничего не менять |
| `kwbackup --config FILE --check-config` | проверить конфиг и наличие бинарников |
| `kwbackup --config FILE --list` | что лежит в облаке, где пропуски |
| `kwbackup --config FILE --verify [--all]` | скачать и проверить копию |
| `kwbackup --config FILE --restore --database --to DIR [--at 2026-11-09]` | достать дамп |
| `kwbackup --config FILE --restore --archive --to DIR` | распаковать архив |
| `kwbackup --config FILE --restore --storage --to DIR` | скачать storage |
| `kwbackup --config FILE --restore --database --into-db db --yes` | залить в живую БД |
| `kwbackup migrate old.conf -o new.yaml` | конвертировать bash-конфиг |
| `kwbackup --selftest` | встроенные тесты |

Полная справка: [`docs/usage.md`](docs/usage.md).
Справочник по всем ключам конфига: [`docs/configuration.md`](docs/configuration.md).
Миграция со старого скрипта: [`docs/migration.md`](docs/migration.md).

## Конфиг

Общие настройки — один раз на хост, [`examples/defaults.yaml`](examples/defaults.yaml):
логи, `rclone.config`, `rclone.remote`, тип БД, компрессор, глубина хранения.

Файл задачи после этого короткий (полный самодостаточный пример —
[`examples/full.yaml`](examples/full.yaml)):

```yaml
project: shop

rclone:
  args:
    # BEGET: ключи на бакет, env_auth = false -> только флагами rclone.
    # Файл с ключами — 0600; можно подставлять "${BEGET_SHOP_KEY}".
    - "--s3-access-key-id=<ACCESS_KEY_BUCKET_SHOP>"
    - "--s3-secret-access-key=<SECRET_KEY_BUCKET_SHOP>"

database:
  enabled: true
  target: "beget:/shop/DB"
  databases: [shop]
  # log, rclone.config/remote, compressor, retention — из defaults
```

Файл `/etc/kwbackup/defaults.yaml` — единственное место, которое читает
программа; ниже него лежат встроенные дефолты (`src/05_builtin_defaults.py`).
Списки `rclone.args`,
`database.args`, `*_compressor_args` и `archive.exclude` дополняются,
остальные значения задача перекрывает.

Готовые варианты: [`examples/database-only.yaml`](examples/database-only.yaml),
[`examples/storage-only.yaml`](examples/storage-only.yaml),
[`examples/archive-only.yaml`](examples/archive-only.yaml).

Секреты:

* **Ключи S3-бакета** (BEGET и подобные провайдеры) в `rclone.conf` не
  работают: там `env_auth = false`, поэтому ключи передаются флагами
  `--s3-access-key-id` / `--s3-secret-access-key` в `rclone.args`.
  Файл с ключами держите с правами `0600`. Можно подставлять из окружения:
  `--s3-access-key-id=${BEGET_SHOP_KEY}`.
* **Флаги бэкенда** удобнее задавать картой `s3-custom-options`
  (`s3-chunk-size: 5M`, `s3-no-check-bucket: true`), она перекрывается
  в каждой секции; `rclone.args` остаётся простым путём.
* **Ключи на аккаунт** можно держать в `rclone.conf` — это штатный вариант;
  флаги в `rclone.args` перекрывают их для конкретного бакета.
* **Пароли БД** — через `password_env` и `/etc/kwbackup/secrets.env` (0600).

## Версионирование

Версия проекта — это subject текущего коммита (`0.0.15`, `0.0.16`, …).
`build.py` подставляет её в `VERSION` при сборке артефакта, а
`debian/changelog` содержит по записи на каждый коммит. Проверка согласия:

```bash
make check-version
```

`kwbackup --version` показывает версию, с которой собран бинарник.

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
`<target>/<db>/DAILY/...`. Архивы: `<target>/<name>_<timestamp>.tar.zst`, где `name` по умолчанию — имя
проекта. Ротация удаляет только файлы своего префикса, поэтому общий `target`
безопасен для нескольких проектов.
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

## Лицензия

MIT, см. [`LICENSE`](LICENSE).

## Legacy

`kwbackup.sh`, `_config.conf` и `kwbackup-config.md` — старая bash-версия,
оставлена для справки и для миграции. Новые установки используют
`dist/kwbackup` и YAML. Обе версии нельзя запускать одновременно на один
и тот же проект: у них разные механизмы блокировок.
