#!/usr/bin/env bash
# NoticeEcho one-line installer for Debian / Ubuntu / Raspberry Pi:
#
#   NOTICEECHO_REPO=https://github.com/your-org/your-repo.git curl -fsSL <install-url>/install.sh | sudo bash
#
# Installs git, clones the repo to /opt/NoticeEcho (override with NOTICEECHO_DIR=...),
# reusing /opt/WXEcho when upgrading an existing installation.
# and runs the full native installer.
set -euo pipefail

legacy_var_name() {
  local suffix="$1"
  printf '%s_%s' "MESH""WX" "$suffix"
}

legacy_dir_var="$(legacy_var_name DIR)"
legacy_repo_var="$(legacy_var_name REPO)"
legacy_no_self_var="$(legacy_var_name NO_SELFUPDATE)"

LEGACY_DIR="${!legacy_dir_var:-}"
LEGACY_REPO="${!legacy_repo_var:-}"
LEGACY_NO_SELFUPDATE="${!legacy_no_self_var:-}"

if [ -z "${WXECHO_DIR:-}" ] && [ -n "$LEGACY_DIR" ]; then
  echo "warning: ${legacy_dir_var} is deprecated; use NOTICEECHO_DIR." >&2
fi
if [ -z "${WXECHO_REPO:-}" ] && [ -n "$LEGACY_REPO" ]; then
  echo "warning: ${legacy_repo_var} is deprecated; use NOTICEECHO_REPO." >&2
fi

[ "$(id -u)" -eq 0 ] || {
  echo "Run with sudo:  curl -fsSL <url>/install.sh | sudo bash" >&2; exit 1; }

DEFAULT_DEST=/opt/NoticeEcho
if [ -d /opt/WXEcho ]; then DEFAULT_DEST=/opt/WXEcho;
elif [ -d /opt/MeshWX ]; then DEFAULT_DEST=/opt/MeshWX; fi
DEST="${NOTICEECHO_DIR:-${WXECHO_DIR:-${LEGACY_DIR:-$DEFAULT_DEST}}}"
REPO="${NOTICEECHO_REPO:-${WXECHO_REPO:-${LEGACY_REPO:-}}}"
[ -n "$REPO" ] || { echo "Set NOTICEECHO_REPO (or WXECHO_REPO) to the project repository URL." >&2; exit 1; }

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq git
fi

# Force $DEST to EXACTLY match the latest published code. `git reset --hard`
# only rewrites tracked files, so it repairs any local corruption (edited
# requirements, half-applied changes) while leaving the gitignored data/ (the
# database + settings) and .venv/ untouched. Re-clone only if there is no usable
# checkout, preserving data/ across the re-clone.
sync_to_latest() {
  git config --global --add safe.directory "$DEST" 2>/dev/null || true
  if [ -d "$DEST/.git" ] && git -C "$DEST" rev-parse --git-dir >/dev/null 2>&1 \
     && git -C "$DEST" fetch --depth 1 --quiet origin main 2>/dev/null \
     && git -C "$DEST" reset --hard --quiet FETCH_HEAD 2>/dev/null; then
    echo ">> synced $DEST to the latest NoticeEcho"
    return 0
  fi
  echo ">> setting up a clean checkout at $DEST"
  local keep=""
  if [ -d "$DEST/data" ]; then keep="$(mktemp -d)"; mv "$DEST/data" "$keep/"; fi
  rm -rf "$DEST"
  git clone --depth 1 "$REPO" "$DEST"
  if [ -n "$keep" ]; then rm -rf "$DEST/data"; mv "$keep/data" "$DEST/data"; rmdir "$keep"; fi
}
sync_to_latest

# We just synced; skip install-linux.sh's own self-update to avoid a double pull.
if [ -z "${WXECHO_NO_SELFUPDATE:-}" ] && [ -n "$LEGACY_NO_SELFUPDATE" ]; then
  echo "warning: ${legacy_no_self_var} is deprecated; use NOTICEECHO_NO_SELFUPDATE." >&2
fi
NOTICEECHO_NO_SELFUPDATE=1 exec bash "$DEST/packaging/install-linux.sh"
