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

