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

# Model id: env → optional settings JSON (TUNE_MODEL_SETTINGS + TUNE_MODEL_KEY)
# → unknown. Agents often store a short alias ("opus", "opus[1m]") rather than a
# full id; both are normalized here, and a stale guess is never substituted.
MODEL_ID="${TUNE_MODEL_ID:-}"
if [[ -z "$MODEL_ID" && -n "${TUNE_MODEL_SETTINGS:-}" ]] && command -v jq >/dev/null; then
  MODEL_ID=$(jq -r --arg k "${TUNE_MODEL_KEY:-model}" '.[$k] // empty' \
             "$TUNE_MODEL_SETTINGS" 2>/dev/null) || MODEL_ID=""
fi
CTX_NOTE=""
[[ "$MODEL_ID" == *"[1m]"* ]] && CTX_NOTE=" (1M context)"
MODEL_ID="${MODEL_ID%%\[*}"
[[ -z "$MODEL_ID" ]] && MODEL_ID=unknown

# "claude-fable-5" -> "Fable 5"; "opus" -> "Opus"; a trailing date is dropped.
if [[ -n "${TUNE_MODEL_NAME:-}" ]]; then
  MODEL_NAME="$TUNE_MODEL_NAME"
else
  rest="${MODEL_ID#claude-}"
  fam="${rest%%-*}"
  ver="${rest#"$fam"}"; ver="${ver#-}"
  ver="$(printf '%s' "$ver" | sed -E 's/-?[0-9]{8}$//; s/-/./g')"
  MODEL_NAME="${fam^}${ver:+ $ver}${CTX_NOTE}"
fi

# Knowledge cutoffs for Anthropic ids, from the platform models overview.
# Anything unrecognised stays "unknown" rather than inheriting a neighbour's date.
if [[ -n "${TUNE_CUTOFF:-}" ]]; then
  CUTOFF="$TUNE_CUTOFF"
else
  case "$MODEL_ID" in
    *fable-5*|*mythos-5*)  CUTOFF="Jan 2026" ;;
    *opus-5*)              CUTOFF="May 2026" ;;
    *opus-4-8*|*opus-4-7*) CUTOFF="Jan 2026" ;;
    *opus-4-6*|*opus-4-5*) CUTOFF="May 2025" ;;
    *sonnet-5*)            CUTOFF="Jan 2026" ;;
    *sonnet-4-6*)          CUTOFF="Aug 2025" ;;
    *sonnet-4-5*)          CUTOFF="Jan 2025" ;;
    *haiku-4-5*)           CUTOFF="Feb 2025" ;;
    *)                     CUTOFF="unknown" ;;
  esac
fi

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
