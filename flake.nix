{
  description = "Source-built Codex with live terminal palette refresh";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          codex = pkgs.callPackage ./package.nix { };
          installer = pkgs.writeShellApplication {
            name = "install-codex-bundle";
            runtimeInputs = [
              pkgs.coreutils
              pkgs.curl
              pkgs.gitMinimal
              pkgs.gnugrep
              pkgs.jq
              pkgs.nix
              pkgs.zstd
            ];
            text = ''
              exec ${./scripts/install-bundle.sh} --flake-ref ${self.outPath} "$@"
            '';
          };
        in
        {
          inherit codex installer;
          default = codex;
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/codex";
          meta.description = "Run the patched Codex CLI";
        };
        install = {
          type = "app";
          program = "${self.packages.${system}.installer}/bin/install-codex-bundle";
          meta.description = "Install the exact verified Codex release bundle";
        };
      });
    };
}
