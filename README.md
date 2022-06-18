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

Значение в скобках - `RCLONE_PROVIDER`

3. Написать файлы `rarfiles-include.conf` и `rarfiles-exclude.conf` - в которых перечислить включаемые и исключаемые пути/файлы для бэкапа типа ARCHIVE 

4. Запустить скрипт, передав ему конфиг и команду бэкапа

# Вызов скрипта бэкапа:

Допустимые ключи:

- `-c` или `--config=/path/to/project.conf` - [ОБЯЗАТЕЛЬНО] задает файл конфига: (`-c config.conf` или `--config=config.conf`)
- `-d` или `--database` - запустить бэкап БД
- `-s` или `--storage` - запустить бэкап STORAGE
- `-a` или `--archive` - запустить бэкап архива
- `-m` или `--sync-mode=` - [ОПЦИОНАЛЬНО] - задает алгоритм заливки в облако. Перекрывает конфиг и значения по умолчанию. Используется для STORAGE и ARCHIVE
- `-f` или `--force` - принудительно запускает процедуры бэкапа, вне зависимости от настроек в конфиге (`ENABLE_BACKUP_DATABASE`, `ENABLE_BACKUP_STORAGE`, `ENABLE_BACKUP_ARCHIVE`) (@TODO)
- `-h` или `-help` - печатает помощь и выходит из скрипта

Обязателен ключ `--config` и хотя бы один из ключей `--database`, `--storage` или `--archive`

# Методология разных типов бэкапа

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