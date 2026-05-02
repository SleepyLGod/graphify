"""Sync helpers for graphify knowledge bases."""
from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from graphify.detect import _is_ignored, _load_graphifyignore
from graphify.kb import KBError, load_config, resolve_paths


class SyncError(KBError):
    """Raised when a sync command cannot complete."""


_SCOPE_PATHS = {
    "raw": Path("raw"),
    "wiki": Path("graphify-out") / "wiki",
    "out": Path("graphify-out"),
    "config": Path(".graphify"),
}


def sync_push(root: Path | str, *, scope: str, remote: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Push a knowledge-base scope to the configured remote."""
    return _sync(root, direction="push", scope=scope, remote=remote, dry_run=dry_run)


def sync_pull(root: Path | str, *, scope: str, remote: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Pull a knowledge-base scope from the configured remote."""
    return _sync(root, direction="pull", scope=scope, remote=remote, dry_run=dry_run)


def sync_status(root: Path | str) -> dict[str, Any]:
    """Return the configured remote and last sync metadata for a knowledge base."""
    paths = resolve_paths(root)
    config = load_config(paths.root)
    state: dict[str, Any] = {}
    if paths.sync_state_file.exists():
        try:
            state = json.loads(paths.sync_state_file.read_text(encoding="utf-8"))
        except (JSONDecodeError, OSError):
            state = {}
    return {
        "kb_root": str(paths.root),
        "configured_remote": config["kb"].get("sync_remote", ""),
        "available_scopes": ["raw", "wiki", "out", "config", "all"],
        "last_sync": state,
    }


def _sync(root: Path | str, *, direction: str, scope: str, remote: str | None, dry_run: bool) -> dict[str, Any]:
    """Run an rclone sync in the requested direction."""
    if direction not in {"push", "pull"}:
        raise SyncError(f"Unsupported sync direction: {direction}")

    paths = resolve_paths(root)
    config = load_config(paths.root)
    remote_target = remote or config["kb"].get("sync_remote", "")
    if not remote_target:
        raise SyncError("No sync remote configured. Set `kb.sync_remote` or pass --remote.")

    if scope not in {"raw", "wiki", "out", "config", "all"}:
        raise SyncError(f"Unsupported scope: {scope}")
    try:
        subprocess.run(["rclone", "version"], capture_output=True, check=True, text=True)
    except FileNotFoundError as exc:
        raise SyncError("rclone is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise SyncError(exc.stderr.strip() or exc.stdout.strip() or "Unable to invoke rclone") from exc

    commands = _rclone_commands(paths.root, remote_target, scope)
    for local_path, remote_path in commands:
        if direction == "push":
            src, dst = local_path, remote_path
        else:
            src, dst = remote_path, local_path
        cmd = ["rclone", "sync", src, dst]
        files_from: str | None = None
        if direction == "push":
            files_from = _write_files_from(paths.root, Path(local_path), scope)
            cmd.extend(["--files-from", files_from])
        if dry_run:
            cmd.append("--dry-run")
        try:
            completed = subprocess.run(cmd, capture_output=True, check=False, text=True)
            if completed.returncode != 0:
                raise SyncError(completed.stderr.strip() or completed.stdout.strip() or "rclone sync failed")
        finally:
            if files_from is not None:
                Path(files_from).unlink(missing_ok=True)
        if direction == "pull" and not dry_run:
            _prune_ignored_files(paths.root, Path(local_path), scope)

    state = {
        "direction": direction,
        "scope": scope,
        "remote": remote_target,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.sync_state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def _rclone_commands(kb_root: Path, remote: str, scope: str) -> list[tuple[str, str]]:
    """Return `(local, remote)` path pairs for the requested sync scope."""
    if scope == "all":
        return [(str(kb_root), remote)]
    local_path = kb_root / _SCOPE_PATHS[scope]
    return [(str(local_path), _join_remote(remote, _SCOPE_PATHS[scope]))]


def _join_remote(remote: str, suffix: Path) -> str:
    """Append a relative suffix to an rclone remote path."""
    base = remote.rstrip("/")
    return f"{base}/{suffix.as_posix()}" if suffix.as_posix() else base


def _write_files_from(kb_root: Path, local_root: Path, scope: str) -> str:
    """Write an rclone `--files-from` list that excludes ignored local files."""
    allowed_files = _included_relative_files(kb_root, local_root, scope)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        for rel_path in allowed_files:
            handle.write(f"{rel_path}\n")
        return handle.name


def _included_relative_files(kb_root: Path, local_root: Path, scope: str) -> list[str]:
    """Return relative file paths to sync from `local_root`."""
    file_paths = sorted(p for p in local_root.rglob("*") if p.is_file())
    git_ignored = _git_ignored_paths(kb_root, file_paths)
    allowed: list[str] = []
    for file_path in file_paths:
        if _should_ignore(kb_root, file_path, scope, git_ignored=git_ignored):
            continue
        allowed.append(file_path.relative_to(local_root).as_posix())
    return allowed


def _prune_ignored_files(kb_root: Path, local_root: Path, scope: str) -> None:
    """Remove ignored files after a pull so restore results match local ignore rules."""
    file_paths = sorted((p for p in local_root.rglob("*") if p.is_file()), reverse=True)
    git_ignored = _git_ignored_paths(kb_root, file_paths)
    for file_path in file_paths:
        if _should_ignore(kb_root, file_path, scope, git_ignored=git_ignored):
            file_path.unlink(missing_ok=True)
    _remove_empty_dirs(local_root)


def _remove_empty_dirs(root: Path) -> None:
    """Remove empty directories under `root`, deepest-first."""
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        if any(directory.iterdir()):
            continue
        directory.rmdir()


def _should_ignore(kb_root: Path, path: Path, scope: str, *, git_ignored: set[str]) -> bool:
    """Return True when a path should be excluded from sync for the requested scope."""
    if _is_always_excluded(kb_root, path):
        return True
    if _relative_git_path(kb_root, path) in git_ignored:
        return True
    return _is_graphify_ignored(kb_root, path, scope)


def _is_always_excluded(kb_root: Path, path: Path) -> bool:
    """Return True for files that should never participate in KB sync."""
    try:
        rel_path = path.relative_to(kb_root)
    except ValueError:
        return False
    return ".git" in rel_path.parts


def _relative_git_path(kb_root: Path, path: Path) -> str | None:
    """Return a git-style relative path under the KB root, or None when unrelated."""
    try:
        rel_path = path.relative_to(kb_root).as_posix()
    except ValueError:
        return None
    if not rel_path or rel_path.startswith(".git/") or rel_path == ".git":
        return None
    return rel_path


def _git_ignored_paths(kb_root: Path, paths: list[Path]) -> set[str]:
    """Return git-ignored relative paths for the provided files."""
    if not (kb_root / ".git").exists():
        return set()
    rel_paths = [rel_path for path in paths if (rel_path := _relative_git_path(kb_root, path))]
    if not rel_paths:
        return set()

    try:
        completed = subprocess.run(
            ["git", "-C", str(kb_root), "check-ignore", "--stdin"],
            input="\n".join(rel_paths),
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError:
        return set()
    if completed.returncode not in {0, 1}:
        return set()
    return {line for line in completed.stdout.splitlines() if line}


def _is_graphify_ignored(kb_root: Path, path: Path, scope: str) -> bool:
    """Return True when `.graphifyignore` excludes a file under the raw corpus."""
    raw_root = kb_root / "raw"
    if scope == "wiki" or not raw_root.exists():
        return False
    try:
        path.relative_to(raw_root)
    except ValueError:
        return False
    patterns = _load_graphifyignore(kb_root)
    return _is_ignored(path, kb_root, patterns)
