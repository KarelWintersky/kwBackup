# kwBackup

Резервные копии сервера в S3-совместимое облако (или любой remote `rclone`):
базы данных, каталоги, архивы и версионная история файлов. Конфиг — YAML,
запуск — одна команда из cron или systemd. Один исполняемый файл на
Python 3.10+, без внешних зависимостей.

Проект вырос из bash-скрипта `kwbackup.sh` 0.9.0: остались GFS-ротация и
заливка через `rclone`, добавились валидируемый конфиг, блокировки, проверка
копий и восстановление.

## Пять задач

| Задача | Что делает |
|---|---|
| `database` | дамп (`mysqldump`/`mariadb-dump`/`pg_dump`/`sqlite3`) → сжатие → заливка |
| `storage` | `rclone sync`/`copy` каталогов |
| `archive` | `tar` + компрессор → заливка |
| `incremental` | цепочки GNU tar: полный раз в месяц, дальше инкременты |
| `vault` | версионные TAR-слои с манифестами — история на любую дату |

Раскладка в облаке:

```
<target>/DAILY|WEEKLY|MONTHLY/<db>_<ts>.sql.zst   одна база
<target>/<db>/DAILY/...                            несколько баз
<target>/<name>_<ts>.tar.zst                       архив
<target>/<dest>/...                                storage
<target>/<UTC>.manifest.json + -part-NNN.tar       vault
```

Ротация удаляет только «свои» файлы — `<db>_…` для дампов, `<name>_…` для
архивов, — поэтому один target можно делить между проектами. Ошибка на одном
апстриме не отменяет остальные: в конце будет ненулевой код и список того,
что упало.

## Установка

```bash
sudo dpkg -i kwbackup_<version>_all.deb
kwbackup --version
```

Пакет ставит `/usr/bin/kwbackup`, conffile `/etc/kwbackup/defaults.yaml` и
примеры в `/usr/share/doc/kwbackup/examples/`. Из исходников — `make build`
(`dist/kwbackup` + `.deb`), тесты — `make test`.

Нужны Linux, `flock`, `tar` и `rclone`. По типу базы — соответствующий клиент,
по компрессору — `zstd`/`gzip`/`pigz`/`rar`.

## Первый бэкап

Понадобятся три файла: настройки хоста, секреты и задача. Ниже — минимальный
сценарий для одной базы в бакет BEGET, всё от root (`sudo -s`).

```bash
umask 077
install -d -m 700 /etc/kwbackup /var/log/kwbackup

# 1. remote rclone: имя remote = ключ upstream в конфиге задачи.
#    У BEGET ключи выдаются на бакет и уходят флагами, поэтому в rclone.conf
#    их можно не писать.
rclone config          # beget: s3, provider Other, endpoint из панели
install -m 600 /root/.config/rclone/rclone.conf /etc/kwbackup/rclone_beget.conf

# 2. настройки хоста: тип БД, пользователь, лог, глубина хранения
install -m 644 debian/kwbackup.defaults.yaml /etc/kwbackup/defaults.yaml
vi /etc/kwbackup/defaults.yaml

# 3. секреты — в окружение, не в конфиг
install -m 600 debian/kwbackup.default /etc/default/kwbackup
vi /etc/default/kwbackup     # MYSQL_PWD, BEGET_SHOP_KEY, BEGET_SHOP_SECRET

# 4. задача
cat > /etc/kwbackup/shop.yaml <<'EOF'
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
  # retention, compressor и тип БД — из /etc/kwbackup/defaults.yaml
EOF

# 5. проверка, план, запуск
set -a; . /etc/default/kwbackup; set +a
kwbackup -c /etc/kwbackup/shop.yaml --check       # 0 — можно запускать
kwbackup -c /etc/kwbackup/shop.yaml --dry-run
kwbackup -c /etc/kwbackup/shop.yaml --database
kwbackup -c /etc/kwbackup/shop.yaml --list
```

Несколько замечаний:

* после `.deb` оба conffile уже на месте — шаги 2 и 3 сводятся к правке;
* `--check` возвращает 0, только если конфиг валиден и всё на месте:
  бинарники, пути, rclone-конфиг, переменные окружения. 2 — ошибка конфига,
  1 — проблемы окружения;
* `--dry-run` печатает точные команды и ничего не заливает;
* `--list` показывает, что реально лежит в облаке.

## Восстановление

Только в каталог, который указал сам. В живую базу инструмент не пишет:
дамп скачивается в том виде, в каком лежит в облаке, импорт — ручной шаг.

```bash
# последняя копия
kwbackup -c shop.yaml --restore --database --to /srv/restore

# состояние на дату
kwbackup -c shop.yaml --restore --database --to /srv/restore --at 2026-11-09

kwbackup -c site.yaml  --restore --archive  --to /srv/restore
kwbackup -c files.yaml --restore --storage  --to /srv/restore
kwbackup -c files.yaml --restore --vault    --to /srv/restore --at 2026-11-17

# импорт дампа — вручную
zstd -dc /srv/restore/shop_2026-11-09-03-30-00.sql.zst | mysql shop
```

Каталог `--to` не очищается — восстанавливайте в пустой. У каждой секции есть
`restore.enabled` (по умолчанию `true`): `false` запрещает восстановление
на этом хосте, обход — `--force`. У `vault` в том же блоке живут
`order`/`priority`/`chown`.

## Расписание

```cron
30 3 * * * root set -a; . /etc/default/kwbackup; set +a; \
    /usr/bin/kwbackup -c /etc/kwbackup/shop.yaml --database
```

Готовые варианты — [`examples/cron/kwbackup`](examples/cron/kwbackup) и
[`examples/systemd/`](examples/systemd). `--list` и `--verify` можно ставить
отдельным заданием: код 5 означает «пора смотреть руками».

## Конфиг

Два слоя: `/etc/kwbackup/defaults.yaml` — общее для хоста (лог, `rclone.args`,
тип БД, глубина хранения), файл задачи — `project`, `upstreams`, `targets`,
`databases`/`sources`/`root`. Списки `rclone.args`, `database.args`,
`*_compressor_args` и `archive.exclude` дополняются, остальные значения
задача перекрывает.

* `retention`: на класс `daily`/`weekly`/`monthly` — `keep: N` или
  `max_age: 7d`. DAILY создаётся каждый запуск, WEEKLY по понедельникам,
  MONTHLY 1-го числа.
* `targets` — `upstream:path`; адреса, ключи и `rclone`-конфиги живут
  в `upstreams`.
* `${ENV}` подставляется в любые строковые значения; секреты удобно держать
  в `/etc/default/kwbackup` и ссылаться на них как `${ИМЯ}`.
* Неизвестный ключ — предупреждение, а не ошибка: опечатка видна в логе,
  запуск продолжается.
* `vault` требует, чтобы `state_dir`, `temp_dir` и журнал не лежали внутри
  `root` без `exclude` — иначе `--check` падает.

Полный справочник: [`docs/configuration.md`](docs/configuration.md).

## Надёжность

* **Блокировки**: `flock` на `(проект, задача)`; снимается ядром даже после
  `kill -9`, «протухших» флагов не бывает.
* **Коды возврата**: 0 — успех, 1 — ошибка выполнения, 2 — конфиг,
  3 — аргументы, 4 — задача уже идёт, 5 — `--list`/`--verify` нашли проблему.
* **Проверка**: `--verify` скачивает последнюю копию и убеждается, что она
  читается и похожа на дамп; `--list` показывает дыры в истории.
* **Уведомления**: mail, webhook, Telegram — одно сообщение на прогон, без
  команд и ключей.
* **Хуки** `before`/`after` у каждой задачи: остановить сервис, снять дамп
  Redis, включить maintenance-режим.
* **Вывод**: числа жёлтым, успех зелёным, ошибки красным; в файле лога
  и syslog цветов нет.

## Документация

| | |
|---|---|
| [docs/usage.md](docs/usage.md) | сценарии, расписание, мониторинг, восстановление, разбор ошибок |
| [docs/configuration.md](docs/configuration.md) | все ключи и значения по умолчанию |
| [docs/migration.md](docs/migration.md) | переезд с `kwbackup.sh` |
| [examples/](examples) | конфиги под каждый сценарий: БД, storage, архив, инкременты, vault |
| [CHANGELOG.md](CHANGELOG.md) | что менялось между версиями |

## Разработка

Код собирается конкатенацией `src/*.py` в один файл; все `import` живут
в `src/00_header.py`, поэтому нет ни бандлера, ни рерайта импортов.

```bash
python3 build.py --test     # сборка + py_compile + selftest
python3 tests/e2e.py        # сквозные тесты на фейковом rclone
make dist                   # dist/kwbackup с версией из subject коммита
make build                  # ещё и .deb
```

Версия — subject текущего коммита (`0.99.x`); `make check-version` сверяет
его с `debian/changelog`. Релизов и тегов пока не было.

## Legacy и лицензия

`kwbackup.sh` и [`examples/legacy_bash_version/`](examples/legacy_bash_version) —
старая bash-версия, оставлена для справки и миграции. Обе версии нельзя
запускать на один проект одновременно: у них разные механизмы блокировок.

MIT, см. [LICENSE](LICENSE).
