# cli-agent-surgery

Tools for reshaping how a CLI coding agent behaves — from the prompt it boots
with, to which sessions survive a reboot, to what survives a context compaction.

| Directory | What it does |
|---|---|
| [`prompt-template/`](prompt-template) | Full system-prompt replacement: a pseudocode template, a renderer that fills it from the environment, and a production wrapper for Claude Code. Recipes for Gemini CLI, opencode, Codex and GLM included. |
| [`claudev/`](claudev) | Session persistence for Claude Code: a registry, tmux holders that outlive the terminal, workspace restore, a picker TUI over every project, and an off-host transcript mirror. |
| `claudevs/` — branch [`feat/claudevs`](../../tree/feat/claudevs), WIP | Deterministic state guard across the compaction boundary: runtime-owned facts are snapshotted before a compaction and re-injected if the summary dropped them. |

Each directory has its own README. MIT.
