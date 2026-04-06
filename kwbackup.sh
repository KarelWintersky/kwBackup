#!/bin/bash

# sudo wget https://raw.githubusercontent.com/KarelWintersky/kwBackup/main/kwbackup.sh -nv -O /usr/local/bin/kwbackup.sh && sudo chmod +x /usr/local/bin/kwbackup.sh

VERSION="0.8.22"

THIS_SCRIPT="${0}"
THIS_SCRIPT_BASEDIR="$(dirname ${THIS_SCRIPT})"

PROCESS_FLAG_FILE=""

ACTION_DISPLAY_HELP=n
ACTION_BACKUP_DATABASE=n
ACTION_BACKUP_STORAGE=n
ACTION_BACKUP_ARCHIVE=n
ACTION_FORCE=n
ACTION_RESET_FLAGS=n

MODE_VERBOSE=n
MODE_DEBUG=n

CONFIG_FILE=-
CONFIG_BASEDIR=-

CLI_UPLOAD_MODE=sync

__what_is_it="
KWBackup Script Version ${VERSION}"

__usage="
Usage: $(basename $0) [OPTIONS]

Options:
  -c, --config=file.conf      Use config file [REQUIRED]
  -d, --database              Backup database [OPTIONAL]
  -s, --storage               Backup storage [OPTIONAL]
  -a, --archive               Backup archive [OPTIONAL]
  -m, --sync-mode=copy|sync   Override upload mode [OPTIONAL]
  -f, --force                 Force backup section, overrides ENABLE_BACKUP_*
  --verbose                   More messages (mainly for debug)
  --reset                     Reset all 'already-running-script' flags
  --install                   Install prerequisites (PV, RAR & PIGZ)
  -h, --help                  Print this help and exit
  -v, --version               Print version and exit
"

RAR_DEB_URI=http://ftp.de.debian.org/debian/pool/non-free/r/rar/rar_5.5.0-1_amd64.deb
URI_SCRIPT_GITHUB=https://raw.githubusercontent.com/KarelWintersky/kwBackup/main/kwbackup.sh

# Определяет цвета
function defineColors() {
    export ANSI_RED="\e[31m"
    export ANSI_GREEN="\e[32m"
    export ANSI_YELLOW="\e[33m"
    export ANSI_WHITE="\e[97m"
    export ANSI_RESET="\e[0m"
}

# функция, выполняющаяся перед завершением скрипта. Она же ловит CTRL-C
function before_exit_script() {
    rm -f "${PROCESS_FLAG_FILE}"
}

# Парсит аргументы командной строки
function parseArgs {
    set -o errexit -o pipefail -o noclobber -o nounset

    ! getopt --test >/dev/null
    if [[ ${PIPESTATUS[0]} -ne 4 ]]; then
        echo "I’m sorry, $(getopt --test) failed in this environment."
        exit 1
    fi

    local LONGOPTS=config:,database,storage,archive,help,sync-mode:,force,version,install,self-update,reset,verbose,debug
    local OPTIONS=c:hdsam:fv
    local PARSED=-

    ! PARSED=$(getopt --options=$OPTIONS --longoptions=$LONGOPTS --name "$0" -- "$@")
    if [[ ${PIPESTATUS[0]} -ne 0 ]]; then exit 2; fi

    eval set -- "$PARSED"

    while true; do
        case "$1" in
        -d | --database)
            ACTION_BACKUP_DATABASE=y
            shift
            ;;
        -s | --storage)
            ACTION_BACKUP_STORAGE=y
            shift
            ;;
        -a | --archive)
            ACTION_BACKUP_ARCHIVE=y
            shift
            ;;
        -h | --help)
            ACTION_DISPLAY_HELP=y
            shift
            ;;
        -c | --config)
            export CONFIG_FILE="$2"
            shift 2
            ;;
        -m | --sync-mode)
            local RSM="$2"
            case $RSM in
            "copy")
                CLI_UPLOAD_MODE="copy"
                ;;
            "sync")
                CLI_UPLOAD_MODE="sync"
                ;;
            *)
                echo "Invalid sync algorithm, must be 'copy' or 'sync'"
                exit 5
                ;;
            esac
            shift 2
            ;;
        --install)
            curl ${RAR_DEB_URI} -o /tmp/rar.deb && sudo dpkg -i /tmp/rar.deb && rm /tmp/rar.deb
            sudo apt install pigz pv zstd
            exit 0
            ;;
        --self-update)
            if [[ "${PWD}" == "/usr/local/bin" ]]; then
                sudo wget ${URI_SCRIPT_GITHUB} -nv -O ${PWD}/$0
                sudo chmod +x ${PWD}/$0
            else
                echo "Can't update local version. Script must be installed to /usr/local/bin/kwbackup.sh"
            fi
            exit 0
            ;;
        --reset)
            ACTION_RESET_FLAGS=y
            shift
            ;;
        -f | --force)
            ACTION_FORCE=y
            shift
            ;;
        --verbose)
            MODE_VERBOSE=y
            shift
            ;;
        --debug)
            MODE_DEBUG=y
            shift
            ;;
        -v | --version)
            echo "${__what_is_it}"
            exit 0
            ;;
        --)
            shift
            break
            ;;
        *)
            echo "Programming error"
            exit 3
            ;;
        esac
    done

    # путь (только путь) к файлу конфига, без финального /
    export CONFIG_BASEDIR="$(dirname ${CONFIG_FILE})"
}

function displayHelp() {
    echo -e "${__what_is_it}"
    echo -e "${__usage}"
}

function displayConfigError() {
    echo -e "Config file ${CONFIG_FILE} ${ANSI_RED}not found${ANSI_RESET}"
}

# Выводит сообщение с учетом VERBOSE MODE
function say() {
    local MESSAGE=$1
    if [[ "${MODE_DEBUG}" = "y" ]]; then
        echo -e ${MESSAGE}
    fi
}

# Вычисляет порт для используемой БД (мультиподдержка баз)
function get_db_port() {
    local db_type=${1:-mysql}
    case "${db_type}" in
        mysql|mariadb) echo "3306" ;;
        pgsql|postgres) echo "5432" ;;
        sqlite) echo "" ;;
        *) echo "3306" ;;
    esac
}

# Вычисляет реальный бинарник для DB_TYPE = mysql
function detectDBType() {
    local mysql_bin=$(command -v mysql 2>/dev/null)

    if [[ -z "$mysql_bin" ]]; then
        return 1
    fi

    # Проверяем символическую ссылку
    if [[ -L "/usr/bin/mysql" ]]; then
        local link_target=$(readlink -f "/usr/bin/mysql")
        if [[ "$link_target" == *"mariadb"* ]]; then
            echo "mariadb"
            return
        fi
    fi

    # Проверяем mariadb бинарник
    local mariadb_bin=$(command -v mariadb 2>/dev/null)
    if [[ -n "$mariadb_bin" && "$mysql_bin" == "$mariadb_bin" ]]; then
        echo "mariadb"
    else
        echo "mysql"
    fi
}

function get_lock_filename() {
    local action=$1
    local action_hash=$(echo -n "${action}_${CONFIG_FILE}_${DB:-_}" | sha1sum | cut -d' ' -f1)
    echo "/dev/shm/kwbackup_${action}_${action_hash}.flag"
}

# Добавляет флаг блокировки
function check_action_lock() {
    local action=$1
    PROCESS_FLAG_FILE=$(get_lock_filename "${action}")

    if [[ -f "${PROCESS_FLAG_FILE}" ]]; then
        say "ERROR: ${action^^} backup already running (config: ${CONFIG_FILE}, DB: ${DB:-all})"
        return 1
    fi

    echo "$(date "+%d.%m.%Y %T") ${action^^} started (config: ${CONFIG_FILE}, DB: ${DB:-all})" > "${PROCESS_FLAG_FILE}"
    trap 'rm -f "${PROCESS_FLAG_FILE}" 2>/dev/null || true' EXIT INT TERM  # cleanup на выход
}

# Удаляет флаг блокировки
function cleanup_action_lock() {
    local action=$1
    PROCESS_FLAG_FILE=$(get_lock_filename "${action}")

    say "Cleaning lock: ${PROCESS_FLAG_FILE}"

    rm -f "${PROCESS_FLAG_FILE}" 2>/dev/null || true
}

# Реальная команда дампа БД
function command_database_dump() {
    [[ "${DB_TYPE}" == "mysql" ]] && DB_TYPE=$(detectDBType)

    local db_port=${DB_PORT:-$(get_db_port "${DB_TYPE}")}

    case "${DB_TYPE}" in
        mariadb)
            mariadb-dump "${DATABASE_MYSQL_OPTIONS[@]:-}" --host="${DB_HOST}" "${db_port:+--port=${db_port}}" "${DB}"
            ;;
        mysql)
            mysqldump "${DATABASE_MYSQL_OPTIONS[@]:-}" --host="${DB_HOST}" "${db_port:+--port=${db_port}}" "${DB}"
            ;;
        postgres|pgsql)
            # pg_dump требует PGPASSWORD или .pgpass
            pg_dump "${DATABASE_PGSQL_OPTIONS[@]:-}" --host="${DB_HOST}" "${db_port:+-p ${db_port}}" --dbname="${DB}" --username="${PGUSER:-postgres}"
            ;;
        sqlite)
            # DB_HOST содержит путь к .db файлу
            sqlite3 "${DB_HOST}" .dump 2>/dev/null
            ;;
        *)
            say "ERROR: Unsupported DB_TYPE: ${DB_TYPE}. Supported: mysql, mariadb, postgres, pgsql, sqlite"
            return 1
            ;;
    esac
}

# Собирает команду RCLONE из аргументов с учетом кастомных команд из конфига
function command_rclone() {
    local cmd_type=$1; shift  # delete/copy/sync
    local custom_cmds=()

    # Парсим кастомные команды
    local custom_cmds=("${RCLONE_CUSTOM_OPTIONS[@]:-}")

    # Собираем аргументы
    local args=("${cmd_type}" "--config" "${RCLONE_CONFIG}" "${custom_cmds[@]}")
    args+=("$@")  # остальные аргументы

    say "rclone ${args[*]}"
    rclone "${args[@]}"
}

# Реальная команда копирования данных в облако
function command_upload_database() {
    local TYPE=$1 AGE=$2

    command_rclone delete "${RCLONE_OPTIONS[@]:-}" --min-age "${AGE}" "${RCLONE_PROVIDER}:${PATH_INSIDE_CONTAINER}/${TYPE}"
    command_rclone copy "${RCLONE_OPTIONS[@]:-}" "${TEMP_PATH}/${FILENAME_ARCHIVE}" "${RCLONE_PROVIDER}:${PATH_INSIDE_CONTAINER}/${TYPE}"
}

# суб-функция, которая делает бэкап на самом деле. Принимает 2 параметра:
# $1 - имя БД для бэкапа
# $2 - суб-путь в целевом контейнере = (DB name для множественных БД в одной задаче ИЛИ '' для одной БД в задаче)
# одна БД бэкапится в CONTAINER/(DAILY|WEEKLY|MONTHLY)/*
# несколько БД бэкапятся в CONTAINER/DB/(DAILY|WEEKLY|MONTHLY)/*
function sub_backupDatabase() {
    local DB=$1
    local PATH_INSIDE_CONTAINER=${CLOUD_CONTAINER_DB}
    [[ -n "${2}" ]] && PATH_INSIDE_CONTAINER+="/${2}"

    local FILENAME_ARCHIVE

    say "-----===== Backupping ${DB} =====-----"

    local use_pipe_view=false
    [[ "${MODE_VERBOSE}" = "y" ]] && use_pipe_view=true

    case "${USE_ARCHIVER}" in
      "rar")
          FILENAME_ARCHIVE="${DB}_${NOW}.sql.rar"
          if ${use_pipe_view}; then
              command_database_dump | pv | rar a -si"${DB}_${NOW}.sql" "${RAR_OPTIONS[@]:-}" "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          else
              command_database_dump | rar a -si"${DB}_${NOW}.sql" "${RAR_OPTIONS[@]:-}" "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          fi
          ;;
      "zstd")
          FILENAME_ARCHIVE="${DB}_${NOW}.sql.zstd"
          if ${use_pipe_view}; then
              command_database_dump | pv | zstd "${ZSTD_OPTIONS[@]:-}" -o "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          else
              command_database_dump | zstd "${ZSTD_OPTIONS[@]:-}" -o "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          fi
          ;;
      "zip")
          FILENAME_ARCHIVE="${DB}_${NOW}.sql.gz"
          if ${use_pipe_view}; then
              command_database_dump | pv | pigz -c > "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          else
              command_database_dump | pigz -c > "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          fi
          ;;
      *)
          FILENAME_ARCHIVE="${DB}_${NOW}.sql"
          if ${use_pipe_view}; then
              command_database_dump | pv > "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          else
              command_database_dump > "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          fi
          ;;
    esac


    #@todo: сделать глубину хранения копий все таки зависимой от параметров, но со значением по умолчанию
    # DB_MIN_AGE_DAILY, DB_MIN_AGE_WEEKLY, DB_MIN_AGE_MONTHLY (какая-то с этим была проблема)

    [[ ${DB_BACKUP_DAILY:-0} = 1 ]] && command_upload_database "DAILY" "7d"
    [[ ${DB_BACKUP_WEEKLY:-0} = 1 ]] && [[ ${NOW_DOW} -eq 1 ]] && command_upload_database "WEEKLY" "43d"
    [[ ${DB_BACKUP_MONTHLY:-0} = 1 ]] && [[ ${NOW_DAY} == 01 ]] && command_upload_database "MONTHLY" "360d"

    say "rm ${TEMP_PATH}/${FILENAME_ARCHIVE}"
    rm "${TEMP_PATH}"/"${FILENAME_ARCHIVE}"
}

# Выполняет действие "Бэкап БД
function actionBackupDatabase() {
    if [[ ${ENABLE_BACKUP_DATABASE:-0} = 0 ]]; then
        [[ "${ACTION_FORCE:-n}" != "y" ]] && { echo "Backup database disabled"; exit 0; }
    fi

    # Берем опции ИЗ КОНФИГА (или дефолт)
    : "${DATABASE_RAR_OPTIONS:=-m3}"
    : "${DATABASE_ZSTD_OPTIONS:=-9}"
    : "${DATABASE_MYSQL_OPTIONS:=-Q --single-transaction --no-tablespaces --extended-insert=false}"
    : "${DATABASE_PGSQL_OPTIONS:=--clean --if-exists --no-owner --no-privileges}"
    : "${DATABASE_SQLITE_OPTIONS:=}"

    # Парсим строки в массивы
    read -ra RAR_OPTIONS <<< "${DATABASE_RAR_OPTIONS}"
    read -ra ZSTD_OPTIONS <<< "${DATABASE_ZSTD_OPTIONS}"
    read -ra DATABASE_MYSQL_OPTIONS <<< "${DATABASE_MYSQL_OPTIONS}"
    read -ra DATABASE_PGSQL_OPTIONS <<< "${DATABASE_PGSQL_OPTIONS}"
    read -ra DATABASE_SQLITE_OPTIONS <<< "${DATABASE_SQLITE_OPTIONS}"

    # Правим опции в зависимости от режима
    if [[ "${MODE_VERBOSE}" = "y" ]]; then
        read -ra RCLONE_OPTIONS <<< "--copy-links --update --verbose --progress"
        DATABASE_PGSQL_OPTIONS+=("--verbose")
    else
        RAR_OPTIONS+=("-inul")
        ZSTD_OPTIONS+=("--quiet")

        read -ra RCLONE_OPTIONS <<< "--copy-links --update"
    fi

    if [[ "$(declare -p DATABASES 2>/dev/null)" =~ "declare -a" ]]; then
        for DB in "${DATABASES[@]}"; do
            check_action_lock "database_${DB}" || return 1  # уникальный флаг на БД!
            sub_backupDatabase "${DB}" "${DB}"
            # trap автоматически очистит PROCESS_FLAG_FILE
        done
    else
        check_action_lock "database_${DATABASES}" || return 1
        sub_backupDatabase "${DATABASES}" ""
    fi

    echo "$(date "+%d.%m.%Y %T %N") : finished task DATABASE" >> "${PROCESS_FLAG_FILE}"
    cleanup_action_lock "database"  # явный cleanup
}

# скрипт бэкапа STORAGE
function actionBackupStorage() {
    if [[ ${ENABLE_BACKUP_STORAGE:-0} = 0 ]]; then
        [[ "${ACTION_FORCE:-n}" != "y" ]] && { say "Backup storage disabled"; return 0; }
    fi

    check_action_lock "storage" || return 1
    trap 'cleanup_action_lock "storage"' RETURN EXIT

    echo "$(date "+%d.%m.%Y %T %N") : started task STORAGE" >> "${PROCESS_FLAG_FILE}"

    # Алгоритм заливки
    local UPLOAD_MODE=${CLI_UPLOAD_MODE:-${STORAGE_BACKUP_ALGO:-sync}}

    # RCLONE_OPTIONS массивом
    local rclone_opts=()
    if [[ "${MODE_VERBOSE}" = "y" ]]; then
        read -ra rclone_opts <<< "--copy-links --update --verbose --progress"
    else
        read -ra rclone_opts <<< "--copy-links --update"
    fi

    # Источники (строка → массив)
    local sources=()
    if [[ "$(declare -p STORAGE_SOURCES 2>/dev/null)" =~ "declare -a" ]]; then
        sources=("${STORAGE_SOURCES[@]}")
    elif [[ -n "${STORAGE_SOURCES}" ]]; then
        sources=("${STORAGE_SOURCES}")
    fi

    local SOURCE_ROOT=${STORAGE_SOURCES_ROOT:-}

    if [[ ${#sources[@]} -eq 0 && -n "${STORAGE_SOURCES_ROOT}" ]]; then
      sources=("")  # пустая строка = корень
    fi

    # Бэкап каждого источника
    for SOURCE in "${sources[@]}"; do
        command_rclone "${UPLOAD_MODE}" "${rclone_opts[@]}" "${SOURCE_ROOT}${SOURCE}" "${RCLONE_PROVIDER}:${CLOUD_CONTAINER_STORAGE}/${SOURCE}"
    done

    echo "$(date "+%d.%m.%Y %T %N") : finished task STORAGE" >> "${PROCESS_FLAG_FILE}"
    cleanup_action_lock "storage"
}

#
# Вызывать: archiveCreateTAR | zstd -T0 -o /tmp/backup_$(date +%s).tar.zst
#
function archiveCreateTAR() {
    local root="${ARCHIVE_ROOT%/}"  # без завершающего слеша
    local -a include=("${ARCHIVE_INCLUDE_LIST[@]}")
    local -a exclude=("${ARCHIVE_EXCLUDE_LIST[@]}")

    # Если нет такого каталога
    [[ ! -d "${root}" ]] && {
        say "ERROR: ARCHIVE_ROOT '${root}' does not exist or is not a directory"
        return 1
    }

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
        say "Нет подходящих файлов для включения"
        return 1
    fi

    tar --absolute-names "${exclude_args[@]}" -c "${include_expanded[@]}"
}

# Скрипт бэкапа архива. Проверено zstd
function actionBackupArchive() {
    if [[ ${ENABLE_BACKUP_ARCHIVE:-0} = 0 ]]; then
        [[ "${ACTION_FORCE:-n}" != "y" ]] && { say "Backup archive disabled"; return 0; }
    fi

    local use_archiver=${ARCHIVE_USE_COMPRESSOR:-${USE_ARCHIVER:-gzip}}

    # Опции архиваторов из конфига (или дефолт)
    : "${ARCHIVE_RAR_OPTIONS:=-m3}"
    : "${ARCHIVE_ZSTD_OPTIONS:=-9}"
    : "${ARCHIVE_GZIP_OPTIONS:=-9}"
    : "${ARCHIVE_ROTATION_PERIOD:=71d}"

    read -ra ARCHIVE_RAR_OPTIONS  <<< "${ARCHIVE_RAR_OPTIONS}"
    read -ra ARCHIVE_ZSTD_OPTIONS <<< "${ARCHIVE_ZSTD_OPTIONS}"
    read -ra ARCHIVE_GZIP_OPTIONS <<< "${ARCHIVE_GZIP_OPTIONS}"

    local ARCHIVE_FILENAME="${ARCHIVE_FILENAME}"

    local use_pv=false
    [[ "${MODE_VERBOSE}" = "y" ]] && use_pv=true

    # Правим опции в зависимости от режима
    if [[ "${MODE_VERBOSE}" = "y" ]]; then
        read -ra RCLONE_OPTIONS <<< "--copy-links --update --verbose --progress"
    else
        ARCHIVE_RAR_OPTIONS+=("-inul")
        ARCHIVE_ZSTD_OPTIONS+=("--quiet")

        read -ra RCLONE_OPTIONS <<< "--copy-links --update"
    fi

    check_action_lock "archive" || return 1
    echo "$(date "+%d.%m.%Y %T %N") : started task ARCHIVE" >> "${PROCESS_FLAG_FILE}"

    case "${use_archiver}" in
        rar)
            ARCHIVE_FILENAME+=".rar"
            local rar_opts=("${ARCHIVE_RAR_OPTIONS[@]:--r -s -m5}")
            [[ "${MODE_VERBOSE}" != "y" ]] && rar_opts+=("-inul")

            # RAR имеет собственный механизм include/exclude через @listfile и -x@listfile
            # Если ARCHIVE_ROOT задан — меняем рабочий каталог
            local rar_cd_cmd=""
            [[ -n "${ARCHIVE_ROOT}" ]] && rar_cd_cmd="cd '${ARCHIVE_ROOT}' &&"

            if [[ -n "${exclude_file}" ]]; then
                eval "${rar_cd_cmd}" rar a \
                    -x@"${exclude_file}" \
                    "${rar_opts[@]}" \
                    "${TEMP_PATH}/${ARCHIVE_FILENAME}" \
                    @"${include_file}"
            else
                # exclude как отдельные -x:pattern
                local rar_excl=()
                for p in "${exclude_paths[@]}"; do rar_excl+=("-x:${p}"); done
                eval "${rar_cd_cmd}" rar a \
                    "${rar_excl[@]}" \
                    "${rar_opts[@]}" \
                    "${TEMP_PATH}/${ARCHIVE_FILENAME}" \
                    @"${include_file}"
            fi
        ;;
        zstd)
            ARCHIVE_FILENAME+=".zstd"

            if ${use_pv}; then
                archiveCreateTAR | pv | zstd "${ARCHIVE_ZSTD_OPTIONS[@]}" -o "${TEMP_PATH}/${ARCHIVE_FILENAME}"
            else
                archiveCreateTAR | zstd "${ARCHIVE_ZSTD_OPTIONS[@]}" -o "${TEMP_PATH}/${ARCHIVE_FILENAME}"
            fi
        ;;
        pigz|gzip)
            ARCHIVE_FILENAME+=".tar.gz"

            if ${use_pv}; then
                archiveCreateTAR | pv | pigz "${ARCHIVE_GZIP_OPTIONS[@]:-}" -c > "${TEMP_PATH}/${ARCHIVE_FILENAME}"
            else
                archiveCreateTAR | pigz "${ARCHIVE_GZIP_OPTIONS[@]:-}" -c > "${TEMP_PATH}/${ARCHIVE_FILENAME}"
            fi
        ;;
        *)
            ARCHIVE_FILENAME+=".tar"

            if ${use_pv}; then
                archiveCreateTAR | pv > "${TEMP_PATH}/${ARCHIVE_FILENAME}"
            else
                archiveCreateTAR > "${TEMP_PATH}/${ARCHIVE_FILENAME}"
            fi
        ;;
    esac

    command_rclone delete "${RCLONE_OPTIONS[@]:-}" --min-age "${ARCHIVE_ROTATION_PERIOD}" "${RCLONE_PROVIDER}:${ARCHIVE_CLOUD_CONTAINER}"
    command_rclone copy "${RCLONE_OPTIONS[@]:-}" "${TEMP_PATH}/${ARCHIVE_FILENAME}" "${RCLONE_PROVIDER}:${ARCHIVE_CLOUD_CONTAINER}"

    rm -f "${TEMP_PATH}/${ARCHIVE_FILENAME}"

    echo "$(date "+%d.%m.%Y %T %N") : finished task ARCHIVE" >> "${PROCESS_FLAG_FILE}"
    cleanup_action_lock "archive"
}


function main() {
    defineColors
    parseArgs "$@"

    [[ ${ACTION_DISPLAY_HELP} = "y" ]] && { displayHelp; exit 1; }

    [[ ! -f "${CONFIG_FILE}" ]] && { displayConfigError; exit 4; }

    # Импортируем конфиг проекта
    . ${CONFIG_FILE}

    # Если в конфиге не указан RCLONE_CONFIG - используется rclone из каталога скрипта
    if { [ -z "${RCLONE_CONFIG}" ] || [ ! -f "${RCLONE_CONFIG}" ]; } && [ -f "${THIS_SCRIPT_BASEDIR}/rclone.conf" ]; then
         RCLONE_CONFIG="${THIS_SCRIPT_BASEDIR}/rclone.conf"
    fi

    [[ ${ACTION_RESET_FLAGS} = "y" ]] && { rm -f /dev/shm/kwbackup* ; }

    local ACTUAL_ACTIONS=0

    [[ ${ACTION_BACKUP_DATABASE} = "y" ]] && { actionBackupDatabase; ((ACTUAL_ACTIONS++)); }
    [[ ${ACTION_BACKUP_STORAGE} = "y" ]] && { actionBackupStorage; ((ACTUAL_ACTIONS++)); }
    [[ ${ACTION_BACKUP_ARCHIVE} = "y" ]] && { actionBackupArchive; ((ACTUAL_ACTIONS++)); }

    [[ ${ACTUAL_ACTIONS} -eq 0 ]] && show_available_actions
}

function show_available_actions() {
      echo -e "No one backup action(s) requested. Allowed: "
      { declare -p ENABLE_BACKUP_DATABASE >/dev/null 2>&1 && [[ ${ENABLE_BACKUP_DATABASE:-0} = 1 ]]; } && echo -e "... database (use ${ANSI_YELLOW}--database${ANSI_RESET} option)"
      { declare -p ENABLE_BACKUP_STORAGE >/dev/null 2>&1 && [[ ${ENABLE_BACKUP_STORAGE:-0} = 1 ]]; } && echo -e "... storage  (use ${ANSI_YELLOW}--storage${ANSI_RESET}  option)"
      { declare -p ENABLE_BACKUP_ARCHIVE >/dev/null 2>&1 && [[ ${ENABLE_BACKUP_ARCHIVE:-0} = 1 ]]; } && echo -e "... archive (use ${ANSI_YELLOW}--archive${ANSI_RESET}  option)"
}

# ---------------- Main ----------------

main "$@"

before_exit_script