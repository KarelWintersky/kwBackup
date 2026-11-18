# Использование

## Установка и первая проверка

```bash
sudo dpkg -i kwbackup_<version>_all.deb     # /usr/bin/kwbackup + /etc/kwbackup/defaults.yaml
kwbackup --version
kwbackup --selftest
```

Из исходников: `make build` (артефакт + пакет) или `make dist` и
`sudo install -m 755 dist/kwbackup /usr/bin/kwbackup`.

Дальше нужен remote в rclone (`rclone config`) и файл секретов:

```bash
sudo install -d -m 700 /etc/kwbackup /var/log/kwbackup
# общие настройки хоста
sudo install -m 644 debian/kwbackup.defaults.yaml /etc/kwbackup/defaults.yaml
# задача: project, upstreams, targets, базы, ключи бакета
sudo install -m 600 examples/database-only.yaml /etc/kwbackup/shop.yaml
# секреты, которые передаются через окружение
sudo install -m 600 debian/kwbackup.default /etc/default/kwbackup
sudoedit /etc/default/kwbackup
```

`/etc/kwbackup/defaults.yaml` ставится с правами `0644` (он идёт conffile в
пакете), файлы задач с ключами держите `0600`: у BEGET
ключи выдаются на бакет и передаются флагами rclone прямо в конфиге, так что
файл задачи — это файл с секретами. Права `0600` — ваша ответственность:
программа их не проверяет.

## Порядок первого запуска

```bash
sudo -s
set -a; . /etc/default/kwbackup; set +a
kwbackup --config /etc/kwbackup/shop.yaml --check
kwbackup --config /etc/kwbackup/shop.yaml --dry-run
kwbackup --config /etc/kwbackup/shop.yaml
kwbackup --config /etc/kwbackup/shop.yaml --list
kwbackup --config /etc/kwbackup/shop.yaml --verify
```

Секреты должны быть в окружении процесса: их загружает `EnvironmentFile=`
в systemd, `set -a; . /etc/default/kwbackup; set +a` в cron, либо строка выше в
интерактивной сессии. `sudo` по умолчанию окружение не передаёт — если
идёте через `sudo -E`, переменные должны быть экспортированы заранее.

`--check` — единственный режим, который валидирует и **выключенные** секции
(`enabled: false`). Обычный запуск их только разбирает: проблемы видны в списке
действий (`disabled, invalid config (see --check)`) и в логе при `--verbose`,
а фатальными становятся лишь когда секция реально выполняется (`--force`,
`--restore`).

`--check` печатает разобранный конфиг и **preflight**: найден ли
`rclone`, `mysqldump`/`pg_dump`, `tar`, компрессор; существует ли
`rclone.config`; доступны ли источники storage, `root` архива, `temp_dir`;
задана ли переменная с паролем. Если preflight нашёл проблемы, `--check`
возвращает **1** (конфиг при этом может быть валиден), пишет их в лог и,
если включены уведомления на `failure`, отправляет сообщение. Ноль — только
когда всё на месте.

```
kwBackup: Backup tool over rclone: database dumps, file storage, archives (Version: 0.99.43)

  config     /etc/kwbackup/shop.yaml
  defaults   /etc/kwbackup/defaults.yaml
  project    shop
  log        level info, file /var/log/kwbackup/shop.log
  console    quiet (warnings and errors)

  rclone
    binary     rclone
    s3         2 flag(s)
  upstreams
    s3a        /etc/kwbackup/rclone_s3a.conf, 3 s3 flag(s)
    s3b        /etc/kwbackup/rclone_s3b.conf
               s3-access-key-id, s3-chunk-size, s3-no-check-bucket
               s3-secret-access-key, s3-upload-cutoff

  database  enabled (mariadb)
    mode       copy
    targets    s3a:/DB, s3b:/DB
    databases  mediamaker
    compressor zstd
    daily      keep 8
    weekly     keep 6
    monthly    keep 12

  storage   disabled

  archive   disabled

  preflight ok
```

Флаги S3 печатаются один раз в `rclone`, а у секций — только переопределения:
`+s3-secret-access-key` (добавлен/изменён) или `-s3-server-side-encryption`
(снят). Значения флагов не печатаются никогда.

`--dry-run` проходит те же проверки и печатает полный план: команды дампа,
сжатия и `rclone copyto`. Ничего не скачивается и не заливается.

## Сценарии

### Ежедневный бэкап всего

```bash
kwbackup --config /etc/kwbackup/shop.yaml --database --storage --archive --incremental
```

Действие всегда указывается явно. Список `--database --storage --archive`
безопасен и для выключенных секций: они пишут `disabled` и пропускаются.

Если не указать ни одного действия, программа **ничего не запускает**,
печатает доступные действия из конфига и выходит с кодом 0:

```
$ kwbackup --config shop.yaml
No one backup action(s) requested. Allowed:
... database  (use --database option)  enabled
... storage   (use --storage  option)  disabled
... archive   (use --archive  option)  not configured

Nothing has been done.
Validate the config with: kwbackup --config shop.yaml --check
```

### Явный набор действий

```bash
kwbackup --config shop.yaml --database
kwbackup --config shop.yaml --database --storage --archive   # всё за прогон
kwbackup --config shop.yaml --storage --mode copy
kwbackup --config shop.yaml --database --force               # даже если enabled: false

# короткие алиасы существуют: -c, -d, -s, -a, -m, -f
```

`-f/--force` игнорирует `enabled: false`, но не отменяет preflight.

`-m sync` — «выстрел в ногу» по явному запросу: целевой каталог приводится
к состоянию «только этот файл», всё остальное в нём удаляется. Для БД это
делается отдельно для каждого класса, для архива — для всего каталога.
Ротация в этом режиме не работает; пояснение видно с `--verbose`/`--debug`.

### Один проект — несколько конфигов

Каждый конфиг — отдельный `project`. Блокировки и логи не пересекаются,
разные задачи могут идти параллельно:

```bash
kwbackup --config /etc/kwbackup/shop.yaml --database &
kwbackup --config /etc/kwbackup/blog.yaml --database &
```

Один и тот же проект параллельно не запустится: второй процесс получит
код 4 (`already running (pid=..., since=..., config=...)`).

### Storage: copy каждый день, sync раз в месяц

`sync` — зеркало: удаляет в облаке то, чего нет в источнике. Ежедневный
прогон с ним рискован (пустой каталог, не смонтированный диск), поэтому
поставьте `copy` один раз на хосте:

```yaml
# /etc/kwbackup/defaults.yaml
storage:
  mode: copy
```

Раз в месяц имеет смысл прогнать зеркало, чтобы убрать удалённое локально:

```cron
30 4 1 * * root set -a; . /etc/default/kwbackup; set +a; \
    /usr/bin/kwbackup -c /etc/kwbackup/shop.yaml --storage -m sync
```

`-m/--mode` глобальный для запуска: `-m sync` вместе с `--database --archive`
сделает зеркалом и их. Поэтому месячный `sync` запускайте только с
`--storage`. Пустой каталог-источник `sync` отвергает, пока не задан
`storage.allow_empty: true`.

## Хуки до и после

У каждой задачи сценария можно задать `before` и `after` — массивы команд
для `/bin/sh -c`:

```yaml
database:
  before: ["redis-cli BGSAVE"]
  after:  ["redis-cli CONFIG REWRITE"]
archive:
  before: ["touch /srv/app/maintenance.on"]
  after:  ["rm -f /srv/app/maintenance.on"]
```

Окружение хука: `KWBACKUP_PROJECT`, `KWBACKUP_ACTION`, `KWBACKUP_PHASE`,
`KWBACKUP_STATUS` (`starting` для before, `ok`/`failed` для after),
`KWBACKUP_TASK_FILE`.

* `before` — по порядку, первый сбой отменяет задачу (код 1);
* `after` — всегда, включая упавший `before`; его ошибки только предупреждают;
* таймаут — `runtime.hook_timeout`, по умолчанию 60 с;
* `--dry-run` и выключенная задача хуки не запускают; при занятой блокировке
  `after` не вызывается.

## Инкрементальный бэкап (`incremental`)

Цепочки GNU tar: раз в месяц — полный дамп, каждый следующий день —
инкремент к нему. Без сжатия, обычные `.tar`.

```yaml
incremental:
  enabled: true
  name: data                        # по умолчанию = project
  root: /srv/data
  include: ["*"]
  exclude: ["*.tmp"]
  initial_day: 1                    # день месяца, с которого начинается цепочка (1..28)
  snapshot: /var/lib/kwbackup/data.snar   # обязателен при enabled: true
  targets: [s3a:/INCR]
  retention: {keep: 3}              # число цепочек (месяцев)
```

Имена файлов:

```
<target>/data_2026-11_full_2026-11-01-03-30-00.tar
<target>/data_2026-11_inc_2026-11-02-03-30-00.tar
<target>/data_2026-11_inc_2026-11-03-03-30-00.tar
```

Цепочка — это месяц. Запуск относится к текущему месяцу, если
`today.day >= initial_day`, иначе к предыдущему: при `initial_day: 25`
с 25 ноября по 24 декабря инкременты идут в цепочку `2026-11`, 25 декабря
начинается `2026-12`. Первый запуск в цепочке делает полный дамп
(`tar --level=0`), остальные — инкременты (`tar -g`).

`snapshot` — файл состояния GNU tar; без него цепочка не строится, поэтому
ключ обязателен. Он локальный, в облако не заливается. Если снапшот потерян,
следующий запуск сделает новый полный дамп, а восстановление возьмёт самый
новый `full_*` в цепочке и инкременты после него.

Ротация считает цепочки: `{keep: 3}` — три последних месяца,
`{max_age: 180d}` — всё, что старше. Удаляется цепочка целиком; отдельный
инкремент без полного не остаётся никогда.

Восстановление:

```bash
kwbackup -c data.yaml --restore --incremental --to /srv/restore
kwbackup -c data.yaml --restore --incremental --to /srv/restore --at 2026-11-10
```

Без `--at` восстанавливается последнее состояние, с `--at` — на дату
(`YYYY-MM` — вся цепочка, `YYYY-MM-DD` — слои до дня включительно). Удалённые
между слоями файлы исчезают из результата: GNU tar записывает удаления
в цепочку.

## Vault: версионный бэкап файлов

Задача `vault` хранит файлы слоями: TAR-тома + JSON-манифесты, история
вечная. Ежедневный запуск идёт по журналу изменений, полный скан нужен
только для первого base и для `--reconcile`.

```bash
# первый запуск: base (полный скан)
kwbackup -c /etc/kwbackup/storage.yaml --vault --full

# ежедневно: вчерашний журнал, только изменения
kwbackup -c /etc/kwbackup/storage.yaml --vault

# сверка без нового base (если журнал потерялся/сомнительный)
kwbackup -c /etc/kwbackup/storage.yaml --vault --reconcile
```

Что не бэкапить — `vault.exclude` (glob по пути, basename или родителю):

```yaml
vault:
  exclude: [".state", "47news_state.sqlite", "*.tmp"]
```

Каталог из `exclude` не обходится; журнальные события для таких путей
игнорируются. Если `state_dir`/`temp_dir`/журнал лежат внутри `root`, их
нужно исключить явно, иначе `--check` и запуск завершатся кодом 1.

Журнал пишет приложение: `YYYY-MM-DD.journal.txt` в каталоге
`vault.journal.path`, строки `ts<TAB>ADDED|MODIFIED|DELETED<TAB>path`.
После успешной заливки файл переименовывается в `.done`; сбой оставляет его
на месте.

Тома TAR — по `volume_size` (2 ГБ по умолчанию), файлы не режутся. Сжатия
нет: фото/видео уже сжаты, а смещения в манифесте требуют обычного TAR.

Восстановление:

```bash
# пробный прогон: найти цепочку и посчитать файлы, ничего не качать
kwbackup -c /etc/kwbackup/storage.yaml --restore --vault --dry-run \
    --to /srv/restore

# последнее состояние
kwbackup -c /etc/kwbackup/storage.yaml --restore --vault --to /srv/restore

# состояние на 17 ноября (последний base <= даты + слои после него)
kwbackup -c /etc/kwbackup/storage.yaml --restore --vault --to /srv/restore \
    --at 2026-11-17

# с конкретного таргета
kwbackup -c /etc/kwbackup/storage.yaml --restore --vault --from nas \
    --to /srv/restore
```

Порядок задаётся `restore`: `order: desc` — свежие слои первыми
(аварийный сценарий «сначала текущий месяц»), `priority` — порядок каталогов
внутри слоя. Удалённые файлы не восстанавливаются; права, владелец и mtime
берутся из манифеста.

Отчёты:

```bash
kwbackup -c /etc/kwbackup/storage.yaml --list
kwbackup -c /etc/kwbackup/storage.yaml --verify
kwbackup -c /etc/kwbackup/storage.yaml --verify --all   # все цепочки
```

Восстановление печатает план: target и дату, число манифестов в индексе,
слои, размер состояния, порядок сортировки, число нужных томов и объём.
`--dry-run` останавливается перед скачиванием томов:

```
vault restore: target s3a:/VAULT, at latest
vault restore: reading manifest index
vault restore: 2 manifest(s) in the index
vault restore: state latest: 23912 file(s), 6.3 GiB
vault restore: sorting 5 volume(s)
dry-run: would download 5 volume(s), 6.3 GiB
```

В терминале числа и размеры — жёлтые, успешные итоги (`done`, `restore`,
имя задачи) — зелёные, ошибки и проблемные места — красные. Это работает
и в логах (INFO), и в отчётах `--check`/`--list`/`--verify`; в `log.file`
и syslog ANSI не попадают. `--list` показывает base
(`FULL`), слои, даты источников, число файлов и томов, размеры и
предупреждает о пропущенных днях. `--verify` проверяет
манифесты, наличие и размеры томов и корректность смещений; код 5 — проблема.
Содержимое файлов не перечитывается (это отдельный, очень дорогой режим).

## Расписание

### systemd

```bash
sudo cp examples/systemd/kwbackup.service examples/systemd/kwbackup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kwbackup.timer

sudo cp examples/systemd/kwbackup-verify.* /etc/systemd/system/
sudo systemctl enable --now kwbackup-verify.timer
```

`OnCalendar=*-*-* 03:30` + `Persistent=true` — пропущенный из-за выключенного
хоста запуск выполнится при загрузке. `RandomizedDelaySec` разводит проекты
по времени.

Ненулевой код возврата делает unit `failed`: `systemctl --failed` и
`journalctl -u kwbackup` показывают причину. Уведомления — через
`OnFailure=` на свой сервис.

### cron

```bash
sudo install -m 644 examples/cron/kwbackup /etc/cron.d/kwbackup
```

Секреты в cron не наследуются: в примере они подхватываются через
`set -a; . /etc/default/kwbackup; set +a` — без `set -a` присваивания
останутся переменными оболочки и не попадут в окружение процесса.
Файлы в `/etc/cron.d` должны быть `0644`,
принадлежать root и не иметь точки в имени.

## Подробность вывода

| Режим | Терминал (stderr) | Стоки (`file`, `syslog`) | rclone |
|---|---|---|---|
| тихий (по умолчанию) | только предупреждения и ошибки | `log.level` | молчит |
| `--verbose` / `runtime.verbose: true` | `info` | `log.level` | `--progress`, вывод идёт прямо в терминал |
| `--debug` / `runtime.debug: true` | `debug`: каждая команда (включает verbose) | `log.level` | `--progress`, вывод в терминал |
| `--dry-run` | `info` (печатается план) | `log.level` | ничего не запускается |

Уровень стоков задаётся только `log.level`; `verbose`/`debug` — про консоль.
Хотите команды в файле или в syslog — `log.level: debug`.

Во время упаковки или дампа `--verbose` рисует живую строку (только если
stderr — терминал):

```
  packing 1.2 GiB, 84 MiB/s (site_2026-11-15-18-39-52.tar.zst)
  packing 1.2 GiB, 84 MiB/s, 35% (site_2026-11-15-18-39-52.tar.zst)   # --debug
```

Длинное имя файла сокращается многоточием, чтобы строка не переносилась, а
короткая строка добивается пробелами — иначе от предыдущей, более длинной,
остался бы хвост. Размер — уже записанный файл (для дампа и архива сжатый). Процент считается
по исходному объёму и есть только в `debug`: дерево обходится второй раз,
а прочитанное последней стадией берётся из `/proc/<pid>/io`. В cron/systemd
строка не печатается — журнал остаётся чистым, итоговый размер виден в
строке `packed`.

Фоновый запуск из cron/systemd по умолчанию ничего не пишет в терминал, кроме
предупреждений и ошибок; ход работы — в `log.file` или в syslog. При `--verbose`/`--debug`
rclone запускается с наследованием терминала, поэтому прогресс и статистика
видны сразу; без флагов его вывод захватывается и попадает в сообщение только
при ошибке.

### Уведомления

Один запуск — одно сообщение. Email, вебхук или Telegram (можно всё сразу):

```yaml
notify:
  on: [failure]                # failure | success | always; [] или null = выкл.
  retries: 0
  retry_delay: 3
  timeout: 10
  mail:
    to: [admin@example.com]
    from: kwbackup@host
    smtp_host: localhost
    port: 25
    tls: false
    user_env: SMTP_USER        # имена переменных, не значения
    password_env: SMTP_PASS
  webhook:
    url: https://monitor.example/hook
    headers: {Authorization: "Bearer ${HOOK_TOKEN}"}
  telegram:
    token_env: TG_TOKEN
    chat_id: "-1001234567890"
```

Почта уходит через `smtplib` напрямую на `smtp_host`: нужен доступный
SMTP-сервер (свой MTA на `localhost:25` или внешний с `tls: true` и AUTH).
Ни `mail`, ни `sendmail` не используются.

Сообщение:

```
[kwbackup] shop: backup failed (exit 1)

task:      /etc/kwbackup/shop.yaml
project:   shop
host:      web1
kind:      backup
status:    FAILED, exit 1
duration:  42.3s
actions:
  database: 2 db(s), 2 uploaded, 30 MiB
failures:
  archive → upstream blacktower1: rclone exited with 1
```

Командных строк, stderr и ключей в нём нет: причина — короткая и безопасная
(`rclone exited with 1`, `No space left on device`, `environment variable X is
not set`), а сломанное называется по имени (БД, апстрим). Ошибка доставки не
меняет код возврата бэкапа. Пустой `on`, отсутствие каналов или секции —
полная тишина, в том числе в логе.

### syslog / journald

```yaml
log:
  level: info
  syslog: true           # /dev/log; при его отсутствии — localhost:514/udp
  facility: daemon
```

```bash
journalctl -t kwbackup -f                    # следить
journalctl -t kwbackup --since today -p warning
```

`file` при этом можно убрать — сообщения пойдут только в syslog. В контейнере
без `/dev/log` укажите сокет явно: `syslog: /run/syslog.sock`. `log.level`
действует на syslog так же, как на файл, а `runtime.verbose`/`debug` — только
на консоль.

## Мониторинг

### `--list` — что реально лежит в облаке

```bash
kwbackup --config shop.yaml --list
```

```
DATABASE shop  ->  beget:/shop/DB
  DAILY     8 files  2026-11-04 .. 2026-11-11  last 3.1 GiB  (keep 8)
          ⚠ gap: 2026-10-28
  WEEKLY    6 files  2026-09-29 .. 2026-11-10  last 3.1 GiB  (keep 6)
  MONTHLY  12 files  2025-12-01 .. 2026-11-01  last 2.9 GiB  (keep 12)

STORAGE  ->  beget:/STORAGE  (sync)
  /srv/app/uploads
      1204 files, 8.4 GiB, newest 2026-11-11 03:31  ->  beget:/STORAGE/uploads
```

Код возврата 5, если: DAILY пуст, последняя DAILY-копия старше суток,
источник storage пуст в облаке, архив пуст. Это готовый watchdog.

### `--verify` — копия действительно читается

```bash
kwbackup --config shop.yaml --verify          # по последней копии каждого класса
kwbackup --config shop.yaml --verify --all    # по всем копиям (долго и дорого)
```

Что проверяется: файл скачивается, размер совпадает с размером в облаке,
`zstd -t` / `gzip -t` / `rar t` проходит, а распакованное содержимое похоже
на дамп (заголовок `MySQL dump`, `PostgreSQL database dump`, …) или на
читаемый tar. Любая проблема — код 5 и строка `✗`.

Скачанные файлы удаляются сразу; для больших БД нужен запас в `temp_dir`.

## Восстановление

Только в каталог, который указан в `--to`. В живую базу инструмент не пишет:
дамп скачивается как есть, импорт — осознанный ручной шаг. Каталог не
очищается, поэтому восстанавливайте в пустой.

У каждой секции есть рубильник:

```yaml
database:
  restore:
    enabled: false     # по умолчанию true
```

`restore.enabled: false` запрещает `--restore` для этой секции (код 1);
обход — `--force`, если действительно надо.

### База данных

```bash
# самая свежая копия (ищется по всем классам: MONTHLY/DAILY/WEEKLY)
kwbackup --config shop.yaml --restore --database --to /srv/restore

# конкретная дата (подстрока имени файла)
kwbackup --config shop.yaml --restore --database --at 2026-11-09 --to /srv/restore
```

В каталоге появится файл в том виде, в котором он лежит в облаке:
`shop_2026-11-09-03-30-00.sql.zst` (или `.sql.gz`, `.sql.rar`, `.sql`).
Дальше — вручную, любым привычным способом:

```bash
# mysql / mariadb
zstd -dc shop_2026-11-09-03-30-00.sql.zst | mysql -h localhost shop

# postgres
zstd -dc shop_2026-11-09-03-30-00.sql.zst | psql -v ON_ERROR_STOP=1 -d shop

# sqlite
zstd -dc app_2026-11-09-03-30-00.sql.zst | sqlite3 /var/lib/app/app.db
```

Почему так: импорт в живую БД — единственная необратимая операция, и цена
ошибки выше, чем удобство. Заодно видно, в какую именно базу идёт заливка.
Импорт **не пересоздаёт** базу: существующие таблицы перезапишутся тем, что
есть в дампе. Для чистого восстановления создайте пустую БД.

### Архив

```bash
kwbackup --config site.yaml --restore --archive --to /srv/restore --at 2026-11-10
```

tar-поток распаковывается в каталог; `.rar` — через `rar x`.
Права и владельцы файлов восстанавливаются такими, какие были в архиве.

### Storage

```bash
kwbackup --config uploads.yaml --restore --storage --to /srv/restore
```

Используется `rclone copy` (никогда не `sync`): в локальном каталоге ничего
не удаляется.

## Логи и блокировки

`log.file` никто не ротирует: добавьте logrotate, пример —
[`examples/logrotate.d/kwbackup`](../examples/logrotate.d/kwbackup).
Файл открывается заново на каждый запуск, сигналы не нужны.

```
/var/log/kwbackup/<project>.log          # если задан log.file (или log.syslog)
/run/kwbackup/<project>.db.<db>.lock
/run/kwbackup/<project>.storage.lock
/run/kwbackup/<project>.archive.lock
```

Если `/run/kwbackup` недоступен для записи — `/tmp/kwbackup-<uid>/`.
Блокировка — `flock`: снимается ядром при завершении процесса, в том числе
после `kill -9`. «Протухших» флагов не бывает, `--reset` не нужен.

## Разбор ошибок

| Симптом | Причина | Что делать |
|---|---|---|
| предупреждение `unknown key, ignored` | опечатка или лишний ключ | ключ проигнорирован, запуск продолжается; сверить с `docs/configuration.md` |
| код 2, `environment variable X is not set` | переменная не попала в окружение процесса | `EnvironmentFile=` в systemd; `set -a; . /etc/default/kwbackup; set +a` в cron; `sudo -E` |
| код 2, `defaults file not found` | указан несуществующий `--defaults` | поправить путь |
| конфиг «дополняется» не тем, что вы ждёте | сработал `/etc/kwbackup/defaults.yaml` | смотреть строку `defaults:` в `--check` |
| код 2, `at least one class must be configured` | пустой `retention` | включить класс |
| код 4, `already running` | параллельный запуск того же проекта | дождаться; проверить `ps` и lock-файл |
| код 1, `binary not found in PATH` | нет `rclone`/`zstd`/`tar`/дампа | установить пакет |
| код 1, `rclone config not found` | неверный `rclone.config` | указать существующий файл |
| код 1, `archive: no files matched the include list` | `include` не совпал ни с чем | проверить маски относительно `root` |
| код 1, `dump produced an empty file` | дамп упал молча | запустить команду дампа вручную, смотреть stderr |
| код 5, `empty` | копий нет | проверить расписание и первый запуск |
| код 5, `newest DAILY copy is N days old` | бэкап не выполняется | смотреть лог и `systemctl --failed` |
| код 5, `✗ ... unsupported format` | файл в облаке повреждён | удалить и перезапустить бэкап |
| в логе `rclone` пусто, но копий нет | каталога ещё нет в облаке | нормально при первом запуске |

## Безопасность

* Скрипт работает от root (нужен доступ к сокету БД и `/etc/kwbackup`).
  systemd-юнит в примерах включает `NoNewPrivileges`, `ProtectSystem=full`,
  `ProtectHome=read-only`.
* Ключи S3-бакета (BEGET, minio-подобные) — единственный рабочий механизм —
  флаги `--s3-access-key-id` / `--s3-secret-access-key` в `rclone.args`.
  Файл с ними — `0600`; ключи можно подставлять из `/etc/default/kwbackup`.
  Пароли БД — через `password_env`.
* `/etc/kwbackup/defaults.yaml` может содержать ключи для единственного бакета:
  тогда это тоже файл `0600`.
* Ключи на аккаунт можно держать в `rclone.conf` — это штатно; флаги
  в `rclone.args` их перекрывают для конкретного бакета.
* `--restore` не пишет в живые базы и не удаляет файлы: дамп скачивается
  как есть, импорт делает человек.
* **Копии не шифруются на клиенте**: в облако уходит обычный `.sql.zst`
  или `.tar.zst`. Шифрование на диске — ответственность провайдера
  (SSE) либо отдельного шага (`zstd | gpg`); в текущей версии не встроено.
  Если это требование — держите бакет приватным и включите серверное
  шифрование у провайдера.
* `--verify` и `--list` только читают и скачивают, ничего не меняют.

## Коды возврата

| Код | Значение |
|---|---|
| 0 | успех |
| 1 | ошибка выполнения (дамп, сжатие, rclone, preflight) |
| 2 | ошибка конфига |
| 3 | ошибка аргументов |
| 4 | задача уже выполняется (lock) |
| 5 | `--list`/`--verify` нашли проблему: копий нет, они стары или повреждены |

`--list` и `--verify` можно ставить в cron/systemd отдельным заданием: код 5
означает «пора смотреть руками».
