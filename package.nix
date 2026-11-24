{
  lib,
  stdenv,
  rustPlatform,
  fetchzip,
  fetchurl,
  pkg-config,
  openssl,
  libcap,
  buildPackages,
  releaseVersion ? null,
  useDependencyCache ? true,
}:
let
  manifest = builtins.fromJSON (builtins.readFile ./build.json);
  version = if releaseVersion == null then manifest.default_version else releaseVersion;
  release = manifest.releases.${version};
  recipe = manifest.build // manifest.profiles.${release.profile} // release;
  binaries = builtins.attrNames recipe.binaries;
  marker = builtins.head recipe.markers;
  assets = lib.mapAttrs (_: asset: fetchurl {
    inherit (asset) url sha256;
  }) recipe.build_assets.${stdenv.hostPlatform.rust.rustcTarget};
  target = stdenv.hostPlatform.rust.rustcTarget;
  python = "${buildPackages.python3}/bin/python3";
  zstd = "${buildPackages.zstd}/bin/zstd";
  seedTool = ./scripts/cargo-dependency-seed.py;
  testPackages = [ "-p" "codex-core" "-p" "codex-tui" "-p" "codex-config" "--lib" ];
  testFlags = testPackages ++ [ "-E"
    # stdenv splits string flags on whitespace. Keep the filter in one token.
    "(package(codex-tui)&(test(/(^|::|_)esc/)|test(palette_refresh)|test(hook_status_reflows)|test(running_hooks_fit)|test(compaction_status)|test(usage_notice)))|test(add_dir_)|test(external_editor_reference)"
  ];

  # Keep the same source directory, vendoring, linker, Cargo settings and native
  # inputs in the dependency builds and in the patched production build.
  base = rustPlatform.buildRustPackage ({
    pname = "codex-palette-patched";
    inherit version;
    src = fetchzip {
      url = recipe.source.url;
      hash = recipe.source.nar_hash;
    };
    cargoRoot = "codex-rs";
    buildAndTestSubdir = "codex-rs";
    cargoHash = recipe.cargo_hash;
    # The vendor setup hook makes writable copies with current mtimes. Stable
    # source mtimes let Cargo reuse build-script outputs after an archive restore.
    prePatch = lib.optionalString useDependencyCache ''
      find "$cargoDepsCopy" -exec touch -h -d @1 {} +
    '';
    cargoBuildFlags = lib.concatMap (name: [ "-p" recipe.binaries.${name}.package ]) binaries;
    requiredSystemFeatures = [ "codex-artifact-publisher" ];
    # The combined tag includes the derivation hash. Keep older installers on
    # releases whose asset inventory they recognize.
    CODEX_NATIVE_RELEASE_ARCHITECTURES = lib.concatStringsSep "," recipe.native_release_architectures;
    OPENSSL_NO_VENDOR = 1;
    OPENSSL_STATIC = 1;
    AWS_LC_SYS_NO_JITTER_ENTROPY = 1;
    # Keep the patch regressions from the former native build. Escape names must
    # start a word: a substring also selects descendants/reschedule/coalesces tests.
    doCheck = true;
    useNextest = true;
    # Test compilation should not pay for release optimization and thin LTO.
    # Installed binaries still come from the release profile below.
    buildType = "release";
    checkType = "ci-test";
    cargoTestFlags = testFlags;
    preCheck = ''
      # nextest's -j controls running tests; its Cargo build has a separate limit.
      cargoTestFlags="--build-jobs $NIX_BUILD_CORES $cargoTestFlags"
      export RUST_MIN_STACK=16777216
      unset NO_COLOR TMUX TERM_PROGRAM
      export TERM=xterm-256color
      export HOME="$TMPDIR/codex-test-home"
      export CODEX_HOME="$HOME/.codex"
      mkdir -p "$CODEX_HOME"
    '';
    nativeBuildInputs = [ pkg-config ];
    nativeCheckInputs = [ buildPackages.gitMinimal ];
    buildInputs = [ openssl (libcap.override { withGo = false; }) ];
    installPhase = ''
      runHook preInstall
      ${lib.concatMapStringsSep "\n" (name: ''
        install -Dm755 target/${stdenv.hostPlatform.rust.rustcTarget}/release/${name} "$out/bin/${name}"
        "$out/bin/${name}" ${lib.escapeShellArgs recipe.binaries.${name}.smoke_args} >/dev/null
      '') binaries}
      ${lib.concatMapStringsSep "\n" (text: ''grep -aFqm1 ${lib.escapeShellArg text} "$out/bin/codex"'') recipe.markers}
      runHook postInstall
    '';
    passthru = {
      inherit marker recipe;
      patchFile = ./patches + "/${builtins.head recipe.patches}";
    };
  } // assets);

  dependencySeed = profile: base.overrideAttrs (_: {
    name = "codex-dependencies-${profile}-${version}";
    # The vendor directory name is part of the absolute paths used by native
    # dependency build scripts. Use the consumer's exact vendoring derivation.
    cargoDeps = base.cargoDeps;
    checkPhase = ":";
    postConfigure = ''
      (cd codex-rs && ${python} ${seedTool} stub)
    '';
    ${if profile == "ci-test" then "buildPhase" else null} = ''
      export CARGO_TARGET_DIR="$PWD/target" CARGO_PROFILE_RELEASE_STRIP=false
      cargoNextestHook
    '';
    cargoTestFlags = testPackages ++ [ "--no-run" ];
    installPhase = ''
      (cd codex-rs && ${python} ${seedTool} clean --profile ${profile} --target ${target})
      mkdir -p "$out"
      set -o pipefail
      tar --sort=name --mtime=@1 --owner=0 --group=0 --numeric-owner -C target -cf - . \
        | ${zstd} -T2 -3 -o "$out/target.tar.zst"
    '';
    dontFixup = true;
  });

  releaseDeps = dependencySeed "release";
  testDeps = dependencySeed "ci-test";
  published = base.overrideAttrs (old: {
    patches = map (name: ./patches + "/${name}") recipe.patches;
    postConfigure = lib.optionalString useDependencyCache ''
      set -o pipefail
      mkdir -p target
      ${zstd} -dc ${releaseDeps}/target.tar.zst | tar -xf - -C target
    '';
    preCheck = old.preCheck + lib.optionalString useDependencyCache ''
      # Keep only the finished binaries when switching to the separate test profile.
      mkdir -p release-binaries
      ${lib.concatMapStringsSep "\n" (name: "mv target/${target}/release/${name} release-binaries/") binaries}
      rm -rf target
      mkdir -p target/${target}/release
      mv release-binaries/* target/${target}/release/
      ${zstd} -dc ${testDeps}/target.tar.zst | tar -xf - -C target
    '';
  });

  # PR jobs can run the release build and the regression suite on independent
  # runners. Both use the same patches and Cargo profiles as publication.
  prBinaries = published.overrideAttrs (_: {
    checkPhase = ":";
    preCheck = "";
  });
  prTests = published.overrideAttrs (_: {
    postConfigure = lib.optionalString useDependencyCache ''
      set -o pipefail
      mkdir -p target
      ${zstd} -dc ${testDeps}/target.tar.zst | tar -xf - -C target
    '';
    buildPhase = ''
      runHook preBuild
      export CARGO_TARGET_DIR="$PWD/target" CARGO_PROFILE_RELEASE_STRIP=false
      runHook postBuild
    '';
    preCheck = base.preCheck;
    installPhase = ''mkdir -p "$out"'';
  });
in
published.overrideAttrs (old: {
  passthru = old.passthru // { inherit releaseDeps testDeps prBinaries prTests; };
})
