#!/bin/sh
# Installs the `stenos` executable from the latest GitHub release.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Stiven-Gjekaj/stenos/main/scripts/install.sh | sh
#
# The newest stable release by default. For the newest pre-release, which is
# what every alpha is published as:
#   ... | sh -s -- --pre
#
# Or, for one exact version:
#   ... | sh -s -- v0.1.3.19
#
# POSIX sh rather than bash, so it runs on a system where bash is not
# installed. It refuses rather than guesses: an unknown platform, a missing
# checksum, or a checksum that does not match all stop the script. A wrong
# executable installed quietly is worse than no executable.

set -eu

REPO="Stiven-Gjekaj/stenos"
INSTALL_DIR="${STENOS_INSTALL_DIR:-$HOME/.local/bin}"
RAW_URL="https://raw.githubusercontent.com/$REPO/main/scripts/install.sh"

fail() {
    echo "install.sh: $1" >&2
    exit 1
}

usage() {
    cat <<USAGE
Install the stenos executable from a GitHub release.

  install.sh                the newest stable release
  install.sh --pre          the newest release including pre-releases
  install.sh v0.1.3.19      one exact version

Set STENOS_INSTALL_DIR to install somewhere other than \$HOME/.local/bin.
USAGE
}

version=""
allow_pre=0
for arg in "$@"; do
    case "$arg" in
        --pre | --prerelease) allow_pre=1 ;;
        -h | --help)
            usage
            exit 0
            ;;
        -*) fail "unknown option '$arg'. Run with --help." ;;
        *) version="$arg" ;;
    esac
done

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

tag_from_json() {
    # Split on commas before matching. A release list arrives as one long line,
    # and the leading .* is greedy, so without the split the pattern would run
    # past every release and return the tag of the oldest one.
    tr ',' '\n' |
        sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' |
        head -1
}

if [ -z "$version" ]; then
    if [ "$allow_pre" -eq 1 ]; then
        # The full list is ordered newest first and omits drafts, so its first
        # tag is the newest release of any kind.
        version=$(fetch "https://api.github.com/repos/$REPO/releases" | tag_from_json)
        [ -n "$version" ] || fail "no releases have been published yet."
    else
        # releases/latest is defined as the newest release that is neither a
        # draft nor a pre-release, which is exactly the stable one wanted here.
        version=$(fetch "https://api.github.com/repos/$REPO/releases/latest" | tag_from_json)
        [ -n "$version" ] || fail "no stable release yet. Every release so far is a
pre-release, so install the newest of those with:
  curl -fsSL $RAW_URL | sh -s -- --pre"
    fi
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
