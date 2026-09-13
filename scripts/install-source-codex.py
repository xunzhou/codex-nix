#!/usr/bin/env python3
"""Build a versioned patch set into the npm-managed Linux Codex installation."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from urllib.parse import urlparse


def load_recipe(manifest, version):
    data = json.loads(manifest.read_text())
    if data.get("schema_version") != 1:
        raise ValueError("unsupported build manifest schema")
    if version not in data["releases"]:
        raise ValueError(f"no reviewed patch set for Codex {version}: {manifest}")
    release = data["releases"][version]
    return {**data["build"], **data["profiles"][release["profile"]], **release,
            "release_repository": data["release_repository"]}


def recipe_key(spec, patches, version):
    identity = hashlib.sha256()
    for item in (Path(__file__).resolve(), *patches):
        identity.update(item.name.encode() + b"\0" + item.read_bytes() + b"\0")
    identity.update(json.dumps(spec, sort_keys=True).encode())
    identity.update(f"{version}:{platform.system()}:{platform.machine()}".encode())
    return f"{version}-{identity.hexdigest()}"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_build_assets(spec, cache, env):
    """Pin native build inputs independently of the crate's default download host."""
    assets = spec.get("build_assets")
    if not assets or env.get("V8_FROM_SOURCE"):
        return
    if env.get("RUSTY_V8_ARCHIVE") and env.get("RUSTY_V8_SRC_BINDING_PATH"):
        return
    target = env.get("CARGO_BUILD_TARGET")
    if not target:
        output = subprocess.check_output([env.get("RUSTC", "rustc"), "-vV"], text=True, env=env)
        target = next((line.removeprefix("host: ") for line in output.splitlines() if line.startswith("host: ")), None)
    if target not in assets:
        raise ValueError(f"no pinned build assets for Rust target {target}; supply both RUSTY_V8_ARCHIVE and RUSTY_V8_SRC_BINDING_PATH")
    directory = (cache / "assets").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    for variable, asset in assets[target].items():
        if env.get(variable):
            continue
        checksum = asset["sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ValueError(f"invalid checksum for {variable}")
        destination = directory / (checksum + "-" + Path(urlparse(asset["url"]).path).name)
        if not destination.is_file() or digest(destination) != checksum:
            with tempfile.TemporaryDirectory(prefix="download-", dir=directory) as temporary:
                download = Path(temporary) / "asset"
                run("curl", "-fL", "--retry", "3", "-o", str(download), asset["url"])
                if digest(download) != checksum:
                    raise ValueError(f"downloaded {variable} checksum mismatch")
                download.replace(destination)
        env[variable] = str(destination)


def normalize_lock(source, version):
    """Release archives may retain 0.0.0 only for local workspace packages."""
    lock = source / "codex-rs/Cargo.lock"
    local_names = set()
    for manifest in (source / "codex-rs").rglob("Cargo.toml"):
        data = tomllib.loads(manifest.read_text())
        if name := data.get("package", {}).get("name"):
            local_names.add(name)
    blocks = lock.read_text().split("[[package]]")
    for index, block in enumerate(blocks[1:], 1):
        package = tomllib.loads(block)
        if package.get("version") == "0.0.0":
            if package.get("source") or package["name"] not in local_names:
                raise ValueError("refusing to rewrite non-workspace lock entry")
            blocks[index] = block.replace('version = "0.0.0"', f'version = "{version}"', 1)
    lock.write_text("[[package]]".join(blocks))


def smoke_bundle(directory, spec):
    for name, definition in spec["binaries"].items():
        run(str(directory / name), *definition["smoke_args"], stdout=subprocess.DEVNULL)


def http_download(url, destination):
    result = subprocess.run(["curl", "--silent", "--show-error", "--location", "--retry", "3",
                             "--write-out", "%{http_code}", "--output", str(destination), url],
                            check=True, capture_output=True, text=True)
    if result.stdout == "404":
        return False
    if result.stdout != "200":
        raise ValueError(f"release download returned HTTP {result.stdout}")
    return True


def unified_bundle_url(spec, key, scratch):
    """Find an exact recipe asset, including older releases beyond the first page."""
    repository = spec["release_repository"]
    name = f"codex-native-{key}-linux-x86_64.tar.gz"
    page = 1
    while True:
        metadata = scratch / "releases.json"
        url = f"https://api.github.com/repos/{repository}/releases?per_page=100&page={page}"
        if not http_download(url, metadata):
            raise ValueError("could not list unified releases")
        releases = json.loads(metadata.read_text())
        if not isinstance(releases, list):
            raise ValueError("invalid release list")
        for release in releases:
            if release.get("draft") or not release.get("tag_name", "").startswith("bundle-codex-v"):
                continue
            matches = [asset for asset in release["assets"] if asset["name"] == name]
            if len(matches) > 1:
                raise ValueError("duplicate native release asset")
            if matches:
                # Construct the URL ourselves; never follow arbitrary metadata URLs.
                tag = release["tag_name"]
                if not re.fullmatch(r"bundle-codex-v[0-9]+\.[0-9]+\.[0-9]+-[a-f0-9]{16}-[0-9a-z]{16}", tag):
                    raise ValueError("invalid unified release tag")
                return f"https://github.com/{repository}/releases/download/{tag}/{name}"
        if len(releases) < 100:
            return None
        page += 1


def download_bundle(spec, key, cache, bundle, version, binaries):
    url = f'https://github.com/{spec["release_repository"]}/releases/download/native-{key}/codex-linux-x86_64.tar.gz'
    if platform.machine() != "x86_64":
        return False
    with tempfile.TemporaryDirectory(prefix="release-", dir=cache) as temporary:
        scratch = Path(temporary)
        archive = scratch / "bundle.tar.gz"
        downloaded = http_download(url, archive)
        if not downloaded:
            unified_url = unified_bundle_url(spec, key, scratch)
            if unified_url:
                downloaded = http_download(unified_url, archive)
                if not downloaded:
                    raise ValueError("listed native release asset is missing")
        if not downloaded:
            if os.environ.get("CODEX_INSTALL_MODE") == "download":
                raise ValueError("no matching native release; rerun with --source to build locally")
            print("No matching GitHub bundle yet; falling back to the shared source recipe", flush=True)
            return False
        pending = scratch / "unpacked"
        pending.mkdir()
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            expected = {*binaries, "sha256.json", "bundle.json"}
            if len(members) != len(expected) or {member.name for member in members} != expected or any(not member.isfile() for member in members):
                raise ValueError("unexpected native bundle contents")
            tar.extractall(pending, filter="data")
        if json.loads((pending / "bundle.json").read_text()) != {"key": key, "version": version}:
            raise ValueError("native bundle does not match the requested recipe")
        if not valid_bundle(pending, version, spec["markers"], binaries):
            raise ValueError("native bundle checksum or version verification failed")
        smoke_bundle(pending, spec)
        if bundle.exists():
            shutil.rmtree(bundle)
        pending.rename(bundle)
        print(f"Downloaded verified Codex {version} native bundle", flush=True)
        return True


def valid_bundle(directory, version, markers, binaries):
    try:
        inventory = json.loads((directory / "sha256.json").read_text())
        if not isinstance(inventory, dict) or set(inventory) != set(binaries):
            return False
        for name in binaries:
            binary = directory / name
            if not os.access(binary, os.X_OK) or digest(binary) != inventory[name]:
                return False
        cli = directory / "codex"
        # grep streams the large executable instead of loading it into Python.
        for marker in markers:
            run("grep", "-aFq", "--", marker, str(cli))
        return subprocess.check_output([str(cli), "--version"], text=True).strip() == f"codex-cli {version}"
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def install_bundle(bundle, vendor, privilege, binaries):
    """Stage the pair, then replace it; restore both originals on failure."""
    inventory = json.loads((bundle / "sha256.json").read_text())
    if all(digest(vendor / name) == inventory[name] for name in binaries):
        print("Installed Codex already matches the complete patch set")
        return
    token = f".codex-patcher-{os.getpid()}"
    staged, backups, replaced = [], [], []
    cleanup_backups = False
    try:
        for name in binaries:
            target = vendor / name
            stage, backup = Path(str(target) + token), Path(str(target) + token + ".bak")
            staged.append(stage)
            backups.append(backup)
            run(*privilege, "cp", "-p", "--", str(target), str(backup))
            run(*privilege, "install", "-m", "0755", str(bundle / name), str(stage))
            if digest(stage) != inventory[name]:
                raise ValueError(f"staged {name} failed verification")
        for name, stage in zip(binaries, staged):
            target = vendor / name
            run(*privilege, "mv", "-f", "--", str(stage), str(target))
            replaced.append(name)
        if any(digest(vendor / name) != inventory[name] for name in binaries):
            raise ValueError("installed binary pair failed verification")
        cleanup_backups = True
    except BaseException:
        for name in reversed(replaced):
            backup = Path(str(vendor / name) + token + ".bak")
            run(*privilege, "mv", "-f", "--", str(backup), str(vendor / name))
        cleanup_backups = True
        raise
    finally:
        # Preserve recovery copies if rollback itself failed.
        for path in staged + (backups if cleanup_backups else []):
            if path.exists():
                run(*privilege, "rm", "-f", "--", str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true", help="build a release bundle without an npm installation")
    parser.add_argument("--version", help="version to build (with --build-only)")
    parser.add_argument("--output", type=Path, help="write a native release archive here")
    parser.add_argument("--source", action="store_true", help="build locally instead of trying GitHub first")
    args = parser.parse_args()
    if (args.version or args.output) and not args.build_only:
        parser.error("--version and --output require --build-only")
    if platform.system() != "Linux":
        raise ValueError("this source installer currently supports Linux")
    root = Path(__file__).resolve().parent.parent
    patches_root = Path(os.environ.get("CODEX_PATCH_ROOT", root / "patches")).resolve()
    manifest = Path(os.environ.get("CODEX_BUILD_MANIFEST", patches_root.parent / "build.json"))
    if args.build_only:
        version = args.version or json.loads(manifest.read_text())["default_version"]
        vendor = None
    else:
        entry = Path(os.environ.get("CODEX_MANAGED_ENTRYPOINT", "/usr/local/bin/codex"))
        package = Path(os.environ.get("CODEX_PACKAGE_ROOT", entry.resolve().parent.parent))
        version = json.loads((package / "package.json").read_text())["version"]
        candidates = list(package.glob("**/vendor/*/bin/codex"))
        if len(candidates) != 1:
            raise ValueError(f"expected one npm native Codex binary, found {len(candidates)}")
        vendor = candidates[0].parent
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"unsupported Codex version: {version}")
    spec = load_recipe(manifest, version)
    binaries = sorted(spec["binaries"], key=lambda name: (name == "codex", name))
    patches = [(patches_root / name).resolve() for name in spec["patches"]]
    if not patches or any(not p.is_relative_to(patches_root) or not p.is_file() for p in patches):
        raise ValueError("patch manifest contains missing or out-of-tree patches")
    if vendor and any(not (vendor / name).is_file() for name in binaries):
        raise ValueError("npm package is missing the Codex native binary pair")
    key = recipe_key(spec, patches, version)
    cache = Path(os.environ.get("CODEX_PATCH_CACHE", Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "codex-patched"))
    cache.mkdir(parents=True, exist_ok=True)
    privilege = shlex.split(os.environ.get("CODEX_PRIVILEGE_COMMAND", "sudo"))
    # The lock covers both cache publication and replacement of the installed pair.
    with (cache / "installer.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        bundle = cache / key
        if not args.build_only and not args.source and os.environ.get("CODEX_INSTALL_MODE") != "source" and not valid_bundle(bundle, version, spec["markers"], binaries):
            download_bundle(spec, key, cache, bundle, version, binaries)
        if not valid_bundle(bundle, version, spec["markers"], binaries):
            with tempfile.TemporaryDirectory(prefix="codex-source-") as temporary:
                scratch = Path(temporary)
                archive = scratch / "source.tar.gz"
                if supplied := os.environ.get("CODEX_SOURCE_ARCHIVE"):
                    shutil.copyfile(supplied, archive)
                else:
                    run("curl", "-fL", "--retry", "3", "-o", str(archive), spec["source"]["url"])
                if digest(archive) != spec["source"]["sha256"]:
                    raise ValueError("source archive checksum mismatch")
                source_parent = scratch / "source"
                source_parent.mkdir()
                with tarfile.open(archive) as tar:
                    tar.extractall(source_parent, filter="data")
                roots = list(source_parent.iterdir())
                if len(roots) != 1 or not roots[0].is_dir():
                    raise ValueError("expected a single source archive directory")
                source = roots[0]
                workspace = tomllib.loads((source / "codex-rs/Cargo.toml").read_text())
                if workspace["workspace"]["package"]["version"] != version:
                    raise ValueError("source archive does not match installed npm version")
                locked = tomllib.loads((source / "codex-rs/Cargo.lock").read_text())["package"]
                for name, expected in spec.get("crate_versions", {}).items():
                    versions = {item["version"] for item in locked if item["name"] == name}
                    if versions != {expected}:
                        raise ValueError(f"build profile does not match locked {name}: {versions}")
                for patch in patches:
                    run("git", "-C", str(source), "apply", "--check", str(patch))
                    run("git", "-C", str(source), "apply", str(patch))
                normalize_lock(source, version)
                target = (cache / "build" / version).resolve()
                env = dict(os.environ, CARGO_TARGET_DIR=str(target), GIT_CONFIG_GLOBAL="/dev/null", CARGO_NET_GIT_FETCH_WITH_CLI="true")
                prepare_build_assets(spec, cache, env)
                cargo = os.environ.get("CODEX_CARGO_COMMAND", "cargo")
                print(f"Building Codex {version} with {len(patches)} patches", flush=True)
                run(cargo, "metadata", "--locked", "--no-deps", "--format-version", "1", cwd=source / "codex-rs", env=env, stdout=subprocess.DEVNULL)
                run(cargo, "build", "--release", "--locked", *[flag for name in binaries for flag in ("-p", spec["binaries"][name]["package"])], cwd=source / "codex-rs", env=env)
                with tempfile.TemporaryDirectory(prefix="bundle-", dir=cache) as pending:
                    pending = Path(pending)
                    for name in binaries:
                        shutil.copy2(target / "release" / name, pending / name)
                        run(os.environ.get("CODEX_STRIP_COMMAND", "strip"), "--strip-debug", str(pending / name))
                    (pending / "sha256.json").write_text(json.dumps({name: digest(pending / name) for name in binaries}))
                    (pending / "bundle.json").write_text(json.dumps({"key": key, "version": version}))
                    if not valid_bundle(pending, version, spec["markers"], binaries):
                        raise ValueError("built Codex bundle failed verification")
                    smoke_bundle(pending, spec)
                    if bundle.exists():
                        shutil.rmtree(bundle)
                    pending.rename(bundle)
        if args.build_only:
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with tarfile.open(args.output, "w:gz") as archive:
                    for name in [*binaries, "sha256.json", "bundle.json"]:
                        archive.add(bundle / name, arcname=name)
                print(f"Native release asset: codex-native-{key}-linux-x86_64.tar.gz")
            print(f"Verified native bundle: {bundle}")
        else:
            install_bundle(bundle, vendor, privilege, binaries)
            print(f"Codex {version}: complete patch set installed")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        sys.exit(f"Codex patcher: {error}")
