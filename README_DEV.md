# Кастомные RCLONE-опции

История возникновения и вопроса.

```bash
export RCLONE_CUSTOM_OPTIONS=(
  "--s3-access-key-id=XXX"
  "--s3-secret-access-key=YYY"
  "--s3-no-check-bucket"
)
```

WTF?

Мой конфиг был:
```ini
[test]
type = swift
env_auth = false
user = test
key = password
auth = https://auth.selcdn.ru/v1.0
endpoint_type = public
```

Есть проблема: бегет не позволяет создавать бакеты внутри стораджа, его политика - один стородж = 1 бакет, то есть для 
множества бакетов придется делать множество секций внутри конфига. Это чертовски неудобно.

Поэтому я ищу вариант передавать в консоли rclone эти параметры. Перплексити советует так:

```bash
rclone \
  --s3-provider Selectel \
  --s3-access-key-id test \
  --s3-secret-access-key password \
  --s3-endpoint https://<s3-endpoint> \
  copy ./local-dir :s3,env_auth=false test:
```

А для бегета, с бакетом `9b61b03121ef-test` 

```
[beget.litworkshop]
type = s3
endpoint = https://s3.ru1.storage.beget.cloud
provider = Other
env_auth = false
access_key_id = XXX
secret_access_key = YYY
```

Перплексити советует:

```
rclone \
  --s3-provider Other \
  --s3-endpoint https://s3.ru1.storage.beget.cloud \
  --s3-access-key-id XXX \
  --s3-secret-access-key YYY \
  ls beget.test:
```

Но у нас нет секции в конфиге, мы вообще не используем конфиг. Что делать?

> Без имени remote в конфиге используйте inline-нотацию с префиксом :s3, — rclone создаст временный remote "на лету".

```bash
rclone  --s3-provider Other \
        --s3-endpoint https://s3.ru1.storage.beget.cloud \
        --s3-access-key-id XXX \ 
        --s3-secret-access-key YYY  \ 
        ncdu \ 
        :s3:9b61b03121ef-test
```

К счастью, в конфиге можно указать лишь частично:


```bash
rclone  --config /var/www.upkeep/kwbackup/test1/rclone_beget.conf \
        --s3-access-key-id XXX \
        --s3-secret-access-key YYY \
        ncdu \
        beget:9b61b03121ef-test
```

При конфиге:
```
[beget]
type = s3
provider = Other
endpoint = https://s3.ru1.storage.beget.cloud
```

У бегета проблема с rclone copy, access denied при copy. Ответ их техподдежки:

> При выполнении данной функции, rclone пытается проверить наличие бакета и создать его, однако в рамках
нашего s3 (бегет) такие запросы недоступны, из-за чего ошибка выходит как отсутствие прав. 
sync не использует подобную механику, поэтому отрабатывает без проблем.

> Что бы решить проблему с copy, в конфигурации rclone для Вашего бакета, укажите параметр 
no_check_bucket = true, после чего проверьте копирование повторно.

То есть или 
```
no_check_bucket = true
```
в конфиге

или --s3-no-check-bucket=1 ключиком

# Сборка TAR-архива

```bash

ARCHIVE_ROOT="/mnt/BLACK_PUBLIC/PHOTOS/"
ARCHIVE_INCLUDE_LIST=("*")
ARCHIVE_EXCLUDE_LIST=("*.mp4")

backup_create_tar() {
    local root="${ARCHIVE_ROOT%/}"  # без завершающего слеша
    local -a include=("${ARCHIVE_INCLUDE_LIST[@]}")
    local -a exclude=("${ARCHIVE_EXCLUDE_LIST[@]}")
    local archive_file="/tmp/backup_$(date +%s).tar"

    local exclude_args=()
    for pat in "${ARCHIVE_EXCLUDE_LIST[@]}"; do
        if [ -n "$pat" ]; then
            exclude_args+=("--exclude=$root/$pat")
        fi
    done

    # Формируем включения — только если есть что раскрывать
    local include_expanded=()

    if [ ${#ARCHIVE_INCLUDE_LIST[@]} -eq 0 ]; then
        # если включений нет — весь корень
        include_expanded=("$root"/*)
    else
        for pat in "${ARCHIVE_INCLUDE_LIST[@]}"; do
            if [ -n "$pat" ]; then
                # здесь bash сам раскроет * в реальные файлы
                local globbed=("$root"/$pat)
                # globbed содержит список файлов или ["$root/HiSummer19/*"] если ничего нет
                if [ -e "${globbed[0]}" ]; then
                    include_expanded+=("${globbed[@]}")
                fi
            fi
        done
    fi

    # Проверка
    if [ ${#include_expanded[@]} -eq 0 ]; then
        echo "Нет подходящих файлов для включения" >&2
        return 1
    fi

    archive_file="/tmp/backup_$(date +%s).tar"

    tar --absolute-names "${exclude_args[@]}" -cf "$archive_file" "${include_expanded[@]}"


    echo "Архив создан: $archive_file"
}

backup_create_tar
```

# Clone config variants

```ini

# SFTP configuration
[sftp]
type = sftp
host = example.com
user = backup_user
port = 22
key_file = /home/user/.ssh/id_rsa

# Local filesystem (for testing)
[local]
type = local
nounc = true



```