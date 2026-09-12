{
  lib,
  stdenv,
  rustPlatform,
  fetchzip,
  fetchurl,
  pkg-config,
  openssl,
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
  doCheck = false;
  nativeBuildInputs = [ pkg-config ];
  buildInputs = [ openssl ];
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
