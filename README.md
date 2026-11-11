# kwbackup

Бэкап баз данных, файлового storage и файловых архивов в S3-совместимое
облако (rclone). Замена `kwbackup.sh` (0.9.0): те же сценарии, но с честными
кодами возврата, блокировками, проверкой копий и конфигом в YAML.

Python 3.10+, **только стандартная библиотека**. В продакшен ставится один файл.

## Сборка и установка

```bash
python3 build.py --test          # собрать dist/kwbackup и прогнать selftest
sudo install -m 755 dist/kwbackup /usr/local/bin/kwbackup
```

`dist/kwbackup` — один исполняемый файл с шебангом, собранный из `src/*.py`
простой конкатенацией в фиксированном порядке (см. `build.py`). Единственное
правило: все `import` живут в `src/00_header.py`, сборщик это проверяет.

## Использование

```bash
kwbackup -c project.yaml -d            # бэкап БД
kwbackup -c project.yaml -d -s -a      # БД + storage + архив за один запуск
kwbackup -c project.yaml               # все включённые в конфиге действия
kwbackup -c project.yaml --dry-run -d  # показать план, ничего не делать
kwbackup -c project.yaml --check-config
kwbackup -c project.yaml --list        # что реально лежит в облаке
kwbackup -c project.yaml --verify      # скачать и проверить последнюю копию
kwbackup -c project.yaml --restore --to /tmp/restore [--at 2026-11-09]
```

Коды возврата: `0` ok, `1` ошибка выполнения, `2` ошибка конфига, `3` ошибка
аргументов, `4` уже запущено (lock), `5` проверка копии не прошла.

`--list` и `--verify` можно ставить в cron как watchdog: они возвращают `5`,
если копий нет, они устарели или повреждены.

## Конфиг

Пример с комментариями: [`configs/kwbackup.example.yaml`](configs/kwbackup.example.yaml).

Ключевое:

* **Retention** — на каждый класс (`daily`/`weekly`/`monthly`) ровно одно из
  `keep: N` или `max_age: 7d`. Оба сразу или ни одного — ошибка конфига.
  `retention_mode` запрещает смешивать правила в одном файле.
* **Расписание** — DAILY каждым запуском, WEEKLY дополнительно по понедельникам,
  MONTHLY дополнительно 1-го числа. `catch_up: true` создаёт пропущенную
  weekly/monthly копию при следующем запуске.
* **Секреты** — только через `${ENV_VAR}` или `rclone.env`; в конфиг не пишутся.

## Разработка

```
src/00_header.py     импорты, константы
src/10_yaml.py       YAML-подмножество (без зависимостей)
src/20_config.py     схема, валидация, env-подстановка
src/30_logging.py    логи и формат размеров
src/40_lock.py       flock-блокировки
src/50_runner.py     пайплайны subprocess
src/60_rclone.py     обёртка rclone
src/70_retention.py  keep / max_age / расписание (чистые функции)
src/80_database.py   дампы и заливка
src/81_storage.py    rclone sync/copy
src/82_archive.py    tar + компрессор
src/83_restore.py    восстановление
src/85_report.py     --list и --verify
src/90_preflight.py  проверки перед запуском
src/92_migrate.py    конвертер старых bash-конфигов
src/95_cli.py        argparse и main
src/99_selftest.py   встроенные тесты (--selftest)
src/zz_main.py       точка входа
```

```bash
python3 build.py --test     # сборка + py_compile + selftest
python3 tests/e2e.py        # сквозные тесты на фейковом rclone
```

`tests/e2e.py` запускает собранный бинарь как процесс, подменяя `rclone`
локальным стабом (облако = временный каталог) и дамперы — заглушками.
Реальные `tar`, `zstd`, `gzip` используются как есть.

## Отличия от kwbackup.sh

| | kwbackup.sh 0.9.0 | kwbackup (python) |
|---|---|---|
| конфиг | исполняемый bash под root | YAML + валидатор |
| `-d -s -a` | выполнялось только первое действие | все действия |
| lock | файл-флаг, протухает навсегда | `flock`, снимается ядром |
| ошибки | часто exit 0 и тишина | код возврата + лог |
| проверка | нет | `--list`, `--verify` |
| восстановление | нет | `--restore` |
| dotfiles в архиве | терялись | включаются |

Старая документация по bash-версии: [`kwbackup-config.md`](kwbackup-config.md).
