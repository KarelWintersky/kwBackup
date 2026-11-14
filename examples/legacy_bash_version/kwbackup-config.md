Пример конфига для kwbackup

```bash
#!/usr/bin/env bash

# Настройки бэкапа конкретного проекта.
# На этой стадии в окружении определены
# CONFIG_BASEDIR - Путь к этому конфигу (без финального /)
# CONFIG_FILE - Путь+имя файла этого конфига

#### [global]
# Datetime values (текущая дата, день начала недели и текущий день)
export NOW=`date '+%F-%H-%M-%S'`
export NOW_DOW=`date '+%u'`
export NOW_DAY=`date '+%d'`

#### --------------------------------------------------------------------------------
#### Общие настройки
#### --------------------------------------------------------------------------------
#### [common]

# Временный каталог
export TEMP_PATH=/tmp

# Используемый архиватор: rar, zstd, pigz
export USE_ARCHIVER=zstd

# файл лога. Не используется.
export LOG_FILE=/var/log/kwbackup.log

#### --------------------------------------------------------------------------------
#### Настройки RCLONE
#### --------------------------------------------------------------------------------
#### [rclone]

# путь к конфигу RCLONE. Если рядом со скриптом kwbackup.sh будет лежать файл rclone.conf - будет использован он.
export RCLONE_CONFIG=/opt/kwbackup/rclone.conf

# Используемый провайдер подключения (то, что указывается в [] в rclone.conf)
export RCLONE_PROVIDER=beget

export RCLONE_CUSTOM_OPTIONS=(
  "--s3-access-key-id=XXX"
  "--s3-secret-access-key=YYY"
  "--s3-no-check-bucket"
)

#### --------------------------------------------------------------------------------
#### Настройки бэкапа баз данных (алгоритм DUMP + ZIP + COPY)
#### --------------------------------------------------------------------------------
#### [database]

# Разрешить ли бэкап БД по сценарию dump, zip and copy (with delete old) (допустимо 0 или 1, отсутствие эквивалентно 0)
export ENABLE_BACKUP_DATABASE=1

# Тип БД, по умолчанию mysql. Поддерживается mysql, mariadb, pgsql, postgres, sqlite. 
# MySQL/MariaDB требует ~/.my.cnf с указанием user&password
# pg_dump требует PGPASSWORD или .pgpass
export DB_TYPE=mysql

# DB HOST 
export DB_HOST=localhost
# export DB_USER=pguser  # используется только для постгреса

# Backup sources: Databases, перечисляются через пробел в скобках
export DATABASES=mediamaker

# Делать ли ежедневные, еженедельные и ежемесячные копии? (0|1)
export DB_BACKUP_DAILY=1
export DB_BACKUP_WEEKLY=1
export DB_BACKUP_MONTHLY=1

# Minimal age interval for daily/weekly/monthly backups (7 days, 7*6+1 days, 30*12 days)
export DB_MIN_AGE_DAILY=7d
export DB_MIN_AGE_WEEKLY=43d
export DB_MIN_AGE_MONTHLY=360d

# Целевой контейнер для хранения бэкапов БД.
# Vожно указать не просто имя контейнера, но и контейнер+путь внутри, например EXAMPLE/STORAGE, это корректный вариант
export DATABASE_CLOUD_CONTAINER="9b61b03121ef-test/DB"

#### --------------------------------------------------------------------------------
#### Настройки бэкапа STORAGE (алгоритм SYNC)
#### --------------------------------------------------------------------------------
#### [storage]

# Разрешить ли бэкап файлового storage по схеме sync (допустимо 0 или 1, отсутствие эквивалентно 0)
export ENABLE_BACKUP_STORAGE=1

# "Корень" для источника данных [ОПЦИОНАЛЬНО]
export STORAGE_SOURCES_ROOT=/mnt/BLACK_PUBLIC/PHOTOS/HiSummer19

# Источники данных для storage. Строка или массив строк ( /tmp/1/ /tmp/2/ )
# Полные пути или подкаталоги, если определен STORAGE_SOURCES_ROOT
export STORAGE_SOURCES=

# Контейнер для STORAGE
# export CLOUD_CONTAINER_STORAGE="RPG_IMAGINARIA_UPLOADS"
export STORAGE_CLOUD_CONTAINER="9b61b03121ef-test/STORAGE"

# Алгоритм бэкапа: sync или copy
export STORAGE_BACKUP_ALGO="copy"

#### --------------------------------------------------------------------------------
#### Настройки бэкапа ФАЙЛОВОГО АРХИВА (алгоритм PACK, SYNC)  (допустимо 0 или 1, отсутствие эквивалентно 0)
#### --------------------------------------------------------------------------------
#### [archive]

# Разрешить ли бэкап архива файлов по списку с исключением файлов из списка (схема: PACK + SYNC)
export ENABLE_BACKUP_ARCHIVE=1

# Используемый компрессор
export ARCHIVE_USE_COMPRESSOR="rar"

# Кастомные ключи сжатия для разных архиваторов
export ARCHIVE_RAR_OPTIONS="-r -s -m5"
export ARCHIVE_ZSTD_OPTIONS="-9"
export ARCHIVE_GZIP_OPTIONS="-9"

# Алгоритм бэкапа: copy или sync
export ARCHIVE_BACKUP_ALGO="copy"

# Имя архива для бэкапа (без расширения!)
export ARCHIVE_FILENAME=HiSummer19_${NOW}

export ARCHIVE_ROOT="/mnt/BLACK_PUBLIC/PHOTOS/HiSummer19/"
export ARCHIVE_INCLUDE_LIST=("*")
export ARCHIVE_EXCLUDE_LIST=("*.mp4")

# путь к файлам include/exclude для RAR
export ARCHIVE_RAR_INCLUDE_LIST="${CONFIG_BASEDIR}/files-include.conf"
export ARCHIVE_RAR_EXCLUDE_LIST="${CONFIG_BASEDIR}/files-exclude.conf"

# Контейнер для бэкапа файлов
export ARCHIVE_CLOUD_CONTAINER="9b61b03121ef-test/ARCHIVE"

# Архивы старше этой даты будут удалены
export ARCHIVE_ROTATION_PERIOD="71d"

#### --------------------------------------------------------------------------------
#### КОНЕЦ
#### --------------------------------------------------------------------------------
#### [end]
```

# Примечания



# TODO

Не проверено:

```bash
# "Корень" для источника данных [ОПЦИОНАЛЬНО]
export STORAGE_SOURCES_ROOT=/srv/LIBDB.Storage/

# Источники данных для storage. Строка или массив строк ( /tmp/1/ /tmp/2/ )
# Полные пути или подкаталоги, если определен STORAGE_SOURCES_ROOT
export STORAGE_SOURCES=(
    files.aait
    files.etks
    files.hait
)
```