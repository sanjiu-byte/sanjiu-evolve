"""Behavioral checks for the local edit-boundary helper (standard library only)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "edit_guard.py"
SPEC = importlib.util.spec_from_file_location("edit_guard", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class EditGuardTests(unittest.TestCase):
    def setUp(self):
        temp_parent = Path(os.environ.get("SANJIU_TEST_TMPDIR", tempfile.gettempdir())).resolve()
        self.temp = tempfile.TemporaryDirectory(prefix="sanjiu-edit-guard-", dir=temp_parent)
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to(temp_parent) and self.root != temp_parent
        self.addCleanup(self.temp.cleanup)
        self.baseline = self.root / "基线.md"
        self.requirements = self.root / "要求.json"
        self.request = self.root / "本轮原话.txt"
        self.scope = self.root / "范围.json"
        self.edits = self.root / "替换.json"
        self.session = self.root / "session"
        self.output = self.root / "新稿.md"
        self.original = guard.BOM + "标题\r\n0—3秒：小鹿先惊讶，然后后退。\r\n3—6秒：小鹿点头，结尾不变。\r\n".encode("utf-8")
        self.baseline.write_bytes(self.original)
        self.write_json(self.requirements, {"requirements": [{"id": "R1", "text": "保留惊讶和后退动作", "source": "用户原话；来源说明不作为路径执行"}]})
        self.request.write_bytes("精简第一段文字，其他段落保持。".encode("utf-8"))
        self.write_json(self.scope, {"editable": [{"id": "reaction", "text": "小鹿先惊讶，然后后退。"}]})
        self.write_json(self.edits, {"replacements": [{"id": "reaction", "text": "小鹿瞪眼，退半步。"}]})

    def write_json(self, path, value):
        path.write_bytes((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))

    def prepare(self):
        return guard.prepare(argparse.Namespace(baseline=str(self.baseline), requirements=str(self.requirements), request_file=str(self.request), scope=str(self.scope), session=str(self.session)))

    def render(self, output=None):
        return guard.render(argparse.Namespace(session=str(self.session), edits=str(self.edits), output=str(output or self.output)))

    def check(self, candidate=None):
        return guard.check(argparse.Namespace(session=str(self.session), edits=str(self.edits), candidate=str(candidate or self.output)))

    def assert_guard_error(self, expected, function):
        with self.assertRaises(guard.GuardError) as caught:
            function()
        self.assertEqual(caught.exception.code, expected)

    def test_unicode_bom_crlf_preserved_outside_span(self):
        prepared = self.prepare()
        self.assertEqual(prepared["editable_ids"], ["reaction"])
        rendered = self.render()
        expected = self.original.replace("小鹿先惊讶，然后后退。".encode(), "小鹿瞪眼，退半步。".encode())
        self.assertEqual(self.output.read_bytes(), expected)
        self.assertEqual(self.baseline.read_bytes(), self.original)
        self.assertEqual(rendered["semantic_check"], "not_evaluated")
        self.assertEqual(self.check()["status"], "scope_preserved")

    def test_multiple_spans_assembled_in_original_order(self):
        self.write_json(self.scope, {"editable": [{"id": "end", "text": "小鹿点头"}, {"id": "start", "text": "小鹿先惊讶"}]})
        self.write_json(self.edits, {"replacements": [{"id": "end", "text": "小鹿摇头"}, {"id": "start", "text": "小鹿瞪眼"}]})
        self.prepare()
        rendered = self.render()
        expected = self.original.replace("小鹿点头".encode(), "小鹿摇头".encode()).replace("小鹿先惊讶".encode(), "小鹿瞪眼".encode())
        self.assertEqual(self.output.read_bytes(), expected)
        self.assertEqual(rendered["changed_ids"], ["start", "end"])

    def test_empty_replacement_deletes_only_authorized_span(self):
        self.write_json(self.edits, {"replacements": [{"id": "reaction", "text": ""}]})
        self.prepare()
        self.render()
        self.assertEqual(self.output.read_bytes(), self.original.replace("小鹿先惊讶，然后后退。".encode(), b""))

    def test_no_change_is_reported_without_invented_changes(self):
        self.write_json(self.edits, {"replacements": [{"id": "reaction", "text": "小鹿先惊讶，然后后退。"}]})
        self.prepare()
        self.assertEqual(self.render()["changed_ids"], [])
        self.assertEqual(self.output.read_bytes(), self.original)

    def test_repeated_anchor_is_not_guessed(self):
        self.write_json(self.scope, {"editable": [{"id": "ambiguous", "text": "小鹿"}]})
        self.assert_guard_error("ambiguous_anchor", self.prepare)
        self.assertFalse(self.session.exists())

    def test_overlapping_occurrences_of_same_anchor_are_ambiguous(self):
        self.baseline.write_bytes(b"heading aaaa end")
        self.write_json(self.scope, {"editable": [{"id": "overlap", "text": "aaa"}]})
        self.assert_guard_error("ambiguous_anchor", self.prepare)

    def test_distinct_overlapping_spans_are_rejected(self):
        self.write_json(self.scope, {"editable": [{"id": "a", "text": "小鹿先惊讶，然后"}, {"id": "b", "text": "然后后退。"}]})
        self.assert_guard_error("overlapping_anchors", self.prepare)

    def test_whole_document_scope_is_rejected_even_with_bom(self):
        self.write_json(self.scope, {"editable": [{"id": "all", "text": self.original.decode("utf-8-sig")}]})
        self.assert_guard_error("whole_document_scope", self.prepare)

    def test_combined_spans_cannot_cover_all_nonwhitespace(self):
        for separator in ["\n ", "\u3000", "\u00a0"]:
            with self.subTest(separator=repr(separator)):
                self.baseline.write_bytes(guard.BOM + ("one" + separator + "two\n").encode())
                self.write_json(self.scope, {"editable": [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}]})
                self.assert_guard_error("whole_document_scope", self.prepare)

    def test_wrong_anchor_and_whitespace_anchor_fail_before_session_creation(self):
        for text, code in [("not in source", "anchor_not_found"), (" \n", "empty_anchor")]:
            with self.subTest(text=text):
                self.write_json(self.scope, {"editable": [{"id": "a", "text": text}]})
                self.assert_guard_error(code, self.prepare)
                self.assertFalse(self.session.exists())

    def test_all_four_source_changes_invalidate_session(self):
        self.prepare()
        for source in [self.baseline, self.requirements, self.request, self.scope]:
            with self.subTest(source=source.name):
                original = source.read_bytes()
                source.write_bytes(original + b"\n")
                self.assert_guard_error("source_changed", self.render)
                self.assertFalse(self.output.exists())
                source.write_bytes(original)

    def test_new_user_direction_requires_new_session(self):
        self.prepare()
        self.request.write_bytes("现在明确重写整个结尾。".encode())
        self.assert_guard_error("source_changed", self.render)
        self.assertEqual(self.baseline.read_bytes(), self.original)

    def test_tampered_snapshot_packet_and_manifest_are_rejected(self):
        self.prepare()
        for name in [*guard.SNAPSHOTS.values(), "packet.md", "manifest.json", "manifest.sha256"]:
            with self.subTest(name=name):
                target = self.session / name
                original = target.read_bytes()
                target.write_bytes(original + b"\n")
                self.assert_guard_error("snapshot_changed", self.render)
                self.assertFalse(self.output.exists())
                target.write_bytes(original)

    def test_source_description_is_not_opened_or_executed(self):
        self.write_json(self.requirements, {"requirements": [{"id": "R1", "text": "保留结尾", "source": "../missing-private-file; $(do-not-run)"}]})
        self.prepare()
        self.render()
        self.assertEqual(self.check()["status"], "scope_preserved")

    def test_requirements_need_unique_ids_and_source(self):
        invalid = [
            {"requirements": [{"id": "R1", "text": "x", "source": ""}]},
            {"requirements": [{"id": "R1", "text": "x", "source": "u"}, {"id": "R1", "text": "y", "source": "v"}]},
        ]
        for value, code in zip(invalid, ["invalid_requirement", "invalid_id"]):
            with self.subTest(code=code):
                self.write_json(self.requirements, value)
                self.assert_guard_error(code, self.prepare)

    def test_replacement_ids_unknown_missing_or_duplicate_fail(self):
        self.prepare()
        cases = [
            ([{"id": "unknown", "text": "a"}], "replacement_ids_mismatch"),
            ([], "invalid_schema"),
            ([{"id": "reaction", "text": "a"}, {"id": "reaction", "text": "b"}], "invalid_id"),
            ([{"id": "reaction", "text": "a"}, {"id": "extra", "text": "b"}], "replacement_ids_mismatch"),
        ]
        for replacements, code in cases:
            with self.subTest(code=code, replacements=replacements):
                self.write_json(self.edits, {"replacements": replacements})
                self.assert_guard_error(code, self.render)
                self.assertFalse(self.output.exists())

    def test_duplicate_json_keys_are_rejected(self):
        self.prepare()
        self.edits.write_bytes(b'{"replacements": [], "replacements": []}')
        self.assert_guard_error("duplicate_json_key", self.render)

    def test_existing_session_is_never_overwritten(self):
        self.prepare()
        manifest = (self.session / "manifest.json").read_bytes()
        self.assert_guard_error("session_exists", self.prepare)
        self.assertEqual((self.session / "manifest.json").read_bytes(), manifest)

    def test_existing_output_and_inputs_cannot_be_overwritten(self):
        self.prepare()
        self.output.write_bytes(b"user draft")
        self.assert_guard_error("output_exists", self.render)
        self.assertEqual(self.output.read_bytes(), b"user draft")
        for path in [self.baseline, self.requirements, self.request, self.scope, self.edits, self.session / "new.md"]:
            with self.subTest(path=path.name):
                self.assert_guard_error("protected_output_path", lambda: self.render(path))
        self.assertEqual(self.baseline.read_bytes(), self.original)

    def test_parent_alias_to_input_is_rejected(self):
        self.prepare()
        self.assert_guard_error("protected_output_path", lambda: self.render(self.root / "not-created" / ".." / self.baseline.name))

    def test_partial_write_failure_does_not_leave_a_candidate(self):
        self.prepare()
        original_open = Path.open

        class FailedWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def fileno(self):
                return self.stream.fileno()

            def write(self, data):
                self.stream.write(data[:10])
                raise OSError("simulated full disk")

        def failing_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return FailedWriter(stream) if path == self.output else stream

        with mock.patch.object(Path, "open", failing_open):
            self.assert_guard_error("write_failed", self.render)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.baseline.read_bytes(), self.original)

    def test_symlink_output_is_not_followed_or_replaced(self):
        self.prepare()
        link = self.root / "link.md"
        absent = self.root / "absent.md"
        try:
            link.symlink_to(absent)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable on this host")
        self.assert_guard_error("output_exists", lambda: self.render(link))
        self.assertTrue(link.is_symlink())
        self.assertFalse(absent.exists())

    def test_candidate_drift_is_detected_including_newlines(self):
        self.prepare()
        self.render()
        original = self.output.read_bytes()
        for changed in [original.replace("结尾不变".encode(), "随便改了结尾".encode()), original.replace(b"\r\n", b"\n")]:
            with self.subTest(changed=guard.digest(changed)):
                self.output.write_bytes(changed)
                report = self.check()
                self.assertEqual(report["status"], "candidate_mismatch")
                self.assertIn("first_byte_offset", report["difference"])
                self.assertEqual(report["semantic_check"], "not_evaluated")

    def test_semantic_damage_inside_allowed_span_is_not_falsely_graded(self):
        self.write_json(self.edits, {"replacements": [{"id": "reaction", "text": "小鹿平静地站着。"}]})
        self.prepare()
        self.render()
        report = self.check()
        self.assertEqual(report["status"], "scope_preserved")
        self.assertEqual(report["semantic_check"], "not_evaluated")
        # A separate semantic review must catch lost emotion; byte preservation cannot.

    def test_cli_exit_codes_and_structured_output(self):
        self.prepare()
        self.render()
        command = [sys.executable, str(SCRIPT), "check", "--session", str(self.session), "--edits", str(self.edits), "--candidate", str(self.output)]
        completed = subprocess.run(command, capture_output=True)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout)["status"], "scope_preserved")
        self.output.write_bytes(self.output.read_bytes() + b"changed")
        completed = subprocess.run(command, capture_output=True)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(json.loads(completed.stdout)["status"], "candidate_mismatch")
        self.request.write_bytes(b"new instructions")
        completed = subprocess.run(command, capture_output=True)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(json.loads(completed.stdout)["code"], "source_changed")

    def test_cli_argument_errors_are_structured(self):
        for arguments in [[], ["render"], ["unknown-command"]]:
            with self.subTest(arguments=arguments):
                completed = subprocess.run([sys.executable, str(SCRIPT), *arguments], capture_output=True)
                self.assertEqual(completed.returncode, 2)
                result = json.loads(completed.stdout)
                self.assertEqual(result["code"], "invalid_arguments")
                self.assertEqual(result["semantic_check"], "not_evaluated")


if __name__ == "__main__":
    unittest.main(verbosity=2)
