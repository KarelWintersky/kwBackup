# kwBackup

Бэкап баз данных, файлового storage и файловых архивов в S3-совместимое
облако (или любой remote `rclone`). Один исполняемый файл, Python 3.10+,
**без внешних зависимостей**.

Замена bash-версии `kwbackup.sh` 0.9.0: те же сценарии и та же схема
ротации, но с валидируемым YAML-конфигом, честными кодами возврата,
блокировками через `flock`, проверкой копий и восстановлением.

## Возможности

* **Четыре задачи сценария**: дамп БД → сжатие → заливка; `rclone sync/copy`
  каталогов; `tar` + компрессор → заливка; инкрементальные цепочки `tar`.
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
* **Логи куда угодно**: файл и/или syslog (`journalctl -t kwbackup`), консоль
  управляется отдельно (`runtime.verbose` / `runtime.debug`).
* `--dry-run`, `--check`, `--selftest`, живой прогресс упаковки в терминале.
* **Уведомления**: webhook, Telegram, SMTP-почта; в сообщении только имена
  и безопасные причины — без команд и ключей.
* **Vault**: версионный бэкап файлов слоями TAR + манифесты, журнал
  изменений, история без месячных full (restore — в работе).
* **Хуки** `before`/`after` у каждой задачи: `redis-cli BGSAVE`, maintenance
  mode, `docker exec`, ротация внешних файлов.

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

# 2. общие настройки хоста: логи, rclone.args, глубина хранения
sudo install -d -m 700 /etc/kwbackup /var/log/kwbackup
sudo install -m 644 debian/kwbackup.defaults.yaml /etc/kwbackup/defaults.yaml

# 3. задача: project, upstreams, targets, базы и ключи бакета
#    (BEGET выдаёт access/secret на бакет — они уходят флагами rclone)
sudo install -m 600 examples/database-only.yaml /etc/kwbackup/shop.yaml
sudoedit /etc/kwbackup/shop.yaml

# 4. пароль БД и прочие секреты — в окружение
sudo install -m 600 debian/kwbackup.default /etc/default/kwbackup
sudoedit /etc/default/kwbackup

# 5. проверка конфига и пробный прогон (от root, с загруженными секретами)
sudo -s
set -a; . /etc/default/kwbackup; set +a
kwbackup --config /etc/kwbackup/shop.yaml --check
kwbackup --config /etc/kwbackup/shop.yaml --dry-run

# 6. боевой запуск и проверка результата
kwbackup --config /etc/kwbackup/shop.yaml --database
kwbackup --config /etc/kwbackup/shop.yaml --list
```

Дальше — расписание: [`examples/cron/kwbackup`](examples/cron/kwbackup).

## Команды

| Команда | Что делает |
|---|---|
| `kwbackup --config FILE --database` | бэкап баз данных из конфига |
| `kwbackup --config FILE --storage` | синхронизация storage |
| `kwbackup --config FILE --archive` | упаковка и заливка архива |
| `kwbackup --config FILE --storage -m sync` | зеркало вместо copy (удаляет старое); `-m` глобальный для всех задач запуска |
| `kwbackup --config FILE` | показать доступные действия (ничего не запускает) |
| `kwbackup --config FILE --dry-run ...` | показать план, ничего не менять |
| `kwbackup --config FILE --check` | проверить конфиг (включая выключенные секции) и окружение; код 1 при проблемах |
| `kwbackup --config FILE --list` | что лежит в облаке, где пропуски |
| `kwbackup --config FILE --verify [--all]` | скачать и проверить копию |
| `kwbackup --config FILE --restore --database --to DIR [--at 2026-11-09]` | достать дамп |
| `kwbackup --config FILE --restore --archive --to DIR` | распаковать архив |
| `kwbackup --config FILE --restore --storage --to DIR` | скачать storage |
| `kwbackup --config FILE --restore --database --into-db db --yes` | залить в живую БД |
| `kwbackup migrate old.conf -o new.yaml` | конвертировать bash-конфиг |
| `kwbackup --selftest` | встроенные тесты |

Секция с `enabled: false` не мешает запуску: её ошибки не фатальны, список
действий помечает её `disabled, invalid config (see --check)`, а `--check`
проверяет и её. Строгая проверка включается и тогда, когда секция реально
выполняется — через `--force` или `--restore`.

Полная справка: [`docs/usage.md`](docs/usage.md).
Справочник по всем ключам конфига: [`docs/configuration.md`](docs/configuration.md).
Миграция со старого скрипта: [`docs/migration.md`](docs/migration.md).

## Конфиг

Общие настройки — один раз на хост, [`debian/kwbackup.defaults.yaml`](debian/kwbackup.defaults.yaml):
логи, `rclone.args`, тип БД, компрессор, глубина хранения.

Файл задачи после этого короткий (полный самодостаточный пример —
[`examples/full.yaml`](examples/full.yaml)):

```yaml
project: shop

upstreams:
  beget:
    config: /etc/kwbackup/rclone_beget.conf
    s3-custom-options:
      s3-access-key-id: ${BEGET_SHOP_KEY}
      s3-secret-access-key: ${BEGET_SHOP_SECRET}
      s3-no-check-bucket: true

database:
  enabled: true
  targets: ["beget:/shop/DB"]
  databases: [shop]
  # log, compressor, retention — из defaults
```

Файл `/etc/kwbackup/defaults.yaml` — единственное место, которое читает
программа; ниже него лежат встроенные дефолты (`src/05_builtin_defaults.py`).
Списки `rclone.args`,
`database.args`, `*_compressor_args` и `archive.exclude` дополняются,
остальные значения задача перекрывает.

### Storage: `copy` по умолчанию и месячный `sync`

Встроенный дефолт `storage.mode` — `sync` (зеркало). Чтобы ежедневный прогон
ничего не удалял, поставьте `copy` один раз на хосте:

```yaml
# /etc/kwbackup/defaults.yaml
storage:
  mode: copy
```

Это системный слой: он перекрывает встроенный `sync` для всех сценариев, у
которых нет своего `storage.mode` в файле задачи. Приоритет:
`-m/--mode` (CLI) → файл сценария → `/etc/kwbackup/defaults.yaml` →
`BUILTIN_DEFAULTS`. Эффективное значение видно в `--check`:

```
storage   enabled (copy)
  mode       copy
```

Раз в месяц имеет смысл прогнать зеркало, чтобы убрать из облака файлы,
удалённые локально:

```bash
kwbackup -c /etc/kwbackup/shop.yaml --storage -m sync
```

В cron — 1-го числа, после ночного бэкапа:

```cron
30 4 1 * * root set -a; . /etc/default/kwbackup; set +a; \
    /usr/local/bin/kwbackup -c /etc/kwbackup/shop.yaml --storage -m sync
```

**`-m/--mode` — глобальный для запуска:** он применяется ко **всем** задачам,
которые выполняются в этом прогоне. `-m sync` вместе с `--database
--archive` сделает зеркалом и их (для БД — по одной копии на класс, для
архива — только новый файл). Поэтому месячный `sync` запускайте только с
`--storage`.

`sync` удаляет в `target/<dest>` всё, чего нет в источнике. Пустой
каталог-источник (не смонтировался диск) задача отвергает, пока не задан
`storage.allow_empty: true`.

Готовые варианты: [`examples/database-only.yaml`](examples/database-only.yaml),
[`examples/storage-only.yaml`](examples/storage-only.yaml),
[`examples/archive-only.yaml`](examples/archive-only.yaml).

### Подключение к БД

Настройки подключения живут в секции `database:`; `upstreams` — это только
хранилища. Пароль в конфиг не пишется: передаётся имя переменной окружения
(`password_env`) либо используются штатные файлы клиентов.

#### MySQL / MariaDB

```yaml
database:
  enabled: true
  type: mariadb            # mysql -> mysqldump, mariadb -> mariadb-dump
  host: localhost
  port: 3306
  user: backup
  password_env: MYSQL_PWD  # имя переменной, не значение
  args:                    # заменяют дефолты целиком
    - --single-transaction
    - --quick
    - --no-tablespaces
  targets: [s3a:/DB]
  databases: [shop]
  retention: {daily: {keep: 8}}
```

```bash
export MYSQL_PWD='...'     # или set -a; . /etc/default/kwbackup; set +a
```

Без `password_env` клиент возьмёт `~/.my.cnf`. Для сокета вместо TCP:
`args: ["--socket=/run/mysqld/mysqld.sock", ...]`.

#### PostgreSQL

```yaml
database:
  enabled: true
  type: postgres           # pgsql — тот же путь
  host: 127.0.0.1
  port: 5432
  user: backup
  password_env: PGPASSWORD # значение переменной уходит в PGPASSWORD
  args: [--no-owner, --no-privileges]
  targets: [s3a:/DB]
  databases: [shop]
  retention: {daily: {keep: 8}}
```

Без `password_env` используется `~/.pgpass`. Для peer-аутентификации через
сокет: `host: /var/run/postgresql`, `password_env` не задаётся.

#### SQLite

```yaml
database:
  enabled: true
  type: sqlite
  host: /var/lib/app/app.db   # путь к файлу, а не хост
  targets: [s3a:/DB]
  databases: [app]            # нужно только для имени дампа
  retention: {daily: {keep: 8}}
```

`host` — путь к базе; `databases` не выбирает базу, а задаёт префикс имени
файла в облаке; `port` и `args` не используются. Восстановление в живую базу
(`--into-db`) для sqlite пишет в `host`, а не в переданное имя — надёжнее
восстановить в каталог (`--restore --to DIR`) и подменить файл вручную.

### Инкрементальный бэкап (`incremental`)

Четвёртая задача: цепочки GNU tar. Раз в месяц — полный дамп, каждый
следующий день — инкремент к нему. Без сжатия, обычные `.tar`.

```yaml
incremental:
  enabled: true
  name: data                        # по умолчанию = project
  root: /srv/data
  include: ["*"]
  exclude: ["*.tmp"]
  initial_day: 1                    # день месяца, с которого начинается цепочка (1..28)
  snapshot: /var/lib/kwbackup/data.snar   # обязателен при enabled: true
  targets: [s3a:/INCR, s3b:/INCR]
  retention: {keep: 3}              # число цепочек (месяцев)
```

**Цепочка и имена файлов.** Цепочка — это месяц, он вшит в имя:

```
<target>/data_2026-11_full_2026-11-01-03-30-00.tar
<target>/data_2026-11_inc_2026-11-02-03-30-00.tar
<target>/data_2026-11_inc_2026-11-03-03-30-00.tar
```

Правило: запуск относится к текущему месяцу, если `today.day >= initial_day`,
иначе к предыдущему. Первый запуск в цепочке делает полный дамп
(`tar --level=0`), остальные — инкременты (`tar -g`). Поэтому `initial_day: 25`
означает: с 25 ноября по 24 декабря — инкременты в цепочку `2026-11`,
25 декабря начинается `2026-12`. Первый запуск при пустом хранилище всегда
полный, даже если `initial_day` ещё не наступил.

**Snapshot.** `snapshot` — файл состояния GNU tar; без него цепочка не
строится, поэтому ключ обязателен при `enabled: true`. Каталог создаётся
самим инструментом с правами `0700`. Снапшот локальный, в облако не
заливается. Если он потерян, следующий запуск сделает новый полный дамп;
восстановление берёт **самый новый** `full_*` в цепочке и инкременты после
него.

**Ротация.** `retention` считает цепочки, а не файлы: `{keep: 3}` — три
последних месяца, `{max_age: 180d}` — всё, что старше. Удаляется цепочка
целиком; отдельный инкремент без полного не остаётся никогда.

**Восстановление.**

```bash
kwbackup --config shop.yaml --restore -i --to /srv/restore
kwbackup --config shop.yaml --restore -i --from s3b --to /srv/restore
kwbackup --config shop.yaml --restore -i --to /srv/restore --at 2026-11-10
```

Инструмент скачивает `full_*` и все `inc_*` цепочки по возрастанию времени
и распаковывает их по очереди (`tar -x -g /dev/null`). Удалённые между
слоями файлы исчезают из результата — GNU tar записывает удаления в цепочку.

#### Зачем `--at`

Без `--at` восстанавливается **последнее** состояние: полный дамп плюс все
инкременты. `--at` задаёт точку во времени и берёт только слои до неё:

```
full  2026-11-01   inc 11-02   inc 11-05   inc 11-09   inc 11-12
--at 2026-11-09  →  full + 11-02 + 11-05 + 11-09   (состояние на 9 ноября)
--at 2026-11-05  →  full + 11-02 + 11-05
без --at         →  full + все четыре инкремента
```

Смысл: увидеть данные **на конкретную дату**, а не «как сейчас». Типичные
случаи — файл испортили 9-го, а заметили 12-го: `--at 2026-11-08` вернёт
состояние до порчи. Или нужно воспроизвести поведение системы на прошлой
неделе. Принимается `YYYY-MM` (вся цепочка) или `YYYY-MM-DD` (слои до этого
дня включительно).

`--list` показывает цепочки, `--verify` проверяет полный дамп и последний
инкремент.

### Переменные окружения: `${ENV}`

В любом **строковом значении** конфига можно писать `${ИМЯ}` — при загрузке
оно будет заменено значением переменной окружения. Работает в обоих слоях
(`/etc/kwbackup/defaults.yaml` и файл задачи), на любой глубине: в `targets`,
путях, `args`, `s3-custom-options`, `env`.

```yaml
database:
  host: ${DB_HOST}
archive:
  root: ${DATA_DIR}/site
rclone:
  s3-custom-options:
    s3-access-key-id: ${BEGET_KEY_ID}
    s3-secret-access-key: ${BEGET_SECRET}
```

Правила:

* подставляется только форма `${NAME}`; `$NAME` без скобок остаётся текстом;
* имя — латиница, цифры и `_`, первый символ — буква или `_`;
* подстановка выполняется **после** слияния слоёв и **до** валидации,
  поэтому отсутствие переменной — ошибка конфига с точным местом:

  ```
  kwBackup: invalid config:
    - config.rclone.env.AWS_ACCESS_KEY_ID: environment variable RCLONE_KEY_ID is not set
  ```

* пустая переменная (`FOO=`) считается заданной: подставится пустая строка;
* в **ключах** не работает — только в значениях;
* для числовых и булевых полей подстановка не подходит: `port: ${PGPORT}`
  даст строку, а валидатор ждёт число. Пишите литерал;
* литеральный `${...}` передать нельзя — экранирования нет. Если нужен
  именно такой текст (например, шаблон в пути), задайте его через
  переменную окружения с этим значением.

#### Откуда программа берёт окружение

`kwBackup` читает **окружение своего процесса** (`os.environ`) в момент
загрузки конфига. Своего ключа `env_file:` у конфига нет: переменные
приходят от того, кто запустил процесс — оболочки, systemd, cron, docker,
CI.

Следствия, на которых чаще всего спотыкаются:

* `export VAR=...` в интерактивной оболочке — работает;
* `VAR=... kwbackup ...` (префикс к команде) — тоже работает;
* **`sudo` по умолчанию окружение сбрасывает** — нужен `sudo -E` или
  `sudo --preserve-env=VAR`;
* **в cron логин-окружения нет** — только `PATH`, `HOME`, `SHELL` и то,
  что задано в самом crontab;
* **`. /etc/default/kwbackup` без `set -a` переменные не экспортирует**: они станут
  переменными оболочки, а дочерний процесс их не увидит;
* systemd даёт юниту своё окружение через `Environment=` и
  `EnvironmentFile=` — там `set -a` не нужен.

`rclone.env` из конфига — это **не** источник `${...}`: это карта переменных
для дочернего процесса rclone. `password_env` — имя переменной, значение
которой читается в момент запуска клиента БД.

#### 1. Файл секретов (рекомендуется)

`/etc/default/kwbackup`, права `0600`, владелец root:

```ini
# комментарии разрешены
RCLONE_KEY_ID=XXXXXXXXXXXXXXXX
RCLONE_SECRET=YYYYYYYYYYYYYYYY
MYSQL_PWD=change-me
DB_HOST=localhost
```

Формат — простые присваивания `КЛЮЧ=значение`:

* без `export` в начале строки (systemd его не понимает, а для shell
  экспорт включается через `set -a`);
* без пробелов вокруг `=`: `MYSQL_PWD = x` даст переменную с именем
  `MYSQL_PWD ` (с пробелом);
* кавычки не нужны; если значение содержит `#`, пробелы или кавычки —
  заключите его в кавычки, systemd и shell разберут их одинаково;
* пустая строка после `=` — переменная задана и пуста (это не ошибка).

#### 2. systemd

```ini
[Service]
EnvironmentFile=/etc/default/kwbackup
# или отдельные значения:
# Environment=DB_HOST=localhost RCLONE_KEY_ID=...
ExecStart=/usr/bin/kwbackup --config /etc/kwbackup/shop.yaml --database
```

`EnvironmentFile=-/path` (с минусом) не считает отсутствие файла ошибкой.
Проверить, что реально попало в юнит: `systemctl show kwbackup -p Environment`.

#### 3. cron

```bash
30 3 * * * root set -a; . /etc/default/kwbackup; set +a; /usr/bin/kwbackup --config /etc/kwbackup/shop.yaml --database
```

`set -a` обязателен: без него присваивания из файла останутся переменными
оболочки и не попадут в окружение `kwBackup`.

#### 4. Вручную и в CI

```bash
set -a; . /etc/default/kwbackup; set +a
kwbackup --config /etc/kwbackup/shop.yaml --check
```

В CI/CD — штатные механизмы секретов раннера (`env:` в GitLab CI,
`env:`/`secrets` в GitHub Actions, `docker run --env-file`), они выставляют
переменные в окружение процесса.

`${ENV}` и `password_env` — разные вещи:

| | Что делает |
|---|---|
| `${ENV}` | подставляет значение в конфиг при загрузке; дальше это обычное поле |
| `password_env: MYSQL_PWD` | хранит в конфиге только **имя** переменной; значение читается в момент запуска клиента БД и в конфиг не попадает |

О чём стоит помнить: значение, подставленное в `rclone.args` или
`s3-custom-options`, попадает в командную строку rclone и видно другим
пользователям хоста через `ps`. Кроме того, при `log.level: debug` в
`log.file` и в syslog печатается полная командная строка (включая ключи) — на
таких хостах держите лог с правами `0600` или не включайте debug в стоки.

### Секреты

Писать секреты прямо в YAML **можно** — программа этого не запрещает, —
но не рекомендуется: файл задачи попадает в резервные копии и репозитории,
а права у него обычно шире, чем нужно. Предпочтительный способ — `${ИМЯ}`
и значения в `/etc/default/kwbackup` (0600). Если всё же пишете ключи
в конфиг, поставьте на файл `chmod 600`.

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
* **Пароли БД** — через `password_env` и `/etc/default/kwbackup` (0600).

## Версионирование

Версия проекта — это subject текущего коммита (`0.99.0`, `0.99.1`, …).
Python-линия продолжает bash-версии (`0.8.0` … `0.9.0+fix1`) и идёт как
`0.99.x` до первого стабильного выпуска.
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
* [examples/](examples) — конфиги под каждый сценарий и cron-пример.
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

`kwbackup.sh` в корне и каталог [`examples/legacy_bash_version/`](examples/legacy_bash_version)
(`_config.conf`, `kwbackup-config.md`, `README_DEV.md`) — старая bash-версия,
оставлена для справки и для миграции. Новые установки используют
`dist/kwbackup` и YAML. Обе версии нельзя запускать одновременно на один
и тот же проект: у них разные механизмы блокировок.
