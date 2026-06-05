"""Tests for the daily pipeline helpers."""

from cron.pipeline import _extract_report


def test_extract_report_pulls_marked_block():
    text = "narration...\n<report>\n# Daily\nbody\n</report>\ntrailing"
    assert _extract_report(text) == "# Daily\nbody"


def test_extract_report_falls_back_to_whole_text():
    assert _extract_report("no markers here") == "no markers here"


def test_extract_report_empty():
    assert _extract_report("") == ""
