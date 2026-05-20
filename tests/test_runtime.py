import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.runtime

IMAGE = os.environ["IMAGE"]
# Mautic's first boot does a full install: schema creation, admin seed, and a
# prod cache warmup — materially heavier than freescout, so the readiness
# deadline is generous.
READY_DEADLINE_S = 240
HEALTHY_DEADLINE_S = 120

# Mautic/Symfony's trusted_hosts (if configured) 421s any request whose Host
# header doesn't match site_url. The fixtures pass MAUTIC_URL=http://localhost:8080,
# so every probe declares Host: localhost:8080 — the random host port we bind
# to is only the TCP destination.
APP_HOST_HEADER = "localhost:8080"
LOGIN_PATH = "/s/login"


def _sh(*args, check=True, capture=True):
    return subprocess.run(
        list(args),
        capture_output=capture, text=True, check=check,
    )


def _exec(container, *args, check=False):
    return subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, check=check,
    )


def _wait_mysql_ready(container, deadline_s=60):
    # MariaDB init takes longer than postgres on first boot (datadir bootstrap
    # + grant rebuild). Credentials are required because the root account is
    # password-protected by MARIADB_ROOT_PASSWORD. MariaDB 11 dropped the
    # mysql/mysqladmin symlinks — invoke the native names directly.
    end = time.time() + deadline_s
    while time.time() < end:
        r = _exec(
            container, "mariadb-admin", "ping",
            "-h", "127.0.0.1", "-uroot", "-ptest", "--silent",
        )
        if r.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"mariadb container {container} not ready within {deadline_s}s")


def _http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"Host": APP_HOST_HEADER})
    return urllib.request.urlopen(req, timeout=timeout)


def _wait_http_200(url, deadline_s):
    end = time.time() + deadline_s
    last_err = None
    while time.time() < end:
        try:
            with _http_get(url, timeout=5) as r:
                if r.status == 200:
                    return
                last_err = f"status={r.status}"
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = repr(e)
        time.sleep(2)
    raise RuntimeError(f"{url} did not return 200 within {deadline_s}s (last={last_err})")


def _host_port(container, container_port):
    r = _sh("docker", "port", container, container_port)
    # Output like "0.0.0.0:32768\n[::]:32768\n" — take first line.
    line = r.stdout.splitlines()[0]
    return int(line.rsplit(":", 1)[1])


def _container_health(container):
    # Returns the parsed .State.Health dict, or None when the daemon does not
    # surface a healthcheck. Under podman, an image built without
    # `--format docker` carries no HEALTHCHECK (it is a Docker-specific config
    # extension dropped from OCI images), so .State.Health is null. Callers
    # skip rather than fail in that case.
    r = _sh("docker", "inspect", "--format", "{{json .State.Health}}",
            container, check=False)
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    if not out or out == "null":
        return None
    try:
        return json.loads(out) or None
    except json.JSONDecodeError:
        return None


def _wait_healthcheck_ok(container, deadline_s):
    # Run the healthcheck script in the container directly. Independent of the
    # daemon's health scheduler and of build format, so it is a reliable
    # readiness signal for the public-URL stack whose redirects point at an
    # unresolvable host (HTTP-polling from the host would chase those).
    end = time.time() + deadline_s
    last = None
    while time.time() < end:
        r = _exec(container, "mautic-healthcheck")
        if r.returncode == 0:
            return
        last = (r.stdout + r.stderr).strip()
        time.sleep(3)
    raise RuntimeError(
        f"mautic-healthcheck never succeeded within {deadline_s}s (last={last!r})"
    )


def _wait_bootstrap_complete(container, deadline_s=READY_DEADLINE_S):
    # The s6 longruns depend on the 20-mautic-bootstrap oneshot, which only
    # finishes after a post-install `cache:clear` — well after nginx starts
    # serving /s/login. Until then the longruns are supervised but not started,
    # so probing their processes right after HTTP-readiness inspects too early.
    end = time.time() + deadline_s
    while time.time() < end:
        logs = _sh("docker", "logs", container, check=False)
        if "bootstrap complete" in (logs.stdout + logs.stderr):
            return
        time.sleep(2)
    raise RuntimeError(f"{container}: bootstrap did not complete within {deadline_s}s")


def _wait_proc(container, needle, deadline_s):
    end = time.time() + deadline_s
    while time.time() < end:
        if _proc_cmdline_contains(container, needle):
            return True
        time.sleep(2)
    return False


def _read_local_php(container, key):
    # Echo a single parameter from the rendered /data/config/local.php.
    r = _exec(
        container, "php", "-r",
        f"include '/data/config/local.php'; echo $parameters['{key}'] ?? '';",
    )
    return r.stdout.strip()


def _users_count(db):
    # No production `users-count` subcommand exists in Mautic's guard, so query
    # the DB sidecar directly. -N -B = no column names, tab-batch output.
    r = _exec(
        db, "mariadb", "-uroot", "-ptest", "mautic", "-N", "-B",
        "-e", "SELECT COUNT(*) FROM users", check=True,
    )
    return int(r.stdout.strip())


def _proc_cmdline_contains(container, needle):
    # busybox `ps` truncates argv for shebang-launched scripts, so read
    # /proc/<pid>/cmdline directly, replacing NUL with space per process so
    # multi-token probes like "messenger:consume email" match within one
    # process. The needle is passed via the environment (N=...), never as an
    # argument, and matched with a shell `case` builtin — otherwise the probing
    # shell's own argv would contain the needle and it would always match
    # itself (a `... | grep -F <needle>` pipeline self-matches).
    script = (
        'for f in /proc/[0-9]*/cmdline; do '
        'c=$(tr "\\0" " " < "$f" 2>/dev/null); '
        'case "$c" in *"$N"*) exit 0;; esac; '
        'done; exit 1'
    )
    return subprocess.run(
        ["docker", "exec", "-e", f"N={needle}", container, "sh", "-c", script],
        capture_output=True, text=True,
    ).returncode == 0


def _dump_logs(container):
    r = _sh("docker", "logs", container, check=False)
    print(r.stdout)
    print(r.stderr)


# ---------------------------------------------------------------------------
# Stack fixtures. Each builds its own network + MariaDB sidecar + mautic
# container, names everything `mtc-*` so cleanup/log globs can target them,
# and tears down in `finally`.
# ---------------------------------------------------------------------------

def _run_mariadb(net, name):
    _sh(
        "docker", "run", "-d", "--name", name, "--network", net,
        "-e", "MARIADB_ROOT_PASSWORD=test",
        "-e", "MARIADB_DATABASE=mautic",
        "-e", "MARIADB_USER=mautic",
        "-e", "MARIADB_PASSWORD=test",
        "mariadb:11",
    )
    _wait_mysql_ready(name)


def _mautic_db_env(db):
    return [
        "-e", f"MAUTIC_DB_HOST={db}",
        "-e", "MAUTIC_DB_PORT=3306",
        "-e", "MAUTIC_DB_DATABASE=mautic",
        "-e", "MAUTIC_DB_USER=mautic",
        "-e", "MAUTIC_DB_PASSWORD=test",
    ]


@pytest.fixture(scope="session")
def stack():
    suffix = secrets.token_hex(4)
    net = f"mtc-net-{suffix}"
    db = f"mtc-db-{suffix}"
    mtc = f"mtc-{suffix}"

    _sh("docker", "network", "create", net)
    try:
        _run_mariadb(net, db)
        _sh(
            "docker", "run", "-d", "--name", mtc, "--network", net,
            "-e", "MAUTIC_URL=http://localhost:8080",
            *_mautic_db_env(db),
            "-e", "ADMIN_EMAIL=admin@smoke.local",
            "-e", "ADMIN_PASSWORD=S3cure-Smoke!2026",
            # Passthrough probe: free here, no extra boot needed.
            "-e", "MAUTIC_PARAM_MAILER_FROM_NAME=Acme",
            "-e", "MAUTIC_PARAM_MAILER_FROM_EMAIL=ops@acme.test",
            "-p", ":8080",
            IMAGE,
        )
        port = _host_port(mtc, "8080")
        try:
            _wait_http_200(f"http://127.0.0.1:{port}{LOGIN_PATH}", READY_DEADLINE_S)
        except RuntimeError:
            _dump_logs(mtc)
            raise
        yield {"mtc": mtc, "db": db, "net": net, "port": port}
    finally:
        for name in (mtc, db):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


@pytest.fixture(scope="session")
def stack_persistent():
    # Same as `stack` but with a named volume at /data, so a `docker restart`
    # preserves the rendered local.php + installed schema — the precondition
    # for the secret-key-stability and no-reseed restart tests.
    suffix = secrets.token_hex(4)
    net = f"mtc-net-{suffix}"
    db = f"mtc-db-{suffix}"
    mtc = f"mtc-{suffix}"
    vol = f"mtc-data-{suffix}"

    _sh("docker", "network", "create", net)
    _sh("docker", "volume", "create", vol)
    try:
        _run_mariadb(net, db)
        _sh(
            "docker", "run", "-d", "--name", mtc, "--network", net,
            "-e", "MAUTIC_URL=http://localhost:8080",
            *_mautic_db_env(db),
            "-e", "ADMIN_EMAIL=admin@smoke.local",
            "-e", "ADMIN_PASSWORD=S3cure-Smoke!2026",
            "-v", f"{vol}:/data",
            "-p", ":8080",
            IMAGE,
        )
        port = _host_port(mtc, "8080")
        try:
            _wait_http_200(f"http://127.0.0.1:{port}{LOGIN_PATH}", READY_DEADLINE_S)
        except RuntimeError:
            _dump_logs(mtc)
            raise
        yield {"mtc": mtc, "db": db, "net": net, "port": port, "vol": vol}
    finally:
        for name in (mtc, db):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)
        subprocess.run(["docker", "volume", "rm", vol], capture_output=True)


@pytest.fixture(scope="session")
def stack_public_url():
    # The case operators actually run: MAUTIC_URL is a real public host, not
    # localhost. Pins down that the loopback healthcheck spoofs that host
    # rather than depending on MAUTIC_URL=localhost. We do NOT HTTP-poll from
    # the host — Mautic redirects to the public host, which is unresolvable —
    # so readiness is the healthcheck script succeeding inside the container.
    suffix = secrets.token_hex(4)
    net = f"mtc-net-{suffix}"
    db = f"mtc-db-{suffix}"
    mtc = f"mtc-{suffix}"
    public_host = "mautic.example.test"

    _sh("docker", "network", "create", net)
    try:
        _run_mariadb(net, db)
        _sh(
            "docker", "run", "-d", "--name", mtc, "--network", net,
            "-e", f"MAUTIC_URL=https://{public_host}",
            *_mautic_db_env(db),
            "-e", "ADMIN_EMAIL=admin@smoke.local",
            "-e", "ADMIN_PASSWORD=S3cure-Smoke!2026",
            "-p", ":8080",
            IMAGE,
        )
        port = _host_port(mtc, "8080")
        try:
            _wait_healthcheck_ok(mtc, READY_DEADLINE_S)
        except RuntimeError:
            _dump_logs(mtc)
            raise
        yield {"mtc": mtc, "db": db, "net": net, "port": port, "host": public_host}
    finally:
        for name in (mtc, db):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


@pytest.fixture(scope="session")
def stack_workers_off():
    # All four longruns toggled off. nginx + php-fpm are base services, so the
    # app still installs and serves /s/login; only the scheduler/messengers
    # `exec sleep infinity` instead of running their workloads.
    suffix = secrets.token_hex(4)
    net = f"mtc-net-{suffix}"
    db = f"mtc-db-{suffix}"
    mtc = f"mtc-{suffix}"

    _sh("docker", "network", "create", net)
    try:
        _run_mariadb(net, db)
        _sh(
            "docker", "run", "-d", "--name", mtc, "--network", net,
            "-e", "MAUTIC_URL=http://localhost:8080",
            *_mautic_db_env(db),
            "-e", "ADMIN_EMAIL=admin@smoke.local",
            "-e", "ADMIN_PASSWORD=S3cure-Smoke!2026",
            "-e", "ENABLE_MAUTIC_SCHEDULER=FALSE",
            "-e", "ENABLE_MAUTIC_WORKER_EMAIL=FALSE",
            "-e", "ENABLE_MAUTIC_WORKER_HIT=FALSE",
            "-e", "ENABLE_MAUTIC_WORKER_FAILED=FALSE",
            "-p", ":8080",
            IMAGE,
        )
        port = _host_port(mtc, "8080")
        try:
            _wait_http_200(f"http://127.0.0.1:{port}{LOGIN_PATH}", READY_DEADLINE_S)
        except RuntimeError:
            _dump_logs(mtc)
            raise
        yield {"mtc": mtc, "db": db, "net": net, "port": port}
    finally:
        for name in (mtc, db):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


@pytest.fixture
def bad_db_stack():
    # Function-scoped factory. Spins up a fresh MariaDB sidecar, pre-populates
    # it with a foreign/partial schema, then boots mautic with --restart=no and
    # blocks on `docker wait`. The guard's job is to abort the boot before
    # migrations touch someone else's database, so we assert on exit code +
    # the diagnostic the guard prints.
    resources = {"networks": [], "containers": []}

    def factory(setup_sql):
        suffix = secrets.token_hex(4)
        net = f"mtc-net-{suffix}"
        db = f"mtc-db-{suffix}"
        mtc = f"mtc-{suffix}"
        resources["networks"].append(net)
        resources["containers"].extend([db, mtc])

        _sh("docker", "network", "create", net)
        _run_mariadb(net, db)
        r = _exec(db, "mariadb", "-uroot", "-ptest", "mautic", "-e", setup_sql)
        assert r.returncode == 0, (
            f"mariadb setup failed: stdout={r.stdout!r} stderr={r.stderr!r}"
        )

        _sh(
            "docker", "run", "-d", "--name", mtc, "--network", net,
            "--restart=no",
            "-e", "MAUTIC_URL=http://localhost:8080",
            "-e", f"MAUTIC_DB_HOST={db}",
            "-e", "MAUTIC_DB_PORT=3306",
            "-e", "MAUTIC_DB_DATABASE=mautic",
            "-e", "MAUTIC_DB_USER=root",
            "-e", "MAUTIC_DB_PASSWORD=test",
            IMAGE,
        )
        try:
            w = subprocess.run(
                ["docker", "wait", mtc],
                capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", mtc], capture_output=True)
            logs = _sh("docker", "logs", mtc, check=False)
            raise RuntimeError(
                "mautic container did not exit within 180s; "
                f"logs:\n{logs.stdout}\n{logs.stderr}"
            )
        exit_code = int(w.stdout.strip())
        logs = _sh("docker", "logs", mtc, check=False)
        return exit_code, logs.stdout + logs.stderr

    yield factory

    for name in resources["containers"]:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    for net in resources["networks"]:
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


# ---------------------------------------------------------------------------
# Lifecycle / config.
# ---------------------------------------------------------------------------

def test_login_responds_200(stack):
    with _http_get(f"http://127.0.0.1:{stack['port']}{LOGIN_PATH}") as r:
        assert r.status == 200
        body = r.read().decode("utf-8", errors="replace")
    # Cheap content sanity — Mautic's login template renders a password field
    # and a _username input.
    assert 'type="password"' in body or 'name="_username"' in body, (
        f"login body did not look like Mautic's login form: {body[:300]!r}"
    )


def test_local_php_has_ops_keys(stack):
    mtc = stack["mtc"]
    assert _read_local_php(mtc, "db_driver") == "pdo_mysql"
    assert _read_local_php(mtc, "db_name") == "mautic"
    assert _read_local_php(mtc, "site_url") == "http://localhost:8080"
    assert _read_local_php(mtc, "db_host") != "", "db_host empty in local.php"


def test_secret_key_generated(stack):
    val = _read_local_php(stack["mtc"], "secret_key")
    assert re.fullmatch(r"[0-9a-f]{64}", val), f"secret_key not 64-char hex: {val!r}"


def test_param_passthrough(stack):
    mtc = stack["mtc"]
    assert _read_local_php(mtc, "mailer_from_name") == "Acme"
    assert _read_local_php(mtc, "mailer_from_email") == "ops@acme.test"


def test_secret_key_stable_across_restart(stack_persistent):
    mtc = stack_persistent["mtc"]
    key1 = _read_local_php(mtc, "secret_key")
    assert re.fullmatch(r"[0-9a-f]{64}", key1), f"secret_key not 64-char hex: {key1!r}"
    _sh("docker", "restart", mtc)
    # `-p :8080` makes the host port ephemeral; Docker may reassign it on
    # restart, so re-query rather than reusing the pre-restart port.
    port = _host_port(mtc, "8080")
    try:
        _wait_http_200(f"http://127.0.0.1:{port}{LOGIN_PATH}", READY_DEADLINE_S)
    except RuntimeError:
        _dump_logs(mtc)
        raise
    key2 = _read_local_php(mtc, "secret_key")
    assert key1 == key2, f"secret_key changed across restart: {key1!r} -> {key2!r}"


@pytest.mark.parametrize("path,flag", [
    ("/data/config", "-d"),
    ("/data/config/local.php", "-f"),
    ("/data/var/logs", "-d"),
    ("/data/var/cache", "-d"),
    ("/data/var/tmp", "-d"),
    ("/data/var/spool", "-d"),
    ("/data/media/files", "-d"),
    ("/data/media/images", "-d"),
])
def test_bootstrap_populated_data(stack, path, flag):
    r = _exec(stack["mtc"], "test", flag, path)
    assert r.returncode == 0, f"bootstrap did not produce {path}"


def test_logs_clean(stack):
    logs = _sh("docker", "logs", stack["mtc"], check=False)
    combined = logs.stdout + logs.stderr
    # Tight pattern: Mautic emits benign deprecation/warning noise, so we only
    # flag hard failures.
    bad = re.findall(r"PHP Fatal|Uncaught|not reachable within", combined)
    assert not bad, f"bad patterns in container logs: {bad[:5]}"


# ---------------------------------------------------------------------------
# s6 longruns.
# ---------------------------------------------------------------------------

# The scheduler stays a shell loop, so its run process persists with argv
# `sh ./run mautic-scheduler` — match `./run mautic-scheduler` to distinguish it
# from the always-present `s6-supervise mautic-scheduler`. The messengers
# `exec php ...`, replacing argv with the consume command.
LONGRUN_NEEDLES = [
    "./run mautic-scheduler",
    "messenger:consume email",
    "messenger:consume hit",
    "messenger:consume failed",
]


@pytest.mark.parametrize("needle", LONGRUN_NEEDLES)
def test_longruns_alive(stack, needle):
    _wait_bootstrap_complete(stack["mtc"])
    assert _wait_proc(stack["mtc"], needle, 60), (
        f"longrun process matching {needle!r} not present in /proc cmdlines"
    )


@pytest.mark.parametrize("needle", LONGRUN_NEEDLES)
def test_longruns_disabled(stack_workers_off, needle):
    # With every toggle FALSE the run scripts `exec sleep infinity`, so neither
    # the run-script path nor the consume command survives in argv. Wait for the
    # bootstrap to finish (longruns start only then) and let them exec sleep.
    mtc = stack_workers_off["mtc"]
    _wait_bootstrap_complete(mtc)
    time.sleep(5)
    assert not _proc_cmdline_contains(mtc, needle), (
        f"longrun {needle!r} should be absent when its toggle is FALSE"
    )


def test_login_still_served_with_workers_off(stack_workers_off):
    with _http_get(f"http://127.0.0.1:{stack_workers_off['port']}{LOGIN_PATH}") as r:
        assert r.status == 200


# ---------------------------------------------------------------------------
# Healthcheck. Under podman, an image built without `--format docker` carries
# no HEALTHCHECK, so the daemon surfaces no health — those tests skip rather
# than fail. The script-based public-URL test below runs regardless.
# ---------------------------------------------------------------------------

def test_healthcheck_reports_healthy(stack):
    end = time.time() + HEALTHY_DEADLINE_S
    last = None
    while time.time() < end:
        health = _container_health(stack["mtc"])
        if health is None:
            pytest.skip(
                "daemon does not surface an image HEALTHCHECK "
                "(build with `docker build --format docker` for health coverage)"
            )
        last = health.get("Status")
        if last == "healthy":
            return
        if last == "unhealthy":
            pytest.fail(f"container went unhealthy: {health.get('Log', [])[-1:]!r}")
        time.sleep(3)
    pytest.fail(f"healthcheck still {last!r} after {HEALTHY_DEADLINE_S}s")


def test_healthcheck_healthy_with_public_url(stack_public_url):
    # The healthcheck script extracts the host from a non-localhost MAUTIC_URL
    # and spoofs it onto the loopback probe; the fixture's readiness gate
    # already ran it successfully, and we re-run it here to assert the
    # Host-spoofing path explicitly.
    r = _exec(stack_public_url["mtc"], "mautic-healthcheck")
    assert r.returncode == 0, (
        f"mautic-healthcheck failed under public MAUTIC_URL: "
        f"rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Admin seed gate.
# ---------------------------------------------------------------------------

def test_admin_seeded_once(stack):
    assert _users_count(stack["db"]) == 1, "expected exactly one seeded admin"


def test_admin_not_reseeded_on_restart(stack_persistent):
    # On restart, config_ok + schema_ok are both true -> the bootstrap takes
    # the migrate path, not reinstall. The user count must not change.
    db, mtc = stack_persistent["db"], stack_persistent["mtc"]
    before = _users_count(db)
    assert before == 1, f"expected one seeded admin before restart, got {before}"
    _sh("docker", "restart", mtc)
    port = _host_port(mtc, "8080")
    try:
        _wait_http_200(f"http://127.0.0.1:{port}{LOGIN_PATH}", READY_DEADLINE_S)
    except RuntimeError:
        _dump_logs(mtc)
        raise
    after = _users_count(db)
    assert after == before, (
        f"users count changed across restart: {before} -> {after}; "
        "admin appears to have been reseeded"
    )


# ---------------------------------------------------------------------------
# nginx security (zz-mautic-security.conf). `<center>nginx</center>` in the
# body marks nginx's stock error page vs. a PHP/app response.
# ---------------------------------------------------------------------------

def _get_status_body(port, path):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, headers={"Host": APP_HOST_HEADER})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_hidden_file_denied(stack):
    status, body = _get_status_body(stack["port"], "/.git/config")
    assert status == 403, f"expected 403 on dotfile, got {status}"
    assert b"<center>nginx</center>" in body, (
        f"expected nginx stock 403 page; body={body[:200]!r}"
    )


def test_well_known_allowed(stack):
    # The hidden-file deny carves out /.well-known/ (RFC 8615), so this must
    # route past the dotfile deny (app/404), not return nginx's stock 403.
    status, _ = _get_status_body(stack["port"], "/.well-known/acme-challenge/x")
    assert status != 403, f"/.well-known/ should not hit the dotfile deny, got {status}"


@pytest.mark.parametrize("path", ["/x.sql", "/x.bak", "/x.conf", "/x.ini", "/x.log", "/x.sh"])
def test_sensitive_extension_denied(stack, path):
    status, body = _get_status_body(stack["port"], path)
    assert status == 403, f"expected 403 on {path}, got {status}"
    assert b"<center>nginx</center>" in body, (
        f"expected nginx stock 403 page for {path}; body={body[:200]!r}"
    )


def test_hsts_absent(stack):
    # SSL_MODE=off: TLS terminates upstream, which owns HSTS — our config
    # intentionally drops Strict-Transport-Security.
    with _http_get(f"http://127.0.0.1:{stack['port']}{LOGIN_PATH}") as r:
        assert r.headers.get("Strict-Transport-Security") is None, (
            "Strict-Transport-Security should be absent (dropped from upstream config)"
        )


def test_referrer_policy_present(stack):
    with _http_get(f"http://127.0.0.1:{stack['port']}{LOGIN_PATH}") as r:
        assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin", (
            f"Referrer-Policy header missing/wrong: {r.headers.get('Referrer-Policy')!r}"
        )


# ---------------------------------------------------------------------------
# Wrong-DB preflight. The db-guard's heredocs wrap mid-phrase, so assert on
# substrings that are contiguous on a single source line (db-guard:96-118).
# `test_empty_db_passes_preflight` is implicitly covered by `stack` booting
# successfully against an empty DB — no separate test needed.
# ---------------------------------------------------------------------------

def test_aborts_migrations_without_core(bad_db_stack):
    # A migrations table + one foreign row, no Mautic core tables. The guard
    # only checks table existence, so the row content is incidental.
    sql = (
        "CREATE TABLE migrations ("
        "  id INT AUTO_INCREMENT PRIMARY KEY,"
        "  version VARCHAR(255) NOT NULL,"
        "  executed_at DATETIME NULL"
        "); "
        "INSERT INTO migrations (version) VALUES ('Version20990101000000');"
    )
    code, logs = bad_db_stack(sql)
    assert code != 0, f"guard should have aborted; logs:\n{logs}"
    assert "missing Mautic core tables" in logs, (
        f"expected migrations-without-core diagnostic; logs:\n{logs}"
    )


def test_aborts_core_names_without_migrations(bad_db_stack):
    # The four core table names but no migrations table — looks like a foreign
    # schema with coincidentally overlapping names.
    sql = (
        "CREATE TABLE leads (id INT); "
        "CREATE TABLE campaigns (id INT); "
        "CREATE TABLE emails (id INT); "
        "CREATE TABLE users (id INT);"
    )
    code, logs = bad_db_stack(sql)
    assert code != 0, f"guard should have aborted; logs:\n{logs}"
    assert "all Mautic core table names" in logs, (
        f"expected core-names-without-migrations diagnostic; logs:\n{logs}"
    )


def test_aborts_foreign_nonempty_db(bad_db_stack):
    code, logs = bad_db_stack("CREATE TABLE my_app (id INT);")
    assert code != 0, f"guard should have aborted; logs:\n{logs}"
    assert "already contains" in logs, f"expected foreign-db diagnostic; logs:\n{logs}"
    assert "MAUTIC_DB_DATABASE likely points" in logs, (
        f"expected wrong-database hint; logs:\n{logs}"
    )
