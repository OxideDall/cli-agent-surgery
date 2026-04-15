#!/usr/bin/env bash
# render.sh — fills {{placeholders}} in a system-prompt template.
# Usage: render.sh [template_path]   (default: ./system_prompt.md next to script)
# Prints rendered prompt to stdout.
#
# Env overrides (all optional):
#   TUNE_ASSISTANT_NAME   — agent name (default: agent)
#   TUNE_CWD              — working directory (default: $PWD)
#   TUNE_ADDITIONAL_DIRS  — extra dirs string (default: none)
#   TUNE_MODEL_NAME       — human-readable model name (default: unknown)
#   TUNE_MODEL_ID         — model identifier (default: unknown)
#   TUNE_CUTOFF           — knowledge cutoff label (default: unknown)
#   TUNE_MEMORY_DIR       — memory directory (default: $XDG_CACHE_HOME/cli-agent-surgery/<cwd-key>/memory)
set -euo pipefail

BASE_DIR="$(dirname "$(readlink -f "$0")")"
TEMPLATE="${1:-$BASE_DIR/system_prompt.md}"

if [[ ! -f "$TEMPLATE" ]]; then
  printf 'render.sh: template not found: %s\n' "$TEMPLATE" >&2
  exit 2
fi

# --- collect values ---------------------------------------------------------

ASSISTANT_NAME="${TUNE_ASSISTANT_NAME:-agent}"

CWD="${TUNE_CWD:-$PWD}"

if git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  IS_GIT=true
else
  IS_GIT=false
fi

ADDITIONAL_DIRS="${TUNE_ADDITIONAL_DIRS:-none}"

case "$(uname -s)" in
  Linux*)               PLATFORM=linux ;;
  Darwin*)              PLATFORM=darwin ;;
  MINGW*|MSYS*|CYGWIN*) PLATFORM=win32 ;;
  *)                    PLATFORM=$(uname -s | tr '[:upper:]' '[:lower:]') ;;
esac

case "${SHELL:-}" in
  */zsh)  SHELL_NAME=zsh ;;
  */bash) SHELL_NAME=bash ;;
  */fish) SHELL_NAME=fish ;;
  "")     SHELL_NAME=unknown ;;
  *)      SHELL_NAME=$(basename "$SHELL") ;;
esac

OS_VERSION=$(uname -sr)

MODEL_NAME="${TUNE_MODEL_NAME:-unknown}"
MODEL_ID="${TUNE_MODEL_ID:-unknown}"
CUTOFF="${TUNE_CUTOFF:-unknown}"

# MEMORY_DIR: default under XDG cache, keyed by cwd
default_mem_key=$(printf '%s' "$CWD" | sed 's|^/||' | tr '/' '-')
MEMORY_DIR="${TUNE_MEMORY_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/cli-agent-surgery/$default_mem_key/memory}"

# --- substitute -------------------------------------------------------------

content=$(<"$TEMPLATE")
sub() { local p="$1" v="$2"; content=${content//"$p"/$v}; }

sub '{{ASSISTANT_NAME}}'   "$ASSISTANT_NAME"
sub '{{CWD}}'              "$CWD"
sub '{{IS_GIT}}'           "$IS_GIT"
sub '{{ADDITIONAL_DIRS}}'  "$ADDITIONAL_DIRS"
sub '{{PLATFORM}}'         "$PLATFORM"
sub '{{SHELL}}'            "$SHELL_NAME"
sub '{{OS_VERSION}}'       "$OS_VERSION"
sub '{{MODEL_NAME}}'       "$MODEL_NAME"
sub '{{MODEL_ID}}'         "$MODEL_ID"
sub '{{CUTOFF}}'           "$CUTOFF"
sub '{{MEMORY_DIR}}'       "$MEMORY_DIR"

# Warn on any remaining unfilled placeholder (stderr, still renders)
if [[ "$content" =~ \{\{[A-Z_]+\}\} ]]; then
  printf 'render.sh: WARN unfilled placeholder(s) found: %s\n' \
    "$(printf '%s' "$content" | grep -oE '\{\{[A-Z_]+\}\}' | sort -u | tr '\n' ' ')" >&2
fi

printf '%s' "$content"
