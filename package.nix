{
  lib,
  stdenv,
  rustPlatform,
  fetchFromGitHub,
  fetchurl,
  pkg-config,
  openssl,
}:

let
  marker = "terminal palette refresh did not return default colors";
  rustyV8Release = "https://github.com/openai/codex/releases/download/rusty-v8-v150.4.0";
in
rustPlatform.buildRustPackage rec {
  pname = "codex-palette-patched";
  version = "0.150.1";

  src = fetchFromGitHub {
    owner = "openai";
    repo = "codex";
    rev = "rust-v${version}";
    hash = "sha256-l8H2//SpzzUvtA3IZ9BXhWBqlaatbFk6rBYeLThozb4=";
  };

  patches = [ ./patches/live-palette-refresh.patch ];

  cargoRoot = "codex-rs";
  buildAndTestSubdir = "codex-rs";
  cargoHash = "sha256-85Po+Pe2xKltxznAv1MhC7BULJ+6AfXwM4jkZQ6+utk=";
  cargoBuildFlags = [
    "-p"
    "codex-cli"
    "-p"
    "codex-code-mode-host"
  ];

  RUSTY_V8_ARCHIVE = fetchurl {
    url = "${rustyV8Release}/librusty_v8_ptrcomp_sandbox_release_x86_64-unknown-linux-gnu.a.gz";
    hash = "sha256-o1x10fJuapg4haRbM0kKTr5U8FBQVosyuJz7QhswtYM=";
  };
  RUSTY_V8_SRC_BINDING_PATH = fetchurl {
    url = "${rustyV8Release}/src_binding_ptrcomp_sandbox_release_x86_64-unknown-linux-gnu.rs";
    hash = "sha256-dyeCauR5vbZF6Acjn7EtH44uI956bPFvXuWSaQ0dhQY=";
  };

  requiredSystemFeatures = [ "codex-artifact-publisher" ];
  doCheck = false;

  nativeBuildInputs = [ pkg-config ];
  buildInputs = [ openssl ];

  installPhase = ''
    runHook preInstall

    install -Dm755 target/${stdenv.hostPlatform.rust.rustcTarget}/release/codex "$out/bin/codex"
    install -Dm755 target/${stdenv.hostPlatform.rust.rustcTarget}/release/codex-code-mode-host "$out/bin/codex-code-mode-host"
    grep -aFqm1 ${lib.escapeShellArg marker} "$out/bin/codex"

    runHook postInstall
  '';

  passthru = {
    inherit marker;
    patchFile = ./patches/live-palette-refresh.patch;
  };
}
