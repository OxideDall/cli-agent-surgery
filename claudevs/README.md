# claudevs — state guard across the compaction boundary

**WIP.** A fork of [`claudev`](../claudev) that adds one thing: when the agent
compacts its context, the facts the runtime owns are checked against the summary
the model wrote, and whatever the summary dropped is put back.

Inspired by SKILL.state (arXiv 2608.26263), but applied to the phase boundary
rather than the step loop — the paper's own numbers only favour a state-carrying
runtime past ~50 steps, while a compaction boundary is exactly where an agent
loses state today.

## How it works

```
PreCompact         snapshot runtime-owned facts        -> snapshots/<sid>.json
PostCompact        diff snapshot against the summary   -> pending/<sid>.md
UserPromptSubmit   deliver the queue once, next turn   -> additionalContext
```

The model never writes these facts, so a summary cannot silently clobber them —
the failure mode that hits 68% of weak-model runs in the paper is structurally
absent. Facts collected:

- verbatim typed prompts of the current epoch
- open todos
- git branch, HEAD, dirty files
- files edited via tools, plus targets of mutating shell commands, each
  confirmed on disk (`is_file()` and mtime inside the epoch)

Nothing is injected when the summary kept everything — a good summary costs zero
tokens of noise.

## Measured on Claude Code 2.1.257

- Hook order is `PreCompact` → `SessionStart(compact)` → `PostCompact`.
- **The diff cannot run at `SessionStart(compact)`.** The summary does not exist
  yet: measured 61 ms behind for a 12-character summary and 5.1 s for a 2739-character
  one — the gap grows with the summary. `PostCompact` receives the text in its
  payload, so the boundary is race-free. This is why delivery is deferred to
  `UserPromptSubmit` instead.
- `PreCompact` fires *before* the "not enough messages to compact" check, so a
  snapshot can be written for a compaction that never happens. The next
  `PreCompact` overwrites it.
- `additionalContext` arrives as a system reminder and is capped at 10,000
  characters; `$HOME` in a hook command is expanded.
- A genuine typed prompt is `origin.kind == "human"` (`promptSource == "sdk"`
  when headless). Slash-command echoes carry no `promptSource`, and task
  notifications carry `promptSource: "system"` — filtering on prose instead of
  these fields pulls `/compact`, `/model` and notifications into the "user
  requests" bucket.
- Files written by a shell heredoc have no `file_path` argument to record, so
  targets are parsed out of the command text; without the on-disk confirmation
  step that parse yields shebang paths and fragments of regex literals.

## Isolation from claudev

Separate registry (`~/.local/state/claudevs`), holder prefix `cvs-`, window class
`claudevs-session`, own `--settings` file. Deliberately shared: the transcript
store, the prompt wrapper, the picker, and the duplicate guard — that one keys on
the session id, because the conflict is over the transcript, not the launcher.

Hooks go in the launcher's own `--settings` file rather than the agent's user
settings: user settings would apply to every running session immediately.

## Install

```bash
install -m755 bin/* ~/.local/bin/
mkdir -p ~/.config/claudevs
sed "s|\$HOME/src/cli-agent-surgery|$PWD/..|" settings.example.json > ~/.config/claudevs/settings.json
```

`CLAUDEVS_SETTINGS`, `CLAUDEVS_WRAPPED` and `CLAUDEVS_PICKER` override the paths.

`hooks/compact-probe` is diagnostic only: it records raw hook payloads and firing
order to `~/.local/state/claudevs/compact-probe.jsonl`. Add it alongside the guard
when investigating, leave it out in normal use.

## Status

Verified: hooks fire through `--settings`; the diff is selective (12 facts, 9
missing, 3 found in a 19,447-character summary); delivery lands once and the queue
is consumed. Not yet: auto-compact has only been exercised through manual
`/compact`, and the injection budget has not been hit in practice.
