# Справочник по конфигу

Полный пример со всеми ключами: [`examples/full.yaml`](../examples/full.yaml).
Проверка файла: `kwbackup -c FILE --check-config`.

Общие правила:

* Неизвестный ключ — ошибка с указанием пути (`config.database.databeses:
  unknown key`). Опечатка не пройдёт молча.
* `${ENV_VAR}` подставляется из окружения; если переменной нет — ошибка
  конфига с именем переменной. Экранирования нет: `${...}` всегда подстановка.
* Пустое значение (`key:` без значения) равно «не задано».
* Комментарии `#` и пустые строки разрешены.

Поддерживается подмножество YAML: блочные мапы и списки, flow-формы
`[a, b]` и `{k: v}`, кавычки, комментарии. Не поддерживаются (и явно
отвергаются с номером строки): якоря `&`/`*`, теги `!`, блочные строки `|`
и `>`, отступы табами, несколько документов `---`.

## Верхний уровень

| Ключ | Тип | Обяз. | По умолчанию | Смысл |
|---|---|---|---|---|
| `version` | int | да | — | всегда `1` |
| `project` | str | да | — | имя проекта: lock-файлы, логи, отчёты |
| `log` | map | нет | — | см. ниже |
| `runtime` | map | нет | — | см. ниже |
| `rclone` | map | да | — | см. ниже |
| `database` | map | нет | выключено | см. ниже |
| `storage` | map | нет | выключено | см. ниже |
| `archive` | map | нет | выключено | см. ниже |

## `log`

| Ключ | Тип | По умолчанию | Смысл |
|---|---|---|---|
| `level` | enum | `info` | `debug` \| `info` \| `warning` \| `error` |
| `file` | str | нет | путь к файлу лога; каталог создаётся |

Лог всегда идёт в stderr; `file` добавляет дублирование на диск.
`--verbose` и `--debug` поднимают уровень до `debug` независимо от конфига.

## `runtime`

| Ключ | Тип | По умолчанию | Смысл |
|---|---|---|---|
| `temp_dir` | str | `/tmp/kwbackup` | куда пишутся дампы/архивы перед заливкой |
| `keep_temp` | bool | `false` | `true` — не удалять локальный файл после заливки |

`temp_dir` должен быть на диске с запасом: одновременно живёт один файл
дампа или архива.

## `rclone`

| Ключ | Тип | По умолчанию | Смысл |
|---|---|---|---|
| `binary` | str | `rclone` | имя в PATH или полный путь |
| `config` | str | системный | `--config` для rclone |
| `remote` | str | **обязательно** | имя remote (`[beget]` в `rclone.conf`) |
| `args` | list | `[]` | добавляются к **каждому** вызову rclone |
| `env` | map | `{}` | переменные окружения для rclone |

`args` попадают и в `lsf`, и в `copyto`, и в `deletefile` — годится для
`--s3-no-check-bucket`, `--transfers`, `--bwlimit`.

```yaml
rclone:
  remote: beget
  args: ["--s3-no-check-bucket", "--transfers=4"]
  env:
    AWS_ACCESS_KEY_ID: ${RCLONE_KEY_ID}
    AWS_SECRET_ACCESS_KEY: ${RCLONE_SECRET}
```

## `database`

| Ключ | Тип | По умолчанию | Смысл |
|---|---|---|---|
| `enabled` | bool | `false` | включает сценарий |
| `type` | enum | `mysql` | `mysql` \| `mariadb` \| `postgres` \| `pgsql` \| `sqlite` |
| `host` | str | `localhost` | для sqlite — путь к файлу `.db` |
| `port` | int | не задан | если не задан — клиент использует свой |
| `user` | str | не задан | mysql: `--user`, postgres: `--username` |
| `password_env` | str | не задан | имя переменной окружения с паролем |
| `args` | list | см. ниже | опции дампа |
| `compressor` | enum | `zstd` | `zstd` \| `gzip` \| `pigz` \| `rar` \| `none` |
| `compressor_args` | list | `[]` | аргументы компрессора |
| `target` | str | обяз. при `enabled` | `remote:path` |
| `databases` | list | обяз. при `enabled` | имена БД (для sqlite — не используется) |
| `catch_up` | bool | `false` | догонять пропущенные weekly/monthly |
| `retention_mode` | enum | нет | `keep` \| `max_age`: запрет смешивать |
| `retention` | map | обяз. при `enabled` | см. «Ротация» |

### Дамп

| `type` | Команда | Опции по умолчанию |
|---|---|---|
| `mysql` | `mysqldump` | `--single-transaction --quick --no-tablespaces --skip-lock-tables` |
| `mariadb` | `mariadb-dump` | то же |
| `postgres`/`pgsql` | `pg_dump` | `--no-owner --no-privileges` |
| `sqlite` | `sqlite3 <host> .dump` | `args` не используются |

Пароли:

* `password_env: MYSQL_PWD` — значение переменной уходит в `MYSQL_PWD`
  (и дополнительно в `PGPASSWORD` для postgres).
* Если ключ не задан — используются штатные механизмы клиентов:
  `~/.my.cnf`, `~/.pgpass`.

### Куда кладётся

| Баз в конфиге | Путь |
|---|---|
| одна | `<target>/DAILY/<db>_<ts>.sql.zst` |
| несколько | `<target>/<db>/DAILY/<db>_<ts>.sql.zst` |

`<ts>` = `YYYY-MM-DD-HH-MM-SS` (локальное время), расширения:
`.sql.zst`, `.sql.gz`, `.sql`, `.sql.rar`.

## Ротация

Классы: `daily`, `weekly`, `monthly`. Каждый класс — либо `false`
(отключён), либо ровно одно правило:

```yaml
retention:
  daily:
    keep: 8          # оставить 8 самых новых файлов
  weekly:
    max_age: 43d     # удалять всё старше 43 дней
  monthly: false
```

| Правило | Смысл | Устойчивость |
|---|---|---|
| `keep: N` | N новейших копий | к пропускам и дублям: окно сдвигается, но N файлов остаётся |
| `max_age: Xs/m/h/d/w` | всё старше X удаляется | окно фиксировано по времени |

`keep` и `max_age` **взаимоисключающие**: оба сразу — ошибка конфига,
ни одного — тоже. Класс без ключа считается отключённым.

`retention_mode: keep | max_age` — если задан, то во всех классах
разрешено только указанное правило; смешивание становится ошибкой.

### Расписание

| Класс | Когда создаётся |
|---|---|
| DAILY | каждый запуск (если включён) |
| WEEKLY | дополнительно, если сегодня понедельник |
| MONTHLY | дополнительно, если сегодня 1-е число |

Один дамп на запуск: в понедельник файл уходит и в `DAILY`, и в `WEEKLY`;
1-го числа — в `DAILY`, `WEEKLY` (если понедельник) и `MONTHLY`.

`catch_up: true` меняет правило на «если за текущую ISO-неделю (или текущий
месяц) копии ещё нет, создать её». Хост, лежавший в понедельник, догонит
weekly при следующем запуске. По умолчанию выключено — строгий паритет
с bash-версией.

### Пересчёт старых окон

| Было (`--min-age`) | Стало (`keep`) |
|---|---|
| `DB_MIN_AGE_DAILY=7d` | `daily: {keep: 7}` |
| `DB_MIN_AGE_WEEKLY=43d` | `weekly: {keep: 6}` |
| `DB_MIN_AGE_MONTHLY=360d` | `monthly: {keep: 12}` |
| `ARCHIVE_ROTATION_PERIOD=71d` | `retention: {keep: 71}` |

## `storage`

| Ключ | Тип | По умолчанию | Смысл |
|---|---|---|---|
| `enabled` | bool | `false` | включает сценарий |
| `mode` | enum | `sync` | `sync` — зеркало; `copy` — только добавлять |
| `target` | str | обяз. при `enabled` | `remote:path` |
| `sources` | list | обяз. при `enabled` | источники, см. ниже |

Источник — строка или мапа:

```yaml
storage:
  sources:
    - /srv/app/uploads                 # dest = "uploads" (basename)
    - path: /srv/app/static            # dest = "static"
      dest: static
    - path: /srv/app/data
      dest: ""                         # весь каталог в корень target
```

`sync` удаляет в облаке файлы, которых больше нет локально.
`copy` не удаляет никогда — облако растёт; ротации у storage нет.

Переопределить режим на один запуск: `-m copy` (применяется только к storage).

## `archive`

| Ключ | Тип | По умолчанию | Смысл |
|---|---|---|---|
| `enabled` | bool | `false` | включает сценарий |
| `compressor` | enum | `zstd` | `zstd` \| `gzip` \| `pigz` \| `rar` \| `none` |
| `compressor_args` | list | `[]` | например `["-9"]` |
| `name` | str | `archive` | к имени добавится `_<timestamp>` |
| `root` | str | обяз. при `enabled` | каталог-источник |
| `include` | list | `["*"]` | маски **относительно root** |
| `exclude` | list | `[]` | маски tar |
| `target` | str | обяз. при `enabled` | `remote:path` |
| `retention` | map | нет | `{keep: N}` или `{max_age: 7d}` |
| `retention_mode` | enum | нет | `keep` \| `max_age` |

* `include: ["*"]` (по умолчанию) — весь каталог, **включая dotfiles**
  на верхнем уровне: `.env`, `.git`, `.htaccess`. Именно то, чего не хватало
  bash-версии.
* Иначе маски раскрываются в Python относительно `root`, с поддержкой `**`:
  `include: ["www/*", "conf/**/*.ini"]`. Маска без совпадений — предупреждение
  в логе; если не совпала ни одна — ошибка запуска.
* `exclude` — маски `tar`, применяются к путям внутри `root`;
  `*.mp4` исключит файл на любой глубине.

Имя файла: `<name>_<ts>.tar.zst` (`.tar.gz`, `.tar`, `.rar`).

`-m/--mode` с архивом не действует (всегда заливка + ротация); при
использовании печатается предупреждение.

## Частые ошибки валидатора

| Сообщение | Что делать |
|---|---|
| `'keep' and 'max_age' are mutually exclusive` | оставить одно правило в классе |
| `retention_mode is 'keep', so 'max_age' is not allowed` | привести все классы к одному режиму |
| `unknown key (allowed: ...)` | опечатка в имени ключа |
| `environment variable X is not set` | добавить переменную (secrets.env) |
| `database.target: required when database.enabled is true` | задать `target` |
| `config.database.retention: at least one class must be configured` | включить хотя бы один класс |
