#!/bin/bash

# sudo wget https://raw.githubusercontent.com/KarelWintersky/kwBackup/main/kwbackup.sh -nv -O /usr/local/bin/kwbackup.sh
# sudo chmod +x /usr/local/bin/kwbackup.sh

VERSION="0.8.11"

THIS_SCRIPT="${0}"
THIS_SCRIPT_BASEDIR="$(dirname ${THIS_SCRIPT})"

PROCESS_FLAG_FILE=""

ACTION_DISPLAY_HELP=n
ACTION_BACKUP_DATABASE=n
ACTION_BACKUP_STORAGE=n
ACTION_BACKUP_ARCHIVE=n
ACTION_FORCE=n
ACTION_RESET_FLAGS=n

VERBOSE_MODE=n

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

#@todo: на самом деле нужно проверять не PID, а вычислять md5-хэш командной строки и создавать файловый флаг в /dev/shm/ или /tmp/
# со стройкой запуска и датой запуска
# при его наличии - говорить что скрипт уже запущен (и, возможно, печатать строку запуска и таймштамп)
# а по окончанию работы файл стирать
# в тот же файл писать временные логи процесса - чтобы было понятно, что происходит или в чем облом процесса
function checkUniqueProcess() {
    if pidof -o %PPID -x $(basename "$0") >/dev/null; then
        echo "$(date "+%d.%m.%Y %T") EXIT: The script is already running." | tee -a "${LOG_FILE:-/var/log/kwbackup.log}"
        exit 1
    fi
}

# Улучшенная версия отслеживания уникальности запущенного процесса
# создается pid-файл с именем = хэшу командной строки. По окончанию работы файл удаляется.
function checkUniqueProcessWithConfig() {
    local CMD_LINE="$0 $@"
    local ARGS_LINE="$@"
    local CMD_LINE_HASH=$(echo -n ${CMD_LINE} | sha1sum -t | /bin/cut -f1 -d" ")
    PROCESS_FLAG_FILE="/dev/shm/kwbackup_${CMD_LINE_HASH}.flag"

    if [[ -f  ${PROCESS_FLAG_FILE} ]]; then
        echo "$(date "+%d.%m.%Y %T") kwBackup already running with args '${ARGS_LINE}'"
        exit 1
    else
        echo "$(date "+%d.%m.%Y %T") kwBackup started with args: '${ARGS_LINE}'" > "${PROCESS_FLAG_FILE}"
    fi
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

    local LONGOPTS=config:,database,storage,archive,help,sync-mode:,force,version,install,self-update,reset,verbose
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
            VERBOSE_MODE=y
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
    if [[ "${VERBOSE_MODE}" = "y" ]]; then
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

# Добавляет флаг блокировки
function check_action_lock() {
    local action=$1
    local action_hash=$(echo -n "${action}_${CONFIG_FILE}_${DB:-_}" | sha1sum | cut -d' ' -f1)
    PROCESS_FLAG_FILE="/dev/shm/kwbackup_${action}_${action_hash}.flag"

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
    local action_hash=$(echo -n "${action}_${DB:-}_${CONFIG_FILE}" | sha1sum | cut -d' ' -f1)
    local ACTION_FLAG_FILE="/dev/shm/kwbackup_${action}_${action_hash}.flag"
    rm -f "${ACTION_FLAG_FILE}" 2>/dev/null || true
}

# Реальная команда дампа БД
function database_dump_command() {
    mysqldump "${MYSQL_OPTIONS[@]:-}" --host "${MYSQL_HOST}" "${DB}"
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
    [[ "${VERBOSE_MODE}" = "y" ]] && use_pipe_view=true

    case "${USE_ARCHIVER}" in
      "rar")
          FILENAME_ARCHIVE="${DB}_${NOW}.sql.rar"
          if ${use_pipe_view}; then
              database_dump_command | pv | rar a -si"${DB}_${NOW}.sql" "${RAR_OPTIONS[@]:-}" "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          else
              database_dump_command | rar a -si"${DB}_${NOW}.sql" "${RAR_OPTIONS[@]:-}" "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          fi
          ;;
      "zstd")
          FILENAME_ARCHIVE="${DB}_${NOW}.sql.zstd"
          if ${use_pipe_view}; then
              database_dump_command | pv | zstd "${ZSTD_OPTIONS[@]:-}" -o "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          else
              database_dump_command | zstd "${ZSTD_OPTIONS[@]:-}" -o "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          fi
          ;;
      "zip")
          FILENAME_ARCHIVE="${DB}_${NOW}.sql.gz"
          if ${use_pipe_view}; then
              database_dump_command | pv | pigz -c > "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          else
              database_dump_command | pigz -c > "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          fi
          ;;
      *)
          FILENAME_ARCHIVE="${DB}_${NOW}.sql"
          if ${use_pipe_view}; then
              database_dump_command | pv > "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          else
              database_dump_command > "${TEMP_PATH}/${FILENAME_ARCHIVE}"
          fi
          ;;
    esac

    _rclone_backup() {
        local TYPE=$1 AGE=$2
        say "rclone delete --config ${RCLONE_CONFIG} --min-age ${AGE} ${RCLONE_PROVIDER}:${PATH_INSIDE_CONTAINER}/${TYPE}"
        rclone delete --config ${RCLONE_CONFIG} --min-age ${AGE} ${RCLONE_PROVIDER}:${PATH_INSIDE_CONTAINER}/${TYPE}

        say "rclone copy --config ${RCLONE_CONFIG} ${RCLONE_OPTIONS} \"${TEMP_PATH}/${FILENAME_ARCHIVE}\" ${RCLONE_PROVIDER}:${PATH_INSIDE_CONTAINER}/${TYPE}"
        rclone copy --config ${RCLONE_CONFIG} ${RCLONE_OPTIONS} "${TEMP_PATH}/${FILENAME_ARCHIVE}" ${RCLONE_PROVIDER}:${PATH_INSIDE_CONTAINER}/${TYPE}
    }

    #@todo: сделать глубину хранения копий все таки зависимой от параметров, но со значением по умолчанию
    # DB_MIN_AGE_DAILY, DB_MIN_AGE_WEEKLY, DB_MIN_AGE_MONTHLY (какая-то с этим была проблема)

    [[ ${DB_BACKUP_DAILY:-0} = 1 ]] && _rclone_backup "DAILY" "7d"
    [[ ${DB_BACKUP_WEEKLY:-0} = 1 ]] && [[ ${NOW_DOW} -eq 1 ]] && _rclone_backup "WEEKLY" "43d"
    [[ ${DB_BACKUP_MONTHLY:-0} = 1 ]] && [[ ${NOW_DAY} == 01 ]] && _rclone_backup "MONTHLY" "360d"

    say "rm ${TEMP_PATH}/${FILENAME_ARCHIVE}"
    rm "${TEMP_PATH}"/"${FILENAME_ARCHIVE}"
}

# Выполняет действие "Бэкап БД
function actionBackupDatabase() {
    if [[ ${ENABLE_BACKUP_DATABASE:-0} = 0 ]]; then
        [[ "${ACTION_FORCE:-n}" != "y" ]] && { echo "Backup database disabled"; exit 0; }
    fi

    # Берем опции ИЗ КОНФИГА (или дефолт)
    : "${DATABASE_RAR_OPTIONS:=-m3 -mdc}"
    : "${DATABASE_ZSTD_OPTIONS:=-5}"
    : "${DATABASE_MYSQL_OPTIONS:=-Q --single-transaction --no-tablespaces --extended-insert=false}"

    # Парсим строки в массивы
    read -ra RAR_OPTIONS <<< "${DATABASE_RAR_OPTIONS}"
    read -ra ZSTD_OPTIONS <<< "${DATABASE_ZSTD_OPTIONS}"
    read -ra MYSQL_OPTIONS <<< "${DATABASE_MYSQL_OPTIONS}"

    # Опции RCLONE в зависимости от режима
    if [[ "${VERBOSE_MODE}" = "y" ]]; then
        read -ra RCLONE_OPTIONS <<< "--copy-links --update --verbose --progress"
    else
        RAR_OPTIONS+=("-inul")  # добавляем к массиву
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
        if [[ "${ACTION_FORCE:-n}" = "n" ]]; then
            echo "Backup storage disabled"
            exit 0
        fi
    fi
    echo "$(date "+%d.%m.%Y %T %N") : started task STORAGE" >> "${PROCESS_FLAG_FILE}"

    # определяем реальный алгоритм заливки данных в хранилище: CLI>Config>'sync'
    local UPLOAD_MODE=${CLI_UPLOAD_MODE:-${STORAGE_BACKUP_ALGO:-sync}}

    local SOURCE_ROOT=${STORAGE_SOURCES_ROOT:-}

    if [[ "${VERBOSE_MODE}" = "y" ]]; then
        RCLONE_OPTIONS="--copy-links --update --verbose --progress" # эквивалентно -LPuv
    else
        RCLONE_OPTIONS="--copy-links --update"
    fi

    if [[ "$(declare -p STORAGE_SOURCES)" =~ "declare -a" ]]; then
        for SOURCE in "${STORAGE_SOURCES[@]}"; do
            say "rclone ${UPLOAD_MODE} --config ${RCLONE_CONFIG} ${RCLONE_OPTIONS} ${SOURCE_ROOT}${SOURCE} ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_STORAGE}/${SOURCE}"
            rclone ${UPLOAD_MODE} --config ${RCLONE_CONFIG} ${RCLONE_OPTIONS} ${SOURCE_ROOT}${SOURCE} ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_STORAGE}/${SOURCE}
        done
    else
        local SOURCE=${STORAGE_SOURCES}
        say "rclone ${UPLOAD_MODE} --config ${RCLONE_CONFIG} ${RCLONE_OPTIONS} --progress ${SOURCE_ROOT}${SOURCE} ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_STORAGE}/${SOURCE}"
        rclone ${UPLOAD_MODE} --config ${RCLONE_CONFIG} ${RCLONE_OPTIONS} --progress ${SOURCE_ROOT}${SOURCE} ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_STORAGE}/${SOURCE}
    fi

    echo "$(date "+%d.%m.%Y %T %N") : finished task STORAGE" >> "${PROCESS_FLAG_FILE}"
}

# Скрипт бэкапа архива
function actionBackupArchive() {
    if [[ ${ENABLE_BACKUP_ARCHIVE:-0} = 0 ]]; then
        if [[ "${ACTION_FORCE:-n}" = "n" ]]; then
            echo "Backup archive disabled"
            exit 0
        fi
    fi
    echo "$(date "+%d.%m.%Y %T %N") : started task ARCHIVE" >> "${PROCESS_FLAG_FILE}"

    local UPLOAD_MODE=${CLI_UPLOAD_MODE:-${ARCHIVE_BACKUP_ALGO:-copy}}

    local RAR_OPTIONS="${ARCHIVE_RAR_OPTIONS:--m3 -mdc -r -s}"
    local RCLONE_OPTIONS="--copy-links --update"

    if [[ "${VERBOSE_MODE}" = "y" ]]; then
        RCLONE_OPTIONS="${RCLONE_OPTIONS} --verbose --progress"
    else
        RAR_OPTIONS="${RAR_OPTIONS} -inul"
    fi

    rar a -x@${RARFILES_EXCLUDE_LIST} ${RAR_OPTIONS} ${TEMP_PATH}/${FILENAME_RAR} @${RARFILES_INCLUDE_LIST}

    rclone delete --config ${RCLONE_CONFIG} --min-age 71d ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_ARCHIVE}/
    rclone ${UPLOAD_MODE} --config ${RCLONE_CONFIG} ${RCLONE_OPTIONS} ${TEMP_PATH}/${FILENAME_RAR} ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_ARCHIVE}/

    rm -f ${TEMP_PATH}/${FILENAME_RAR}

    echo "$(date "+%d.%m.%Y %T %N") : finished task ARCHIVE" >> "${PROCESS_FLAG_FILE}"
}

function main() {
    defineColors
    parseArgs "$@"

    [[ ${ACTION_DISPLAY_HELP} = "y" ]] && { displayHelp; exit 1; }

    [[ ! -f "${CONFIG_FILE}" ]] && { displayConfigError; exit 4; }

#    if [ "${VERBOSE_MODE}" = "y" ]; then
#        echo "${__what_is_it}"
#    fi

    # Импортируем конфиг проекта
    . ${CONFIG_FILE}

    # Trap for CTRL-C
#    trap before_exit_script INT

    if { [ -z "${RCLONE_CONFIG}" ] || [ ! -f "${RCLONE_CONFIG}" ]; } && [ -f "${THIS_SCRIPT_BASEDIR}/rclone.conf" ]; then
         RCLONE_CONFIG="${THIS_SCRIPT_BASEDIR}/rclone.conf"
    fi

#    checkUniqueProcessWithConfig "$@"

    if [ ${ACTION_RESET_FLAGS} = "y" ]; then
        rm -f /dev/shm/kwbackup*
    fi

    local ACTUAL_ACTIONS=0

    [[ ${ACTION_BACKUP_DATABASE} = "y" ]] && { actionBackupDatabase; ((ACTUAL_ACTIONS++)); }
    [[ ${ACTION_BACKUP_STORAGE} = "y" ]] && { actionBackupStorage; ((ACTUAL_ACTIONS++)); }
    [[ ${ACTION_BACKUP_ARCHIVE} = "y" ]] && { actionBackupArchive; ((ACTUAL_ACTIONS++)); }

    [[ ${ACTUAL_ACTIONS} -eq 0 ]] && show_available_actions

#    local NO_ACTION=1
#    if [ ${ACTION_BACKUP_DATABASE} = "y" ]; then
#       actionBackupDatabase
#        NO_ACTION=0
#    fi
#    if [ ${ACTION_BACKUP_STORAGE} = "y" ]; then
#        actionBackupStorage
#        NO_ACTION=0
#    fi
#    if [ ${ACTION_BACKUP_ARCHIVE} = "y" ]; then
#        actionBackupArchive
#        NO_ACTION=0
#    fi
#    if [ ${NO_ACTION=0;} = 1 ]; then
#      echo -e "No one backup action(s) requested. Allowed: "
#      { declare -p ENABLE_BACKUP_DATABASE >/dev/null 2>&1 && [[ ${ENABLE_BACKUP_DATABASE:-0} = 1 ]]; } && echo -e "... database (use ${ANSI_YELLOW}--database${ANSI_RESET} option)"
#      { declare -p ENABLE_BACKUP_STORAGE >/dev/null 2>&1 && [[ ${ENABLE_BACKUP_STORAGE:-0} = 1 ]]; } && echo -e "... storage  (use ${ANSI_YELLOW}--storage${ANSI_RESET}  option)"
#      { declare -p ENABLE_BACKUP_ARCHIVE >/dev/null 2>&1 && [[ ${ENABLE_BACKUP_ARCHIVE:-0} = 1 ]]; } && echo -e "... archive (use ${ANSI_YELLOW}--archive${ANSI_RESET}  option)"
#    fi
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