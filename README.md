# kwBackup
KW Backup scripts with sync to Selectel Cloud Storage

# Как это использовать "с нуля"? 

1. Написать конфигурацию проекта, за основу можно взять файл `_config.conf`

В этом файле 5 значимых секций:

- Общие настройки - пути к временному каталогу и используемый архиватор. ZIP (используется pigz) или RAR.
- Настройки rclone - имя секции в конфиге rclone и путь к конфигу rclone.
- Настройки бэкапа БД
- Настройки бэкапа STORAGE
- Настройки бэкапа АРХИВА

2. Написать файл `rclone.conf`, который упомянуть в конфиге проекта:
```
export RCLONE_CONFIG=${CONFIG_BASEDIR}/rclone.conf
```

Можно не указывать rclone-конфиг для каждого проекта, а использовать общий. В этом случае он должен лежать рядом со скриптом `kwbackup.sh` и называться `rclone.conf`.

Формат конфига rclone таков:
```
[section_1]
type = swift
env_auth = false
user = <user>
key = <password>
auth = https://auth.selcdn.ru/v1.0
endpoint_type = public

[section_2]
...
```

3. Написать файлы `rarfiles-include.conf` и `rarfiles-exclude.conf` - в которых перечислить включаемые и исключаемые пути/файлы для бэкапа типа ARCHIVE 

Например:
```
# cat rarfiles-include.conf

/var/www/livemap/*

# cat rarfiles-exclude.conf

/var/www/livemap/cache
/var/www/livemap/logs

```

Если не предполагается использование сценария "ARCHIVE" - этот шаг можно опустить.  

4. Запустить скрипт, передав ему конфиг и команду бэкапа, например:

```
/bin/bash kwbackup.sh --config /path/to/project.conf --database 
```

5. Важно помнить, что нигде в конфиге не указаны ключи доступа к MySQL. Их следует указать в файле `.my.cnf` в домашнем
каталоге пользователя, исполняющего скрипт. Этот файл имеет вид:
```
[client]
user=root
password=password
```

# Вызов скрипта бэкапа:

Допустимые ключи:

- `-c` или `--config=/path/to/project.conf` - [ОБЯЗАТЕЛЬНО] задает файл конфига: (`-c config.conf` или `--config=config.conf`)
- `-d` или `--database` - запустить бэкап БД
- `-s` или `--storage` - запустить бэкап STORAGE
- `-a` или `--archive` - запустить бэкап архива
- `-m` или `--sync-mode=` - [ОПЦИОНАЛЬНО] - задает алгоритм заливки в облако. Перекрывает конфиг и значения по умолчанию. Используется для STORAGE и ARCHIVE
- `-f` или `--force` - принудительно запускает сценарии бэкапа, вне зависимости от настроек в конфиге (`ENABLE_BACKUP_DATABASE`, `ENABLE_BACKUP_STORAGE`, `ENABLE_BACKUP_ARCHIVE`)
- `-v` или `--verbose` - печатает расширенную информацию о происходящих процессах (бэкап, упаковка, аплоад)
- `-h` или `-help` - печатает помощь и выходит из скрипта
- `--install` - устанавливает пререквезиты - RAR и PIGZ (потребует ввода пароля sudo)

Обязателен ключ `--config`.

Если ни один из ключей `--database`, `--storage` или `--archive` не указан - скрипт выведет возможные (разрешенные в конфиге) 
сценарии бэкапа.

Ключ `--force` принудительно запускает сценарий.

# О структуре файла конфига

Это файл, имеющий шебанг `#!/usr/bin/env bash` и подгружаемый через `SOURCE config.conf`. Поэтому все опции в файле предваряются
ключевым словом `export`

В принципе, файл шаблона конфига достаточно прокомментирован, нет смысла повторять разбор всех опций. Я только разберу некоторые
неочевидные моменты:

## RCLONE_PROVIDER

Этот ключ содержит название нужной секции из конфига `rclone.conf` 

## DB_BACKUP_DAILY, DB_BACKUP_WEEKLY, DB_BACKUP_MONTHLY

Этот механизм позволяет хранить ежедневные, еженедельные и ежемесячные копии БД.

Ежедневные копии (даже если они делаются реже чем раз в день) складываются в субдиректорию `DAILY`. 
Они хранятся `DB_MIN_AGE_DAILY` дней (по умолчанию 7d) 

В первый день недели может делаться еженедельная копия (точнее, берется уже сделанный архив дампа БД), она заливается
в субдиректорию `WEEKLY` и хранится `DB_MIN_AGE_WEEKLY` дней (7*6+1 = 43 по умолчанию)

В первый день месяца может делаться ежемесячная копия (берется архив дампа БД), она заливается в субдиректорию MONTHLY
и хранится `DB_MIN_AGE_MONTHLY` дней (360d по умолчанию).

В версии 0.8.5 и ранее опции `DB_MIN_AGE_DAILY`, `DB_MIN_AGE_WEEKLY`, `DB_MIN_AGE_MONTHLY` не используются, но подсказывают нам,
сколько хранятся соответствующие копии базы.

## STORAGE_SOURCES_ROOT + STORAGE_SOURCES

Параметр `STORAGE_SOURCES_ROOT` имеет смысл для бэкапа нескольких подкаталогов в одном каталоге-корне. Так, например, в проекте LibDB мы
указываем
```
export STORAGE_SOURCES_ROOT=/srv/LIBDB.Storage/
export STORAGE_SOURCES=(
    files.aait
    files.etks
    files.hait
)
```
В этом случае в целевом контейнере будут созданы каталоги `files.aait`, `files.etks` и `files.hait`, в которые будет выгружен 
контент каталогов `/srv/LIBDB.Storage/files.aait`, `/srv/LIBDB.Storage/files.etks`, `/srv/LIBDB.Storage/files.hait`

Зачем так сделано? Если мы перечислим в STORAGE_SOURCES полные пути:
```
export STORAGE_SOURCES=(
    /srv/LIBDB.Storage/files.aait
    /srv/LIBDB.Storage/files.etks
    /srv/LIBDB.Storage/files.hait
)
```

То в целевой контейнер файлы будут выгружены с повторением **полного указанного пути**, то есть будут созданы подкаталоги:
`srv` > `LIBDB.Storage` > `files...` > `*`.

Это может быть неудобно - например, если путь к файлам очень длинный или содержит симлинки (кстати, не проверялось). 
С другой стороны, абсолютные пути могут использоваться для хранения в контейнере STORAGE любых путей/файлов, 
доступных в файловой системе. 

Какой способ выбрать - according to your wishes.



# Сценарии бэкапа

## Database

dump, pack (rar|zip), copy (с удалением копий старше определенной даты)

## Storage

copy или sync в зависимости от:
- значения ключа `--sync-mode` (по умолчанию sync)
- значения `STORAGE_BACKUP_ALGO` в секции конфига
- по умолчанию sync

Условия перечислены в порядке убывания приоритета. 

Таким образом, можно сделать 2 крон-задачи на одном конфиге:

`kwbackup.sh --storage --config --sync-mode=copy --config=/etc/kwbackup/example.conf` 
- которая копирует файлы в STORAGE

`kwbackup.sh --storage --config --sync-mode=sync --config=/etc/kwbackup/example.conf` 
- которая обновляет файлы в хранилище, удаляя несуществующие.

В случае второй команды `--sync-mode=sync` можно опустить, будет использоваться значение ключа `STORAGE_BACKUP_ALGO`, 
а в случае его отсутствия - значение `sync`.

## Archive

Создается архив с таймштампом NOW, включающий файлы, перечисленные в `Config::RARFILES_INCLUDE_LIST`, исключающий файлы, 
перечисленные в `Config::RARFILES_EXCLUDE_LIST` с опциями `-r -s -m5 -mde`

Режим заливки в облако задается аналогично STORAGE:

`--sync-mode` > Config: `ARCHIVE_BACKUP_ALGO` > "copy"


# Как поставить RAR? 

В `/etc/apt/sources.list` добавить:
```
deb http://mirror.yandex.ru/debian/ stable main contrib non-free
deb-src http://mirror.yandex.ru/debian/ stable main contrib non-free
deb http://security.debian.org/ stable/updates main contrib non-free
deb-src http://security.debian.org/ stable/updates main contrib non-free
```

и `apt update && apt install rar `

Или из пакета: https://debian.pkgs.org/11/debian-nonfree-amd64/rar_5.5.0-1_amd64.deb.html : 

```
wget http://ftp.de.debian.org/debian/pool/non-free/r/rar/rar_5.5.0-1_amd64.deb
sudo dpkg -i rar_5.5.0-1_amd64.deb
```

# TODO

- Добавить в файл конфига проекта опциональные параметры доступа к БД
- Добавить в файл конфига проекта опции MySQL и RAR, опциональные параметры
- Добавить поддержку PostgreSQL