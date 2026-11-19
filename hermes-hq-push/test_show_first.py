"""show_first: a clarify about a picture the bot made waits until the picture is in the chat. Run from the plugin
folder: ``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_show_first.py``."""
import importlib.util
import os
import time
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("show_first_under_test", Path(__file__).with_name("show_first.py"))
show_first = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(show_first)


@pytest.fixture(autouse=True)
def _clean():
    show_first._turns.clear()
    yield
    show_first._turns.clear()


@pytest.fixture
def mock(tmp_path):
    path = tmp_path / "mock.png"
    path.write_bytes(b"\x89PNG")
    return str(path)


def _ask():
    return {"questions": [{"question": "A or B?", "choices": ["A", "B"]}]}


def test_clarify_after_looking_at_own_mock_is_held_with_the_media_line(mock):
    show_first.note_turn("s")
    assert show_first.check("vision_analyze", {"image_url": mock, "question": "ok?"}, "s") is None
    verdict = show_first.check("clarify", _ask(), "s")
    assert verdict["action"] == "block"
    assert f"MEDIA:{mock}" in verdict["message"]


def test_held_only_once_per_turn(mock):
    show_first.check("vision_analyze", {"image_url": mock}, "s")
    assert show_first.check("clarify", _ask(), "s")["action"] == "block"
    show_first.check("vision_analyze", {"image_url": mock}, "s")
    assert show_first.check("clarify", _ask(), "s") is None


def test_media_line_in_the_bots_words_lets_the_question_through(mock):
    show_first.check("vision_analyze", {"image_url": mock}, "s")
    show_first.note_text("s", f"Here are both options:\nMEDIA:{mock}")
    assert show_first.check("clarify", _ask(), "s") is None


def test_preview_directive_and_desktop_preview_count_as_shown(mock):
    show_first.check("vision_analyze", {"image_url": mock}, "s")
    show_first.note_text("s", '::preview{file="/tmp/x.html"}')
    assert show_first.check("clarify", _ask(), "s") is None
    show_first.note_turn("s")
    show_first.check("vision_analyze", {"image_url": mock}, "s")
    show_first.check("desktop_preview", {"action": "open"}, "s")
    assert show_first.check("clarify", _ask(), "s") is None


def test_words_without_a_picture_do_not_count(mock):
    show_first.check("vision_analyze", {"image_url": mock}, "s")
    show_first.note_text("s", "Let me ask you which one.")
    assert show_first.check("clarify", _ask(), "s")["action"] == "block"


def test_a_picture_the_person_sent_is_not_held(tmp_path):
    theirs = tmp_path / ".dispatch-attachments" / "abc" / "photo.jpg"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"x")
    show_first.check("vision_analyze", {"image_url": str(theirs)}, "s")
    assert show_first.check("clarify", _ask(), "s") is None


def test_an_old_file_or_a_web_image_is_not_held(tmp_path, mock):
    old = tmp_path / "old.png"
    old.write_bytes(b"x")
    past = time.time() - show_first.FRESH_SECONDS - 60
    os.utime(old, (past, past))
    show_first.check("vision_analyze", {"image_url": str(old)}, "s")
    show_first.check("vision_analyze", {"image_url": "https://example.com/a.png"}, "s")
    show_first.check("vision_analyze", {"image_url": "/nope/missing.png"}, "s")
    assert show_first.check("clarify", _ask(), "s") is None


def test_a_new_turn_starts_clean_and_sessions_are_apart(mock):
    show_first.check("vision_analyze", {"image_url": mock}, "s")
    show_first.note_turn("s")
    assert show_first.check("clarify", _ask(), "s") is None
    show_first.check("vision_analyze", {"image_url": mock}, "other")
    assert show_first.check("clarify", _ask(), "s") is None
    assert show_first.check("clarify", _ask(), "other")["action"] == "block"


def test_only_the_latest_three_pictures_are_named(tmp_path):
    for i in range(5):
        p = tmp_path / f"m{i}.png"
        p.write_bytes(b"x")
        show_first.check("vision_analyze", {"image_url": str(p)}, "s")
    message = show_first.check("clarify", _ask(), "s")["message"]
    assert message.count("MEDIA:") == 3 and "m4.png" in message and "m1.png" not in message


def test_other_tools_and_bad_args_never_raise():
    assert show_first.check("terminal", None, None) is None
    assert show_first.check("vision_analyze", {"image_url": 7}, None) is None
    show_first.note_text(None, None)
    assert show_first.check("clarify", "junk", None) is None
