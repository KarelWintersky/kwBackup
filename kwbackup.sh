#!/bin/bash

VERSION="0.8.5"

THIS_SCRIPT="${0}"
THIS_SCRIPT_BASEDIR="$(dirname ${THIS_SCRIPT})"

ACTION_DISPLAY_HELP=n
ACTION_BACKUP_DATABASE=n
ACTION_BACKUP_STORAGE=n
ACTION_BACKUP_ARCHIVE=n
ACTION_FORCE=n

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
  --install                   Install prerequesites (RAR & PIGZ)
  -h, --help                  Print this help and exit
  -v, --version               Print version and exit
"

RAR_DEB_URI=http://ftp.de.debian.org/debian/pool/non-free/r/rar/rar_5.5.0-1_amd64.deb

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
    if pidof -o %PPID -x `basename "$0"` > /dev/null; then
        echo "$(date "+%d.%m.%Y %T") EXIT: The script is already running." | tee -a "${LOG_FILE:-/var/log/kwbackup.log}"
        exit 1
    fi
}

# Парсит аргументы командной строки
function parseArgs {
    set -o errexit -o pipefail -o noclobber -o nounset

    ! getopt --test > /dev/null
    if [[ ${PIPESTATUS[0]} -ne 4 ]]; then echo "I’m sorry, `getopt --test` failed in this environment."; exit 1; fi

    local LONGOPTS=config:,database,storage,archive,help,sync-mode:,force,version,install,verbose
    local OPTIONS=c:hdsam:fv
    local PARSED=-

    ! PARSED=$(getopt --options=$OPTIONS --longoptions=$LONGOPTS --name "$0" -- "$@")
    if [[ ${PIPESTATUS[0]} -ne 0 ]]; then exit 2; fi

    eval set -- "$PARSED"

    while true; do
        case "$1" in
            -d|--database)
                ACTION_BACKUP_DATABASE=y;
                shift;
            ;;
            -s|--storage)
                ACTION_BACKUP_STORAGE=y;
                shift;
            ;;
            -a|--archive)
                ACTION_BACKUP_ARCHIVE=y;
                shift;
            ;;
            -h|--help)
                ACTION_DISPLAY_HELP=y;
                shift;
            ;;
            -c|--config)
                export CONFIG_FILE="$2";
                shift 2;
            ;;
            -m|--sync-mode)
                local RSM="$2";
                case $RSM in
                    "copy")
                        CLI_UPLOAD_MODE="copy";
                    ;;
                    "sync")
                        CLI_UPLOAD_MODE="sync";
                    ;;
                    #"sql")
                    #    CLI_UPLOAD_MODE="sql";
                    #;;
                    *)
                        echo "Invalid sync algorithm, must be 'copy' or 'sync'";
                        exit 5;
                    ;;
                esac
                shift 2;
            ;;
            --install)
                curl ${RAR_DEB_URI} -o /tmp/rar.deb && sudo dpkg -i /tmp/rar.deb && rm /tmp/rar.deb
                sudo apt install pigz
                exit 0;
            ;;
            -f|--force)
                ACTION_FORCE=y;
                shift;
            ;;
            --verbose)
                VERBOSE_MODE=y
                shift;
            ;;
            -v|--version)
                echo "${__what_is_it}"
                exit 0;
            ;;
            --)
                shift;
                break;
            ;;
            *)
                echo "Programming error";
                exit 3;
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
    echo -e "Config file ${CONFIG_FILE} ${ANSI_RED}not found${ANSI_RESET}";
}

# суб-функция, которая делает бэкап на самом деле. Принимает 2 параметра:
# $1 - имя БД для бэкапа
# $2 - суб-путь в целевом контейнере = (DB name для множественных БД в одной задаче ИЛИ '' для одной БД в задаче)
# одна БД бэкапится в CONTAINER/(DAILY|WEEKLY|MONTHLY)/*
# несколько БД бэкапятся в CONTAINER/DB/(DAILY|WEEKLY|MONTHLY)/*
function sub_backupDatabase() {
    local DB=$1
    local CONTAINER_SUBPATH=$2

    echo "-----===== Backupping ${DB} =====-----"
    case "${USE_ARCHIVER}" in
        "rar")
            FILENAME_ARCHIVE=${DB}_${NOW}.sql.rar
            mysqldump ${MYSQL_OPTIONS} -h ${MYSQL_HOST} "${DB}" | rar a -si${DB}_${NOW}.sql ${RAR_OPTIONS} ${TEMP_PATH}/${FILENAME_ARCHIVE}
        ;;
        "zip")
            FILENAME_ARCHIVE=${DB}_${NOW}.sql.gz
            mysqldump ${MYSQL_OPTIONS} -h ${MYSQL_HOST} "${DB}" | pigz -c > ${TEMP_PATH}/${FILENAME_ARCHIVE}
        ;;
        *)
            FILENAME_ARCHIVE=${DB}_${NOW}.sql
            mysqldump ${MYSQL_OPTIONS} -h ${MYSQL_HOST} "${DB}" > ${TEMP_PATH}/${FILENAME_ARCHIVE}
        ;;
    esac

    #@todo: сделать глубину хранения копий все таки зависимой от параметров, но со значением по умолчанию

    if [[ ${DB_BACKUP_DAILY:-0} = 1 ]]; then
        rclone delete --config ${RCLONE_CONFIG} --min-age 7d ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_DB}/${CONTAINER_SUBPATH}/DAILY
        rclone copy --config ${RCLONE_CONFIG} ${RCLONE_OPTIONS} "${TEMP_PATH}"/"${FILENAME_ARCHIVE}" ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_DB}/${CONTAINER_SUBPATH}/DAILY
    fi

    if [[ ${DB_BACKUP_WEEKLY:-0} = 1 ]]; then
        # if it is a sunday (7th day of week) - make store weekly backup (42 days = 7*6 + 1, so we storing last six weeks)
        if [[ ${NOW_DOW} -eq 1 ]]; then
            rclone delete --config ${RCLONE_CONFIG} --min-age 43d ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_DB}/${CONTAINER_SUBPATH}/WEEKLY
            rclone copy --config ${RCLONE_CONFIG} ${RCLONE_OPTIONS} "${TEMP_PATH}"/"${FILENAME_ARCHIVE}" ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_DB}/${CONTAINER_SUBPATH}/WEEKLY
        fi
    fi

    if [[ ${DB_BACKUP_MONTHLY:-0} = 1 ]]; then
    # backup for first day of month
        if [[ ${NOW_DAY} == 01 ]]; then
            rclone delete --config ${RCLONE_CONFIG} --min-age 360d ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_DB}/${CONTAINER_SUBPATH}/MONTHLY
            rclone copy --config ${RCLONE_CONFIG} ${RCLONE_OPTIONS} "${TEMP_PATH}"/"${FILENAME_ARCHIVE}" ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_DB}/${CONTAINER_SUBPATH}/MONTHLY
        fi
    fi

    rm "${TEMP_PATH}"/"${FILENAME_ARCHIVE}"
}

# Выполняет действие "Бэкап БД
function actionBackupDatabase() {
    if [[ ${ENABLE_BACKUP_DATABASE:-0} = 0 ]]; then
        if [[ "${ACTION_FORCE:-n}" = "n" ]]; then
            echo "Backup database disabled";
            exit 0;
        fi
    fi

    RAR_OPTIONS=""
    FILENAME_ARCHIVE=
    MYSQL_OPTIONS="-Q --no-tablespaces --extended-insert=false --single-transaction"

    if [[ "${VERBOSE_MODE}" = "y" ]]; then
        RAR_OPTIONS="-m5 -mde"
        RCLONE_OPTIONS="--copy-links --update --verbose --progress"  # -LPuv
    else
        RAR_OPTIONS="-m5 -mde -inul"
        RCLONE_OPTIONS="--copy-links --update"
    fi

    if [[ "$(declare -p DATABASES)" =~ "declare -a" ]]; then
        # бэкапим несколько БД
        for DB in "${DATABASES[@]}"
        do
            sub_backupDatabase ${DB} ${DB};
        done
    else
        # бэкапим одну БД
        local DB=${DATABASES}
        sub_backupDatabase ${DB} "";
    fi
}

# скрипт бэкапа STORAGE
function actionBackupStorage() {
    if [[ ${ENABLE_BACKUP_STORAGE:-0} = 0 ]]; then
        if [[ "${ACTION_FORCE:-n}" = "n" ]]; then
            echo "Backup storage disabled";
            exit 0;
        fi
    fi

    # определяем реальный алгоритм заливки данных в хранилище: CLI>Config>'sync'
    local UPLOAD_MODE=${CLI_UPLOAD_MODE:-${STORAGE_BACKUP_ALGO:-sync}}

    local SOURCE_ROOT=${STORAGE_SOURCES_ROOT:-}

    if [[ "$(declare -p STORAGE_SOURCES)" =~ "declare -a" ]]; then
        for SOURCE in "${STORAGE_SOURCES[@]}"
        do
            rclone ${UPLOAD_MODE} --config ${RCLONE_CONFIG} -Luv --progress ${SOURCE_ROOT}${SOURCE} ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_STORAGE}/${SOURCE}
        done
    else
        local SOURCE=${STORAGE_SOURCES}
        rclone ${UPLOAD_MODE} --config ${RCLONE_CONFIG} -Luv --progress ${SOURCE_ROOT}${SOURCE} ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_STORAGE}/${SOURCE}
    fi
}

# Скрипт бэкапа архива
function actionBackupArchive() {
    if [[ ${ENABLE_BACKUP_ARCHIVE:-0} = 0 ]]; then
        if [[ "${ACTION_FORCE:-n}" = "n" ]]; then
            echo "Backup archive disabled";
            exit 0;
        fi
    fi

    local UPLOAD_MODE=${CLI_UPLOAD_MODE:-${ARCHIVE_BACKUP_ALGO:-copy}}

    rar a -x@${RARFILES_EXCLUDE_LIST} -m5 -mde -s -r ${TEMP_PATH}/${FILENAME_RAR} @${RARFILES_INCLUDE_LIST}

    rclone delete --config ${RCLONE_CONFIG} --min-age 71d ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_ARCHIVE}/
    rclone ${UPLOAD_MODE} --config ${RCLONE_CONFIG} -Luv ${TEMP_PATH}/${FILENAME_RAR} ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_ARCHIVE}/

    rm ${TEMP_PATH}/${FILENAME_RAR}

    exit;
}


function main() {
  defineColors;
  checkUniqueProcess "$@";
  parseArgs "$@";

  if [ ${ACTION_DISPLAY_HELP} = "y" ]; then
      displayHelp;
      exit 1;
  fi

  if [ ! -f "${CONFIG_FILE}" ]; then
      displayConfigError;
      exit 4;
  fi

  if [ "${VERBOSE_MODE}" = "y" ]; then
      echo "${__what_is_it}";
  fi

  # Импортируем конфиг проекта

  . ${CONFIG_FILE}

  # проверяем существование глобального rclone.conf и говорим, что будем грузить его
  if [ -f "${THIS_SCRIPT_BASEDIR}/rclone.conf" ]; then
      RCLONE_CONFIG="${THIS_SCRIPT_BASEDIR}/rclone.conf"
  fi

  local NO_ACTION=1

  if [ ${ACTION_BACKUP_DATABASE} = "y" ]; then
      actionBackupDatabase;
      NO_ACTION=0;
  fi

  if [ ${ACTION_BACKUP_STORAGE} = "y" ]; then
      actionBackupStorage;
      NO_ACTION=0;
  fi

  if [ ${ACTION_BACKUP_ARCHIVE} = "y" ]; then
      actionBackupArchive;
      NO_ACTION=0;
  fi

  if [ ${NO_ACTION=0;} = 1 ]; then
    echo -e "No one backup action(s) requested. Allowed: "
    if [ ${ENABLE_BACKUP_DATABASE} = 1 ]; then echo -e "... database (use ${ANSI_YELLOW}--database${ANSI_RESET} option)"; fi
    if [ ${ENABLE_BACKUP_STORAGE} = 1 ]; then echo -e  "... storage  (use ${ANSI_YELLOW}--storage${ANSI_RESET}  option)"; fi
    if [ ${ENABLE_BACKUP_ARCHIVE} = 1 ]; then echo -e  "... archive  (use ${ANSI_YELLOW}--archive${ANSI_RESET}  option)"; fi
  fi
}

# ---------------- Main ----------------

main "$@"