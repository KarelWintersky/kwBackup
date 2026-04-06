```bash
export DATABASE_RAR_OPTIONS="-m3 -mdc -mfb=273 -mmt=on"
export DATABASE_ZSTD_OPTIONS="-19 --no-progress --long"
export DATABASE_MYSQL_OPTIONS="--single-transaction --routines --triggers --quote-names --no-tablespaces --extended-insert=false"
```



# NB

❌ НЕ РАБОТАЕТ с массивами
`read -ra custom_cmds <<< "${RCLONE_CUSTOM_OPTIONS}"`

✅ РАБОТАЕТ
`local custom_cmds=("${RCLONE_CUSTOM_OPTIONS[@]}")`

${ARRAY[@]} — единственный способ скопировать весь массив! <<< "${ARRAY}" всегда дает только первый элемент.