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

### Codex

The KB CLI path delegates full extraction to the installed Codex graphify skill.

Required:

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
```

Fields:

- `provider`
  - currently supported value: `codex_skill`
- `model`
  - optional Codex model override
  - leave empty to use the Codex default
- `sync_remote`
  - optional `rclone` remote path such as `gdrive:ai-wiki`

`graphify` does not manage model API keys itself in this flow. Authentication belongs to Codex.

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

The KB wrapper now records real Codex token usage from `codex exec --json`. New runs in `graphify-out/cost.json` should include:

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

Older runs may still contain zeros from before the KB wrapper switched to real `codex exec --json` usage accounting. Check the newest run, not historical ones.

### Google Drive OAuth errors with rclone

If you are only trying to get a personal Drive remote working, the simplest setup is usually to use `rclone`'s default OAuth client instead of a custom Google OAuth app.
