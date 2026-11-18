# kwBackup

Резервное копирование сервера в S3-совместимое облако: базы данных,
файловые каталоги, архивы и версионная история изменений. Одна команда
в cron, декларативный YAML на каждый проект, один исполняемый файл
на Python 3.10+ без внешних зависимостей.

## Содержание

- [Зачем это](#зачем-это)
- [Как это работает](#как-это-работает)
- [Установка](#установка)
- [Настройка хоста](#настройка-хоста)
- [Первый сценарий](#первый-сценарий)
- [Задачи](#задачи)
  - [database](#database)
  - [storage](#storage)
  - [archive](#archive)
  - [incremental](#incremental)
  - [vault](#vault)
- [Восстановление](#восстановление)
- [Расписание и мониторинг](#расписание-и-мониторинг)
- [Безопасность](#безопасность)
- [Миграция с bash-версии](#миграция-с-bash-версии)
- [Разработка](#разработка)

## Зачем это

Сервер обрастает скриптами: `mysqldump | gzip` в cron, `rclone sync`
загрузок, `tar` раз в месяц. Через год выясняется, что ротация удаляет не те
файлы, один из трёх скриптов молча упал прошлой весной, а проверить, что
копия действительно читается, нечем.

kwBackup собирает это в одну программу с предсказуемым поведением:

* сценарий — YAML-файл, а не набор `export` и функций;
* каждая копия проверяема: `--list` показывает историю, `--verify`
  скачивает и читает её;
* восстановление — такая же команда, а не «помню, там был tar»;
* параллельные запуски исключены блокировками;
* ошибки видны в кодах возврата, логах и уведомлениях.

Проект вырос из bash-скрипта `kwbackup.sh` 0.9.0. Идеи остались прежними —
GFS-ротация, заливка через rclone, один конфиг на проект, — но вместо
исходника на 600 строк с глобальными переменными теперь данные и валидатор.

## Как это работает

Один запуск выглядит так:

1. читается `/etc/kwbackup/defaults.yaml` (настройки хоста), поверх него —
   файл задачи;
2. проверяется окружение: бинарники, пути, rclone-конфиги, переменные;
3. берётся блокировка `flock` на пару «проект, задача»;
4. выполняются включённые задачи: дамп или архив → сжатие → заливка
   на каждый target → ротация;
5. итог уходит в лог, а если настроено — в почту, webhook или Telegram.

Задачи независимы: упавшая заливка на один апстрим не отменяет остальные.
В конце будет ненулевой код и список того, что сломалось.

| Задача | Что делает | Что появляется в облаке |
|---|---|---|
| `database` | дамп (`mysqldump`, `mariadb-dump`, `pg_dump`, `sqlite3`) → сжатие → заливка | `<target>/DAILY/<db>_<ts>.sql.zst` |
| `storage` | `rclone copy` или `sync` каталогов | `<target>/<dest>/…` |
| `archive` | `tar` + компрессор → заливка | `<target>/<name>_<ts>.tar.zst` |
| `incremental` | цепочки GNU tar: полный раз в месяц + инкременты | `<target>/<name>_<YYYY-MM>_full|inc_<ts>.tar` |
| `vault` | версионные TAR-слои с JSON-манифестами | `<target>/<UTC>.manifest.json` + `-part-NNN.tar` |

Если баз в задаче несколько, к пути добавляется имя базы:
`<target>/<db>/DAILY/…`. Ротация удаляет только «свои» файлы — `<db>_…`
для дампов, `<name>_…` для архивов, — поэтому один target можно делить
между проектами, и они не вычищают друг друга.

### Ротация

Классы три: `daily`, `weekly`, `monthly`. DAILY создаётся каждый запуск,
WEEKLY — дополнительно по понедельникам, MONTHLY — 1-го числа. Один дамп
может попасть сразу в несколько классов.

Глубина задаётся на класс: `keep: N` — оставить N новейших копий,
`max_age: 7d` — удалять всё старше. Это разные стратегии: `keep` переживает
пропуски и дубли, `max_age` держит фиксированное окно. Смешивать их в одном
классе нельзя, `retention_mode` запрещает и по всем классам сразу.

## Установка

```bash
sudo dpkg -i kwbackup_<version>_all.deb
kwbackup --version
kwbackup --selftest
```

Пакет ставит `/usr/bin/kwbackup`, conffile `/etc/kwbackup/defaults.yaml`,
примеры в `/usr/share/doc/kwbackup/examples/` и стартовый файл секретов
`/etc/default/kwbackup`.

Из исходников:

```bash
make build      # dist/kwbackup + ../kwbackup_<version>_all.deb
make test       # встроенные проверки и сквозные тесты
make dist       # только dist/kwbackup
```

Для работы нужны Linux с `flock` и `tar`, сам `rclone`, клиент вашей СУБД
и компрессор (`zstd`, `gzip`, `pigz` или `rar`) — в зависимости от того,
что включено в сценарии.

## Настройка хоста

Три вещи настраиваются один раз и переиспользуются всеми задачами:
доступ к облаку, общие параметры и секреты.

### rclone

Заливка идёт через rclone, поэтому ему нужен remote, описывающий
S3-провайдера. Удобно держать отдельный файл на бакет:

```ini
# /etc/kwbackup/rclone_beget.conf
[beget]
type = s3
provider = Other
endpoint = <endpoint из панели провайдера>
region = <region>
acl = private
```

Имя секции (`[beget]`) — это ключ upstream: он же используется в `targets`
как `beget:/путь`. Файл подключается через `upstreams.<имя>.config`.

Ключи доступа к бакету в `rclone.conf` не работают: у BEGET и подобных
провайдеров `env_auth = false`, ключи выдаются на бакет и передаются флагами
`--s3-access-key-id` / `--s3-secret-access-key`. В конфиге задачи для этого
есть `s3-custom-options`. Если ключи у вас на аккаунт, их можно оставить
в `rclone.conf`, а `s3-custom-options` не задавать.

### defaults.yaml

`/etc/kwbackup/defaults.yaml` — единственный файл, который читает программа,
кроме задачи. Здесь живёт всё общее для хоста:

```yaml
log:
  level: info
  file: /var/log/kwbackup/kwbackup.log
  syslog: false

runtime:
  temp_dir: /tmp/kwbackup

database:
  type: mariadb            # mysql | mariadb | postgres | sqlite
  host: localhost
  user: backup
  password_env: MYSQL_PWD
  retention:
    daily: {keep: 8}
    weekly: {keep: 6}
    monthly: {keep: 12}

storage:
  mode: copy               # sync удаляет в облаке лишнее

archive:
  compressor: zstd
  compressor_args: ["-9"]
```

В файле задачи остаётся только то, что относится к проекту: `project`,
`upstreams`, `targets`, `databases`, `sources`, `root`. Списки опций
(`rclone.args`, `database.args`, `*_compressor_args`, `archive.exclude`)
дополняются значениями из задачи, остальные ключи задача перекрывает.

Опечатка в ключе — предупреждение, а не отказ: `unknown key, ignored`
попадёт в лог, а бэкап продолжится. Неверное значение, отсутствие
обязательного поля и конфликт правил ротации — ошибка конфига.

### Секреты

Пароли и ключи не стоит хранить в YAML: файл задачи попадает в резервные
копии и репозитории. Штатное место — `/etc/default/kwbackup` с правами
`0600`:

```ini
MYSQL_PWD=...
BEGET_SHOP_KEY=...
BEGET_SHOP_SECRET=...
```

В конфиге на них ссылаются как `${ИМЯ}` — подстановка работает в любом
строковом значении. Переменные должны быть в окружении процесса: systemd
делает это через `EnvironmentFile=`, cron и ручной запуск — через
`set -a; . /etc/default/kwbackup; set +a`. `sudo` окружение не передаёт,
поэтому `sudo -E` работает только с заранее экспортированными переменными.

## Первый сценарий

Минимальный бэкап — одна база в бакет. Начните с примера из пакета
и поправьте в нём три вещи: `project`, `targets` и `databases`.

```bash
sudo install -m 600 /usr/share/doc/kwbackup/examples/database-only.yaml \
    /etc/kwbackup/shop.yaml
sudoedit /etc/kwbackup/shop.yaml
```

Файл задачи выглядит так:

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
  # retention, compressor, тип БД и хост — из defaults.yaml
```

Дальше — проверка, примерка и боевой запуск:

```bash
set -a; . /etc/default/kwbackup; set +a

kwbackup -c /etc/kwbackup/shop.yaml --check     # 0 — можно запускать
kwbackup -c /etc/kwbackup/shop.yaml --dry-run   # точные команды, без заливки
kwbackup -c /etc/kwbackup/shop.yaml --database
kwbackup -c /etc/kwbackup/shop.yaml --list
```

`--check` — единственный режим, который проверяет и выключенные секции,
и окружение: найден ли `rclone`, клиент БД, компрессор, rclone-конфиг,
существуют ли пути и заданы ли переменные. Код 0 — всё на месте, 2 — ошибка
конфига, 1 — проблемы окружения. `--list` после первого прогона покажет,
что реально лежит в облаке.

## Задачи

### database

Дамп снимается один раз за запуск и заливается во все `targets` задачи.
Тип определяет команду: `mysql` → `mysqldump`, `mariadb` → `mariadb-dump`,
`postgres`/`pgsql` → `pg_dump`, `sqlite` → `sqlite3 <path> .dump`.

```yaml
database:
  enabled: true
  type: mariadb
  targets: ["beget:/shop/DB", "nas:/backup/shop"]
  databases: [shop, shop_stats]
  retention:
    daily: {keep: 8}
    weekly: {keep: 6}
    monthly: {keep: 12}
```

Раскладка зависит от числа баз: одна база пишется в `<target>/DAILY`,
несколько — в `<target>/<db>/DAILY`, чтобы не смешивать файлы.

* `password_env` — имя переменной с паролем (значение уходит в `MYSQL_PWD`
  или `PGPASSWORD`). Без него клиент берёт `~/.my.cnf` или `~/.pgpass`.
* `args` **дополняют** дефолтные опции, а не заменяют их.
* `ignore_table: [shop.sessions]` исключает таблицы из дампа.
* `catch_up: true` создаёт пропущенную weekly/monthly копию при следующем
  запуске, если хост лежал в понедельник или 1-го числа.
* `before`/`after` — команды вокруг задачи, например `redis-cli BGSAVE`.

### storage

Копирование каталогов через rclone. Источников может быть сколько угодно,
у каждого свой подкаталог в целевом контейнере:

```yaml
storage:
  mode: copy
  targets: ["beget:/shop/STORAGE"]
  sources:
    - /srv/shop/uploads            # dest = "uploads"
    - path: /srv/shop/static
      dest: static
    - path: /srv/shop/data
      dest: ""                     # содержимое в корень target
```

`copy` только добавляет и обновляет файлы, `sync` приводит каталог
к состоянию источника и **удаляет** в облаке всё лишнее. Для ежедневного
прогона безопаснее `copy`, а зеркало запускать раз в месяц отдельным
заданием:

```bash
kwbackup -c /etc/kwbackup/shop.yaml --storage -m sync
```

`-m/--mode` действует на весь запуск, поэтому месячный `sync` запускайте
только с `--storage`. Пустой каталог-источник `sync` отвергает, пока не
задан `storage.allow_empty: true`: это защита от «диск не смонтировался».

### archive

Каталог упаковывается в tar-поток, сжимается и заливается одним файлом.

```yaml
archive:
  compressor: zstd
  compressor_args: ["-9"]
  name: shop
  root: /srv/shop
  include: ["*"]
  exclude: ["*.mp4", "*/cache/*", "logs"]
  targets: ["beget:/shop/ARCHIVE"]
  retention: {keep: 5}
```

* `include: ["*"]` — весь каталог, включая dotfiles верхнего уровня
  (`.env`, `.htaccess`). Иначе — маски относительно `root`, с поддержкой
  `**`.
* `exclude` — маски tar; `logs` исключает каталог на любой глубине.
* `name` — не подпись, а признак владения: ротация удаляет только файлы
  `<name>_…`. Переименовали — старые файлы перестанут попадать под ротацию.
* Нет `retention` — архив не чистится никогда. Это безопасно и бесконечно
  дорого; задавайте правило осознанно.

### incremental

Цепочки GNU tar: раз в месяц полный дамп, каждый следующий день —
инкремент к нему. Сжатия нет, файлы обычные `.tar`.

```yaml
incremental:
  enabled: true
  name: data
  root: /srv/data
  include: ["*"]
  initial_day: 1
  snapshot: /var/lib/kwbackup/data.snar
  targets: ["beget:/INCR"]
  retention: {keep: 3}
```

Цепочка — это месяц, он вшит в имя файла
(`data_2026-11_full_2026-11-01-03-30-00.tar`). Запуск относится к текущему
месяцу, если `today.day >= initial_day`, иначе к предыдущему. Первый запуск
в цепочке делает полный дамп, остальные — инкременты.

`snapshot` — файл состояния GNU tar, обязателен и локален. Если он потерян,
следующий запуск сделает новый полный дамп, а восстановление возьмёт самый
новый `full_*` и инкременты после него. Ротация удаляет цепочку целиком:
`keep: 3` — три последних месяца.

Восстановление умеет состояние на дату:

```bash
kwbackup -c data.yaml --restore --incremental --to /srv/restore --at 2026-11-10
```

### vault

Версионный бэкап файлов: история хранится слоями, без ежемесячных полных
копий. Первый запуск делает base — полный скан, дальше в облако уходят
только изменённые файлы, целиком, в обычный TAR. Сжатия нет: фото и видео
уже сжаты, а манифесту нужны смещения внутри тома.

```yaml
vault:
  enabled: true
  root: /srv/storage
  state_dir: /var/lib/kwbackup/storage
  volume_size: 2G
  exclude: [".state", "*.tmp"]
  journal:
    type: txt
    period: daily
    path: /var/lib/storage/journal
  restore:
    enabled: true
    order: desc
    priority: [photos, videos, titles]
    chown: false
  targets: ["beget:/VAULT"]
```

На каждый запуск появляется слой:

```
2026-11-18T020000Z.manifest.json              base, дата, список томов
2026-11-18T020000Z-part-001.tar               изменённые файлы
2026-11-18T020000Z-part-001.manifest.json     файлы тома и их смещения
2026-11-18T020000Z-deleted.manifest.json      удаления
```

Слой создаётся даже пустым — по истории видно, что бэкап работал. Новый base
делает только `--full` (и самый первый запуск); `--reconcile` пересобирает
корректирующий слой без нового base.

Ежедневный запуск не сканирует диск целиком: приложение само ведёт журнал
`YYYY-MM-DD.journal.txt` в `journal.path`, берёт вчерашний файл и после
успешной заливки переименовывает его в `.done`. Журнал — подсказка: каждый
путь всё равно сверяется `stat` с индексом, поэтому запоздавшие записи
безвредны.

Тома качаются только при восстановлении, по одному, и сразу удаляются.
`--dry-run` читает манифесты и печатает план: сколько томов и гигабайт
придётся скачать и в каком порядке.

`state_dir`, `temp_dir` и журнал не должны лежать внутри `root` — иначе они
попадут в бэкап, и `--check` откажется запускаться. Автоисключений нет:
каталоги нужно перечислить в `exclude`.

## Восстановление

Восстановление всегда идёт в каталог, указанный в `--to`. В живую базу
инструмент не пишет: дамп скачивается как есть, импорт — осознанный ручной
шаг. Это сделано намеренно — перезапись живой базы единственная
необратимая операция, и решение о ней не должно приниматься скриптом.

```bash
# база: последняя копия или состояние на дату
kwbackup -c shop.yaml --restore --database --to /srv/restore --at 2026-11-09

# архив, storage, vault
kwbackup -c site.yaml  --restore --archive  --to /srv/restore
kwbackup -c files.yaml --restore --storage  --to /srv/restore
kwbackup -c files.yaml --restore --vault    --to /srv/restore --at 2026-11-17
```

Дамп ложится в каталог в том виде, в каком он в облаке: `.sql.zst`,
`.sql.gz`, `.sql.rar` или `.sql`. Импорт — привычной командой:

```bash
zstd -dc /srv/restore/shop_2026-11-09-03-30-00.sql.zst | mysql shop
```

Каталог `--to` не очищается, поэтому восстанавливайте в пустой. У каждой
секции есть блок `restore`: `enabled` (по умолчанию `true`) запрещает
восстановление на этом хосте, если поставить `false`; обход — `--force`.
У `vault` там же порядок слоёв (`order`, `priority`) и `chown`.

## Расписание и мониторинг

Ежедневный запуск — одна строка в cron:

```cron
30 3 * * * root set -a; . /etc/default/kwbackup; set +a; \
    /usr/bin/kwbackup -c /etc/kwbackup/shop.yaml --database --storage
```

Готовые варианты — [`examples/cron/kwbackup`](examples/cron/kwbackup)
и [`examples/systemd/`](examples/systemd).

Коды возврата рассчитаны на watchdog: 0 — успех, 1 — ошибка выполнения,
2 — конфиг, 3 — аргументы, 4 — задача уже идёт, 5 — `--list` или `--verify`
нашли проблему. Последние две команды можно ставить отдельным ночным
заданием: они только читают.

`--verify` скачивает последнюю копию и проверяет, что она читается и похожа
на дамп; для `vault` — что манифесты и тома на месте и имеют ожидаемые
размеры. `--list` показывает историю и дыры в ней.

Уведомления настраиваются в секции `notify`: почта, webhook, Telegram.
Сообщение одно на прогон, в нём только имена, статус и причина — ни команд,
ни ключей.

Логи идут в файл и/или syslog (`journalctl -t kwbackup`). Цвета есть только
в терминале: числа жёлтым, успех зелёным, ошибки красным; в файл и syslog
ANSI не попадает.

## Безопасность

* Программа работает от root: ей нужен доступ к сокету БД и `/etc/kwbackup`.
* Ключи бакета и пароль БД — в `/etc/default/kwbackup` (0600) или в
  `rclone.conf`, а не в файле задачи. Если ключи всё же в YAML — `chmod 600`.
* `--debug` (и `log.level: debug`) печатает полные команды rclone, включая
  флаги с ключами. Это осознанно — так воспроизводят сломанную команду
  руками, — поэтому лог с debug держите `0600`.
* Клиентского шифрования нет: в облако уходит обычный `.sql.zst` или
  `.tar.zst`. Если это требование, включите серверное шифрование
  у провайдера или оберните шаг в `zstd | gpg`.
* `--list` и `--verify` ничего не меняют; `--restore` не пишет в базы.

## Миграция с bash-версии

```bash
kwbackup migrate /etc/kwbackup/old.conf -o /etc/kwbackup/shop.yaml
kwbackup -c /etc/kwbackup/shop.yaml --check
```

Конвертер переносит пути, контейнеры и глубину хранения. Что меняется
по смыслу: `DB_MIN_AGE_*` и `ARCHIVE_ROTATION_PERIOD` превращаются
в `keep: N` — «оставить N копий» вместо «удалять старше N дней». Если нужен
именно временной порог, поставьте `max_age` вручную. Подробности и таблица
соответствий — в [`docs/migration.md`](docs/migration.md).

Обе версии нельзя запускать на один проект одновременно: у них разные
механизмы блокировок.

## Разработка

Продакшен — один файл `dist/kwbackup`, собранный конкатенацией `src/*.py`
в порядке числовых префиксов. Все `import` живут в `src/00_header.py`,
поэтому нет ни бандлера, ни рерайта импортов. Каталог `docs/` — справочник
по ключам и сценариям, `examples/` — готовые конфиги.

```bash
python3 build.py --test     # сборка + py_compile + selftest
python3 tests/e2e.py        # сквозные тесты на фейковом rclone
make dist                   # артефакт с версией из subject коммита
make build                  # ещё и .deb
make check-version          # subject коммита == верх debian/changelog
```

Версия проекта — subject текущего коммита (`0.99.x`); линейка продолжает
bash-версии `0.8.0 … 0.9.0+fix1` и идёт до первого стабильного выпуска.
Релизов и тегов пока не было.

## Документация

| | |
|---|---|
| [docs/usage.md](docs/usage.md) | сценарии, расписание, мониторинг, разбор ошибок |
| [docs/configuration.md](docs/configuration.md) | все ключи и значения по умолчанию |
| [docs/migration.md](docs/migration.md) | переезд с `kwbackup.sh` |
| [examples/](examples) | конфиги под каждый сценарий: БД, storage, архив, инкременты, vault |
| [CHANGELOG.md](CHANGELOG.md) | что менялось между версиями |

`kwbackup.sh` в корне и [`examples/legacy_bash_version/`](examples/legacy_bash_version) —
старая bash-версия, оставлена для справки и миграции.

## Лицензия

MIT, см. [LICENSE](LICENSE).
