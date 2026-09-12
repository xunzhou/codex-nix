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


BINARIES = ("codex-code-mode-host", "codex")  # Install the CLI last.


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


def valid_bundle(directory, version, markers):
    try:
        inventory = json.loads((directory / "sha256.json").read_text())
        if not isinstance(inventory, dict) or set(inventory) != set(BINARIES):
            return False
        for name in BINARIES:
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


def install_bundle(bundle, vendor, privilege):
    """Stage the pair, then replace it; restore both originals on failure."""
    inventory = json.loads((bundle / "sha256.json").read_text())
    if all(digest(vendor / name) == inventory[name] for name in BINARIES):
        print("Installed Codex already matches the complete patch set")
        return
    token = f".codex-patcher-{os.getpid()}"
    staged, backups, replaced = [], [], []
    cleanup_backups = False
    try:
        for name in BINARIES:
            target = vendor / name
            stage, backup = Path(str(target) + token), Path(str(target) + token + ".bak")
            staged.append(stage)
            backups.append(backup)
            run(*privilege, "cp", "-p", "--", str(target), str(backup))
            run(*privilege, "install", "-m", "0755", str(bundle / name), str(stage))
            if digest(stage) != inventory[name]:
                raise ValueError(f"staged {name} failed verification")
        for name, stage in zip(BINARIES, staged):
            target = vendor / name
            run(*privilege, "mv", "-f", "--", str(stage), str(target))
            replaced.append(name)
        if any(digest(vendor / name) != inventory[name] for name in BINARIES):
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
    argparse.ArgumentParser(description=__doc__).parse_args()
    if platform.system() != "Linux":
        raise ValueError("this source installer currently supports Linux")
    root = Path(__file__).resolve().parent.parent
    patches_root = Path(os.environ.get("CODEX_PATCH_ROOT", root / "patches")).resolve()
    entry = Path(os.environ.get("CODEX_MANAGED_ENTRYPOINT", "/usr/local/bin/codex"))
    package = Path(os.environ.get("CODEX_PACKAGE_ROOT", entry.resolve().parent.parent))
    metadata = json.loads((package / "package.json").read_text())
    version = metadata["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"unsupported Codex version: {version}")
    manifest = patches_root / "series" / f"{version}.json"
    if not manifest.is_file():
        raise ValueError(f"no reviewed patch set for Codex {version}: {manifest}")
    spec = json.loads(manifest.read_text())
    patches = [(patches_root / name).resolve() for name in spec["patches"]]
    if not patches or any(not p.is_relative_to(patches_root) or not p.is_file() for p in patches):
        raise ValueError("patch manifest contains missing or out-of-tree patches")
    candidates = list(package.glob("**/vendor/*/bin/codex"))
    if len(candidates) != 1:
        raise ValueError(f"expected one npm native Codex binary, found {len(candidates)}")
    vendor = candidates[0].parent
    if any(not (vendor / name).is_file() for name in BINARIES):
        raise ValueError("npm package is missing the Codex native binary pair")
    identity = hashlib.sha256()
    for item in (Path(__file__).resolve(), manifest, *patches):
        identity.update(item.name.encode() + b"\0" + item.read_bytes() + b"\0")
    identity.update(f"{version}:{platform.system()}:{platform.machine()}".encode())
    key = f"{version}-{identity.hexdigest()}"
    cache = Path(os.environ.get("CODEX_PATCH_CACHE", Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "codex-patched"))
    cache.mkdir(parents=True, exist_ok=True)
    privilege = shlex.split(os.environ.get("CODEX_PRIVILEGE_COMMAND", "sudo"))
    # The lock covers both cache publication and replacement of the installed pair.
    with (cache / "installer.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        bundle = cache / key
        if not valid_bundle(bundle, version, spec["markers"]):
            with tempfile.TemporaryDirectory(prefix="codex-source-") as temporary:
                scratch = Path(temporary)
                archive = scratch / "source.tar.gz"
                if supplied := os.environ.get("CODEX_SOURCE_ARCHIVE"):
                    shutil.copyfile(supplied, archive)
                else:
                    run("curl", "-fL", "--retry", "3", "-o", str(archive), f"https://github.com/openai/codex/archive/refs/tags/rust-v{version}.tar.gz")
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
                run(cargo, "build", "--release", "--locked", "-p", "codex-cli", "-p", "codex-code-mode-host", cwd=source / "codex-rs", env=env)
                with tempfile.TemporaryDirectory(prefix="bundle-", dir=cache) as pending:
                    pending = Path(pending)
                    for name in BINARIES:
                        shutil.copy2(target / "release" / name, pending / name)
                        run(os.environ.get("CODEX_STRIP_COMMAND", "strip"), "--strip-debug", str(pending / name))
                    (pending / "sha256.json").write_text(json.dumps({name: digest(pending / name) for name in BINARIES}))
                    if not valid_bundle(pending, version, spec["markers"]):
                        raise ValueError("built Codex bundle failed verification")
                    if bundle.exists():
                        shutil.rmtree(bundle)
                    pending.rename(bundle)
        install_bundle(bundle, vendor, privilege)
        print(f"Codex {version}: complete patch set installed")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        sys.exit(f"Codex patcher: {error}")
