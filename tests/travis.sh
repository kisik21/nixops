#!/usr/bin/env nix-shell
#!nix-shell -i bash ../shell.nix

set -eux

fold() {
    name="$1"
    shift
    printf 'travis_fold:start:%s\r\n' "$name"
    exec "$@"
    printf 'travis_fold:end:%s\r\n' "$name"
}


fold "mypy-ratchet" ./tests/mypy-ratchet.sh
fold "mypy-ratchet" black . --check --diff
fold "coverage-tests" ./dev-shell --run "./coverage-tests.py -a '!libvirtd,!gce,!ec2,!azure' -v"
fold "release.nix" nix-build --quiet release.nix -A build.x86_64-linux -I nixpkgs=channel:nixos-19.09
