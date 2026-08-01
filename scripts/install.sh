#!/bin/sh
# Installs the `stenos` executable from the latest GitHub release.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Stiven-Gjekaj/stenos/main/scripts/install.sh | sh
#
# Or, for a specific version:
#   ... | sh -s -- v0.1.1.0
#
# POSIX sh rather than bash, so it runs on a system where bash is not
# installed. It refuses rather than guesses: an unknown platform, a missing
# checksum, or a checksum that does not match all stop the script. A wrong
# executable installed quietly is worse than no executable.

set -eu

REPO="Stiven-Gjekaj/stenos"
INSTALL_DIR="${STENOS_INSTALL_DIR:-$HOME/.local/bin}"

fail() {
    echo "install.sh: $1" >&2
    exit 1
}

need() {
    command -v "$1" >/dev/null 2>&1 || fail "this script needs '$1', which is not installed"
}

need uname
need mkdir
need unzip

# One of these fetches; curl first because it is the more common.
if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL "$1"; }
    fetch_to() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -qO- "$1"; }
    fetch_to() { wget -qO "$2" "$1"; }
else
    fail "this script needs curl or wget, and neither is installed"
fi

# --- Work out the platform ---------------------------------------------------

os=$(uname -s)
arch=$(uname -m)

case "$os" in
    Linux)  os_part="linux" ;;
    Darwin) os_part="macos" ;;
    *)      fail "no prebuilt executable for '$os'. Install from source: uv sync" ;;
esac

case "$arch" in
    x86_64 | amd64)  arch_part="x86_64" ;;
    aarch64 | arm64) arch_part="arm64" ;;
    *) fail "no prebuilt executable for '$arch'. Install from source: uv sync" ;;
esac

# Apple Silicon is the only macOS build. GitHub has withdrawn its Intel
# runners, and a freezer cannot cross-build for another architecture.
if [ "$os_part" = "macos" ] && [ "$arch_part" != "arm64" ]; then
    fail "no prebuilt executable for Intel macOS. Install from source: brew install opus && uv sync --extra cuda"
fi

target="${os_part}-${arch_part}"

# --- Work out the version ----------------------------------------------------

version="${1:-}"
if [ -z "$version" ]; then
    version=$(fetch "https://api.github.com/repos/$REPO/releases/latest" |
        sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' |
        head -1)
    [ -n "$version" ] || fail "cannot find the latest version. Give one: install.sh v0.1.1.0"
fi

archive="stenos-${target}.zip"
base="https://github.com/$REPO/releases/download/$version"

echo "Installing stenos $version for $target"

# --- Download and check ------------------------------------------------------

tmp=$(mktemp -d)
# Runs on success and on failure, so a partial download is never left behind.
trap 'rm -rf "$tmp"' EXIT INT TERM

fetch_to "$base/$archive" "$tmp/$archive" ||
    fail "cannot download $archive. Check that $version has a build for $target."

if fetch_to "$base/SHA256SUMS" "$tmp/SHA256SUMS" 2>/dev/null; then
    if command -v sha256sum >/dev/null 2>&1; then
        actual=$(sha256sum "$tmp/$archive" | cut -d' ' -f1)
    elif command -v shasum >/dev/null 2>&1; then
        actual=$(shasum -a 256 "$tmp/$archive" | cut -d' ' -f1)
    else
        fail "this script needs sha256sum or shasum to check the download"
    fi

    expected=$(grep " $archive\$" "$tmp/SHA256SUMS" | cut -d' ' -f1 | head -1)
    [ -n "$expected" ] || fail "SHA256SUMS has no entry for $archive"

    if [ "$actual" != "$expected" ]; then
        fail "the checksum does not match.
  expected $expected
  actual   $actual
Do not use this download."
    fi
    echo "Checksum matches."
else
    fail "cannot download SHA256SUMS. Refusing to install an unchecked executable."
fi

# --- Install -----------------------------------------------------------------

unzip -q "$tmp/$archive" -d "$tmp"
mkdir -p "$INSTALL_DIR"
mv "$tmp/stenos-${target}/stenos" "$INSTALL_DIR/stenos"
chmod +x "$INSTALL_DIR/stenos"

echo "Installed to $INSTALL_DIR/stenos"

# Tell the user only when it is true. A message about the PATH that appears
# every time gets ignored the one time it matters.
case ":$PATH:" in
    *":$INSTALL_DIR:"*) ;;
    *)
        echo
        echo "$INSTALL_DIR is not on your PATH. Add this to your shell profile:"
        echo "    export PATH=\"$INSTALL_DIR:\$PATH\""
        ;;
esac

"$INSTALL_DIR/stenos" --version
