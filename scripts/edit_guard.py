#!/usr/bin/env python3
"""Freeze a local edit contract and preserve all bytes outside selected spans.

This is a text-boundary tool, not a semantic grader or an automatic host hook.
It does not execute instructions found in any input file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


VERSION = 1
SNAPSHOTS = {
    "baseline": "baseline.md",
    "requirements": "requirements.json",
    "request": "request.txt",
    "scope": "scope.json",
}
BOM = b"\xef\xbb\xbf"


class GuardError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes(path: Path) -> bytes:
    try:
        if not path.is_file():
            raise GuardError("missing_file", f"Input is not a regular file: {path}")
        return path.read_bytes()
    except OSError as exc:
        raise GuardError("read_failed", f"Cannot read file: {path} ({exc.__class__.__name__})") from exc


def decode(data: bytes, label: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GuardError("invalid_utf8", f"{label} must be UTF-8 text") from exc


def parse_json(data: bytes, label: str):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise GuardError("duplicate_json_key", f"Duplicate JSON key in {label}")
            result[key] = value
        return result

    try:
        return json.loads(decode(data, label), object_pairs_hook=unique_keys)
    except json.JSONDecodeError as exc:
        raise GuardError("invalid_json", f"{label}: invalid JSON at line {exc.lineno}, column {exc.colno}") from exc


def encode_json(data) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def rows(document, key: str, fields: set[str]) -> list[dict]:
    if not isinstance(document, dict) or set(document) != {key}:
        raise GuardError("invalid_schema", f"Expected an object with only '{key}'")
    values = document[key]
    if not isinstance(values, list) or not values:
        raise GuardError("invalid_schema", f"'{key}' must be a non-empty list")
    ids = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != fields:
            raise GuardError("invalid_schema", f"Each '{key}' item requires exactly: {', '.join(sorted(fields))}")
        if not all(isinstance(value[field], str) for field in fields):
            raise GuardError("invalid_schema", f"All '{key}' fields must be strings")
        if not value["id"].strip() or value["id"] in ids:
            raise GuardError("invalid_id", f"'{key}' IDs must be non-empty and unique")
        ids.add(value["id"])
    return values


def validate_requirements(data: bytes) -> list[dict]:
    result = rows(parse_json(data, "requirements"), "requirements", {"id", "text", "source"})
    if any(not row["text"].strip() or not row["source"].strip() for row in result):
        raise GuardError("invalid_requirement", "Requirement text and source must be non-empty")
    return result


def locate_spans(baseline: bytes, scope: bytes) -> list[dict]:
    decode(baseline, "baseline")
    editable = rows(parse_json(scope, "scope"), "editable", {"id", "text"})
    spans = []
    for row in editable:
        if not row["text"].strip():
            raise GuardError("empty_anchor", "Editable text must contain non-whitespace text")
        anchor = row["text"].encode("utf-8")
        start = baseline.find(anchor)
        if start < 0:
            raise GuardError("anchor_not_found", f"Editable text does not exactly match baseline: {row['id']}")
        if baseline.find(anchor, start + 1) >= 0:
            raise GuardError("ambiguous_anchor", f"Editable text occurs more than once: {row['id']}")
        spans.append({"id": row["id"], "start": start, "end": start + len(anchor)})
    spans.sort(key=lambda span: span["start"])
    for left, right in zip(spans, spans[1:]):
        if left["end"] > right["start"]:
            raise GuardError("overlapping_anchors", "Editable spans overlap; select distinct spans")
    protected = []
    end = 0
    for span in spans:
        protected.append(baseline[end:span["start"]])
        end = span["end"]
    protected.append(baseline[end:])
    protected_bytes = b"".join(protected)
    if protected_bytes.startswith(BOM):
        protected_bytes = protected_bytes[len(BOM):]
    if not protected_bytes.decode("utf-8").strip():
        raise GuardError("whole_document_scope", "The whole document cannot be editable in a local-edit session")
    return spans


def new_file(path: Path, content: bytes):
    created_identity = None
    try:
        with path.open("xb") as stream:
            stat = os.fstat(stream.fileno())
            created_identity = (stat.st_dev, stat.st_ino)
            stream.write(content)
    except FileExistsError as exc:
        raise GuardError("output_exists", f"Refusing to overwrite: {path}") from exc
    except OSError as exc:
        # Remove only the incomplete file this call created, never an existing file.
        if created_identity is not None:
            try:
                stat = path.stat()
                if (stat.st_dev, stat.st_ino) == created_identity:
                    path.unlink()
            except OSError:
                pass
        raise GuardError("write_failed", f"Cannot create file: {path} ({exc.__class__.__name__})") from exc


def prepare(args) -> dict:
    source_paths = {
        "baseline": Path(args.baseline).resolve(),
        "requirements": Path(args.requirements).resolve(),
        "request": Path(args.request_file).resolve(),
        "scope": Path(args.scope).resolve(),
    }
    data = {name: read_bytes(path) for name, path in source_paths.items()}
    baseline_text = decode(data["baseline"], "baseline")
    requirements = validate_requirements(data["requirements"])
    request = decode(data["request"], "request")
    if not request.strip():
        raise GuardError("empty_request", "Current request must be non-empty")
    spans = locate_spans(data["baseline"], data["scope"])
    requested_session = Path(args.session).absolute()
    if os.path.lexists(requested_session):
        raise GuardError("session_exists", f"Refusing to reuse existing session: {requested_session}")
    session = requested_session.resolve()
    packet = (
        "# 本轮局部改稿材料\n\n"
        "以下为本轮数据。范围由任务执行者依据用户要求选定；工具不判断选区是否正确。\n"
        "使用 render 仅替换已选片段；范围外字节保留。情绪、因果和最新要求仍需逐项复核。\n\n"
        "## 当前有效要求及来源\n\n" + json.dumps({"requirements": requirements}, ensure_ascii=False, indent=2)
        + "\n\n## 本轮用户原话\n\n" + request
        + "\n\n## 可修改片段\n\n" + decode(data["scope"], "scope")
        + "\n\n## 原稿\n\n" + baseline_text
    ).encode("utf-8")
    manifest = {
        "schema_version": VERSION,
        "sources": {name: {"path": str(source_paths[name]), "sha256": digest(raw)} for name, raw in data.items()},
        "snapshots": {SNAPSHOTS[name]: digest(raw) for name, raw in data.items()},
        "packet_sha256": digest(packet),
    }
    manifest_bytes = encode_json(manifest)
    for name, path in source_paths.items():
        if read_bytes(path) != data[name]:
            raise GuardError("source_changed", "An input changed during preparation; prepare again")
    try:
        session.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise GuardError("session_exists", f"Refusing to reuse existing session: {session}") from exc
    except OSError as exc:
        raise GuardError("session_create_failed", f"Cannot create session: {session}") from exc
    # The seal is written last. A partial session is unusable and is never resumed.
    for name, raw in data.items():
        new_file(session / SNAPSHOTS[name], raw)
    new_file(session / "packet.md", packet)
    new_file(session / "manifest.json", manifest_bytes)
    new_file(session / "manifest.sha256", (digest(manifest_bytes) + "\n").encode("ascii"))
    return {
        "status": "prepared", "session": str(session), "packet": str(session / "packet.md"),
        "editable_ids": [span["id"] for span in spans],
        "source_freshness": "four_input_files_verified",
        "semantic_check": "not_evaluated",
    }


def load_session(path: str):
    session = Path(path).resolve()
    raw = read_bytes(session / "manifest.json")
    seal = read_bytes(session / "manifest.sha256")
    if seal != (digest(raw) + "\n").encode("ascii"):
        raise GuardError("snapshot_changed", "Session manifest changed or preparation was incomplete; prepare again")
    manifest = parse_json(raw, "manifest")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != VERSION:
        raise GuardError("invalid_session", "Unsupported session format; prepare again")
    if set(manifest.get("sources", {})) != set(SNAPSHOTS) or set(manifest.get("snapshots", {})) != set(SNAPSHOTS.values()):
        raise GuardError("invalid_session", "Invalid source or snapshot inventory; prepare again")
    data = {}
    source_paths = []
    for name, filename in SNAPSHOTS.items():
        source = manifest["sources"][name]
        if not isinstance(source, dict) or set(source) != {"path", "sha256"} or not isinstance(source["path"], str):
            raise GuardError("invalid_session", "Invalid source record; prepare again")
        original = Path(source["path"])
        if not original.is_absolute():
            raise GuardError("invalid_session", "Source paths must be absolute")
        frozen = read_bytes(session / filename)
        if digest(frozen) != manifest["snapshots"][filename] or digest(frozen) != source["sha256"]:
            raise GuardError("snapshot_changed", f"Frozen {name} changed; prepare a new session")
        if digest(read_bytes(original)) != source["sha256"]:
            raise GuardError("source_changed", f"Original {name} changed; read current requirements and prepare a new session")
        data[name] = frozen
        source_paths.append(original.resolve())
    if digest(read_bytes(session / "packet.md")) != manifest.get("packet_sha256"):
        raise GuardError("snapshot_changed", "Context packet changed; prepare a new session")
    validate_requirements(data["requirements"])
    spans = locate_spans(data["baseline"], data["scope"])
    return session, data, spans, source_paths


def expected_output(args):
    session, data, spans, source_paths = load_session(args.session)
    edits_path = Path(args.edits).resolve()
    replacements = rows(parse_json(read_bytes(edits_path), "edits"), "replacements", {"id", "text"})
    by_id = {row["id"]: row["text"].encode("utf-8") for row in replacements}
    if set(by_id) != {span["id"] for span in spans}:
        raise GuardError("replacement_ids_mismatch", "Provide each editable ID exactly once, with no extra IDs")
    baseline = data["baseline"]
    parts = []
    end = 0
    changed = []
    for span in spans:
        parts.extend([baseline[end:span["start"]], by_id[span["id"]]])
        if baseline[span["start"]:span["end"]] != by_id[span["id"]]:
            changed.append(span["id"])
        end = span["end"]
    parts.append(baseline[end:])
    return session, b"".join(parts), changed, source_paths + [edits_path]


def render(args) -> dict:
    session, expected, changed, protected_paths = expected_output(args)
    requested_output = Path(args.output).absolute()
    output = requested_output.resolve()
    if output == session or session in output.parents or output in protected_paths:
        raise GuardError("protected_output_path", "Output must be a new draft outside the session and input files")
    if os.path.lexists(requested_output) or os.path.lexists(output):
        raise GuardError("output_exists", f"Refusing to overwrite: {output}")
    new_file(output, expected)
    return {
        "status": "rendered", "candidate": str(output), "candidate_sha256": digest(expected),
        "changed_ids": changed, "mechanical_check": "outside_scope_preserved",
        "source_freshness": "four_input_files_verified_at_command_start",
        "semantic_check": "not_evaluated",
    }


def check(args) -> dict:
    _, expected, changed, _ = expected_output(args)
    candidate_path = Path(args.candidate).resolve()
    candidate = read_bytes(candidate_path)
    matches = candidate == expected
    result = {
        "status": "scope_preserved" if matches else "candidate_mismatch",
        "candidate": str(candidate_path), "candidate_sha256": digest(candidate),
        "expected_sha256": digest(expected), "changed_ids": changed,
        "source_freshness": "four_input_files_verified_at_command_start",
        "semantic_check": "not_evaluated",
    }
    if not matches:
        offset = next((i for i, (left, right) in enumerate(zip(expected, candidate)) if left != right), min(len(expected), len(candidate)))
        result["difference"] = {"first_byte_offset": offset, "expected_bytes": len(expected), "candidate_bytes": len(candidate)}
    return result


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise GuardError("invalid_arguments", message)


def parser() -> argparse.ArgumentParser:
    root = JsonArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="Freeze current inputs and a bounded editing scope")
    for flag in ["baseline", "requirements", "request-file", "scope", "session"]:
        prep.add_argument("--" + flag, required=True)
    for name in ["render", "check"]:
        command = commands.add_parser(name)
        command.add_argument("--session", required=True)
        command.add_argument("--edits", required=True)
        command.add_argument("--output" if name == "render" else "--candidate", required=True)
    return root


def main(argv=None) -> int:
    try:
        args = parser().parse_args(argv)
        result = {"prepare": prepare, "render": render, "check": check}[args.command](args)
        code = 1 if result["status"] == "candidate_mismatch" else 0
    except GuardError as exc:
        result = {"status": "error", "code": exc.code, "message": str(exc), "semantic_check": "not_evaluated"}
        code = 2
    except (OSError, UnicodeError, TypeError, ValueError, KeyError, RuntimeError) as exc:
        result = {"status": "error", "code": "invalid_input_or_io", "message": exc.__class__.__name__, "semantic_check": "not_evaluated"}
        code = 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
