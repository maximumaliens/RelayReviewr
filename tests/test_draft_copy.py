"""Seam 1: the pure assembler that builds a draft from a stored report. Every expected string below is written
out by hand; none is derived through the assembler."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ccrelay.draft_copy import CopyError, assemble  # noqa: E402

REPORT = "alpha\nbeta\ngamma\nbeta\ndelta\n"


class CopyAll(unittest.TestCase):
    def test_whole_report_is_returned_exactly(self):
        reports = {
            "final newline": "one\ntwo\n",
            "no final newline": "one\ntwo",
            "crlf": "one\r\ntwo\r\n",
            "mixed endings": "one\r\ntwo\nthree\r",
            "trailing spaces and tabs": "one  \t\n\ttwo \t \n",
            "indentation": "def f():\n    return 1\n\n        deep\n",
            "non-ascii": "naïve café — 日本語 ✓\nsecond ünïcode\n",
            "several final newlines": "one\n\n\n",
            "odd unicode separators": "a b\x0bc\x0cd\x1ce\n",
            "single line": "only",
        }
        for name, report in reports.items():
            with self.subTest(name):
                text, parts = assemble(report, "all", None)
                self.assertEqual(text, report)
                self.assertEqual(parts, [{"kind": "copied", "start": 0, "end": len(report),
                                          "from_line": 1, "to_line": report.count("\n") + (0 if report.endswith("\n") else 1)}])

    def test_additions_follow_with_one_lf_after_a_final_newline(self):
        text, parts = assemble("one\ntwo\n", "all", "Reviewer notes\nmind this")
        self.assertEqual(text, "one\ntwo\n\nReviewer notes\nmind this")
        self.assertEqual([p["kind"] for p in parts], ["copied", "separator", "additions"])
        self.assertEqual([(p["start"], p["end"]) for p in parts], [(0, 8), (8, 9), (9, 33)])

    def test_additions_follow_with_two_lfs_without_a_final_newline(self):
        text, _ = assemble("one\ntwo", "all", "Notes")
        self.assertEqual(text, "one\ntwo\n\nNotes")

    def test_crlf_source_gets_an_lf_only_separator_and_keeps_its_own_bytes(self):
        text, _ = assemble("one\r\ntwo\r\n", "all", "Notes")
        self.assertEqual(text, "one\r\ntwo\r\n\nNotes")

    def test_several_final_newlines_are_all_kept(self):
        text, _ = assemble("one\n\n\n", "all", "Notes")
        self.assertEqual(text, "one\n\n\n\nNotes")

    def test_additions_are_stored_exactly_as_given(self):
        text, _ = assemble("x\n", "all", "  \n indented  \n\n")
        self.assertEqual(text, "x\n\n  \n indented  \n\n")


class Selections(unittest.TestCase):
    def test_one_selection_is_the_raw_slice(self):
        text, parts = assemble(REPORT, [{"from": "alpha", "to": "gamma"}], None)
        self.assertEqual(text, "alpha\nbeta\ngamma\n")
        self.assertEqual(parts, [{"kind": "copied", "start": 0, "end": 17, "from_line": 1, "to_line": 3}])

    def test_selection_ending_on_the_last_line_keeps_the_final_newline(self):
        text, _ = assemble(REPORT, [{"from": "gamma", "to": "delta"}], None)
        self.assertEqual(text, "gamma\nbeta\ndelta\n")

    def test_selection_ending_on_a_last_line_with_no_newline_has_none(self):
        text, _ = assemble("a\nb\nc", [{"from": "b", "to": "c"}], None)
        self.assertEqual(text, "b\nc")

    def test_matching_ignores_surrounding_whitespace_and_line_ending_but_the_slice_keeps_them(self):
        report = "head\r\n   \tbody text  \r\n   tail \r\nafter\r\n"
        text, _ = assemble(report, [{"from": "  body text", "to": "tail\t"}], None)
        self.assertEqual(text, "   \tbody text  \r\n   tail \r\n")

    def test_same_line_may_start_and_end_a_selection(self):
        text, _ = assemble(REPORT, [{"from": "delta", "to": "delta"}], None)
        self.assertEqual(text, "delta\n")

    def test_two_selections_are_joined_by_a_separator_that_is_not_part_of_either(self):
        text, parts = assemble(REPORT, [{"from": "alpha", "to": "alpha"}, {"from": "delta", "to": "delta"}], None)
        self.assertEqual(text, "alpha\n\ndelta\n")
        self.assertEqual([(p["kind"], p["start"], p["end"]) for p in parts],
                         [("copied", 0, 6), ("separator", 6, 7), ("copied", 7, 13)])

    def test_selection_without_a_final_newline_gets_two_lfs_before_the_next_part(self):
        text, _ = assemble("a\nb\nc", [{"from": "a", "to": "a"}, {"from": "c", "to": "c"}], "N")
        self.assertEqual(text, "a\n\nc\n\nN")

    def test_adjacent_selections_still_get_a_separator(self):
        text, _ = assemble(REPORT, [{"from": "alpha", "to": "alpha"}, {"from": "gamma", "to": "gamma"}], None)
        self.assertEqual(text, "alpha\n\ngamma\n")
        text, _ = assemble("a\nb\nc\n", [{"from": "a", "to": "a"}, {"from": "b", "to": "b"}], None)
        self.assertEqual(text, "a\n\nb\n")

    def test_selections_with_additions_and_crlf(self):
        text, _ = assemble("a\r\nb\r\nc\r\n", [{"from": "a", "to": "a"}, {"from": "c", "to": "c"}], "Amend")
        self.assertEqual(text, "a\r\n\nc\r\n\nAmend")

    def test_non_ascii_offsets_are_character_offsets(self):
        text, parts = assemble("é日\nz\n", [{"from": "é日", "to": "é日"}], "N")
        self.assertEqual(text, "é日\n\nN")
        self.assertEqual([(p["start"], p["end"]) for p in parts], [(0, 3), (3, 4), (4, 5)])

    def test_odd_unicode_separators_do_not_split_lines(self):
        report = "a b\x0bc\nnext\n"
        text, _ = assemble(report, [{"from": "a b\x0bc", "to": "a b\x0bc"}], None)
        self.assertEqual(text, "a b\x0bc\n")


class Ambiguity(unittest.TestCase):
    def test_repeated_to_boundary_is_rejected_with_the_matching_line_numbers(self):
        with self.assertRaises(CopyError) as cm:
            assemble(REPORT, [{"from": "alpha", "to": "beta"}], None)
        msg = str(cm.exception)
        self.assertIn("line 2: beta", msg)
        self.assertIn("line 4: beta", msg)
        self.assertIn("alpha", msg)  # the line before the first match
        self.assertIn("gamma", msg)  # a line beside the matches
        self.assertIn("to_line", msg)

    def test_retry_with_the_supplied_line_number_copies_that_passage(self):
        text, _ = assemble(REPORT, [{"from": "alpha", "to": "beta", "to_line": 4}], None)
        self.assertEqual(text, "alpha\nbeta\ngamma\nbeta\n")
        text, _ = assemble(REPORT, [{"from": "alpha", "to": "beta", "to_line": 2}], None)
        self.assertEqual(text, "alpha\nbeta\n")

    def test_repeated_from_boundary_is_rejected_and_from_line_resolves_it(self):
        with self.assertRaises(CopyError) as cm:
            assemble(REPORT, [{"from": "beta", "to": "delta"}], None)
        self.assertIn("line 2: beta", str(cm.exception))
        self.assertIn("from_line", str(cm.exception))
        text, _ = assemble(REPORT, [{"from": "beta", "from_line": 4, "to": "delta"}], None)
        self.assertEqual(text, "beta\ndelta\n")

    def test_to_is_matched_only_from_the_resolved_from_onward(self):
        text, _ = assemble(REPORT, [{"from": "gamma", "to": "beta"}], None)  # only line 4 is at or after line 3
        self.assertEqual(text, "gamma\nbeta\n")
        with self.assertRaises(CopyError):
            assemble(REPORT, [{"from": "gamma", "to": "alpha"}], None)

    def test_a_line_number_that_disagrees_with_its_text_is_rejected(self):
        with self.assertRaises(CopyError):
            assemble(REPORT, [{"from": "gamma", "from_line": 2, "to": "delta"}], None)
        with self.assertRaises(CopyError):
            assemble(REPORT, [{"from": "alpha", "to": "delta", "to_line": 4}], None)


class Rejections(unittest.TestCase):
    def rejected(self, *args):
        with self.assertRaises(CopyError):
            assemble(*args)

    def test_invalid_arguments(self):
        cases = {
            "empty report": ("", "all", None),
            "copy is neither all nor a list": (REPORT, "everything", None),
            "copy is a number": (REPORT, 3, None),
            "empty list": (REPORT, [], None),
            "selection is not an object": (REPORT, ["alpha"], None),
            "missing to": (REPORT, [{"from": "alpha"}], None),
            "missing from": (REPORT, [{"to": "alpha"}], None),
            "empty from": (REPORT, [{"from": "", "to": "alpha"}], None),
            "whitespace-only to": (REPORT, [{"from": "alpha", "to": " \t"}], None),
            "non-string boundary": (REPORT, [{"from": 1, "to": "alpha"}], None),
            "unknown key": (REPORT, [{"from": "alpha", "to": "beta", "extra": 1}], None),
            "boundary not found": (REPORT, [{"from": "omega", "to": "delta"}], None),
            "to not found": (REPORT, [{"from": "alpha", "to": "omega"}], None),
            "partial line does not match": (REPORT, [{"from": "alph", "to": "delta"}], None),
            "line number out of range": (REPORT, [{"from": "alpha", "from_line": 9, "to": "delta"}], None),
            "line number zero": (REPORT, [{"from": "alpha", "from_line": 0, "to": "delta"}], None),
            "line number is a bool": (REPORT, [{"from": "alpha", "from_line": True, "to": "delta"}], None),
            "line number is a string": (REPORT, [{"from": "alpha", "from_line": "1", "to": "delta"}], None),
            "out of order": (REPORT, [{"from": "delta", "to": "delta"}, {"from": "alpha", "to": "alpha"}], None),
            "overlapping": (REPORT, [{"from": "alpha", "to": "gamma"}, {"from": "gamma", "to": "delta"}], None),
            "identical": (REPORT, [{"from": "alpha", "to": "alpha"}, {"from": "alpha", "to": "alpha"}], None),
            "whitespace-only additions": (REPORT, "all", " \n\t"),
            "empty additions": (REPORT, "all", ""),
            "additions not a string": (REPORT, "all", 5),
        }
        for name, args in cases.items():
            with self.subTest(name):
                self.rejected(*args)

    def test_a_line_after_a_final_newline_is_not_a_line(self):
        self.rejected("a\nb\n", [{"from": "a", "from_line": 3, "to": "b"}], None)


if __name__ == "__main__":
    unittest.main()
