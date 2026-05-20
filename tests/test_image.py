"""Fast image-inspection tests — no container boot.

Run model (IMAGE points at a pre-built tag):

    docker build --format docker -t mautic:test .   # see note below
    pip install -r tests/requirements.txt
    IMAGE=mautic:test pytest -v tests/ -m 'not runtime'   # fast lane
    IMAGE=mautic:test pytest -v tests/                     # full suite

`--format docker` is only needed when `docker` is podman: podman's default
OCI build drops the Dockerfile HEALTHCHECK (a Docker-specific config
extension), which makes the healthcheck tests skip. Real Docker/buildkit and
CI preserve it without the flag.
"""
import json
import os
import subprocess

import pytest

IMAGE = os.environ["IMAGE"]


def _inspect():
    out = subprocess.run(
        ["docker", "inspect", IMAGE],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)[0]


@pytest.fixture(scope="session")
def inspect():
    return _inspect()


def _run(*args, check=False):
    return subprocess.run(
        ["docker", "run", "--rm", "--entrypoint=", IMAGE, *args],
        capture_output=True, text=True, check=check,
    )


class TestImageMetadata:
    def test_required_oci_labels(self, inspect):
        # revision/created are intentionally omitted: they come from GIT_SHA /
        # BUILD_DATE build-args that are empty on local builds.
        labels = inspect["Config"].get("Labels") or {}
        for key in (
            "org.opencontainers.image.title",
            "org.opencontainers.image.description",
            "org.opencontainers.image.source",
            "org.opencontainers.image.licenses",
            "org.opencontainers.image.version",
        ):
            assert labels.get(key), f"missing OCI label: {key}"

    def test_runs_as_non_root(self, inspect):
        user = inspect["Config"].get("User", "")
        assert user in ("www-data", "82"), f"expected www-data/82, got {user!r}"

    def test_healthcheck_defined(self):
        # podman's image-level `inspect` uses the OCI config struct, which has
        # no Healthcheck field even when the image carries one — it only
        # surfaces on a *container's* Config.Healthcheck. Create a throwaway
        # container (don't run it) and read it from there; works under both
        # Docker and podman. A podman OCI build (no `--format docker`) drops
        # the HEALTHCHECK entirely, so skip with a pointer rather than fail.
        cid = subprocess.run(
            ["docker", "create", IMAGE],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        try:
            out = subprocess.run(
                ["docker", "inspect", "--format", "{{json .Config.Healthcheck}}", cid],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        finally:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
        hc = json.loads(out) if out and out != "null" else None
        if not hc:
            pytest.skip(
                "image has no HEALTHCHECK (podman OCI build drops it; "
                "build with `docker build --format docker` for health coverage)"
            )
        assert hc.get("Test"), f"Healthcheck present but has no Test command: {hc!r}"

    def test_exposes_8080(self, inspect):
        ports = inspect["Config"].get("ExposedPorts") or {}
        assert "8080/tcp" in ports, f"8080/tcp not exposed; got {list(ports)}"

    def test_image_size_under_limit(self, inspect):
        # Calibrated ~25% above the observed local build (~811 MB). Mautic +
        # Symfony + the composer-built artifact make this image large; tighten
        # if pruning lands. Bump deliberately, never reflexively.
        size_mb = inspect["Size"] / (1024 * 1024)
        assert size_mb < 1050, f"image size {size_mb:.0f} MB exceeds 1050 MB guardrail"

    def test_default_env_present(self, inspect):
        env = dict(e.split("=", 1) for e in inspect["Config"].get("Env") or [])
        assert env.get("AUTORUN_ENABLED") == "false"
        assert env.get("SSL_MODE") == "off"
        assert env.get("APP_BASE_DIR") == "/var/www/html"
        assert env.get("ENABLE_MAUTIC_SCHEDULER") == "TRUE"
        assert env.get("ENABLE_MAUTIC_WORKER_EMAIL") == "TRUE"
        assert env.get("ENABLE_MAUTIC_WORKER_HIT") == "TRUE"
        assert env.get("ENABLE_MAUTIC_WORKER_FAILED") == "TRUE"
        assert env.get("PHP_OPCACHE_ENABLE") == "1"
        assert env.get("PHP_MEMORY_LIMIT") == "512M"


class TestImageFilesystem:
    @pytest.mark.parametrize("link,target", [
        ("/var/www/html/public", "docroot"),
        ("/var/www/html/config", "/data/config"),
        ("/var/www/html/var/logs", "/data/var/logs"),
        ("/var/www/html/docroot/media/files", "/data/media/files"),
        ("/var/www/html/docroot/media/images", "/data/media/images"),
    ])
    def test_data_symlinks(self, link, target):
        r = _run("readlink", link)
        assert r.returncode == 0, f"readlink {link} failed: {r.stderr}"
        assert r.stdout.strip() == target, f"{link} -> {r.stdout.strip()!r}, expected {target!r}"

    def test_data_dir_owned_by_www_data(self):
        r = _run("stat", "-c", "%U:%G", "/data")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "www-data:www-data"

    @pytest.mark.parametrize("binary", [
        # No pg_isready — this image is MySQL/MariaDB only. mysqladmin is
        # deliberate: 20-mautic-bootstrap.sh calls it directly for the DB
        # wait, so this catches a "MariaDB renamed the binary" drift.
        "php", "nginx", "composer", "mysqladmin", "curl", "git",
    ])
    def test_runtime_binaries_present(self, binary):
        r = _run("which", binary)
        assert r.returncode == 0, f"{binary} not found on PATH"
        assert r.stdout.strip(), f"which {binary} returned empty"

    @pytest.mark.parametrize("ext", [
        # opcache reports under its internal name 'Zend OPcache'.
        "intl", "pdo_mysql", "mysqli", "zip", "bcmath", "sockets", "gd",
        "imap", "exif", "Zend OPcache", "amqp", "redis", "iconv",
    ])
    def test_php_extensions_loaded(self, ext):
        r = _run("php", "-m")
        assert r.returncode == 0, r.stderr
        modules = {line.strip() for line in r.stdout.splitlines() if line.strip()}
        assert ext in modules, f"PHP module {ext!r} not loaded; got {sorted(modules)}"

    @pytest.mark.parametrize("svc", [
        "mautic-scheduler",
        "mautic-messenger-email",
        "mautic-messenger-hit",
        "mautic-messenger-failed",
    ])
    def test_s6_longrun_run_scripts_executable(self, svc):
        r = _run("test", "-x", f"/etc/s6-overlay/s6-rc.d/{svc}/run")
        assert r.returncode == 0, f"{svc} run script missing or not executable"

    @pytest.mark.parametrize("svc", [
        "mautic-scheduler",
        "mautic-messenger-email",
        "mautic-messenger-hit",
        "mautic-messenger-failed",
    ])
    def test_s6_longruns_depend_on_bootstrap(self, svc):
        r = _run(
            "test", "-f",
            f"/etc/s6-overlay/s6-rc.d/{svc}/dependencies.d/20-mautic-bootstrap",
        )
        assert r.returncode == 0, f"{svc} dependency marker on bootstrap missing"

    def test_s6_bootstrap_oneshot_installed(self):
        # The base image's docker-php-serversideup-s6-init moves our
        # /etc/entrypoint.d/20-mautic-bootstrap.sh into /etc/s6-overlay/scripts/
        # with a rename suffix we don't want to couple to — glob is deliberate.
        r = _run("sh", "-c", "ls /etc/s6-overlay/scripts/ | grep -E 'mautic-bootstrap'")
        assert r.returncode == 0, (
            "no mautic-bootstrap script in /etc/s6-overlay/scripts/ "
            f"(stdout={r.stdout!r}, stderr={r.stderr!r})"
        )

    @pytest.mark.parametrize("path", [
        "/var/www/html/bin/console",
        "/var/www/html/composer.json",
        "/var/www/html/docroot/index.php",
    ])
    def test_mautic_app_present(self, path):
        r = _run("test", "-f", path)
        assert r.returncode == 0, f"{path} missing"

    def test_mautic_console_works(self):
        # Drift guard: `--version` short-circuits before needing DB/config,
        # so it is safe against an unpopulated /data. If the artifact is
        # broken (missing autoload, wrong PHP), this is the cheapest signal.
        r = _run("sh", "-c", "cd /var/www/html && php bin/console --version")
        assert r.returncode == 0, f"console --version failed: {r.stderr!r}"
        assert "Mautic" in r.stdout, f"expected 'Mautic' in version banner: {r.stdout!r}"

    def test_db_guard_usage(self):
        path = "/usr/local/bin/mautic-db-guard"
        r = _run("test", "-x", path)
        assert r.returncode == 0, f"{path} missing or not executable"
        r = _run(path)
        assert r.returncode == 2, f"expected exit 2 from no-args, got {r.returncode}"
        assert "usage: mautic-db-guard" in r.stderr, (
            f"usage line missing from stderr: {r.stderr!r}"
        )

    def test_local_php_render_usage(self):
        path = "/usr/local/bin/mautic-local-php-render"
        r = _run("test", "-x", path)
        assert r.returncode == 0, f"{path} missing or not executable"
        r = _run(path)
        assert r.returncode == 2, f"expected exit 2 from no-args, got {r.returncode}"
        assert "usage: mautic-local-php-render" in r.stderr, (
            f"usage line missing from stderr: {r.stderr!r}"
        )

    def test_healthcheck_script_present(self):
        r = _run("test", "-x", "/usr/local/bin/mautic-healthcheck")
        assert r.returncode == 0, "mautic-healthcheck missing or not executable"

    def test_nginx_security_config(self):
        # Our zz-mautic-security.conf replaces the base's security.conf, which
        # the Dockerfile removes. Assert both: ours present, theirs gone.
        r = _run("test", "-f", "/etc/nginx/server-opts.d/zz-mautic-security.conf")
        assert r.returncode == 0, "zz-mautic-security.conf missing"
        r = _run("test", "!", "-f", "/etc/nginx/server-opts.d/security.conf")
        assert r.returncode == 0, "base security.conf should have been removed"


# Marked runtime: a full image rebuild (~30s with buildkit cache, minutes
# cold). Lives outside TestImageFilesystem so `-m 'not runtime'` keeps the
# fast image lane fast.
@pytest.mark.runtime
class TestCustomUidRebuild:
    """Rebuild with --build-arg WWW_DATA_UID/GID and verify the new UID
    actually owns /data. Regression guard: the base's set-file-permissions
    only touches a hardcoded path list, so /data needs an explicit chown in
    the Dockerfile (line 136) or the rebuilt image's www-data can't write to
    its own volume.
    """

    UID = "1000"
    GID = "1000"

    @pytest.fixture(scope="class")
    def image(self):
        ctx = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tag = "mtc-uid1000-test"
        r = subprocess.run(
            ["docker", "build",
             "--build-arg", f"WWW_DATA_UID={self.UID}",
             "--build-arg", f"WWW_DATA_GID={self.GID}",
             "-t", tag, ctx],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            pytest.fail(
                f"docker build failed (rc={r.returncode})\n"
                f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
            )
        try:
            yield tag
        finally:
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)

    def test_www_data_user_remapped(self, image):
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint=", image,
             "id", "-u", "www-data"],
            capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == self.UID

    def test_data_dir_remapped(self, image):
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint=", image,
             "stat", "-c", "%u:%g", "/data"],
            capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == f"{self.UID}:{self.GID}"
