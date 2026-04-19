"""Non-interactive semantic extraction providers."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class ProviderError(RuntimeError):
    """Raised when a semantic extraction provider fails."""


_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": True,
                "required": ["id", "label", "file_type", "source_file"],
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "file_type": {"type": "string"},
                    "source_file": {"type": "string"},
                },
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": True,
                "required": ["source", "target", "relation", "confidence", "source_file"],
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "relation": {"type": "string"},
                    "confidence": {"type": "string"},
                    "source_file": {"type": "string"},
                },
            },
        },
        "hyperedges": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "input_tokens": {"type": "integer"},
        "output_tokens": {"type": "integer"},
    },
    "required": ["nodes", "edges", "hyperedges", "input_tokens", "output_tokens"],
}


class CodexExecProvider:
    """Semantic extraction provider backed by `codex exec`."""

    def __init__(self, *, model: str | None = None, binary: str = "codex") -> None:
        self._model = model
        self._binary = binary

    def extract_chunk(self, files: list[Path], *, kb_root: Path) -> dict[str, Any]:
        """Extract a graph fragment for a chunk of files."""
        prompt = self._build_prompt(files, kb_root)
        kb_root.mkdir(parents=True, exist_ok=True)
        output_dir = kb_root / ".graphify" / "provider-tmp"
        output_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile("w", suffix=".json", dir=output_dir, delete=False) as schema_file:
            json.dump(_OUTPUT_SCHEMA, schema_file)
            schema_path = Path(schema_file.name)
        with tempfile.NamedTemporaryFile("w", suffix=".json", dir=output_dir, delete=False) as output_file:
            output_path = Path(output_file.name)

        try:
            command = [
                self._binary,
                "exec",
                "--skip-git-repo-check",
                "--full-auto",
                "--ephemeral",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-C",
                str(kb_root),
            ]
            image_files = [path for path in files if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}]
            for image_path in image_files:
                command.extend(["--image", str(image_path)])
            if self._model:
                command.extend(["--model", self._model])
            command.append(prompt)

            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            if completed.returncode != 0:
                stderr = completed.stderr.strip() or completed.stdout.strip()
                raise ProviderError(f"codex exec failed: {stderr}")
            if not output_path.exists():
                raise ProviderError("codex exec did not write an output file")
            payload = _parse_json_payload(output_path.read_text(encoding="utf-8"))
            payload.setdefault("nodes", [])
            payload.setdefault("edges", [])
            payload.setdefault("hyperedges", [])
            payload.setdefault("input_tokens", 0)
            payload.setdefault("output_tokens", 0)
            return payload
        finally:
            schema_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)

    def _build_prompt(self, files: list[Path], kb_root: Path) -> str:
        """Build the extraction prompt for a single chunk."""
        rel_files = []
        for file_path in files:
            try:
                rel_files.append(str(file_path.resolve().relative_to(kb_root.resolve())))
            except ValueError:
                rel_files.append(str(file_path))
        file_list = "\n".join(f"- {path}" for path in rel_files)
        return (
            "Read the listed local files from the working directory and extract a graph fragment.\n"
            "Return JSON only. Follow the JSON schema exactly.\n\n"
            "Files:\n"
            f"{file_list}\n\n"
            "Rules:\n"
            "- Extract entities, modules, concepts, documents, and important visual concepts.\n"
            "- file_type must be one of code, document, paper, image.\n"
            "- relation should prefer calls, implements, references, cites, conceptually_related_to, "
            "shares_data_with, semantically_similar_to, rationale_for.\n"
            "- confidence must be EXTRACTED, INFERRED, or AMBIGUOUS.\n"
            "- source_file must be the relative file path.\n"
            "- For images, describe the visual content instead of OCR-only text.\n"
            "- Use empty arrays if nothing meaningful is present.\n"
        )


def _parse_json_payload(payload: str) -> dict[str, Any]:
    """Parse the last agent message as JSON, tolerating fenced code blocks."""
    stripped = payload.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"Provider returned invalid JSON: {exc}") from exc
