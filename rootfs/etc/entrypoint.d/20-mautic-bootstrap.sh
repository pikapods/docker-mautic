#!/bin/sh
# Mautic bootstrap — runs once before s6 starts nginx + php-fpm + workers.
# POSIX sh. Pipelines avoided so console exit status is never masked.
set -eu

APP_DIR=/var/www/html
CONFIG_DIR=/data/config
LOCAL_PHP=${CONFIG_DIR}/local.php

log() { printf '[mautic-bootstrap] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------------------
# Preflight: /data writability + config-as-file rejection.
# ---------------------------------------------------------------------------
if ! ( : > /data/.write-test ) 2>/dev/null; then
    cat >&2 <<EOF
ERROR: /data is not writable by the container (container UID:GID $(id -u):$(id -g)).
       Ownership of the bind-mount target must match how the container sees it.
       The right fix depends on your runtime — see README "User & permissions":
         - rootful docker/podman bind mount: chown the host dir to $(id -u):$(id -g)
         - rootless podman: add --userns=keep-id:uid=$(id -u),gid=$(id -g)
         - or use a named volume
         - or rebuild with --build-arg WWW_DATA_UID=...
EOF
    exit 1
fi
rm -f /data/.write-test

if [ -e "$CONFIG_DIR" ] && [ ! -d "$CONFIG_DIR" ]; then
    die "/data/config exists and is not a directory; expected /data/config/ holding local.php"
fi

# ---------------------------------------------------------------------------
# Env validation.
# ---------------------------------------------------------------------------
: "${MAUTIC_URL:?MAUTIC_URL is required (e.g. https://mautic.example.com)}"
: "${MAUTIC_DB_HOST:?MAUTIC_DB_HOST is required}"
: "${MAUTIC_DB_DATABASE:?MAUTIC_DB_DATABASE is required}"
: "${MAUTIC_DB_USER:?MAUTIC_DB_USER is required}"
: "${MAUTIC_DB_PASSWORD:?MAUTIC_DB_PASSWORD is required}"

MAUTIC_DB_PORT=${MAUTIC_DB_PORT:-3306}
MAUTIC_DB_TABLE_PREFIX=${MAUTIC_DB_TABLE_PREFIX:-}
ADMIN_FIRSTNAME=${ADMIN_FIRSTNAME:-Admin}
ADMIN_LASTNAME=${ADMIN_LASTNAME:-User}

export MAUTIC_URL MAUTIC_DB_HOST MAUTIC_DB_PORT MAUTIC_DB_DATABASE \
       MAUTIC_DB_USER MAUTIC_DB_PASSWORD MAUTIC_DB_TABLE_PREFIX

# ---------------------------------------------------------------------------
# /data tree.
# ---------------------------------------------------------------------------
mkdir -p \
    "$CONFIG_DIR" \
    /data/var/logs \
    /data/var/cache \
    /data/var/tmp \
    /data/var/spool \
    /data/media/files \
    /data/media/images

chown -R www-data:www-data /data

# ---------------------------------------------------------------------------
# Render local.php from env. The helper preserves secret_key if it already
# exists in the file; everything else (db_*, site_url) is overwritten from
# env. MAUTIC_PARAM_<KEY> env vars pass through as <key> => <value>.
# ---------------------------------------------------------------------------
log "rendering $LOCAL_PHP"
if ! mautic-local-php-render "$LOCAL_PHP"; then
    die "mautic-local-php-render failed"
fi
chown www-data:www-data "$LOCAL_PHP"
chmod 0640 "$LOCAL_PHP"

# ---------------------------------------------------------------------------
# Wait for DB. 30s deadline, fail fast on timeout.
# ---------------------------------------------------------------------------
log "waiting for MySQL at $MAUTIC_DB_HOST:$MAUTIC_DB_PORT (30s deadline)"
deadline=$(( $(date +%s) + 30 ))
while :; do
    if mysqladmin ping -h "$MAUTIC_DB_HOST" -P "$MAUTIC_DB_PORT" --silent >/dev/null 2>&1; then
        break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        die "DB at $MAUTIC_DB_HOST:$MAUTIC_DB_PORT not reachable within 30s"
    fi
    sleep 1
done
log "DB is reachable"

# ---------------------------------------------------------------------------
# Preflight: refuse to migrate against a non-Mautic / partially-migrated DB.
# ---------------------------------------------------------------------------
log "preflight: checking DB is empty or Mautic-owned"
rc=0
( cd "$APP_DIR" && mautic-db-guard preflight ) || rc=$?
case "$rc" in
    0) ;;
    1) exit 1 ;;  # guard already printed an actionable error
    *) die "mautic-db-guard preflight crashed (exit $rc)" ;;
esac
unset rc

# ---------------------------------------------------------------------------
# Install (first boot) or migrate (subsequent boots).
#
# Probe 1 — the local.php sanity check used by upstream docker-mautic:
#   non-empty db_driver + site_url means we've at least configured a DB.
# Probe 2 — mautic-db-guard installed: real schema check on the DB.
# Both must agree before we treat the install as complete.
# ---------------------------------------------------------------------------
config_ok=1
php -r "include('$LOCAL_PHP'); exit(!empty(\$parameters['db_driver']) && !empty(\$parameters['site_url']) ? 0 : 1);" \
    || config_ok=0

schema_ok=1
( cd "$APP_DIR" && mautic-db-guard installed ) >/dev/null 2>&1 || schema_ok=0

if [ "$config_ok" = 1 ] && [ "$schema_ok" = 1 ]; then
    log "Mautic already installed; running migrations"
    ( cd "$APP_DIR" && php bin/console doctrine:migrations:sync-metadata-storage -n --env=prod --no-debug ) >&2 \
        || die "doctrine:migrations:sync-metadata-storage failed"
    ( cd "$APP_DIR" && php bin/console doctrine:migrations:migrate -n --allow-no-migration --env=prod --no-debug ) >&2 \
        || die "doctrine:migrations:migrate failed"
elif [ -n "${ADMIN_EMAIL:-}" ] && [ -n "${ADMIN_PASSWORD:-}" ]; then
    log "Mautic not installed; running mautic:install with seeded admin"
    # mautic:install's checkIfInstalled() treats any local.php carrying both
    # db_driver and site_url as "already installed" and skips schema creation.
    # Our pre-render (needed by mautic-db-guard) populates exactly those keys,
    # so remove it here — schema_ok=0 already proved the DB has no Mautic
    # schema. mautic:install writes its own local.php from the CLI args below,
    # and the post-install re-render reasserts ops-managed keys from env.
    rm -f "$LOCAL_PHP"
    ( cd "$APP_DIR" && php bin/console mautic:install --force \
        --admin_email "$ADMIN_EMAIL" \
        --admin_password "$ADMIN_PASSWORD" \
        --admin_firstname "$ADMIN_FIRSTNAME" \
        --admin_lastname "$ADMIN_LASTNAME" \
        --db_driver pdo_mysql \
        --db_host "$MAUTIC_DB_HOST" \
        --db_port "$MAUTIC_DB_PORT" \
        --db_name "$MAUTIC_DB_DATABASE" \
        --db_user "$MAUTIC_DB_USER" \
        --db_password "$MAUTIC_DB_PASSWORD" \
        --db_table_prefix "$MAUTIC_DB_TABLE_PREFIX" \
        "$MAUTIC_URL" ) >&2 \
        || die "mautic:install failed"
    # mautic:install writes its own local.php; re-render to apply our
    # passthrough conventions and reassert ops-managed keys.
    mautic-local-php-render "$LOCAL_PHP" \
        || die "post-install mautic-local-php-render failed"
    chown www-data:www-data "$LOCAL_PHP"
    chmod 0640 "$LOCAL_PHP"
else
    log "WARN: Mautic not installed and ADMIN_EMAIL/ADMIN_PASSWORD not set;"
    log "      skipping install. Finish setup via $MAUTIC_URL/installer."
fi

# ---------------------------------------------------------------------------
# Cache warmup. Best-effort: a stale cache should not block the container.
# ---------------------------------------------------------------------------
( cd "$APP_DIR" && php bin/console cache:clear --env=prod --no-debug ) >&2 \
    || log "WARN: cache:clear returned non-zero"

log "bootstrap complete"
