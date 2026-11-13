#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for framescope. Run with: python -m unittest discover -s tests -v"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import framescope as fs  # noqa: E402


class TempTreeCase(unittest.TestCase):
    """A source tree made of plain files, gone after each test."""

    def setUp(self):
        self.home = os.environ.get("HOME") or tempfile.gettempdir()
        self.tmp = tempfile.mkdtemp(prefix="framescope-test-", dir=self.home)
        self.addCleanup(self._remove, self.tmp)

    @staticmethod
    def _remove(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def write(self, rel, text, encoding="utf-8"):
        full = os.path.join(self.tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(text.encode(encoding) if isinstance(text, str) else text)
        return full

    def index(self):
        return fs.build_index(self.tmp)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

class ParseTests(unittest.TestCase):

    def test_single_frame_with_function(self):
        text = ('Traceback (most recent call last):\n'
                '  File "app.py", line 7, in main\n'
                '    crash()\n'
                'RuntimeError: boom\n')
        stacks = fs.parse_text(text)
        self.assertEqual(len(stacks), 1)
        self.assertEqual(len(stacks[0]["frames"]), 1)
        f = stacks[0]["frames"][0]
        self.assertEqual(f["file"], "app.py")
        self.assertEqual(f["line"], 7)
        self.assertEqual(f["function"], "main")

    def test_frame_without_function(self):
        text = '  File "<snippet>", line 1\n    x = (1 +\n        ^\n'
        f = fs.parse_text(text)[0]["frames"][0]
        self.assertEqual(f["file"], "<snippet>")
        self.assertEqual(f["line"], 1)
        self.assertEqual(f["function"], "")

    def test_echo_captured(self):
        text = ('  File "app.py", line 3, in f\n'
                '    return 1 / 0\n'
                'ZeroDivisionError: division by zero\n')
        f = fs.parse_text(text)[0]["frames"][0]
        self.assertEqual(f["echo"], "    return 1 / 0")

    def test_caret_captured(self):
        text = ('  File "app.py", line 3, in f\n'
                '    return 1 / 0\n'
                '           ~^~\n'
                'ZeroDivisionError: division by zero\n')
        f = fs.parse_text(text)[0]["frames"][0]
        self.assertEqual(f["caret"], "           ~^~")

    def test_pure_caret_line_with_no_tilde(self):
        text = ('  File "x.py", line 1\n'
                '    x = (1 +\n'
                '        ^\n')
        f = fs.parse_text(text)[0]["frames"][0]
        self.assertIsNotNone(f["caret"])

    def test_no_echo_when_exception_line_follows(self):
        text = ('  File "app.py", line 3, in f\n'
                'RuntimeError: boom\n')
        f = fs.parse_text(text)[0]["frames"][0]
        self.assertIsNone(f["echo"])

    def test_no_echo_when_next_frame_follows(self):
        text = ('  File "a.py", line 1, in f\n'
                '  File "b.py", line 2, in g\n')
        frames = fs.parse_text(text)[0]["frames"]
        self.assertEqual(len(frames), 2)
        self.assertIsNone(frames[0]["echo"])

    def test_multiple_frames_keep_order(self):
        text = ('Traceback (most recent call last):\n'
                '  File "a.py", line 10, in outer\n'
                '  File "b.py", line 20, in inner\n'
                '  File "c.py", line 30, in deepest\n')
        frames = fs.parse_text(text)[0]["frames"]
        self.assertEqual([f["line"] for f in frames], [10, 20, 30])
        self.assertEqual([f["function"] for f in frames],
                         ["outer", "inner", "deepest"])

    def test_chained_exceptions_make_two_stacks(self):
        text = ('Traceback (most recent call last):\n'
                '  File "a.py", line 2, in <module>\n'
                'ValueError: inner\n'
                '\n'
                'The above exception was the direct cause of the following exception:\n'
                '\n'
                'Traceback (most recent call last):\n'
                '  File "b.py", line 5, in <module>\n'
                'RuntimeError: outer\n')
        stacks = fs.parse_text(text)
        self.assertEqual(len(stacks), 2)
        self.assertEqual(stacks[0]["index"], 1)
        self.assertEqual(stacks[1]["index"], 2)

    def test_chain_note_recorded(self):
        text = ('Traceback (most recent call last):\n'
                '  File "a.py", line 1, in <module>\n'
                'ValueError: x\n'
                'During handling of the above exception, another exception occurred:\n'
                'Traceback (most recent call last):\n'
                '  File "b.py", line 2, in <module>\n'
                'TypeError: y\n')
        stacks = fs.parse_text(text)
        self.assertEqual(len(stacks), 2)
        self.assertTrue(any("During handling" in n for n in stacks[1]["notes"]))

    def test_repeat_note_recorded(self):
        text = ('Traceback (most recent call last):\n'
                '  File "a.py", line 1, in f\n'
                '  [Previous line repeated 995 more times]\n'
                'RecursionError: maximum recursion depth exceeded\n')
        stacks = fs.parse_text(text)
        self.assertIn("previous line repeated 995 more times",
                      stacks[0]["notes"][0])

    def test_virtual_filename_parsed_as_frame(self):
        text = ('  File "<frozen importlib._bootstrap>", line 1387, in _find_and_load\n')
        f = fs.parse_text(text)[0]["frames"][0]
        self.assertEqual(f["file"], "<frozen importlib._bootstrap>")
        self.assertEqual(f["line"], 1387)

    def test_crlf_input(self):
        text = ('Traceback (most recent call last):\r\n'
                '  File "app.py", line 4, in f\r\n'
                '    broken()\r\n'
                'ValueError: x\r\n')
        f = fs.parse_text(text)[0]["frames"][0]
        self.assertEqual(f["line"], 4)
        self.assertEqual(f["echo"].strip(), "broken()")

    def test_windows_path_kept_verbatim(self):
        text = r'  File "C:\work\proj\src\app.py", line 12, in run' + "\n"
        f = fs.parse_text(text)[0]["frames"][0]
        self.assertEqual(f["file"], r"C:\work\proj\src\app.py")

    def test_log_prefix_before_frame(self):
        text = ('2026-11-13 09:00:00   File "app.py", line 8, in f\n'
                '2026-11-13 09:00:00 ValueError: x\n')
        f = fs.parse_text(text)[0]["frames"][0]
        self.assertEqual(f["line"], 8)

    def test_echo_line_containing_the_word_file_is_kept(self):
        text = ('  File "app.py", line 2, in f\n'
                '    print(\'File "x.py", line 9\')\n'
                'ValueError: x\n')
        f = fs.parse_text(text)[0]["frames"][0]
        self.assertIn('File "x.py"', f["echo"])

    def test_empty_input_has_no_stacks(self):
        self.assertEqual(fs.parse_text(""), [])
        self.assertEqual(fs.parse_text("just some log text\n"), [])

    def test_stack_created_without_header(self):
        text = '  File "app.py", line 1, in f\n'
        stacks = fs.parse_text(text)
        self.assertEqual(len(stacks), 1)
        self.assertEqual(stacks[0]["index"], 1)

    def test_exception_like_echo_is_rejected(self):
        text = ('  File "app.py", line 1, in f\n'
                '    ValueError: something\n')
        f = fs.parse_text(text)[0]["frames"][0]
        self.assertIsNone(f["echo"])

    def test_decode_strips_bom(self):
        self.assertEqual(fs._decode(b"\xef\xbb\xbfhello\n"), "hello\n")

    def test_decode_replaces_bad_bytes(self):
        self.assertNotIn("\ufffd", fs._decode(b"ok\n"))
        self.assertIn("\ufffd", fs._decode(b"a\xffb"))


# --------------------------------------------------------------------------- #
# path normalisation and index
# --------------------------------------------------------------------------- #

class PartsTests(unittest.TestCase):

    def test_windows_separators(self):
        self.assertEqual(fs._parts(r"C:\work\proj\src\app.py"),
                         ["work", "proj", "src", "app.py"])

    def test_posix_separators(self):
        self.assertEqual(fs._parts("/home/runner/work/src/app.py"),
                         ["home", "runner", "work", "src", "app.py"])

    def test_mixed_separators(self):
        self.assertEqual(fs._parts(r"/home/runner\proj/src\app.py"),
                         ["home", "runner", "proj", "src", "app.py"])

    def test_dot_segments_dropped(self):
        self.assertEqual(fs._parts("./src/./app.py"), ["src", "app.py"])

    def test_single_component(self):
        self.assertEqual(fs._parts("app.py"), ["app.py"])

    def test_empty_path(self):
        self.assertEqual(fs._parts(""), [])


class IndexTests(TempTreeCase):

    def test_index_registers_all_suffixes(self):
        self.write("src/pkg/app.py", "x = 1\n")
        idx = self.index()
        self.assertEqual(idx["by_suffix"]["app.py"], ["src/pkg/app.py"])
        self.assertEqual(idx["by_suffix"]["pkg/app.py"], ["src/pkg/app.py"])
        self.assertEqual(idx["by_suffix"]["src/pkg/app.py"], ["src/pkg/app.py"])

    def test_index_is_case_insensitive_on_lookup(self):
        self.write("Src/App.py", "x = 1\n")
        idx = self.index()
        self.assertEqual(idx["by_suffix"]["src/app.py"], ["Src/App.py"])

    def test_index_counts_files(self):
        self.write("a.py", "a\n")
        self.write("b/c.py", "c\n")
        idx = self.index()
        self.assertEqual(len(idx["files"]), 2)
        self.assertFalse(idx["truncated"])

    def test_skip_dirs_not_indexed(self):
        self.write("__pycache__/hidden.py", "x\n")
        self.write(".git/inside.py", "x\n")
        self.write("real.py", "x\n")
        idx = self.index()
        self.assertEqual(idx["files"], ["real.py"])

    def test_same_basename_gives_two_hits(self):
        self.write("src/app.py", "a\n")
        self.write("tests/app.py", "b\n")
        idx = self.index()
        self.assertEqual(len(idx["by_suffix"]["app.py"]), 2)
        self.assertEqual(len(idx["by_suffix"]["src/app.py"]), 1)

    def test_cap_truncates(self):
        self.write("a.py", "a\n")
        self.write("b.py", "b\n")
        idx = fs.build_index(self.tmp, cap=1)
        self.assertTrue(idx["truncated"])

    def test_non_directory_root_raises(self):
        bad = os.path.join(self.tmp, "missing")
        with self.assertRaises(ValueError):
            fs.build_index(bad)

    def test_root_name_prefixed_suffix_registered(self):
        self.write("src/app.py", "a\n")
        idx = self.index()
        name = os.path.basename(os.path.abspath(self.tmp)).lower()
        self.assertEqual(idx["by_suffix"][name + "/src/app.py"], ["src/app.py"])

    def test_root_absolute(self):
        self.write("a.py", "a\n")
        idx = self.index()
        self.assertEqual(idx["root"], os.path.abspath(self.tmp))


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #

class ResolveTests(TempTreeCase):

    def frame(self, path, line=1):
        return {"file": path, "line": line, "function": "", "echo": None,
                "caret": None}

    def test_exact_absolute_path_under_root(self):
        full = self.write("src/app.py", "x = 1\n")
        res = fs.resolve(self.frame(full), self.index())
        self.assertEqual(res["status"], fs.STATUS_OK)
        self.assertEqual(res["resolved"], "src/app.py")
        self.assertEqual(res["via"], "exact path under root")

    def test_exact_relative_path(self):
        self.write("src/app.py", "x = 1\n")
        res = fs.resolve(self.frame("src/app.py"), self.index())
        self.assertEqual(res["status"], fs.STATUS_OK)
        self.assertEqual(res["resolved"], "src/app.py")

    def test_longest_suffix_wins(self):
        self.write("src/app.py", "a\n")
        self.write("other/app.py", "b\n")
        res = fs.resolve(self.frame("/home/runner/work/proj/src/app.py"),
                         self.index())
        self.assertEqual(res["status"], fs.STATUS_OK)
        self.assertEqual(res["resolved"], "src/app.py")
        # /home/runner/work/proj/src/app.py -> src/app.py drops 4 parts
        self.assertEqual(res["dropped"], 4)

    def test_shortest_suffix_used_when_needed(self):
        self.write("app.py", "a\n")
        res = fs.resolve(self.frame("/home/runner/work/proj/app.py"), self.index())
        self.assertEqual(res["status"], fs.STATUS_OK)
        self.assertEqual(res["resolved"], "app.py")
        self.assertEqual(res["dropped"], 4)

    def test_candidate_ladder_is_longest_first(self):
        self.write("app.py", "a\n")
        res = fs.resolve(self.frame("/a/b/app.py"), self.index())
        self.assertEqual([c["path"] for c in res["candidates"]],
                         ["a/b/app.py", "b/app.py", "app.py"])

    def test_ambiguous_suffix(self):
        self.write("src/app.py", "a\n")
        self.write("tests/app.py", "b\n")
        res = fs.resolve(self.frame("/home/runner/work/app.py"), self.index())
        self.assertEqual(res["status"], fs.STATUS_AMBIGUOUS)
        self.assertIsNone(res["resolved"])
        self.assertEqual(len(res["hits"]), 2)

    def test_missing_file(self):
        self.write("other.py", "a\n")
        res = fs.resolve(self.frame("/build/src/gone.py"), self.index())
        self.assertEqual(res["status"], fs.STATUS_MISSING)
        self.assertEqual(res["hits"], [])

    def test_virtual_filename_skipped(self):
        res = fs.resolve(self.frame("<frozen importlib._bootstrap>"), self.index())
        self.assertEqual(res["status"], fs.STATUS_SKIP)
        self.assertEqual(res["candidates"], [])

    def test_virtual_filename_string(self):
        res = fs.resolve(self.frame("<string>"), self.index())
        self.assertEqual(res["status"], fs.STATUS_SKIP)

    def test_log_path_cannot_escape_root(self):
        outside = os.path.join(self.home, "framescope-outside-file.py")
        self.write("inside.py", "x\n")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("secret\n")
        try:
            res = fs.resolve(self.frame(outside), self.index())
            self.assertNotEqual(res["via"], "exact path under root")
        finally:
            os.remove(outside)

    def test_windows_drive_stripped_for_suffix(self):
        self.write("src/app.py", "a\n")
        res = fs.resolve(self.frame(r"C:\agent\_work\1\s\src\app.py"),
                         self.index())
        self.assertEqual(res["status"], fs.STATUS_OK)
        self.assertEqual(res["resolved"], "src/app.py")

    def test_case_insensitive_suffix_lookup(self):
        self.write("Src/App.py", "a\n")
        res = fs.resolve(self.frame("/build/sources/src/app.py"), self.index())
        self.assertEqual(res["status"], fs.STATUS_OK)
        self.assertEqual(res["resolved"], "Src/App.py")

    def test_root_name_disambiguates_basename_collision(self):
        self.write("glob.py", "a\n")
        self.write("zipfile/_path/glob.py", "b\n")
        name = os.path.basename(os.path.abspath(self.tmp)).lower()
        path = "/home/runner/work/cpython/%s/glob.py" % name
        res = fs.resolve(self.frame(path), self.index())
        self.assertEqual(res["status"], fs.STATUS_OK)
        self.assertEqual(res["resolved"], "glob.py")

    def test_root_name_prefix_still_finds_nested_file(self):
        self.write("glob.py", "a\n")
        self.write("zipfile/_path/glob.py", "b\n")
        name = os.path.basename(os.path.abspath(self.tmp)).lower()
        path = "/home/runner/work/cpython/%s/zipfile/_path/glob.py" % name
        res = fs.resolve(self.frame(path), self.index())
        self.assertEqual(res["status"], fs.STATUS_OK)
        self.assertEqual(res["resolved"], "zipfile/_path/glob.py")

    def test_no_parts_gives_missing(self):
        self.write("a.py", "a\n")
        res = fs.resolve(self.frame("/"), self.index())
        self.assertEqual(res["status"], fs.STATUS_MISSING)


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #

class AnalyzeTests(TempTreeCase):

    def run_frames(self, source, frames, context=1, root=None):
        self.write("app.py", source)
        stacks = [{"index": 1, "notes": [], "frames": frames}]
        idx = fs.build_index(root or self.tmp)
        return fs.analyze(stacks, idx, context)[0]["frames"][0]

    @staticmethod
    def frame(line=2, echo="    x = value / 0", caret=None):
        return {"file": "/build/proj/app.py", "line": line, "function": "f",
                "echo": echo, "caret": caret}

    def test_matching_echo_is_ok(self):
        f = self.run_frames("a = 1\nx = value / 0\nb = 2\n",
                            [self.frame(2)])
        self.assertEqual(f["status"], fs.STATUS_OK)
        self.assertEqual(f["resolved"], "app.py")
        self.assertEqual(f["local"], "x = value / 0")

    def test_no_echo_status(self):
        f = self.run_frames("a = 1\nx = value / 0\nb = 2\n",
                            [self.frame(2, echo=None)])
        self.assertEqual(f["status"], fs.STATUS_NO_ECHO)

    def test_stale_with_relocation(self):
        f = self.run_frames("a = 1\npatched = True\nx = value / 0\n",
                            [self.frame(2)])
        self.assertEqual(f["status"], fs.STATUS_STALE)
        self.assertEqual(f["relocations"], [3])
        self.assertIn("line 3", f["reason"])

    def test_stale_without_relocation(self):
        f = self.run_frames("a = 1\ncompletely different\nb = 2\n",
                            [self.frame(2)])
        self.assertEqual(f["status"], fs.STATUS_STALE)
        self.assertEqual(f["relocations"], [])
        self.assertIn("not in this file", f["reason"])

    def test_line_past_end_is_bad_line(self):
        f = self.run_frames("a = 1\nb = 2\n", [self.frame(99)])
        self.assertEqual(f["status"], fs.STATUS_BAD_LINE)
        self.assertIn("2 lines", f["reason"])

    def test_line_zero_is_bad_line(self):
        f = self.run_frames("a = 1\n", [self.frame(0)])
        self.assertEqual(f["status"], fs.STATUS_BAD_LINE)

    def test_line_one_with_no_echo_is_no_echo(self):
        f = self.run_frames("a = 1\n", [self.frame(1, echo=None)])
        self.assertEqual(f["status"], fs.STATUS_NO_ECHO)
        self.assertEqual([c["n"] for c in f["context"]], [1])

    def test_missing_file_status(self):
        stacks = [{"index": 1, "notes": [], "frames": [self.frame(1, echo=None)]}]
        f = fs.analyze(stacks, self.index(), 1)[0]["frames"][0]
        self.assertEqual(f["status"], fs.STATUS_MISSING)
        self.assertEqual(f["context"], [])

    def test_virtual_frame_status(self):
        fr = self.frame(1, echo=None)
        fr["file"] = "<string>"
        f = self.run_frames("a = 1\n", [fr])
        self.assertEqual(f["status"], fs.STATUS_SKIP)
        self.assertEqual(f["context"], [])

    def test_context_window_bounds(self):
        f = self.run_frames("l1\nl2\nl3\nl4\nl5\n",
                            [self.frame(3, echo="    l3")], context=1)
        self.assertEqual([c["n"] for c in f["context"]], [2, 3, 4])
        self.assertEqual([c["target"] for c in f["context"]],
                         [False, True, False])

    def test_context_zero_is_single_line(self):
        f = self.run_frames("l1\nl2\nl3\n",
                            [self.frame(2, echo="    l2")], context=0)
        self.assertEqual(f["context"],
                         [{"n": 2, "text": "l2", "target": True}])

    def test_context_clipped_at_file_start(self):
        f = self.run_frames("l1\nl2\nl3\n",
                            [self.frame(1, echo="    l1")], context=2)
        self.assertEqual([c["n"] for c in f["context"]], [1, 2, 3])

    def test_context_clipped_at_file_end(self):
        f = self.run_frames("l1\nl2\nl3\n",
                            [self.frame(3, echo="    l3")], context=2)
        self.assertEqual([c["n"] for c in f["context"]], [1, 2, 3])

    def test_caret_shown_only_when_line_matches(self):
        ok = self.run_frames("a = 1\nx = value / 0\nb = 2\n",
                             [self.frame(2, caret="           ~^~")])
        self.assertIsNotNone(ok["caret_text"])
        stale = self.run_frames("a = 1\npatched\nx = value / 0\n",
                                [self.frame(2, caret="           ~^~")])
        self.assertIsNone(stale["caret_text"])
        self.assertIsNotNone(stale["caret_body"])

    def test_caret_preserves_source_indent(self):
        f = self.run_frames("def f():\n    return 1 / 0\n",
                            [self.frame(2, echo="    return 1 / 0",
                                        caret="           ~^~")])
        self.assertTrue(f["caret_text"].startswith("    "))

    def test_bom_source_is_read(self):
        path = self.write("app.py", b"\xef\xbb\xbfx = value / 0\nb = 2\n")
        self.assertEqual(fs._read_lines(path)[0], "x = value / 0")

    def test_non_utf8_source_survives(self):
        path = self.write("app.py", b"caf\xe9 = 1\n")
        self.assertEqual(len(fs._read_lines(path)), 1)

    def test_crlf_source_line_matches_echo(self):
        f = self.run_frames("a = 1\r\nx = value / 0\r\nb = 2\r\n",
                            [self.frame(2)])
        self.assertEqual(f["status"], fs.STATUS_OK)

    def test_unicode_source_matches_echo(self):
        f = self.run_frames("a = 1\n消息 = '你好'\nb = 2\n",
                            [self.frame(2, echo="    消息 = '你好'")])
        self.assertEqual(f["status"], fs.STATUS_OK)

    def test_echo_compared_after_stripping_indent(self):
        f = self.run_frames("def f():\n    return 1\n",
                            [self.frame(2, echo="        return 1")])
        self.assertEqual(f["status"], fs.STATUS_OK)

    def test_empty_file_bad_line(self):
        f = self.run_frames("", [self.frame(1, echo=None)])
        self.assertEqual(f["status"], fs.STATUS_BAD_LINE)

    def test_multiple_frames_analysed(self):
        self.write("app.py", "a = 1\nb = 2\n")
        stacks = [{"index": 1, "notes": [], "frames": [
            self.frame(1, echo="    a = 1"),
            self.frame(2, echo="    b = 2"),
        ]}]
        frames = fs.analyze(stacks, self.index(), 0)[0]["frames"]
        self.assertEqual([f["status"] for f in frames],
                         [fs.STATUS_OK, fs.STATUS_OK])

    def test_ambiguous_frame_stays_unresolved(self):
        self.write("src/app.py", "a\n")
        self.write("tests/app.py", "a\n")
        stacks = [{"index": 1, "notes": [], "frames": [self.frame(1, echo=None)]}]
        f = fs.analyze(stacks, self.index(), 1)[0]["frames"][0]
        self.assertEqual(f["status"], fs.STATUS_AMBIGUOUS)


class SummariseTests(TempTreeCase):

    def test_counts_every_status(self):
        self.write("app.py", "a = 1\n")
        stacks = [{"index": 1, "notes": [], "frames": [
            {"file": "/b/app.py", "line": 1, "function": "", "echo": "    a = 1",
             "caret": None},
            {"file": "<string>", "line": 1, "function": "", "echo": None,
             "caret": None},
            {"file": "/b/gone.py", "line": 1, "function": "", "echo": None,
             "caret": None},
        ]}]
        fs.analyze(stacks, self.index(), 0)
        s = fs.summarise(stacks)
        self.assertEqual(s["frames"], 3)
        self.assertEqual(s["stacks"], 1)
        self.assertEqual(s["by_status"][fs.STATUS_OK], 1)
        self.assertEqual(s["by_status"][fs.STATUS_SKIP], 1)
        self.assertEqual(s["by_status"][fs.STATUS_MISSING], 1)

    def test_all_status_keys_present(self):
        s = fs.summarise([])
        self.assertEqual(set(s["by_status"]), set(fs.ALL_STATUSES))
        self.assertEqual(s["frames"], 0)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

class RenderTests(TempTreeCase):

    def prepared(self, source, line=2, echo="    x = value / 0"):
        self.write("app.py", source)
        stacks = [{"index": 1, "notes": [], "frames": [
            {"file": "/build/proj/app.py", "line": line, "function": "f",
             "echo": echo, "caret": None}]}]
        idx = self.index()
        fs.analyze(stacks, idx, 1)
        return stacks, idx

    def test_human_contains_frame_and_source(self):
        stacks, idx = self.prepared("a = 1\nx = value / 0\nb = 2\n")
        out = fs.format_human(stacks, idx, 1)
        self.assertIn("[1] /build/proj/app.py:2 in f", out)
        self.assertIn("> ", out)
        self.assertIn("x = value / 0", out)
        self.assertIn("app.py", out)

    def test_human_summary_counts(self):
        stacks, idx = self.prepared("a = 1\nx = value / 0\nb = 2\n")
        out = fs.format_human(stacks, idx, 1)
        self.assertIn("1 ok", out)
        self.assertIn("summary", out)

    def test_human_explain_shows_ladder_and_check(self):
        stacks, idx = self.prepared("a = 1\nx = value / 0\nb = 2\n")
        out = fs.format_human(stacks, idx, 1, explain=True)
        self.assertIn("candidates (longest suffix first)", out)
        self.assertIn("echo    x = value / 0", out)
        self.assertIn("local   x = value / 0", out)

    def test_human_without_explain_has_no_ladder(self):
        stacks, idx = self.prepared("a = 1\nx = value / 0\nb = 2\n")
        out = fs.format_human(stacks, idx, 1)
        self.assertNotIn("candidates (longest suffix first)", out)

    def test_human_reports_unresolved_reason(self):
        stacks, idx = self.prepared("a = 1\n", line=99, echo=None)
        out = fs.format_human(stacks, idx, 1)
        self.assertIn("bad-line", out)
        self.assertIn("outside the local file", out)

    def test_human_note_about_cap(self):
        stacks, idx = self.prepared("a = 1\nx = value / 0\nb = 2\n")
        idx["truncated"] = True
        out = fs.format_human(stacks, idx, 1)
        self.assertIn("index capped", out)

    def test_json_is_valid(self):
        stacks, idx = self.prepared("a = 1\nx = value / 0\nb = 2\n")
        payload = json.loads(fs.format_json(stacks, idx, 1))
        self.assertEqual(payload["tool"], "framescope")
        self.assertEqual(payload["context"], 1)

    def test_json_frame_fields(self):
        stacks, idx = self.prepared("a = 1\nx = value / 0\nb = 2\n")
        f = json.loads(fs.format_json(stacks, idx, 1))["stacks"][0]["frames"][0]
        self.assertEqual(f["line"], 2)
        self.assertEqual(f["status"], fs.STATUS_OK)
        self.assertEqual(f["resolved"], "app.py")
        self.assertEqual(f["echo"], "x = value / 0")
        self.assertEqual(len(f["context"]), 3)
        self.assertEqual(f["candidates"][-1]["hits"], 1)

    def test_json_summary(self):
        stacks, idx = self.prepared("a = 1\nx = value / 0\nb = 2\n")
        p = json.loads(fs.format_json(stacks, idx, 1))
        self.assertEqual(p["summary"]["frames"], 1)
        self.assertEqual(p["summary"]["by_status"][fs.STATUS_OK], 1)

    def test_json_unicode_not_escaped(self):
        stacks, idx = self.prepared("a = 1\n消息 = '你好'\nb = 2\n",
                                    echo="    消息 = '你好'")
        raw = fs.format_json(stacks, idx, 0)
        self.assertIn("你好", raw)

    def test_human_multiple_stacks_labelled(self):
        self.write("app.py", "a = 1\n")
        stacks = [
            {"index": 1, "notes": [], "frames": [
                {"file": "/b/app.py", "line": 1, "function": "f",
                 "echo": "    a = 1", "caret": None}]},
            {"index": 2, "notes": ["During handling"], "frames": [
                {"file": "/b/app.py", "line": 1, "function": "g",
                 "echo": "    a = 1", "caret": None}]},
        ]
        idx = self.index()
        fs.analyze(stacks, idx, 0)
        out = fs.format_human(stacks, idx, 0)
        self.assertIn("stack 1", out)
        self.assertIn("stack 2", out)
        self.assertIn("note: During handling", out)


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #

class CliTests(TempTreeCase):

    def invoke(self, argv, stdin_bytes=None, root=None):
        full = ["--root", root or self.tmp] + argv
        buf = io.StringIO()
        err = io.StringIO()
        stdin = mock.Mock()
        stdin.buffer.read.return_value = stdin_bytes or b""
        with mock.patch.object(sys, "stdin", stdin), \
             contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = fs.main(full)
        return code, buf.getvalue(), err.getvalue()

    @staticmethod
    def log(path="app.py", line=2, echo="    x = value / 0"):
        out = ('Traceback (most recent call last):\n'
               '  File "%s", line %d, in f\n' % (path, line))
        if echo:
            out += echo + "\n"
        out += "ValueError: boom\n"
        return out.encode("utf-8")

    def logfile(self, name="traceback.txt", data=None):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as handle:
            handle.write(data if data is not None else self.log())
        return path

    def test_stdin_ok_exit_zero(self):
        self.write("app.py", "a = 1\nx = value / 0\nb = 2\n")
        code, out, err = self.invoke(["-"], self.log())
        self.assertEqual(code, 0)
        self.assertIn("1 ok", out)
        self.assertEqual(err, "")

    def test_file_argument(self):
        self.write("app.py", "a = 1\nx = value / 0\nb = 2\n")
        path = self.logfile()
        code, out, _ = self.invoke([path])
        self.assertEqual(code, 0)
        self.assertIn("app.py:2", out)

    def test_stale_exit_one(self):
        self.write("app.py", "a = 1\npatched\nx = value / 0\n")
        code, out, _ = self.invoke(["-"], self.log())
        self.assertEqual(code, 1)
        self.assertIn("1 stale", out)

    def test_missing_exit_one(self):
        self.write("other.py", "a = 1\n")
        code, out, _ = self.invoke(["-"], self.log())
        self.assertEqual(code, 1)
        self.assertIn("1 missing", out)

    def test_virtual_exit_zero(self):
        code, out, _ = self.invoke(["-"], self.log(path="<string>", echo=None))
        self.assertEqual(code, 0)
        self.assertIn("1 skip", out)

    def test_no_frames_exit_two(self):
        code, _, err = self.invoke(["-"], b"nothing here\n")
        self.assertEqual(code, 2)
        self.assertIn("no traceback frames", err)

    def test_bad_root_exit_two(self):
        code, _, err = self.invoke(["-"], self.log(),
                                   root=os.path.join(self.tmp, "nope"))
        self.assertEqual(code, 2)
        self.assertIn("not a directory", err)

    def test_missing_log_file_exit_two(self):
        code, _, err = self.invoke([os.path.join(self.tmp, "absent.txt")])
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_json_flag(self):
        self.write("app.py", "a = 1\nx = value / 0\nb = 2\n")
        code, out, _ = self.invoke(["--json", "-"], self.log())
        self.assertEqual(code, 0)
        p = json.loads(out)
        self.assertEqual(p["summary"]["frames"], 1)

    def test_explain_flag(self):
        self.write("app.py", "a = 1\nx = value / 0\nb = 2\n")
        code, out, _ = self.invoke(["--explain", "-"], self.log())
        self.assertEqual(code, 0)
        self.assertIn("candidates (longest suffix first)", out)

    def test_context_flag(self):
        self.write("app.py", "l1\nl2\nl3\nl4\nl5\n")
        code, out, _ = self.invoke(["-C", "0", "-"],
                                   self.log(line=3, echo="    l3"))
        self.assertEqual(code, 0)
        self.assertNotIn("  2 |", out)

    def test_context_out_of_range(self):
        with self.assertRaises(SystemExit) as ctx:
            self.invoke(["-C", "99", "-"], self.log())
        self.assertEqual(ctx.exception.code, 2)

    def test_negative_context(self):
        with self.assertRaises(SystemExit):
            self.invoke(["-C", "-1", "-"], self.log())

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            self.invoke(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_bom_in_log(self):
        self.write("app.py", "a = 1\nx = value / 0\nb = 2\n")
        code, out, _ = self.invoke(["-"], b"\xef\xbb\xbf" + self.log())
        self.assertEqual(code, 0)

    def test_crlf_log(self):
        self.write("app.py", "a = 1\nx = value / 0\nb = 2\n")
        code, out, _ = self.invoke(["-"], self.log().replace(b"\n", b"\r\n"))
        self.assertEqual(code, 0)
        self.assertIn("1 ok", out)

    def test_chained_log_two_stacks(self):
        self.write("app.py", "a = 1\nx = value / 0\nb = 2\n")
        data = (self.log() + b"\nThe above exception was the direct cause of "
                b"the following exception:\n\n" + self.log())
        code, out, _ = self.invoke(["-"], data)
        self.assertEqual(code, 0)
        self.assertIn("2 stack(s)", out)

    def test_exit_codes_are_ints(self):
        self.write("app.py", "a = 1\nx = value / 0\nb = 2\n")
        code, _, _ = self.invoke(["-"], self.log())
        self.assertIsInstance(code, int)


class EndToEndTests(TempTreeCase):
    """A traceback captured by a real interpreter, read back by the CLI."""

    def test_captured_traceback_resolves(self):
        script = os.path.join(self.tmp, "src")
        os.makedirs(script, exist_ok=True)
        program = os.path.join(script, "app.py")
        with open(program, "w", encoding="utf-8") as handle:
            handle.write("def f():\n    return 1 / 0\n\nf()\n")
        import subprocess
        proc = subprocess.run([sys.executable, program],
                              capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Traceback", proc.stderr)
        log = os.path.join(self.tmp, "tb.txt")
        with open(log, "w", encoding="utf-8") as handle:
            handle.write(proc.stderr)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fs.main(["--root", self.tmp, log])
        self.assertEqual(code, 0)
        self.assertIn("src/app.py", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
