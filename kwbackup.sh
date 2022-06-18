#!/bin/bash

VERSION="0.8.0"

THIS_SCRIPT="${0}"
THIS_SCRIPT_BASEDIR="$(dirname ${THIS_SCRIPT})"

ACTION_DISPLAY_HELP=n
ACTION_BACKUP_DATABASE=n
ACTION_BACKUP_STORAGE=n
ACTION_BACKUP_ARCHIVE=n
ACTION_FORCE=n

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
  -h, --help                  Print this help and exit
  -v, --version               Print version and exit
"

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

    local LONGOPTS=config:,database,storage,archive,help,sync-mode:,force,version
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
            -f|--force)
                ACTION_FORCE=y;
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
    CONFIG_BASEDIR="$(dirname ${CONFIG_FILE})"
}

function displayHelp() {
    echo "${__what_is_it}"
    echo "${__usage}"
}

function displayConfigError() {
    echo "Config file ${CONFIG_FILE} ${RED}not found${RESET}";
}

# Выполняет действие "Бэкап БД
# Возможны два алгоритма сжатия - zip (нужен pigz) или rar
function actionBackupDatabase() {
    if [[ ${ENABLE_BACKUP_DATABASE:-0} = 0 ]]; then
        echo "Backup database disabled";
        exit 0;
    fi

    local FILENAME_ARCHIVE=

    for DB in "${DATABASES[@]}"
    do
        #@todo: zip, rar, sql (сделать CASE)
        # FILENAME_SQL=${DB}_${NOW}.sql
        if [[ ${USE_ARCHIVER:-rar} = "rar" ]]; then
            FILENAME_ARCHIVE=${DB}_${NOW}.sql.rar
            mysqldump -Q --single-transaction -h "${MYSQL_HOST}" "${DB}" | rar a -si${DB}_${NOW}.sql -m5 -mde ${TEMP_PATH}/${FILENAME_ARCHIVE}
        else
            FILENAME_ARCHIVE=${DB}_${NOW}.sql.gz
            mysqldump -Q --single-transaction -h "${MYSQL_HOST}" "${DB}" | pigz -c > ${TEMP_PATH}/${FILENAME_ARCHIVE}
        fi

        if [[ ${DB_BACKUP_DAILY:-0} = 1 ]]; then
            rclone delete --config ${RCLONE_CONFIG} --min-age 7d ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_DB}/DAILY
            rclone copy --config ${RCLONE_CONFIG} -L -u -v "${TEMP_PATH}"/"${FILENAME_ARCHIVE}" ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_DB}/DAILY
        fi

        if [[ ${DB_BACKUP_WEEKLY:-0} = 1 ]]; then
            # if it is a sunday (7th day of week) - make store weekly backup (42 days = 7*6 + 1, so we storing last six weeks)
            if [[ ${NOW_DOW} -eq 1 ]]; then
                rclone delete --config ${RCLONE_CONFIG} --min-age 43d ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_DB}/WEEKLY
                rclone copy --config ${RCLONE_CONFIG} -L -u -v "${TEMP_PATH}"/"${FILENAME_ARCHIVE}" ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_DB}/WEEKLY
            fi
        fi

        if [[ ${DB_BACKUP_MONTHLY:-0} = 1 ]]; then
        # backup for first day of month
            if [[ ${NOW_DAY} == 01 ]]; then
                rclone delete --config ${RCLONE_CONFIG} --min-age 360d ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_DB}/MONTHLY
                rclone copy --config ${RCLONE_CONFIG} -L -u -v "${TEMP_PATH}"/"${FILENAME_ARCHIVE}" ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_DB}/MONTHLY
            fi
        fi

        rm "${TEMP_PATH}"/"${FILENAME_ARCHIVE}"
    done
}

function actionBackupStorage() {
    if [[ ${ENABLE_BACKUP_STORAGE:-0} = 0 ]]; then
        echo "Backup storage disabled";
        exit 0;
    fi

    # определяем реальный алгоритм заливки данных в хранилище: CLI>Config>'sync'
    local UPLOAD_MODE=${CLI_UPLOAD_MODE:-${STORAGE_BACKUP_ALGO:-sync}}

    # echo "Sync mode from cli: ${CLI_UPLOAD_MODE}"
    # echo "Sync mode from config: ${STORAGE_BACKUP_ALGO}"
    # echo "Final mode: ${UPLOAD_MODE}"
    # exit 0;

    if [[ "$(declare -p STORAGE_SOURCES)" =~ "declare -a" ]]; then
        for SOURCE in "${STORAGE_SOURCES[@]}"
        do
            rclone ${UPLOAD_MODE} --config ${RCLONE_CONFIG} -L --progress -u -v ${SOURCE} ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_STORAGE}/${SOURCE}
        done
    else
        rclone ${UPLOAD_MODE} --config ${RCLONE_CONFIG} -L --progress -u -v ${STORAGE_SOURCES} ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_STORAGE}/${SOURCE}
    fi
}

function actionBackupArchive() {
    if [[ ${ENABLE_BACKUP_ARCHIVE:-0} = 0 ]]; then
        echo "Backup file archives disabled";
        exit 0;
    fi

    local UPLOAD_MODE=${CLI_UPLOAD_MODE:-${ARCHIVE_BACKUP_ALGO:-copy}}

    rar a -x@${RARFILES_EXCLUDE_LIST} -m5 -mde -s -r ${TEMP_PATH}/${FILENAME_RAR} @${RARFILES_INCLUDE_LIST}

    rclone delete --config ${RCLONE_CONFIG} --min-age 71d ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_ARCHIVE}/
    rclone ${UPLOAD_MODE} --config ${RCLONE_CONFIG} -L -u -v ${TEMP_PATH}/${FILENAME_RAR} ${RCLONE_PROVIDER}:${CLOUD_CONTAINER_ARCHIVE}/

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

  # Импортируем конфиг проекта

  . ${CONFIG_FILE}

  # проверяем существование глобального rclone.conf и говорим, что будем грузить его
  if [ -f "${THIS_SCRIPT_BASEDIR}/rclone.conf" ]; then
    RCLONE_CONFIG="${THIS_SCRIPT_BASEDIR}/rclone.conf"
  fi

  if [ ${ACTION_BACKUP_DATABASE} = "y" ]; then
    actionBackupDatabase;
  fi

  if [ ${ACTION_BACKUP_STORAGE} = "y" ]; then
    actionBackupStorage;
  fi

  if [ ${ACTION_BACKUP_ARCHIVE} = "y" ]; then
    actionBackupArchive;
  fi
}

# ---------------- Main ----------------

main "$@"