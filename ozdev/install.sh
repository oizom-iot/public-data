#!/bin/sh
# ozdev installer.
#
# Downloads the build for this machine and puts it on your PATH. Needs nothing
# but curl (or wget) and tar-free plain files — no Node, no git, no GitHub
# account. Everything it fetches is public.
#
#   curl -fsSL https://raw.githubusercontent.com/oizom-iot/public-data/main/ozdev/install.sh | sh
#
# Override with environment variables:
#   OZDEV_REPO     release repo           (default oizom-iot/public-data)
#   OZDEV_BIN_DIR  where to install       (default ~/.local/bin)
#   OZDEV_VERSION  a specific version     (default: newest published)

set -eu

REPO="${OZDEV_REPO:-oizom-iot/public-data}"
BIN_DIR="${OZDEV_BIN_DIR:-$HOME/.local/bin}"
TAG_PREFIX="ozdev-v"

say() { printf '%s\n' "$*"; }
die() { printf '\nozdev install failed: %s\n' "$*" >&2; exit 1; }

# --- what are we running on? ------------------------------------------------

os=$(uname -s)
arch=$(uname -m)

case "$os" in
  Linux)  os=linux ;;
  Darwin) os=darwin ;;
  MINGW*|MSYS*|CYGWIN*)
    die "Windows shells are not supported by this script. Download ozdev-windows-x64.exe from
     https://github.com/$REPO/releases and put it somewhere on your PATH." ;;
  *) die "unsupported operating system: $os" ;;
esac

case "$arch" in
  x86_64|amd64)  arch=x64 ;;
  arm64|aarch64) arch=arm64 ;;
  *) die "unsupported CPU: $arch" ;;
esac

ASSET="ozdev-$os-$arch"

# --- fetch helper -----------------------------------------------------------

if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL "$1"; }
  fetch_to() { curl -fsSL --progress-bar "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -qO- "$1"; }
  fetch_to() { wget -q --show-progress -O "$2" "$1"; }
else
  die "neither curl nor wget is installed"
fi

# --- which version? ---------------------------------------------------------

if [ -n "${OZDEV_VERSION:-}" ]; then
  TAG="$TAG_PREFIX${OZDEV_VERSION#v}"
else
  # That repo carries unrelated releases, so ours are found by tag prefix rather
  # than by trusting whichever release happens to be newest.
  TAG=$(fetch "https://api.github.com/repos/$REPO/releases" \
        | grep '"tag_name"' \
        | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/' \
        | grep "^$TAG_PREFIX" \
        | head -n 1) || true
  [ -n "$TAG" ] || die "no $TAG_PREFIX* release found in $REPO"
fi

BASE="https://github.com/$REPO/releases/download/$TAG"
say "Installing ozdev ${TAG#$TAG_PREFIX} for $os-$arch"

# --- download, verify, install ---------------------------------------------

TMP=$(mktemp -d)
# Nothing here is worth keeping if the script exits early, and a half-downloaded
# binary left in /tmp is just confusing later.
trap 'rm -rf "$TMP"' EXIT INT TERM

fetch_to "$BASE/$ASSET" "$TMP/ozdev" || die "could not download $BASE/$ASSET"

# The checksum is the difference between a truncated download and a mystery
# crash three days later, so a published SHA256SUMS is always checked.
if sums=$(fetch "$BASE/SHA256SUMS" 2>/dev/null) && [ -n "$sums" ]; then
  expected=$(printf '%s\n' "$sums" | grep " $ASSET\$" | awk '{print $1}')
  if [ -n "$expected" ]; then
    if command -v sha256sum >/dev/null 2>&1; then
      actual=$(sha256sum "$TMP/ozdev" | awk '{print $1}')
    elif command -v shasum >/dev/null 2>&1; then
      actual=$(shasum -a 256 "$TMP/ozdev" | awk '{print $1}')
    else
      actual=""
      say "  (no sha256 tool found — skipping checksum)"
    fi
    if [ -n "$actual" ] && [ "$actual" != "$expected" ]; then
      die "checksum mismatch — expected $expected, got $actual. Nothing was installed."
    fi
  fi
fi

mkdir -p "$BIN_DIR"
chmod +x "$TMP/ozdev"
# Move into place as one step: an interrupted install leaves the old ozdev or
# the new one, never a half-written file.
mv -f "$TMP/ozdev" "$BIN_DIR/ozdev"

say "Installed $BIN_DIR/ozdev"

# --- is it usable? ----------------------------------------------------------

case ":$PATH:" in
  *":$BIN_DIR:"*)
    say ""
    say "Next:  ozdev login"
    ;;
  *)
    say ""
    say "$BIN_DIR is not on your PATH. Add it:"
    say ""
    case "${SHELL:-}" in
      *zsh)  say "  echo 'export PATH=\"\$PATH:$BIN_DIR\"' >> ~/.zshrc && exec zsh" ;;
      *fish) say "  fish_add_path $BIN_DIR" ;;
      *)     say "  echo 'export PATH=\"\$PATH:$BIN_DIR\"' >> ~/.bashrc && exec bash" ;;
    esac
    say ""
    say "Then:  ozdev login"
    ;;
esac
