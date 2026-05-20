# docker-mautic

Single-container Mautic image. nginx + php-fpm + scheduler + Symfony
Messenger workers, all supervised by s6 inside one container. All
mutable state lives under `/data`.

Derived from `serversideup/php:*-fpm-nginx-alpine`.

This image powers Mautic on [PikaPods](https://www.pikapods.com) and is
maintained by the PikaPods team. It's published here for our users'
reference and the benefit of the wider community.

## Quick start

```sh
docker compose up -d
# follow first-boot logs
docker compose logs -f mautic
```

Browse to <http://localhost:8080/s/login> and sign in with the
credentials from `compose.yaml`.

## Build

```sh
podman build \
    --build-arg MAUTIC_VERSION=7.1.1 \
    --build-arg PHP_VERSION=8.3 \
    -t docker-mautic:7.1.1 .
```

Build args:

| Arg | Default | Notes |
|---|---|---|
| `MAUTIC_VERSION` | `7.1.1` | Pinned stable. `composer create-project mautic/recommended-project:<ver>` resolves it. Override to bump or to build a dev branch (`7.x-dev`). |
| `PHP_VERSION` | `8.3` | Must satisfy Mautic's `~8.2` constraint. |
| `BASE_IMAGE` | `serversideup/php:${PHP_VERSION}-fpm-nginx-alpine` | Tag-based default tracks upstream. Production builds should override with a digest reference (`serversideup/php@sha256:…`) for full reproducibility. |
| `WWW_DATA_UID` / `WWW_DATA_GID` | `82` | Rebuild with `$(id -u)` / `$(id -g)` for bind-mount UX without host-side chown. |

## Environment variables

Required:

| Var | Description |
|---|---|
| `MAUTIC_URL` | Public URL, no trailing slash (e.g. `https://mautic.example.com`). Used for `site_url` in `local.php` and for the healthcheck `Host:` header. |
| `MAUTIC_DB_HOST` | MySQL/MariaDB host. |
| `MAUTIC_DB_DATABASE` | Database name. Must be dedicated to Mautic — the bootstrap guard refuses to migrate a foreign schema. |
| `MAUTIC_DB_USER` | DB user. |
| `MAUTIC_DB_PASSWORD` | DB password. |

Optional:

| Var | Default | Description |
|---|---|---|
| `MAUTIC_DB_PORT` | `3306` | DB port. |
| `MAUTIC_DB_TABLE_PREFIX` | (empty) | Table prefix. |
| `ADMIN_EMAIL` | — | If set on first boot, seeds the admin user via `mautic:install`. |
| `ADMIN_PASSWORD` | — | Required when `ADMIN_EMAIL` is set. |
| `ADMIN_FIRSTNAME` | `Admin` | |
| `ADMIN_LASTNAME` | `User` | |
| `ENABLE_MAUTIC_SCHEDULER` | `TRUE` | Set to anything else to park the scheduler in `sleep infinity`. |
| `ENABLE_MAUTIC_WORKER_EMAIL` / `_HIT` / `_FAILED` | `TRUE` | Per-transport toggles for the Messenger workers. |
| `MAUTIC_PARAM_<KEY>` | — | Passthrough into `local.php` as `<key> => <value>`. E.g. `MAUTIC_PARAM_MAILER_DSN=smtp://user:pass@smtp.example.com:587`. Sentinel values `unset` / `null` / empty string delete the key. |
| `PHP_*` | — | Forwarded to the base image's php.ini renderer (`PHP_MEMORY_LIMIT`, `PHP_UPLOAD_MAX_FILESIZE`, `PHP_DATE_TIMEZONE`, …). |

If neither `ADMIN_EMAIL` nor an existing install is detected on first
boot, the bootstrap logs a warning and skips install — finish setup via
`<MAUTIC_URL>/installer`.

## Volumes

| Path | Purpose | Persistent |
|---|---|---|
| `/data` | All user state (config, logs, uploads) | Yes |
| `/var/www/html` | App code | No — baked into image |

`/data` layout:

```
/data/
├── config/local.php   # rendered each boot, preserves secret_key
├── var/logs/          # symlinked from /var/www/html/var/logs
├── media/files/       # user uploads
└── media/images/      # generated/uploaded images
```

The `var/cache/`, `var/tmp/`, and `var/spool/` directories stay inside
the image and are rebuilt on demand. Bind-mount them yourself if you
need spool persistence.

## Bootstrap behaviour

The `20-mautic-bootstrap.sh` hook runs once before s6 starts the long-run
services. Phases:

1. Reject if `/data` is not writable or `/data/config` is a regular file.
2. Validate required env vars.
3. Create `/data` tree, chown to `www-data`.
4. Render `/data/config/local.php` from env. DB block + `site_url` are
   overwritten every boot. `secret_key` is preserved if already set;
   otherwise a 32-byte hex value is generated. `MAUTIC_PARAM_*` env vars
   are merged in.
5. Wait for the DB (`mysqladmin ping`, 30s deadline).
6. `mautic-db-guard preflight` — pass if the DB is empty or has both a
   Doctrine `migrations` table and Mautic core tables. Refuse otherwise.
7. If installed: `doctrine:migrations:sync-metadata-storage` then
   `doctrine:migrations:migrate`.
   Otherwise, if `ADMIN_EMAIL`+`ADMIN_PASSWORD` are set: `mautic:install`.
   Otherwise: log warning, skip — finish setup via the web installer.
8. `cache:clear` (best-effort).

## User & permissions

The container runs as `www-data` (UID 82 on Alpine). For bind mounts the
host directory must be owned by 82:82, or rebuild with
`--build-arg WWW_DATA_UID=$(id -u) WWW_DATA_GID=$(id -g)`. Named volumes
just work.

## Non-goals (this iteration)

- pytest integration tests.
- CI / GitHub Actions / digest pinning / multi-tag publishing.
- Plugin/theme installation hooks beyond a passthrough directory.
- Postgres support.
- Version-migration / upgrade tooling.
