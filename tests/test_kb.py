"""Tests for local knowledge-base helpers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

from graphify.detect import detect_incremental, save_manifest
from graphify.kb import _parse_codex_usage, build_kb, init_kb, load_config


def test_init_kb_creates_layout(tmp_path):
    paths = init_kb(tmp_path / "ai-wiki")
    assert paths.root.exists()
    assert paths.corpus.exists()
    assert paths.out.exists()
    assert paths.config_file.exists()
    config = load_config(paths.root)
    assert config["kb"]["provider"] == "codex_skill"


def test_detect_incremental_with_rich_manifest(tmp_path):
    root = tmp_path / "kb"
    raw = root / "raw"
    raw.mkdir(parents=True)
    note = raw / "note.md"
    note.write_text("# Note\n\nVersion one.")

    files = {
        "code": [],
        "document": [str(note)],
        "paper": [],
        "image": [],
        "video": [],
    }
    manifest = root / "graphify-out" / "manifest.json"
    save_manifest(files, manifest_path=str(manifest), root=root)

    note.write_text("# Note\n\nVersion two.")
    result = detect_incremental(raw, manifest_path=str(manifest), relative_root=root)
    assert result["new_files"]["document"] == [str(note)]
    assert result["new_total"] == 1


def test_detect_incremental_respects_candidate_paths(tmp_path):
    root = tmp_path / "kb"
    raw = root / "raw"
    raw.mkdir(parents=True)
    one = raw / "one.md"
    two = raw / "two.md"
    one.write_text("one")
    two.write_text("two")

    files = {
        "code": [],
        "document": [str(one), str(two)],
        "paper": [],
        "image": [],
        "video": [],
    }
    manifest = root / "graphify-out" / "manifest.json"
    save_manifest(files, manifest_path=str(manifest), root=root)

    one.write_text("changed")
    result = detect_incremental(
        raw,
        manifest_path=str(manifest),
        relative_root=root,
        candidate_paths={"raw/one.md"},
    )
    assert result["new_files"]["document"] == [str(one)]
    assert result["unchanged_files"]["document"] == [str(two)]


def test_detect_incremental_ignores_file_that_disappears_before_stat(tmp_path, monkeypatch):
    root = tmp_path / "kb"
    raw = root / "raw"
    raw.mkdir(parents=True)
    note = raw / "note.md"
    note.write_text("note")

    files = {
        "code": [],
        "document": [str(note)],
        "paper": [],
        "image": [],
        "video": [],
    }
    manifest = root / "graphify-out" / "manifest.json"
    save_manifest(files, manifest_path=str(manifest), root=root)

    original_stat = Path.stat

    def flaky_stat(self: Path, *args, **kwargs):
        if self == note:
            raise OSError("gone")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr("graphify.detect.Path.stat", flaky_stat)
    result = detect_incremental(raw, manifest_path=str(manifest), relative_root=root)
    assert result["new_files"]["document"] == []
    assert result["deleted_files"] == ["raw/note.md"]


def test_load_manifest_legacy_payload(tmp_path):
    from graphify.detect import load_manifest

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"raw/note.md": 123.0}), encoding="utf-8")
    loaded = load_manifest(str(manifest))
    assert loaded["raw/note.md"]["mtime"] == 123.0


def test_build_kb_delegates_to_codex_skill_and_generates_wiki(tmp_path, monkeypatch):
    root = tmp_path / "ai-wiki"
    paths = init_kb(root)
    (root / ".agents" / "skills" / "graphify").mkdir(parents=True)
    (root / ".agents" / "skills" / "graphify" / "SKILL.md").write_text("stub", encoding="utf-8")
    (paths.corpus / "sample.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    stale_wiki = paths.out / "wiki" / "Validation_And_Persistence.md"
    stale_wiki.parent.mkdir(parents=True, exist_ok=True)
    stale_wiki.write_text("stale", encoding="utf-8")
    office = paths.corpus / "report.docx"
    office.write_text("placeholder", encoding="utf-8")
    sidecar_hash = hashlib.sha256(str(office.resolve()).encode()).hexdigest()[:8]
    sidecar_rel = f"raw/graphify-out/converted/report_{sidecar_hash}.md"

    def fake_run(cmd, capture_output=True, text=True, check=False):
        assert cmd[:2] == ["codex", "exec"]
        assert "--json" in cmd
        assert cmd[-1] == "$graphify ./raw --no-viz"
        sidecar = paths.corpus / "graphify-out" / "converted" / f"report_{sidecar_hash}.md"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("<!-- converted from report.docx -->\n\n# Report\n", encoding="utf-8")
        graph = {
            "nodes": [
                {
                    "id": "sample_answer",
                    "label": "answer()",
                    "file_type": "code",
                    "source_file": "raw/sample.py",
                    "community": 0,
                },
                {
                    "id": "office_report",
                    "label": "Report",
                    "file_type": "document",
                    "source_file": sidecar_rel,
                    "community": 0,
                }
            ],
            "links": [],
        }
        (paths.out / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
        (paths.out / "GRAPH_REPORT.md").write_text(
            '## Communities\n\n### Community 0 - "Functions"\n\n'
            f"- `{sidecar_rel}`\n",
            encoding="utf-8",
        )
        (paths.manifest_file).write_text(
            json.dumps({"version": 2, "files": {"raw/sample.py": {}, sidecar_rel: {}}}),
            encoding="utf-8",
        )
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t1"}),
                json.dumps({"type": "item.completed", "item": {"id": "x", "type": "agent_message", "text": "OK"}}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 12,
                            "cached_input_tokens": 3,
                            "output_tokens": 34,
                        },
                    }
                ),
            ]
        )
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr("graphify.kb.subprocess.run", fake_run)
    summary = build_kb(root, include_wiki=True, include_html=False)
    assert summary.total_nodes == 2
    assert summary.total_edges == 0
    assert summary.total_communities == 1
    assert summary.input_tokens == 12
    assert summary.output_tokens == 34
    assert summary.cached_input_tokens == 3
    assert summary.usage_available is True
    assert summary.wiki_index_path == paths.out / "wiki" / "index.md"
    assert summary.wiki_index_path.exists()
    graph = json.loads((paths.out / "graph.json").read_text(encoding="utf-8"))
    source_files = {node["source_file"] for node in graph["nodes"]}
    assert "raw/report.docx" in source_files
    assert sidecar_rel not in source_files
    report_text = (paths.out / "GRAPH_REPORT.md").read_text(encoding="utf-8")
    assert "raw/report.docx" in report_text
    assert sidecar_rel not in report_text
    wiki_text = (paths.out / "wiki" / "Functions.md").read_text(encoding="utf-8")
    assert "raw/report.docx" in wiki_text
    assert sidecar_rel not in wiki_text
    assert not stale_wiki.exists()
    cost = json.loads((paths.out / "cost.json").read_text(encoding="utf-8"))
    assert cost["runs"][-1]["input_tokens"] == 12
    assert cost["runs"][-1]["cached_input_tokens"] == 3
    assert cost["runs"][-1]["usage_available"] is True


def test_build_kb_rejects_unsupported_provider_from_config(tmp_path):
    root = tmp_path / "ai-wiki"
    paths = init_kb(root)
    paths.config_file.write_text(
        '[kb]\nprovider = "codex_exec"\nmodel = ""\nsync_remote = ""\n',
        encoding="utf-8",
    )
    (paths.corpus / "sample.py").write_text("def answer():\n    return 42\n", encoding="utf-8")

    try:
        build_kb(root, include_wiki=False, include_html=False)
    except RuntimeError as exc:
        assert "currently supports only 'codex_skill'" in str(exc)
    else:
        raise AssertionError("build_kb should reject unsupported providers")


def test_build_kb_update_reuses_existing_usage_on_noop(tmp_path):
    root = tmp_path / "ai-wiki"
    paths = init_kb(root)
    (paths.corpus / "sample.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    graph = {
        "nodes": [{"id": "sample_answer", "label": "answer()", "file_type": "code", "source_file": "raw/sample.py", "community": 0}],
        "links": [],
    }
    paths.out.mkdir(parents=True, exist_ok=True)
    (paths.out / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    (paths.out / "GRAPH_REPORT.md").write_text("report", encoding="utf-8")
    (paths.out / "cost.json").write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "date": "2026-04-20T00:00:00+00:00",
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cached_input_tokens": 5,
                        "usage_available": True,
                        "files": 1,
                    }
                ],
                "total_input_tokens": 10,
                "total_output_tokens": 20,
            }
        ),
        encoding="utf-8",
    )
    save_manifest(
        {"code": [str(paths.corpus / "sample.py")], "document": [], "paper": [], "image": [], "video": []},
        manifest_path=str(paths.manifest_file),
        root=paths.root,
    )

    summary = build_kb(root, update=True, include_wiki=False, include_html=False)
    assert summary.changed_files == 0
    assert summary.input_tokens == 10
    assert summary.output_tokens == 20
    assert summary.cached_input_tokens == 5
    assert summary.usage_available is True


def test_cli_update_uses_ast_only_fallback_outside_kb(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    calls: dict[str, Path] = {}

    def fake_rebuild(path: Path, *, follow_symlinks: bool = False) -> bool:
        calls["path"] = path
        return True

    monkeypatch.setattr("graphify.watch._rebuild_code", fake_rebuild)
    monkeypatch.setattr(sys, "argv", ["graphify", "update", str(repo)])

    from graphify.__main__ import main

    main()
    assert calls["path"] == repo


def test_parse_codex_usage_without_usage_block():
    usage = _parse_codex_usage('{"type":"thread.started"}\n{"type":"turn.completed"}\n')
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["cached_input_tokens"] == 0
    assert usage["usage_available"] is False
