<?php

/**
 * collect-upstream-pins.php — emit exact composer pins from an upstream lock.
 *
 * Reads the mautic/mautic monorepo composer.lock for a release tag (path in
 * $argv[1]) and prints space-separated "name:version" for every production
 * package whose name is NOT under the mautic/ vendor. Those pins are fed to
 * `composer require --no-update` in the builder so the third-party tree matches
 * the exact set Mautic validated for this release, instead of floating on every
 * rebuild. Mautic's own plugins/themes are left at the exacts
 * recommended-project declares, so they are deliberately skipped here.
 *
 * $argv[2] is recommended-project's composer.json (i.e. /build/composer.json).
 * The monorepo lock is a SUPERSET of the deployable's tree: it also carries a
 * few build-time Composer *plugins* (cweagans/composer-patches,
 * composer/package-versions-deprecated) that recommended-project deliberately
 * omits and does not list in `config.allow-plugins`. Force-requiring those as
 * root deps drags in a plugin the deployable never sanctioned, and Composer
 * aborts the non-interactive install. So a package whose lock `type` is
 * `composer-plugin` is pinned ONLY when recommended-project's allow-plugins
 * sanctions it (e.g. composer/installers); unsanctioned build plugins are
 * dropped. Non-plugin extras are harmless inert libraries and kept.
 *
 * Versions are emitted verbatim: composer treats a concrete version (including
 * the v-prefixed "v3.26.0" and the bare "13" some packages carry) as an exact
 * match, which is what we want.
 *
 * Exits non-zero with a message on stderr if either input is unreadable, not
 * valid JSON, or the lock yields no third-party packages — all of which should
 * fail the build loudly rather than silently letting composer float.
 */

if ($argc < 3) {
    fwrite(STDERR, "usage: collect-upstream-pins.php <upstream.lock> <recommended-project-composer.json>\n");
    exit(1);
}

/**
 * @return array decoded JSON
 */
function read_json(string $path): array
{
    $raw = @file_get_contents($path);
    if ($raw === false) {
        fwrite(STDERR, "collect-upstream-pins: cannot read: {$path}\n");
        exit(1);
    }
    $data = json_decode($raw, true);
    if (!is_array($data)) {
        fwrite(STDERR, "collect-upstream-pins: not valid JSON: {$path}\n");
        exit(1);
    }
    return $data;
}

$lock = read_json($argv[1]);
if (!isset($lock['packages']) || !is_array($lock['packages'])) {
    fwrite(STDERR, "collect-upstream-pins: lock file has no 'packages' array: {$argv[1]}\n");
    exit(1);
}

$rp = read_json($argv[2]);
$allowPlugins = $rp['config']['allow-plugins'] ?? [];

$pins = [];
foreach ($lock['packages'] as $pkg) {
    if (!isset($pkg['name'], $pkg['version'])) {
        continue;
    }
    $name = $pkg['name'];

    // Skip Mautic's own packages (core-lib etc.); recommended-project pins
    // those to the release exact already.
    if (strncmp($name, 'mautic/', 7) === 0) {
        continue;
    }

    // Composer plugins run code during install. Only pin the ones
    // recommended-project explicitly sanctions in allow-plugins; the monorepo
    // lock's build-only plugins would otherwise abort the non-interactive
    // install.
    $type = $pkg['type'] ?? 'library';
    if ($type === 'composer-plugin'
        && (($allowPlugins[$name] ?? false) !== true)
    ) {
        continue;
    }

    $pins[] = $name . ':' . $pkg['version'];
}

if ($pins === []) {
    fwrite(STDERR, "collect-upstream-pins: no third-party packages found in {$argv[1]}\n");
    exit(1);
}

echo implode(' ', $pins);
