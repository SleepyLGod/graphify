"""Tests for sync helpers."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from graphify.kb import init_kb
from graphify.sync import _included_relative_files, _join_remote, _prune_ignored_files, _rclone_commands, sync_status


def test_join_remote_preserves_scope_path():
    assert _join_remote("drive:ai-wiki", Path("graphify-out") / "wiki") == "drive:ai-wiki/graphify-out/wiki"


def test_rclone_commands_for_out_scope(tmp_path):
    init_kb(tmp_path / "ai-wiki")
    commands = _rclone_commands(tmp_path / "ai-wiki", "drive:ai-wiki", "out")
    assert commands == [
        (str(tmp_path / "ai-wiki" / "graphify-out"), "drive:ai-wiki/graphify-out"),
    ]


def test_sync_status_reads_state(tmp_path):
    paths = init_kb(tmp_path / "ai-wiki")
    paths.sync_state_file.write_text(
        json.dumps({"direction": "push", "scope": "all", "remote": "drive:ai-wiki"}),
        encoding="utf-8",
    )
    status = sync_status(paths.root)
    assert status["last_sync"]["direction"] == "push"
    assert "all" in status["available_scopes"]


def test_sync_status_without_state(tmp_path):
    paths = init_kb(tmp_path / "ai-wiki")
    status = sync_status(paths.root)
    assert status["last_sync"] == {}


def test_sync_status_with_corrupt_state(tmp_path):
    paths = init_kb(tmp_path / "ai-wiki")
    paths.sync_state_file.write_text("{", encoding="utf-8")
    status = sync_status(paths.root)
    assert status["last_sync"] == {}


def test_included_relative_files_respects_gitignore(tmp_path):
    paths = init_kb(tmp_path / "ai-wiki")
    subprocess.run(["git", "init", str(paths.root)], check=True, capture_output=True, text=True)
    (paths.root / ".gitignore").write_text(".DS_Store\n", encoding="utf-8")
    (paths.root / "raw" / "notes.md").write_text("notes", encoding="utf-8")
    (paths.root / "raw" / ".DS_Store").write_text("junk", encoding="utf-8")

    files = _included_relative_files(paths.root, paths.root / "raw", "raw")

    assert files == ["notes.md"]


def test_included_relative_files_respects_graphifyignore_under_raw(tmp_path):
    paths = init_kb(tmp_path / "ai-wiki")
    nested_out = paths.root / "raw" / "graphify-out"
    nested_out.mkdir(parents=True)
    (paths.root / "raw" / "notes.md").write_text("notes", encoding="utf-8")
    (nested_out / "junk.json").write_text("{}", encoding="utf-8")

    files = _included_relative_files(paths.root, paths.root, "all")

    assert "raw/notes.md" in files
    assert "raw/graphify-out/junk.json" not in files


def test_prune_ignored_files_removes_pulled_junk(tmp_path):
    paths = init_kb(tmp_path / "ai-wiki")
    subprocess.run(["git", "init", str(paths.root)], check=True, capture_output=True, text=True)
    (paths.root / ".gitignore").write_text(".DS_Store\n", encoding="utf-8")
    nested_out = paths.root / "raw" / "graphify-out"
    nested_out.mkdir(parents=True)
    ds_store = paths.root / "raw" / ".DS_Store"
    ds_store.write_text("junk", encoding="utf-8")
    junk = nested_out / "junk.json"
    junk.write_text("{}", encoding="utf-8")
    keep = paths.root / "raw" / "notes.md"
    keep.write_text("notes", encoding="utf-8")

    _prune_ignored_files(paths.root, paths.root, "all")

    assert keep.exists()
    assert not ds_store.exists()
    assert not junk.exists()
    assert not nested_out.exists()
