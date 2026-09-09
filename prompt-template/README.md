# cli-agent-surgery

System-prompt override for CLI coding agents. A pseudocode-style prompt template (`system_prompt.md`) and a renderer (`render.sh`) that fills environment placeholders, producing a ready-to-inject prompt for the agent of your choice.

## What's inside

- **`system_prompt.md`** — prompt template. Pseudocode/module format. Covers reasoning style, hypothesis investigation (no hedging), quality bar, risky actions, tool usage, output efficiency, auto-memory. Uses `{{PLACEHOLDER}}` syntax.
- **`render.sh`** — bash script. Substitutes placeholders from env vars and system state. Prints rendered prompt to stdout.

## Placeholders

| Placeholder | Source env var | Default |
|---|---|---|
| `{{ASSISTANT_NAME}}` | `TUNE_ASSISTANT_NAME` | `agent` |
| `{{CWD}}` | `TUNE_CWD` | `$PWD` |
| `{{IS_GIT}}` | auto-detected | `true`/`false` |
| `{{ADDITIONAL_DIRS}}` | `TUNE_ADDITIONAL_DIRS` | `none` |
| `{{PLATFORM}}` | `uname -s` | `linux`/`darwin`/`win32` |
| `{{SHELL}}` | `$SHELL` | `bash`/`zsh`/`fish` |
| `{{OS_VERSION}}` | `uname -sr` | — |
| `{{MODEL_NAME}}` | `TUNE_MODEL_NAME` | `unknown` |
| `{{MODEL_ID}}` | `TUNE_MODEL_ID` | `unknown` |
| `{{CUTOFF}}` | `TUNE_CUTOFF` | `unknown` |
| `{{MEMORY_DIR}}` | `TUNE_MEMORY_DIR` | `$XDG_CACHE_HOME/cli-agent-surgery/<cwd-key>/memory` |

Any `{{FOO}}` not substituted is reported on stderr but renders as-is.

## Usage

Render once, then feed the result to your agent:

```bash
./render.sh > /tmp/prompt.md
```

### Claude Code ([docs](https://docs.anthropic.com/en/docs/claude-code/cli-reference))

Full system-prompt replacement is supported via `--system-prompt-file`:

```bash
TUNE_ASSISTANT_NAME=Claude \
TUNE_MODEL_NAME="Opus 4.6" \
TUNE_MODEL_ID=claude-opus-4-6 \
TUNE_CUTOFF="May 2025" \
./render.sh > /tmp/prompt.md

claude --system-prompt-file /tmp/prompt.md
```

Or inline with process substitution: `claude --system-prompt-file <(./render.sh)`.

### Gemini CLI ([docs](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/system-prompt.md))

Full replacement via `GEMINI_SYSTEM_MD` env var:

```bash
TUNE_ASSISTANT_NAME=Gemini ./render.sh > /tmp/prompt.md
GEMINI_SYSTEM_MD=/tmp/prompt.md gemini
```

### opencode ([docs](https://opencode.ai/docs/agents/))

No CLI flag; override via `opencode.json` agent config:

```json
{
  "agent": {
    "tuned": { "prompt": "{file:/tmp/prompt.md}" }
  }
}
```

Then: `./render.sh > /tmp/prompt.md && opencode --agent tuned`.

### Codex CLI ([issue #11588](https://github.com/openai/codex/issues/11588))

No full-replacement support (as of writing). Closest: append context via `AGENTS.md` in the project root — not a true override:

```bash
TUNE_ASSISTANT_NAME=Codex ./render.sh > ./AGENTS.md
codex
```

### Z.AI GLM CLI ([source](https://github.com/guizmo-ai/zai-glm-cli))

No full-replacement support. Append via `.zai/ZAI.md`:

```bash
mkdir -p .zai && TUNE_ASSISTANT_NAME=GLM ./render.sh > .zai/ZAI.md
zai-glm-cli
```

## Writing a wrapper

A thin bash wrapper per agent keeps invocation a single command. Minimal Claude Code example:

```bash
#!/usr/bin/env bash
set -euo pipefail
DIR="$(dirname "$(readlink -f "$0")")"
RENDERED="${TUNE_RENDERED:-$DIR/.rendered_prompt.md}"
TUNE_ASSISTANT_NAME=Claude "$DIR/render.sh" > "$RENDERED"
exec claude --system-prompt-file "$RENDERED" "$@"
```

Save as `claude-tuned`, `chmod +x`, symlink into `$PATH`.

## Customize the template

Edit `system_prompt.md` directly. Modules are self-contained — add, remove, or rewrite any without breaking the rest. The `Language` module hardcodes the response locale — change `response_locale` to switch.

## License

MIT

## Resolving the model automatically

`TUNE_MODEL_NAME` / `TUNE_MODEL_ID` / `TUNE_CUTOFF` win when set. Otherwise point
`TUNE_MODEL_SETTINGS` at the agent's settings JSON (`TUNE_MODEL_KEY` names the
field, default `model`) and the renderer resolves the rest:

```bash
TUNE_MODEL_SETTINGS=~/.claude/settings.json ./render.sh
```

Short aliases are handled: `"opus[1m]"` renders as `Opus (1M context)` with id
`opus`. A cutoff is filled in only for a recognised full id — an unknown one
stays `unknown` instead of inheriting a neighbour's date.

## Ready-made wrapper

`claude-wrapped` is the wrapper from the section above, in production form:
per-process render path (parallel launches would otherwise race on one file),
binary lookup across PATH / common dirs / VS Code extension bundle, and a drift
guard that warns once per agent-binary change — the stock prompt evolves with the
binary, so the template needs re-reading when it does.

```bash
ln -s "$PWD/claude-wrapped" ~/.local/bin/claude-tuned
```
