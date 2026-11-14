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
sudo install -m 600 examples/defaults.yaml /etc/kwbackup/defaults.yaml
# задача: project, target, базы, ключи бакета
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

`--check` печатает разобранный конфиг и **preflight**: найден ли
`rclone`, `mysqldump`/`pg_dump`, `tar`, компрессор; существует ли
`rclone.config`; доступны ли источники storage, `root` архива, `temp_dir`;
задана ли переменная с паролем. Предупреждения не меняют код возврата,
но при реальном запуске preflight уже блокирует работу.

```
kwBackup: Backup tool over rclone: database dumps, file storage, archives (Version: 0.99.43)

  config     /etc/kwbackup/shop.yaml
  defaults   /etc/kwbackup/defaults.yaml
  project    shop

  rclone
    remote     beget
    config     /etc/kwbackup/rclone_beget.conf
    s3         5 flag(s)
               s3-access-key-id, s3-chunk-size, s3-no-check-bucket
               s3-secret-access-key, s3-upload-cutoff

  database  enabled (mariadb)
    mode       copy
    target     beget:/DB
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
kwbackup --config /etc/kwbackup/shop.yaml --database --storage --archive
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

| Флаг | Терминал (stderr) | Лог-файл | rclone |
|---|---|---|---|
| без флагов | только предупреждения и ошибки | `info` | молчит |
| `--verbose` | `info` | `info` | `--progress`, вывод идёт прямо в терминал |
| `--debug` | `debug`: каждая команда и её аргументы | `debug` | `--progress`, вывод в терминал |
| `--dry-run` | `info` (печатается план) | `info` | ничего не запускается |

Фоновый запуск из cron/systemd по умолчанию ничего не пишет в журнал, кроме
предупреждений и ошибок; ход работы — в `log.file`. При `--verbose`/`--debug`
rclone запускается с наследованием терминала, поэтому прогресс и статистика
видны сразу; без флагов его вывод захватывается и попадает в сообщение только
при ошибке.

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

Всегда сначала в каталог, в живую БД — отдельным явным шагом.

### База данных

```bash
# самая свежая копия (ищется по всем классам: MONTHLY/DAILY/WEEKLY)
kwbackup --config shop.yaml --restore --database --to /srv/restore

# конкретная дата (подстрока имени файла)
kwbackup --config shop.yaml --restore --database --at 2026-11-09 --to /srv/restore
```

В каталоге появится `shop_2026-11-09-03-30-00.sql` — уже распакованный.

Заливка в живую БД — только с подтверждением:

```bash
kwbackup --config shop.yaml --restore --database --at 2026-11-09 --into-db shop_test --yes
```

Без `--yes` команда откажется (код 1). Для mysql используется `mysql`,
для postgres — `psql -v ON_ERROR_STOP=1`, для sqlite — `sqlite3`.
Заливка **не пересоздаёт** базу: существующие таблицы будут перезаписаны
тем, что есть в дампе. Для чистого восстановления создайте пустую БД
и передайте её имя в `--into-db`.

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

```
/var/log/kwbackup/<project>.log          # если задан log.file
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
* `--into-db` — единственная разрушительная операция, требует `--yes`.
* **Копии не шифруются на клиенте**: в облако уходит обычный `.sql.zst`
  или `.tar.zst`. Шифрование на диске — ответственность провайдера
  (SSE) либо отдельного шага (`zstd | gpg`); в текущей версии не встроено.
  Если это требование — держите бакет приватным и включите серверное
  шифрование у провайдера.
* `--verify` и `--list` только читают и скачивают, ничего не меняют.
