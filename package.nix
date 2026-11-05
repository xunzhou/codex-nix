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
in
rustPlatform.buildRustPackage ({
  pname = "codex-palette-patched";
  inherit version;
  src = fetchzip {
    url = recipe.source.url;
    hash = recipe.source.nar_hash;
  };
  patches = map (name: ./patches + "/${name}") recipe.patches;
  cargoRoot = "codex-rs";
  buildAndTestSubdir = "codex-rs";
  cargoHash = recipe.cargo_hash;
  cargoBuildFlags = lib.concatMap (name: [ "-p" recipe.binaries.${name}.package ]) binaries;
  preBuild = ''
    export NIX_BUILD_CORES=2
  '';
  requiredSystemFeatures = [ "codex-artifact-publisher" ];
  OPENSSL_NO_VENDOR = 1;
  OPENSSL_STATIC = 1;
  AWS_LC_SYS_NO_JITTER_ENTROPY = 1;
  # Keep the complete patch regression selection from the former native build.
  doCheck = true;
  useNextest = true;
  cargoTestFlags = [
    "-p" "codex-tui" "--lib" "--test-threads=2" "-E"
    "test(esc) | test(palette_refresh) | test(hook_status_reflows) | test(running_hooks_fit) | test(compaction_status) | test(usage_notice)"
  ];
  preCheck = ''
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
} // assets)
