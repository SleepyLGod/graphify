## Local Knowledge Base Workflow

This guide covers the local knowledge-base CLI flow:

- `graphify init-kb`
- `graphify build`
- `graphify update`
- `graphify sync`

It is separate from the interactive `/graphify` or `$graphify` host-skill flow. Use it when you want a stable local folder layout, repeatable terminal commands, and optional cloud sync for a knowledge base such as `~/ai-wiki`.

## What You Get

A knowledge base has this layout:

```text
<kb_root>/
  raw/
  graphify-out/
  .graphify/
    config.toml
```

- `raw/` holds the original source material.
- `graphify-out/` holds generated outputs such as `graph.json`, `GRAPH_REPORT.md`, `wiki/`, `cache/`, and `transcripts/`.
- `.graphify/config.toml` holds KB-specific configuration.

## Prerequisites

### Python environment

Use the project virtualenv and install the optional dependencies you need.

Example with `uv`:

```bash
uv venv --python 3.11 .venv
source .venv/bin/activate
uv sync --active --extra all
```

`--extra all` includes:

- Leiden clustering support
- PDF extraction
- Office conversion
- audio and video transcription dependencies
- file watching

### Codex or Claude Code

The KB CLI path delegates full extraction to an installed host graphify skill.

You can use either:

- Codex through `provider = "codex_skill"`
- Claude Code through `provider = "claude_skill"`

#### Codex requirements

- `codex` must be installed and authenticated
- the graphify Codex skill must be installed

Install once:

```bash
graphify install --platform codex
graphify codex install
```

Codex users should also enable multi-agent support in `~/.codex/config.toml`:

```toml
[features]
multi_agent = true
```

### Optional: rclone

`graphify sync` uses `rclone` as its backend. Install and configure an `rclone` remote before using sync commands.

## Create a Knowledge Base

```bash
graphify init-kb ~/ai-wiki --git
```

This creates:

- `~/ai-wiki/raw/`
- `~/ai-wiki/graphify-out/`
- `~/ai-wiki/.graphify/config.toml`
- `~/ai-wiki/.graphifyignore`

Using `--git` is recommended because sync will then also respect `.gitignore`.

## Configuration

Minimal config:

```toml
[kb]
provider = "codex_skill"
model = ""
sync_remote = ""

[codex]
runner_command = ""

[claude]
runner = "claude"
runner_command = ""
runner_args = []

[claude.runners.claude]
command = "claude"
args = []

[claude.runners.von-claude]
command = "von-claude"
args = []
```

Fields:

- `provider`
  - supported values: `codex_skill`, `claude_skill`
- `model`
  - optional host model override
  - leave empty to use the host default
- `sync_remote`
  - optional `rclone` remote path such as `gdrive:ai-wiki`
- `codex.runner_command`
  - optional Codex CLI executable path
  - leave empty to resolve `codex` from `PATH`
- `claude.runner`
  - default named runner for `claude_skill`
  - default value: `claude`
- `claude.runners.<name>.command`
  - executable command for a named runner
- `claude.runners.<name>.args`
  - optional TOML string array of extra runner arguments
- `claude.runner_command` and `claude.runner_args`
  - legacy fallback fields, kept for compatibility

`graphify` does not manage model API keys itself in this flow. Authentication belongs to the selected host.

#### Codex requirements

- the graphify Codex skill must be installed
- the real `codex` executable must be visible to Python, not only as a shell function or alias
- if you use `nvm`, either add the real bin directory to `PATH` before running graphify or configure `codex.runner_command`

Install the Codex skill once:

```bash
graphify install --platform codex
```

Example configuration with an explicit Codex path:

```toml
[kb]
provider = "codex_skill"
model = ""
sync_remote = ""

[codex]
runner_command = "/Users/von/.nvm/versions/node/v22.22.2/bin/codex"
```

#### Claude Code requirements

- the graphify Claude skill must be installed
- official `claude` is the default runner
- `von-claude` is available as a named custom runner
- both commands must be available on `PATH` if you use them by name
- shell functions that inject API settings are not visible to graphify; use exported environment variables or a real wrapper script if the runner needs custom environment

Install the Claude skill once:

```bash
graphify install --platform claude
graphify claude install
```

Example configuration for an installed official CLI:

```toml
[kb]
provider = "claude_skill"
model = ""
sync_remote = ""

[claude]
runner = "claude"

[claude.runners.claude]
command = "claude"
args = []
```

Example configuration for your custom Claude service:

```toml
[kb]
provider = "claude_skill"
model = ""
sync_remote = ""

[claude]
runner = "von-claude"

[claude.runners.von-claude]
command = "von-claude"
args = []
```

You can override the runner per command:

- persistently in `.graphify/config.toml`
- per command with `--runner`

Examples:

```bash
graphify build ~/ai-wiki --provider claude_skill
graphify build ~/ai-wiki --provider codex_skill --runner /Users/von/.nvm/versions/node/v22.22.2/bin/codex
graphify build ~/ai-wiki --provider claude_skill --runner von-claude
graphify build ~/ai-wiki --provider claude_skill --runner /Users/von/bin/claude-custom
graphify update ~/ai-wiki --provider codex_skill
graphify update ~/ai-wiki --provider claude_skill --runner claude
```

## Build

Run a full build:

```bash
graphify build ~/ai-wiki
```

Useful variant:

```bash
graphify build ~/ai-wiki --no-html
```

Outputs:

- `graphify-out/graph.json`
- `graphify-out/GRAPH_REPORT.md`
- `graphify-out/wiki/index.md`
- `graphify-out/cost.json`
- optional `graphify-out/graph.html`

The KB wrapper now records real host-run token usage from structured host output. New runs in `graphify-out/cost.json` should include:

- `input_tokens`
- `output_tokens`
- `cached_input_tokens`
- `usage_available`

## Update

Run an incremental update:

```bash
graphify update ~/ai-wiki
```

Current behavior:

- code changes do not trigger LLM semantic extraction
- non-code files reuse semantic cache when possible
- if there are no detected file changes, update returns the existing graph summary without rerunning extraction

That last point matters for retries: if a previous transcription or semantic pass failed because of a missing dependency or missing local model, and `Changed files: 0`, run `graphify build` after fixing the environment.

## Supported Content Types

The KB CLI flow is intended to handle:

- code
- markdown and text
- PDFs
- images
- `.docx`
- `.xlsx`
- audio and video files

Notes:

- Office files are converted to internal markdown sidecars during extraction.
- Final user-visible provenance now points back to the original Office file, not the sidecar path.
- Audio and video transcription use `faster-whisper`.

For Whisper, you can optionally pin the model:

```bash
export GRAPHIFY_WHISPER_MODEL=base
```

If the first transcription run needs to download a model, it may take longer than later runs.

## Sync

Set a remote in `.graphify/config.toml`:

```toml
[kb]
provider = "codex_skill"
model = ""
sync_remote = "gdrive:ai-wiki"
```

Available commands:

```bash
graphify sync status ~/ai-wiki
graphify sync push ~/ai-wiki --scope wiki
graphify sync push ~/ai-wiki --scope all
graphify sync pull ~/ai-wiki --scope wiki
graphify sync pull ~/ai-wiki --scope all
```

Available scopes:

- `raw`
- `wiki`
- `out`
- `config`
- `all`

Scope meanings:

- `raw` -> `raw/`
- `wiki` -> `graphify-out/wiki/`
- `out` -> `graphify-out/`
- `config` -> `.graphify/`
- `all` -> the whole KB root

### Sync ignore behavior

Sync now respects local ignore rules:

- `.gitignore` is respected when the KB root is a git repo
- `.graphifyignore` is respected for files under `raw/`

That means ignored files such as `.DS_Store` or unwanted material under `raw/` will not be pushed, and ignored junk pulled from a remote mirror will be pruned locally.

## Office Provenance

Office extraction still uses internal markdown sidecars, but final user-visible outputs are normalized to the original source files.

Expected:

- `graph.json` points to `raw/report.docx`
- `GRAPH_REPORT.md` points to `raw/report.docx`
- `wiki/*.md` points to `raw/report.docx`

Not expected anymore:

- `raw/graphify-out/converted/...`

## Troubleshooting

### `Codex graphify skill is not installed`

Install it:

```bash
graphify install --platform codex
graphify codex install
```

### `Changed files: 0` after fixing Whisper or another dependency

`update` will not retry old failures when there are no new file changes. Run:

```bash
graphify build ~/ai-wiki
```

### `cost.json` still shows old zero-token runs

Older runs may still contain zeros from before the KB wrapper switched to real structured host usage accounting. Check the newest run, not historical ones.

### Google Drive OAuth errors with rclone

If you are only trying to get a personal Drive remote working, the simplest setup is usually to use `rclone`'s default OAuth client instead of a custom Google OAuth app.
