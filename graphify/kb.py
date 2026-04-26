"""Knowledge-base workflow helpers for local graphify projects."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from graphify.analyze import god_nodes
from graphify.build import build_from_json
from graphify.cluster import score_all
from graphify.detect import OFFICE_EXTENSIONS, detect_incremental
from graphify.wiki import to_wiki


_DEFAULT_CONFIG = """# graphify knowledge-base config
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
"""

_DEFAULT_CLAUDE_RUNNERS = {
    "claude": {"command": "claude", "args": []},
    "von-claude": {"command": "von-claude", "args": []},
}


class KBError(RuntimeError):
    """Raised when a knowledge-base command cannot complete."""


@dataclass(frozen=True)
class KBPaths:
    """Resolved directory layout for a graphify knowledge base."""

    root: Path
    corpus: Path
    out: Path
    config_dir: Path
    config_file: Path
    manifest_file: Path
    sync_state_file: Path


@dataclass(frozen=True)
class BuildSummary:
    """Summary of a knowledge-base build or update run."""

    kb_root: Path
    graph_path: Path
    report_path: Path
    html_path: Path
    wiki_index_path: Path | None
    total_nodes: int
    total_edges: int
    total_communities: int
    semantic_files_extracted: int
    semantic_cache_hits: int
    changed_files: int
    deleted_files: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    semantic_stats_available: bool = False
    usage_available: bool = False


def resolve_paths(root: Path | str) -> KBPaths:
    """Return normalized paths for a graphify knowledge base."""
    resolved_root = Path(root).expanduser().resolve()
    corpus = resolved_root / "raw" if (resolved_root / "raw").exists() else resolved_root
    out = resolved_root / "graphify-out"
    config_dir = resolved_root / ".graphify"
    return KBPaths(
        root=resolved_root,
        corpus=corpus,
        out=out,
        config_dir=config_dir,
        config_file=config_dir / "config.toml",
        manifest_file=out / "manifest.json",
        sync_state_file=config_dir / "sync-state.json",
    )


def load_config(root: Path | str) -> dict[str, Any]:
    """Load `.graphify/config.toml`, returning defaults when absent."""
    paths = resolve_paths(root)
    config: dict[str, Any] = {
        "kb": {"provider": "codex_skill", "model": "", "sync_remote": ""},
        "codex": {"runner_command": ""},
        "claude": {
            "runner": "claude",
            "runner_command": "",
            "runner_args": [],
            "runners": {name: dict(value) for name, value in _DEFAULT_CLAUDE_RUNNERS.items()},
        },
    }
    if not paths.config_file.exists():
        return config

    try:
        import tomllib
    except ImportError:  # pragma: no cover - exercised only on Python 3.10
        import tomli as tomllib

    loaded = tomllib.loads(paths.config_file.read_text(encoding="utf-8"))
    config["kb"].update(loaded.get("kb", {}))
    codex_config = loaded.get("codex", {})
    if isinstance(codex_config, dict):
        config["codex"].update(codex_config)
    claude_config = loaded.get("claude", {})
    if isinstance(claude_config, dict):
        runners = claude_config.get("runners")
        if claude_config.get("runner_command") and "runner" not in claude_config:
            config["claude"]["runner"] = ""
        for key, value in claude_config.items():
            if key != "runners":
                config["claude"][key] = value
        if isinstance(runners, dict):
            for name, runner in runners.items():
                if isinstance(runner, dict):
                    current = dict(config["claude"]["runners"].get(name, {}))
                    current.update(runner)
                    config["claude"]["runners"][name] = current
    return config


def is_kb_root(root: Path | str) -> bool:
    """Return True when the path looks like a graphify knowledge-base root."""
    paths = resolve_paths(root)
    return paths.config_file.exists() and (paths.root / "raw").exists()


def init_kb(root: Path | str, *, init_git: bool = False) -> KBPaths:
    """Create a new local graphify knowledge-base directory structure."""
    paths = resolve_paths(root)
    paths.root.mkdir(parents=True, exist_ok=True)
    corpus = paths.root / "raw"
    corpus.mkdir(parents=True, exist_ok=True)
    paths.out.mkdir(parents=True, exist_ok=True)
    paths.config_dir.mkdir(parents=True, exist_ok=True)

    if not paths.config_file.exists():
        paths.config_file.write_text(_DEFAULT_CONFIG, encoding="utf-8")

    ignore_file = paths.root / ".graphifyignore"
    if not ignore_file.exists():
        ignore_file.write_text(
            "# Ignore generated or private corpus files here\n"
            "graphify-out/\n"
            ".graphify/\n",
            encoding="utf-8",
        )

    if init_git and not (paths.root / ".git").exists():
        subprocess.run(["git", "init", str(paths.root)], check=True, capture_output=True, text=True)

    return KBPaths(
        root=paths.root,
        corpus=corpus,
        out=paths.out,
        config_dir=paths.config_dir,
        config_file=paths.config_file,
        manifest_file=paths.manifest_file,
        sync_state_file=paths.sync_state_file,
    )


def build_kb(
    root: Path | str,
    *,
    model: str | None = None,
    provider_name: str | None = None,
    runner_command: str | None = None,
    update: bool = False,
    include_wiki: bool = True,
    include_html: bool = True,
) -> BuildSummary:
    """Build or update a local graphify knowledge base via the configured host skill."""
    paths = resolve_paths(root)
    if not paths.corpus.exists():
        raise KBError(f"Corpus path not found: {paths.corpus}")

    config = load_config(paths.root)
    provider = provider_name or (config["kb"].get("provider") or "codex_skill")
    provider_model = model or (config["kb"].get("model") or None)
    if provider not in {"codex_skill", "claude_skill"}:
        raise KBError(
            "Unsupported provider: "
            f"{provider}. This command currently supports only 'codex_skill' and 'claude_skill'."
        )

    incremental = detect_incremental(
        paths.corpus,
        manifest_path=str(paths.manifest_file),
        relative_root=paths.root,
        candidate_paths=_git_candidate_paths(paths.root) if update else None,
    )
    detection = incremental
    if detection.get("total_files", 0) == 0:
        raise KBError(f"No supported files found in {paths.corpus}")

    if update and incremental.get("new_total", 0) == 0 and not incremental.get("deleted_files"):
        if not (paths.out / "graph.json").exists():
            raise KBError("No existing graph found. Run `graphify build` first.")
        wiki_index = None
        if include_wiki and not (paths.out / "wiki" / "index.md").exists():
            wiki_index = _generate_wiki_from_graph(paths)
        return _load_existing_summary(paths, wiki_index=wiki_index)

    if provider == "codex_skill":
        selected_runner_command = _codex_runner(config, override_command=runner_command)
        _run_codex_skill(
            paths,
            update=update,
            include_html=include_html,
            model=provider_model,
            runner_command=selected_runner_command,
        )
    else:
        selected_runner_command, selected_runner_args = _claude_runner(config, override_command=runner_command)
        _run_claude_skill(
            paths,
            update=update,
            include_html=include_html,
            model=provider_model,
            runner_command=selected_runner_command,
            runner_args=selected_runner_args,
        )
    _normalize_office_provenance(paths, include_html=include_html)
    wiki_index = _generate_wiki_from_graph(paths) if include_wiki else None
    return _summarize_outputs(
        paths,
        incremental=incremental,
        detection=detection,
        wiki_index=wiki_index,
        include_html=include_html,
    )


def _load_existing_summary(paths: KBPaths, *, wiki_index: Path | None = None) -> BuildSummary:
    """Return a summary from a previously built graph without mutating files."""
    graph_data = json.loads((paths.out / "graph.json").read_text(encoding="utf-8"))
    nodes = graph_data.get("nodes", [])
    links = graph_data.get("links", graph_data.get("edges", []))
    communities = {node.get("community") for node in nodes if node.get("community") is not None}
    existing_wiki = wiki_index or (paths.out / "wiki" / "index.md")
    usage = _latest_cost_usage(paths.out / "cost.json")
    return BuildSummary(
        kb_root=paths.root,
        graph_path=paths.out / "graph.json",
        report_path=paths.out / "GRAPH_REPORT.md",
        html_path=paths.out / "graph.html",
        wiki_index_path=existing_wiki if existing_wiki.exists() else None,
        total_nodes=len(nodes),
        total_edges=len(links),
        total_communities=len(communities),
        semantic_files_extracted=0,
        semantic_cache_hits=0,
        changed_files=0,
        deleted_files=0,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cached_input_tokens=usage["cached_input_tokens"],
        semantic_stats_available=False,
        usage_available=usage["usage_available"],
    )


def _run_codex_skill(
    paths: KBPaths,
    *,
    update: bool,
    include_html: bool,
    model: str | None,
    runner_command: str,
) -> None:
    """Run the installed Codex graphify skill against the KB corpus."""
    if _find_codex_skill_path(paths.root) is None:
        raise KBError(
            "Codex graphify skill is not installed. Run `graphify install --platform codex` first."
        )

    prompt_parts = [_codex_skill_trigger(paths)]
    if update:
        prompt_parts.append("--update")
    if not include_html:
        prompt_parts.append("--no-viz")
    prompt = " ".join(prompt_parts)

    codex_command = _resolve_executable(runner_command)
    command = [
        codex_command,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--full-auto",
        "--ephemeral",
        "-C",
        str(paths.root),
    ]
    if model:
        command.extend(["--model", model])
    command.append(prompt)

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=_runner_env(codex_command),
        )
    except FileNotFoundError as exc:
        raise KBError(
            f"Codex runner not found: {runner_command}. "
            "Shell functions and aliases are not visible to graphify; configure "
            "`codex.runner_command` or pass `--runner /absolute/path/to/codex`."
        ) from exc
    usage = _parse_codex_usage(completed.stdout)
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise KBError(f"Codex skill execution failed: {stderr}")
    if not (paths.out / "graph.json").exists():
        raise KBError("Codex skill completed without writing graphify-out/graph.json")
    if not (paths.out / "GRAPH_REPORT.md").exists():
        raise KBError("Codex skill completed without writing graphify-out/GRAPH_REPORT.md")
    _append_cost_run(
        paths.out / "cost.json",
        usage,
        files=_count_manifest_files(paths.manifest_file),
    )


def _run_claude_skill(
    paths: KBPaths,
    *,
    update: bool,
    include_html: bool,
    model: str | None,
    runner_command: str,
    runner_args: list[str],
) -> None:
    """Run the installed Claude graphify skill via an explicit runner command."""
    if _find_claude_skill_path(paths.root) is None:
        raise KBError(
            "Claude graphify skill is not installed. Run `graphify install --platform claude` first."
        )

    prompt_parts = [_claude_skill_trigger(paths)]
    if update:
        prompt_parts.append("--update")
    if not include_html:
        prompt_parts.append("--no-viz")
    prompt = " ".join(prompt_parts)

    resolved_runner_command = _resolve_executable(runner_command)
    command = [
        resolved_runner_command,
        *runner_args,
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if model:
        command.extend(["--model", model])
    command.append(prompt)

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(paths.root),
            env=_runner_env(resolved_runner_command),
        )
    except FileNotFoundError as exc:
        raise KBError(f"Claude runner not found: {runner_command}") from exc

    result = _parse_claude_result(completed.stdout)
    if completed.returncode != 0:
        errors = result.get("errors", [])
        details = "; ".join(str(error) for error in errors if error) or completed.stderr.strip()
        if not details:
            details = completed.stdout.strip()
        raise KBError(f"Claude skill execution failed: {details}")
    if result.get("subtype") != "success":
        errors = result.get("errors", [])
        details = "; ".join(str(error) for error in errors if error) or "unknown Claude error"
        raise KBError(f"Claude skill execution failed: {details}")
    if not (paths.out / "graph.json").exists():
        raise KBError("Claude skill completed without writing graphify-out/graph.json")
    if not (paths.out / "GRAPH_REPORT.md").exists():
        raise KBError("Claude skill completed without writing graphify-out/GRAPH_REPORT.md")
    _append_cost_run(
        paths.out / "cost.json",
        _claude_usage_from_result(result),
        files=_count_manifest_files(paths.manifest_file),
    )


def _codex_skill_trigger(paths: KBPaths) -> str:
    """Return the slash-like skill invocation for the KB corpus."""
    corpus_arg = "./raw" if paths.corpus == paths.root / "raw" else "."
    return f"$graphify {corpus_arg}"


def _claude_skill_trigger(paths: KBPaths) -> str:
    """Return the slash-command invocation for Claude Code."""
    corpus_arg = "./raw" if paths.corpus == paths.root / "raw" else "."
    return f"/graphify {corpus_arg}"


def _resolve_executable(command: str) -> str:
    """Resolve a PATH command to an absolute executable when possible."""
    if _has_path_separator(command):
        return command
    return shutil.which(command) or command


def _runner_env(command: str) -> dict[str, str]:
    """Return an environment that keeps a resolved runner discoverable by child processes."""
    env = os.environ.copy()
    command_path = Path(command).expanduser()
    if command_path.parent != Path("."):
        env["PATH"] = f"{command_path.parent}{os.pathsep}{env.get('PATH', '')}"
    if "claude" in command_path.name:
        env["CLAUDE_CODE_TEAMMATE_COMMAND"] = str(command_path)
    return env


def _has_path_separator(command: str) -> bool:
    """Return True when a command already includes a filesystem path component."""
    return os.sep in command or (os.altsep is not None and os.altsep in command)


def _find_codex_skill_path(root: Path) -> Path | None:
    """Return the first installed Codex graphify skill path that exists."""
    candidates = [
        root / ".agents" / "skills" / "graphify" / "SKILL.md",
        Path.home() / ".agents" / "skills" / "graphify" / "SKILL.md",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _find_claude_skill_path(root: Path) -> Path | None:
    """Return the first installed Claude graphify skill path that exists."""
    candidates = [
        root / ".claude" / "skills" / "graphify" / "SKILL.md",
        Path.home() / ".claude" / "skills" / "graphify" / "SKILL.md",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _codex_runner(config: dict[str, Any], *, override_command: str | None = None) -> str:
    """Return the selected Codex runner command."""
    if override_command:
        return override_command
    command = str(config.get("codex", {}).get("runner_command", "")).strip()
    return command or "codex"


def _claude_runner(config: dict[str, Any], *, override_command: str | None = None) -> tuple[str, list[str]]:
    """Return the selected Claude runner command and args."""
    if override_command:
        return _resolve_claude_runner(config, override_command)

    claude_config = config.get("claude", {})
    runner = str(claude_config.get("runner", "")).strip()
    if runner:
        return _resolve_claude_runner(config, runner)

    command = str(claude_config.get("runner_command", "")).strip()
    if command:
        return command, _claude_runner_args(config)

    return _resolve_claude_runner(config, "claude")


def _resolve_claude_runner(config: dict[str, Any], selector: str) -> tuple[str, list[str]]:
    """Resolve a named Claude runner or use the selector as a direct command."""
    runners = config.get("claude", {}).get("runners", {})
    runner = runners.get(selector) if isinstance(runners, dict) else None
    if runner is None:
        return selector, []
    if not isinstance(runner, dict):
        raise KBError(f"Invalid Claude runner `{selector}`: expected a TOML table.")
    command = str(runner.get("command", "")).strip()
    if not command:
        raise KBError(f"Invalid Claude runner `{selector}`: missing command.")
    args = runner.get("args", [])
    if args in ("", None):
        args = []
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise KBError(f"Invalid Claude runner `{selector}`: args must be a TOML string array.")
    return command, list(args)


def _claude_runner_args(config: dict[str, Any]) -> list[str]:
    """Return validated Claude runner args from config."""
    raw_args = config.get("claude", {}).get("runner_args", [])
    if raw_args in ("", None):
        return []
    if not isinstance(raw_args, list) or any(not isinstance(arg, str) for arg in raw_args):
        raise KBError("Invalid `claude.runner_args`: expected a TOML string array.")
    return list(raw_args)


def _generate_wiki_from_graph(paths: KBPaths) -> Path:
    """Generate wiki markdown from the current graph outputs."""
    graph_data = json.loads((paths.out / "graph.json").read_text(encoding="utf-8"))
    graph = build_from_json(graph_data)
    communities = _communities_from_graph(graph)
    labels = _labels_from_report(paths.out / "GRAPH_REPORT.md")
    cohesion = score_all(graph, communities) if communities else {}
    gods = god_nodes(graph)
    wiki_dir = paths.out / "wiki"
    if wiki_dir.exists():
        for existing in wiki_dir.glob("*.md"):
            existing.unlink()
    to_wiki(
        graph,
        communities,
        wiki_dir,
        community_labels=labels,
        cohesion=cohesion,
        god_nodes_data=gods,
    )
    return wiki_dir / "index.md"


def _communities_from_graph(graph: Any) -> dict[int, list[str]]:
    """Rebuild community membership from node attributes stored in graph.json."""
    communities: dict[int, list[str]] = {}
    for node_id, attrs in graph.nodes(data=True):
        community = attrs.get("community")
        if community is None:
            continue
        communities.setdefault(int(community), []).append(node_id)
    return communities


def _labels_from_report(report_path: Path) -> dict[int, str]:
    """Extract community labels from GRAPH_REPORT.md headings when present."""
    if not report_path.exists():
        return {}
    labels: dict[int, str] = {}
    pattern = re.compile(r'^### Community (\d+) - "(.*)"$')
    for line in report_path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line.strip())
        if match:
            labels[int(match.group(1))] = match.group(2)
    return labels


def _summarize_outputs(
    paths: KBPaths,
    *,
    incremental: dict[str, Any],
    detection: dict[str, Any],
    wiki_index: Path | None,
    include_html: bool,
) -> BuildSummary:
    """Build a summary from outputs already written by the Codex skill."""
    graph_data = json.loads((paths.out / "graph.json").read_text(encoding="utf-8"))
    nodes = graph_data.get("nodes", [])
    links = graph_data.get("links", graph_data.get("edges", []))
    communities = {node.get("community") for node in nodes if node.get("community") is not None}
    usage = _latest_cost_usage(paths.out / "cost.json")
    return BuildSummary(
        kb_root=paths.root,
        graph_path=paths.out / "graph.json",
        report_path=paths.out / "GRAPH_REPORT.md",
        html_path=paths.out / "graph.html",
        wiki_index_path=wiki_index if wiki_index and wiki_index.exists() else None,
        total_nodes=len(nodes),
        total_edges=len(links),
        total_communities=len(communities),
        semantic_files_extracted=0,
        semantic_cache_hits=0,
        changed_files=incremental.get("new_total", detection.get("total_files", 0)),
        deleted_files=len(incremental.get("deleted_files", [])),
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cached_input_tokens=usage["cached_input_tokens"],
        semantic_stats_available=False,
        usage_available=usage["usage_available"],
    )


def _latest_cost_usage(cost_path: Path) -> dict[str, int | bool]:
    """Return usage fields from the most recent recorded run."""
    if not cost_path.exists():
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "usage_available": False,
        }
    try:
        payload = json.loads(cost_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "usage_available": False,
        }
    runs = payload.get("runs", [])
    if not runs:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "usage_available": False,
        }
    latest = runs[-1]
    return {
        "input_tokens": int(latest.get("input_tokens", 0)),
        "output_tokens": int(latest.get("output_tokens", 0)),
        "cached_input_tokens": int(latest.get("cached_input_tokens", 0)),
        "usage_available": bool(latest.get("usage_available", False)),
    }


def _parse_codex_usage(output: str) -> dict[str, int | bool]:
    """Parse the final `turn.completed` usage block from `codex exec --json` output."""
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "usage_available": False,
    }
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "turn.completed":
            continue
        event_usage = event.get("usage")
        if not isinstance(event_usage, dict):
            continue
        usage = {
            "input_tokens": int(event_usage.get("input_tokens", 0)),
            "output_tokens": int(event_usage.get("output_tokens", 0)),
            "cached_input_tokens": int(event_usage.get("cached_input_tokens", 0)),
            "usage_available": True,
        }
    return usage


def _parse_claude_result(output: str) -> dict[str, Any]:
    """Parse the final Claude `result` message from stream-json output."""
    result: dict[str, Any] = {}
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "result":
            continue
        result = event
    return result


def _claude_usage_from_result(result: dict[str, Any]) -> dict[str, int | bool]:
    """Convert a Claude result message into graphify cost fields."""
    usage = result.get("usage")
    if isinstance(usage, dict):
        parsed = {
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "cached_input_tokens": int(usage.get("cache_read_input_tokens", 0)),
            "usage_available": True,
        }
        if (
            parsed["input_tokens"] > 0
            or parsed["output_tokens"] > 0
            or parsed["cached_input_tokens"] > 0
        ):
            return parsed

    model_usage = result.get("modelUsage")
    if isinstance(model_usage, dict):
        input_tokens = 0
        output_tokens = 0
        cached_input_tokens = 0
        for item in model_usage.values():
            if not isinstance(item, dict):
                continue
            input_tokens += int(item.get("inputTokens", 0))
            output_tokens += int(item.get("outputTokens", 0))
            cached_input_tokens += int(item.get("cacheReadInputTokens", 0))
        if input_tokens > 0 or output_tokens > 0 or cached_input_tokens > 0:
            return {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": cached_input_tokens,
                "usage_available": True,
            }

    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "usage_available": False,
    }


def _append_cost_run(cost_path: Path, usage: dict[str, int | bool], *, files: int) -> None:
    """Append a run-level usage record to `graphify-out/cost.json`."""
    if cost_path.exists():
        try:
            payload = json.loads(cost_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
    else:
        payload = {}

    runs = payload.get("runs", [])
    if not isinstance(runs, list):
        runs = []
    total_input = int(payload.get("total_input_tokens", 0))
    total_output = int(payload.get("total_output_tokens", 0))

    runs.append(
        {
            "date": datetime.now(timezone.utc).isoformat(),
            "input_tokens": int(usage["input_tokens"]),
            "output_tokens": int(usage["output_tokens"]),
            "cached_input_tokens": int(usage["cached_input_tokens"]),
            "usage_available": bool(usage["usage_available"]),
            "files": files,
        }
    )
    payload = {
        "runs": runs,
        "total_input_tokens": total_input + int(usage["input_tokens"]),
        "total_output_tokens": total_output + int(usage["output_tokens"]),
    }
    cost_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _count_manifest_files(manifest_path: Path) -> int:
    """Count files from the current manifest, falling back to zero when absent."""
    if not manifest_path.exists():
        return 0
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    files = payload.get("files")
    if isinstance(files, dict):
        return len(files)
    return 0


def _normalize_source_files(
    payload: dict[str, Any],
    *,
    kb_root: Path,
    office_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Rewrite absolute `source_file` paths relative to the KB root when possible."""
    office_map = office_map or _office_sidecar_map(kb_root)
    for node in payload.get("nodes", []):
        source_file = node.get("source_file")
        if source_file:
            node["source_file"] = _normalize_source_file_value(source_file, kb_root=kb_root, office_map=office_map)
    for edge in [*payload.get("edges", []), *payload.get("links", [])]:
        source_file = edge.get("source_file")
        if source_file:
            edge["source_file"] = _normalize_source_file_value(source_file, kb_root=kb_root, office_map=office_map)
    for hyperedge in payload.get("hyperedges", []):
        source_file = hyperedge.get("source_file")
        if source_file:
            hyperedge["source_file"] = _normalize_source_file_value(
                source_file,
                kb_root=kb_root,
                office_map=office_map,
            )
    return payload


def _relative_path(path: str | Path, kb_root: Path) -> str:
    """Return a KB-relative path string when possible."""
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(kb_root.resolve())).replace("\\", "/")
    except ValueError:
        return str(candidate).replace("\\", "/")


def _normalize_source_file_value(source_file: str | Path, *, kb_root: Path, office_map: dict[str, str]) -> str:
    """Return a KB-relative `source_file`, remapping Office sidecars to the original file."""
    relative = _relative_path(source_file, kb_root)
    return office_map.get(relative, relative)


def _office_sidecar_map(kb_root: Path) -> dict[str, str]:
    """Map converted Office sidecar paths back to the original KB-relative Office files."""
    raw_root = kb_root / "raw"
    if not raw_root.exists():
        return {}

    mapping: dict[str, str] = {}
    converted_root = raw_root / "graphify-out" / "converted"
    for office_file in sorted(path for path in raw_root.rglob("*") if path.suffix.lower() in OFFICE_EXTENSIONS):
        try:
            office_file.relative_to(converted_root)
            continue
        except ValueError:
            pass
        name_hash = hashlib.sha256(str(office_file.resolve()).encode()).hexdigest()[:8]
        sidecar = converted_root / f"{office_file.stem}_{name_hash}.md"
        mapping[_relative_path(sidecar, kb_root)] = _relative_path(office_file, kb_root)
    return mapping


def _normalize_office_provenance(paths: KBPaths, *, include_html: bool) -> None:
    """Rewrite final KB outputs so Office provenance points at original files, not sidecars."""
    office_map = _office_sidecar_map(paths.root)
    if not office_map:
        return

    graph_path = paths.out / "graph.json"
    if graph_path.exists():
        graph_payload = json.loads(graph_path.read_text(encoding="utf-8"))
        normalized_graph = _normalize_source_files(graph_payload, kb_root=paths.root, office_map=office_map)
        graph_path.write_text(json.dumps(normalized_graph, indent=2), encoding="utf-8")

    _rewrite_text_provenance(paths.out / "GRAPH_REPORT.md", office_map)
    if include_html:
        _rewrite_text_provenance(paths.out / "graph.html", office_map)


def _rewrite_text_provenance(path: Path, office_map: dict[str, str]) -> None:
    """Replace Office sidecar paths with original Office file paths in a text artifact."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    updated = text
    for sidecar, original in office_map.items():
        updated = updated.replace(sidecar, original)
    if updated != text:
        path.write_text(updated, encoding="utf-8")


def _git_candidate_paths(root: Path) -> set[str] | None:
    """Return repo-relative changed/untracked files when the KB root is a git repo."""
    if not (root / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--short"],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        return None
    candidates: set[str] = set()
    for line in result.stdout.splitlines():
        entry = line[3:].strip()
        if not entry:
            continue
        candidates.add(entry.split(" -> ", 1)[-1])
    return candidates
