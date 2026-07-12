# Mautic image — single-container, derived from serversideup/php.
# See README.md for design notes and usage.
#
# Build:
#   podman build \
#     --build-arg MAUTIC_VERSION=7.1.1 \
#     --build-arg PHP_VERSION=8.3 \
#     -t ghcr.io/pikapods/docker-mautic:7.1.1 .
#
# CI also passes BASE_IMAGE (digest-pinned), BASE_DIGEST, IMAGE_REVISION,
# GIT_SHA, and BUILD_DATE to populate OCI labels and pin the base. Local
# builds work without them via the defaults — but defaults are
# best-effort reproducible only: the BASE_IMAGE tag floats with
# upstream, so production builds should always set BASE_IMAGE to a
# digest-pinned reference.

ARG PHP_VERSION=8.3
# BASE_IMAGE lets CI pin to a digest (serversideup/php@sha256:...) so the
# build is reproducible and we can label the exact base used. Local
# `podman build .` falls back to the tag-based default.
ARG BASE_IMAGE=serversideup/php:${PHP_VERSION}-fpm-nginx-alpine

# ---------------------------------------------------------------------------
# Stage 1 — builder. Runs composer create-project (which triggers npm
# install + webpack via Mautic's post-install scripts), then prunes the
# resulting tree.
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS builder

USER root

# Pinned stable. Override with --build-arg MAUTIC_VERSION=<tag> to bump.
# Do NOT use floating constraints like '7.x-dev' or '^7.1' as the
# default: build reproducibility depends on this being an exact tag
# that composer resolves to the same artifact across builds.
ARG MAUTIC_VERSION=7.1.1

RUN apk add --no-cache git unzip nodejs npm curl \
    && install-php-extensions intl pdo_mysql mysqli zip bcmath sockets gd imap exif opcache iconv

# Helper that reads the upstream monorepo lock and emits exact composer pins.
COPY build/collect-upstream-pins.php /usr/local/lib/

# composer create-project lays down Mautic's deployable artifact —
# docroot/, var/, config/ split — and runs npm + webpack via post-install
# scripts. mautic:assets:generate is a safety net in case the webpack step
# did not run (e.g. plugin-level overrides).
#
# recommended-project ships NO composer.lock, so a plain create-project
# re-resolves every floating transitive dep on each build — the tree drifts
# freely (that is how Twig 3.28.0 slipped in and 500'd /s/login). To make the
# build reproducible we resolve against the exact set Mautic validated for
# this release: the mautic/mautic monorepo commits a composer.lock at every
# tag, and empirically it covers 100% of our third-party surface.
#
# Flow: lay down recommended-project's composer.json WITHOUT installing, fetch
# the upstream lock for ${MAUTIC_VERSION}, pin every third-party (non-mautic/*)
# prod package from it via `require --no-update`, then install once. The
# scaffold (docroot split) runs as a composer plugin, not a script, so it is
# produced regardless of --no-scripts; npm/webpack run on the final, pinned
# install. Mautic's own plugins/themes stay at recommended-project's exacts.
#
# COMPOSER_POLICY_ADVISORIES_BLOCK=0 on the install: Composer 2.10 blocks
# resolving to any version with an active security advisory. Pinning to
# upstream's validated lock deliberately selects the exact versions Mautic
# 7.1.x shipped, some of which have since had advisories filed against them
# (symfony, twig, guzzle, aws-sdk, …). Floating dodged those by picking newer
# patched releases — which is exactly how the broken Twig 3.28 got in. We
# accept upstream's tree verbatim: same security posture as a stock Mautic
# ${MAUTIC_VERSION} install, no better and no worse, but reproducible.
RUN COMPOSER_ALLOW_SUPERUSER=1 COMPOSER_PROCESS_TIMEOUT=10000 \
        composer create-project \
            --no-interaction --no-progress --no-dev --no-scripts --no-install \
            "mautic/recommended-project:${MAUTIC_VERSION}" /build \
    && curl -fsSL \
        "https://raw.githubusercontent.com/mautic/mautic/${MAUTIC_VERSION}/composer.lock" \
        -o /tmp/upstream-core.lock \
    && cd /build \
    && COMPOSER_ALLOW_SUPERUSER=1 composer require \
            --no-interaction --no-update --no-scripts \
            $(php /usr/local/lib/collect-upstream-pins.php /tmp/upstream-core.lock composer.json) \
    && COMPOSER_ALLOW_SUPERUSER=1 COMPOSER_PROCESS_TIMEOUT=10000 \
       COMPOSER_POLICY_ADVISORIES_BLOCK=0 \
        composer install --no-interaction --no-progress --no-dev \
    && php bin/console mautic:assets:generate --no-debug --env=prod \
    && rm -rf var/cache/js \
    && if [ -d node_modules ]; then \
           find node_modules -mindepth 1 -maxdepth 1 \
               -not \( -name 'jquery' -or -name 'vimeo-froogaloop2' \) \
               -exec rm -rf {} +; \
       fi \
    && rm -rf .git

# ---------------------------------------------------------------------------
# Stage 2 — final.
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE}

ARG PHP_VERSION
ARG MAUTIC_VERSION=7.1.1
ARG IMAGE_REVISION=r1
ARG BASE_DIGEST=
ARG GIT_SHA=
ARG BUILD_DATE=

LABEL org.opencontainers.image.title="Mautic" \
      org.opencontainers.image.description="Self-maintained single-container Mautic" \
      org.opencontainers.image.source="https://github.com/pikapods/docker-mautic" \
      org.opencontainers.image.licenses="GPL-3.0" \
      org.opencontainers.image.version="${MAUTIC_VERSION}-${IMAGE_REVISION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.base.name="serversideup/php:${PHP_VERSION}-fpm-nginx-alpine" \
      org.opencontainers.image.base.digest="${BASE_DIGEST}"

USER root

# Runtime deps. mariadb-client provides mysqladmin for the bootstrap DB
# wait. amqp + redis ext are optional but cheap to ship for users who
# configure async Symfony Messenger transports.
RUN apk add --no-cache \
        git \
        mariadb-client \
        tzdata \
    && install-php-extensions \
        intl \
        pdo_mysql \
        mysqli \
        zip \
        bcmath \
        sockets \
        gd \
        imap \
        exif \
        opcache \
        amqp \
        redis \
        iconv

COPY --from=builder --chown=www-data:www-data /build /var/www/html

# Mautic's web root is docroot/; serversideup/php's nginx expects
# /var/www/html/public. A symlink keeps us on the base's default nginx
# config without overriding NGINX_WEBROOT.
#
# All mutable state under /data with symlinks back into the app tree.
# Targets do not resolve until /data is populated by the bootstrap —
# fine, the bootstrap mkdir -p's them on first boot.
RUN ln -s docroot /var/www/html/public \
    && rm -rf /var/www/html/config \
              /var/www/html/var/logs \
              /var/www/html/docroot/media/files \
              /var/www/html/docroot/media/images \
    && ln -s /data/config /var/www/html/config \
    && ln -s /data/var/logs /var/www/html/var/logs \
    && ln -s /data/media/files /var/www/html/docroot/media/files \
    && ln -s /data/media/images /var/www/html/docroot/media/images \
    && mkdir -p /data \
    && chown www-data:www-data /data \
    && chown -R www-data:www-data /var/www/html

# Build-arg UID/GID override. The base image fixes www-data at 82:82;
# rebuild with --build-arg WWW_DATA_UID=$(id -u) --build-arg
# WWW_DATA_GID=$(id -g) for bind-mount UX without host-side chown.
# Guarded so the default-build path adds no extra layer work.
ARG WWW_DATA_UID=82
ARG WWW_DATA_GID=82
RUN if [ "$WWW_DATA_UID" != "82" ] || [ "$WWW_DATA_GID" != "82" ]; then \
        docker-php-serversideup-set-id www-data "${WWW_DATA_UID}:${WWW_DATA_GID}" \
     && docker-php-serversideup-set-file-permissions --owner "${WWW_DATA_UID}:${WWW_DATA_GID}" \
     && chown "${WWW_DATA_UID}:${WWW_DATA_GID}" /data; \
    fi

VOLUME /data

# Overlay entrypoint hook, s6 services, helpers, nginx site config.
COPY rootfs/ /

# - chmod *before* docker-php-serversideup-s6-init: the init tool moves
#   /etc/entrypoint.d/*.sh into /etc/s6-overlay/scripts/ and renames them,
#   so chmod afterwards at the original path would fail.
# - chown /etc/nginx to www-data: ServerSideUp's 10-init-webserver-config
#   runs as www-data and renders /etc/nginx/nginx.conf at boot. After our
#   COPY rootfs/, the directory ends up root-owned and nginx fails to
#   start with "Permission denied" opening nginx.conf.
RUN chmod +x /etc/entrypoint.d/20-mautic-bootstrap.sh \
             /etc/s6-overlay/s6-rc.d/mautic-scheduler/run \
             /etc/s6-overlay/s6-rc.d/mautic-messenger-email/run \
             /etc/s6-overlay/s6-rc.d/mautic-messenger-hit/run \
             /etc/s6-overlay/s6-rc.d/mautic-messenger-failed/run \
             /usr/local/bin/mautic-db-guard \
             /usr/local/bin/mautic-healthcheck \
             /usr/local/bin/mautic-local-php-render \
    && rm /etc/nginx/server-opts.d/security.conf \
    && chown -R www-data:www-data /etc/nginx \
    && docker-php-serversideup-s6-init

# Image defaults.
# AUTORUN_ENABLED=false: we own the boot sequence; the base's Laravel
# automations would otherwise interfere.
# SSL_MODE=off: TLS terminates at the reverse proxy.
ENV AUTORUN_ENABLED=false \
    SSL_MODE=off \
    APP_BASE_DIR=/var/www/html \
    ENABLE_MAUTIC_SCHEDULER=TRUE \
    ENABLE_MAUTIC_WORKER_EMAIL=TRUE \
    ENABLE_MAUTIC_WORKER_HIT=TRUE \
    ENABLE_MAUTIC_WORKER_FAILED=TRUE \
    PHP_OPCACHE_ENABLE=1 \
    PHP_MEMORY_LIMIT=512M \
    PHP_UPLOAD_MAX_FILESIZE=512M \
    PHP_POST_MAX_SIZE=512M \
    PHP_MAX_EXECUTION_TIME=300 \
    PHP_DATE_TIMEZONE=UTC

# Health endpoint hits /s/login (Mautic admin login route under the /s
# prefix). Spoofs the Host header to match MAUTIC_URL so Symfony's
# trusted_hosts (if configured) does not 421 the loopback probe.
# start-period is generous to absorb first-boot install + cache warmup.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD mautic-healthcheck || exit 1

EXPOSE 8080

USER www-data
