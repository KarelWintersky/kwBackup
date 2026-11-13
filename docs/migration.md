# Миграция со старого bash-конфига

```bash
kwbackup migrate /etc/kwbackup/_config.conf -o /etc/kwbackup/shop.yaml -p shop
kwbackup --config /etc/kwbackup/shop.yaml --check
kwbackup --config /etc/kwbackup/shop.yaml --dry-run
```

Без `-o` результат печатается в stdout. Существующий файл не перезаписывается
без `-f`. Имя проекта (`-p`) нужно, потому что оно попадает в lock-файлы и
логи; без него берётся из имени файла (если это `_config.conf` — из имени
каталога).

## Как читается старый конфиг

Старый конфиг — исполняемый bash, поэтому единственный надёжный способ
получить значения — спросить сам bash:

```bash
bash -c 'set -a; . "$1"; shift; for v in "$@"; do declare -p "$v"; done; exit 0' \
     -- old.conf TEMP_PATH LOG_FILE ...
```

Читаются только известные имена переменных (список фиксирован), массивы
разбираются из `declare -a`. **Файл всё равно выполняется bash'ом**, как и
раньше: это та же модель доверия, что у `kwbackup.sh`. Не запускайте
конвертер от root на конфиге, которому не доверяете.

## Соответствие ключей

| Старое (`kwbackup.sh`) | Новое (YAML) |
|---|---|
| `TEMP_PATH` | `runtime.temp_dir` (`/tmp` → `/tmp/kwbackup`) |
| `LOG_FILE` | `log.file` |
| `RCLONE_CONFIG` | `rclone.config` |
| `RCLONE_PROVIDER` | `rclone.remote` |
| `RCLONE_CUSTOM_OPTIONS` (массив) | `rclone.args` |
| `ENABLE_BACKUP_DATABASE` | `database.enabled` |
| `DB_TYPE` | `database.type` |
| `DB_HOST` | `database.host` |
| `DB_USER` | `database.user` |
| `DATABASES` (массив или строка) | `database.databases` |
| `DATABASE_MYSQL_OPTIONS` / `PGSQL` / `SQLITE` | `database.args` (по типу БД) |
| `USE_ARCHIVER` | `database.compressor` (`zip`/`pigz` → `gzip`) |
| `DATABASE_ZSTD_OPTIONS` / `RAR_OPTIONS` | `database.compressor_args` |
| `DATABASE_CLOUD_CONTAINER` | `database.target` = `<remote>:<container>` |
| `DB_BACKUP_DAILY/WEEKLY/MONTHLY` | `retention.daily/weekly/monthly` (вкл/выкл) |
| `DB_MIN_AGE_DAILY/WEEKLY/MONTHLY` | `keep: N` (см. ниже) |
| `ENABLE_BACKUP_STORAGE` | `storage.enabled` |
| `STORAGE_SOURCES_ROOT` + `STORAGE_SOURCES` | `storage.sources` (полные пути) |
| `STORAGE_CLOUD_CONTAINER` | `storage.target` |
| `STORAGE_BACKUP_ALGO` | `storage.mode` |
| `ENABLE_BACKUP_ARCHIVE` | `archive.enabled` |
| `ARCHIVE_USE_COMPRESSOR` | `archive.compressor` |
| `ARCHIVE_ZSTD/GZIP/RAR_OPTIONS` | `archive.compressor_args` |
| `ARCHIVE_FILENAME` | `archive.name` (хвост `_<timestamp>` отрезается) |
| `ARCHIVE_ROOT` | `archive.root` |
| `ARCHIVE_INCLUDE_LIST` / `EXCLUDE_LIST` | `archive.include` / `exclude` |
| `ARCHIVE_CLOUD_CONTAINER` | `archive.target` |
| `ARCHIVE_ROTATION_PERIOD` | `archive.retention: {keep: N}` |

## Возраст → число копий

Приоритет в новых конфигах — количество (`keep`), поэтому старые окна
пересчитываются: `keep = round(возраст / период класса)`.

| Было | Период | Стало |
|---|---|---|
| `DB_MIN_AGE_DAILY=7d` | 1 день | `daily: {keep: 7}` |
| `DB_MIN_AGE_WEEKLY=43d` | 7 дней | `weekly: {keep: 6}` |
| `DB_MIN_AGE_MONTHLY=360d` | 30 дней | `monthly: {keep: 12}` |
| `ARCHIVE_ROTATION_PERIOD=71d` | 1 день | `retention: {keep: 71}` |

Исходное значение остаётся в комментарии рядом, чтобы было видно, откуда
взялось число:

```yaml
retention:
  weekly: {keep: 6}          # было max_age: 43d
```

Если возраст не разбирается (`7 days`, `1 month`), поле переносится как
`max_age` без пересчёта.

## Что не переносится

* `ARCHIVE_RAR_INCLUDE_LIST` / `ARCHIVE_RAR_EXCLUDE_LIST` — списки файлов
  в формате rar. Новые `include`/`exclude` работают с любым компрессором,
  но это маски, а не файлы со списками. В конфиг добавляется комментарий.
  Если списки были большими — перенесите маски вручную.
* Переменные `NOW`, `NOW_DOW`, `NOW_DAY` — вычислялись в старом конфиге,
  новая версия делает это сама.
* `RCLONE_CUSTOM_OPTIONS` с ключами доступа переносятся в `rclone.args` —
  это и есть рабочий механизм для BEGET (ключи на бакет, `env_auth = false`).
  Файл с ключами держите `0600`; ключи можно заменить на `${BEGET_SHOP_KEY}`
  из `/etc/default/kwbackup`.

## Что проверить после миграции

1. `--check`. Если в старом конфиге было пусто, конвертер оставит
   пусто и напишет комментарий:
   * `databases: []` → заполнить список БД;
   * `sources: []` → заполнить источники storage;
   * `root: ""` → указать каталог архива.
2. Секреты: ключи бакета — в `rclone.args` (файл `0600`) или через `${...}`
   из `/etc/default/kwbackup`; пароль БД — `password_env`.
3. `--dry-run`: план команд дампа и заливки.
4. `--list` **до** первого запуска: путь `target` должен указывать туда же,
   где лежат старые копии, иначе новая версия их не увидит и не будет
   учитывать в ротации. Проверьте раскладку:
   * одна БД — `<target>/DAILY/...`;
   * несколько БД — `<target>/<db>/DAILY/...`;
   * архив — `<target>/<name>_<timestamp>.tar.zst`.
5. Старые файлы с расширением `.sql.zstd` тоже распознаются: дата и имя
   берутся из имени файла, `--list` и ротация их видят.

## Параллельная работа версий

Не запускайте `kwbackup.sh` и `kwbackup` одновременно на один и тот же
проект: у них разные механизмы блокировок (`/dev/shm/kwbackup_*.flag`
против `flock` в `/run/kwbackup`). Они не увидят друг друга и могут писать
в одно и то же место.

Порядок переключения: остановить cron/systemd со старым скриптом →
смигрировать конфиг → проверить `--list` → включить новый таймер.
