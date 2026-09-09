#!/usr/bin/env bash
# Boot-time holder: every registered claudev session gets its own tmux session
# named cv-<sid8>, started before the compositor exists. Idempotent — an
# already-running holder is left untouched, so a service restart never kills work.
set -u
# systemd user units start with a bare env; HOME is the one thing we must be sure of.
: "${HOME:?run this from a systemd --user unit or export HOME}"
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/usr/sbin:/usr/local/sbin"

REG="$HOME/.local/state/claudev/sessions"
LOG="$HOME/.claudev-headless.log"
exec >>"$LOG" 2>&1
echo "===== start $(date '+%F %T') kernel=$(uname -r)"

mkdir -p "$REG"
shopt -s nullglob

# No lock here: a locked fd would leak into the tmux server we spawn and never
# be released. tmux new-session is atomic on its own — a racing claudev-restore
# either wins or gets "duplicate session".

started=0 kept=0
for rec in "$REG"/*; do
  sid="$(basename "$rec")"
  name="cv-${sid:0:8}"
  if tmux has-session -t "=$name" 2>/dev/null; then
    echo "keep    $sid -> $name (уже поднята)"
    kept=$((kept + 1))
    continue
  fi
  cwd="$(sed -n 1p "$rec")"
  mode="$(sed -n 2p "$rec")"
  [[ -d "$cwd" ]] || cwd="$HOME"
  [[ "$mode" == "claude" ]] && att=--attach-plain || att=--attach

  tmux new-session -d -s "$name" -c "$cwd" \
    bash -lc "exec '$HOME/.local/bin/claudev' $att '$sid'"
  tmux set-option -t "$name" status off
  tmux set-option -t "$name" destroy-unattached off
  tmux set-option -t "$name" @claudev_sid "$sid"
  tmux set-option -t "$name" @claudev_cwd "$cwd"
  echo "spawn   $sid -> $name cwd=$cwd mode=$mode"
  started=$((started + 1))
done

echo "готово: поднято=$started сохранено=$kept"
