#!/usr/bin/env python3
"""Exercise dependency reuse across a fresh target directory and a patched workspace."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SEED = Path(__file__).resolve().parents[1] / "scripts/cargo-dependency-seed.py"


def vendor_crate(vendor, name):
    crate = vendor / name
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text(
        f'[package]\nname="{name}"\nversion="1.0.0"\nedition="2021"\n')
    (crate / "src/lib.rs").write_text("pub const VALUE: i32 = 10;\n")
    files = {path.relative_to(crate).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in (crate / "Cargo.toml", crate / "src/lib.rs")}
    (crate / ".cargo-checksum.json").write_text(json.dumps({"package": None, "files": files}))


def cargo(workspace, env, profile):
    args = ["build", "--bin", "fixture"] if profile == "release" else ["test", "--lib", "--no-run"]
    output = subprocess.check_output(["cargo", *args, "--offline", "--locked", "--target",
                                      env["FIXTURE_TARGET"], "--profile", profile,
                                      "--message-format=json"], cwd=workspace, env=env, text=True)
    return [message for line in output.splitlines() if
            (message := json.loads(line)).get("reason") == "compiler-artifact"]


def main():
    with tempfile.TemporaryDirectory(prefix="codex-dep-cache-") as name:
        root = Path(name)
        source = root / "source"
        (source / "src").mkdir(parents=True)
        (source / ".cargo").mkdir()
        vendor = root / "vendor"
        for crate in ("runtime-dep", "test-dep"):
            vendor_crate(vendor, crate)
        (source / ".cargo/config.toml").write_text(
            '[source.crates-io]\nreplace-with="vendor"\n[source.vendor]\n'
            f'directory={json.dumps(str(vendor))}\n')
        (source / "Cargo.toml").write_text(
            '[package]\nname="fixture"\nversion="0.1.0"\nedition="2021"\n'
            '[dependencies]\nruntime-dep="1"\n[dev-dependencies]\ntest-dep="1"\n'
            '[profile.ci-test]\ninherits="test"\n')
        (source / "src/main.rs").write_text('fn main() { println!("{}", fixture::value()); }\n')
        (source / "src/lib.rs").write_text(
            'pub fn value() -> i32 { runtime_dep::VALUE + 1 }\n'
            '#[test] fn patched() { assert_eq!(value(), test_dep::VALUE + 2); }\n')
        host = next(line.split()[1] for line in
                    subprocess.check_output(["rustc", "-vV"], text=True).splitlines()
                    if line.startswith("host:"))
        env = dict(os.environ, CARGO_HOME=str(root / "cargo-home"), CARGO_NET_OFFLINE="true",
                   FIXTURE_TARGET=host)
        subprocess.run(["cargo", "generate-lockfile", "--offline"], cwd=source, env=env, check=True)
        for profile in ("release", "ci-test"):
            seed = root / f"seed-{profile}"
            patched = root / f"patched-{profile}"
            shutil.copytree(source, seed)
            shutil.copytree(source, patched)
            env["CARGO_TARGET_DIR"] = str(seed / "target")
            subprocess.run([sys.executable, SEED, "stub"], cwd=seed, env=env, check=True)
            cargo(seed, env, profile)
            subprocess.run([sys.executable, SEED, "clean", "--profile", profile, "--target", host], cwd=seed, env=env, check=True)
            archive = root / f"{profile}.tar"
            subprocess.run(["tar", "--sort=name", "--mtime=@1", "-C", seed / "target",
                            "-cf", archive, "."], check=True)
            shutil.rmtree(seed)
            (patched / "target").mkdir()
            subprocess.run(["tar", "-xf", archive, "-C", patched / "target"], check=True)
            lib = patched / "src/lib.rs"
            lib.write_text(lib.read_text().replace("VALUE + 1", "VALUE + 2"))
            os.utime(lib, (1, 1))  # Nix sources can be older than their restored fingerprints.
            env["CARGO_TARGET_DIR"] = str(patched / "target")
            artifacts = cargo(patched, env, profile)
            expected = {"runtime_dep"} if profile == "release" else {"runtime_dep", "test_dep"}
            fresh = {a["target"]["name"] for a in artifacts if a["fresh"]}
            assert expected <= fresh, f"{profile}: dependencies rebuilt: {expected - fresh}"
            local = [a for a in artifacts if a["target"]["name"] == "fixture"]
            assert local and all(not a["fresh"] for a in local), f"{profile}: stale workspace output"
            binary = next(a["executable"] for a in local if a.get("executable"))
            output = subprocess.check_output([binary], text=True)
            if profile == "release":
                assert output.strip() == "12", output
            else:
                assert "1 passed" in output, output
            print(f"{profile}: patched workspace rebuilt with {len(expected)} fresh dependencies")


if __name__ == "__main__":
    main()
