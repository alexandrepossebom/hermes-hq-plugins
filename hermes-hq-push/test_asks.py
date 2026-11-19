"""Ask-to-act cards (asks.py): the tool, the card switch, the id the app shares and the person's answer."""
import contextlib
import hashlib
import os
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent

if "hermes_hq_asks" in sys.modules:
    asks = sys.modules["hermes_hq_asks"]
else:
    _spec = importlib.util.spec_from_file_location("hermes_hq_asks", HERE / "asks.py")
    asks = importlib.util.module_from_spec(_spec)
    sys.modules["hermes_hq_asks"] = asks
    _spec.loader.exec_module(asks)

ARGS = {"question": "Start with the pilot group?", "then": "I'll draft the reply. Nothing gets sent.",
        "app": "Slack", "from": "Dana Reyes", "where": "DM", "when": "11:03 AM",
        "quote": "Ready to go live. Start with the pilot group?",
        "attachments": [{"name": "Q3_Plan", "note": "This is what I have so far."}],
        "link": {"title": "Open Dana's DM", "url": "https://example.slack.com/archives/D123"}}


class Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def registered(tmp_path, serves=True):
    ctx = Ctx()
    asks.register_tool(ctx, lambda: tmp_path, lambda: serves)
    return ctx.tools["ask_to_act"]


def test_waits_with_the_shared_id(tmp_path):
    out = json.loads(registered(tmp_path)["handler"](dict(ARGS)))
    assert out["ask"] == "waiting"
    expected = "a-" + hashlib.sha256("Start with the pilot group?\nDana Reyes\nReady to go live. Start with the pilot "
                                     "group?\nYes\nNo".encode()).hexdigest()[:8]
    assert out["id"] == expected
    assert "end your reply" in out["note"]


def test_fixed_vector():
    # The app's AskCardsTests carries the same value.
    assert asks.ask_id("Start with the pilot group?", "Dana Reyes", "Ready to go live. Start with the pilot group?",
                       ["Yes", "No"]) == "a-" + hashlib.sha256(
        b"Start with the pilot group?\nDana Reyes\nReady to go live. Start with the pilot group?\nYes\nNo").hexdigest()[:8]


def test_only_after_an_app_says_it_draws_the_card(tmp_path):
    tool = registered(tmp_path)
    assert tool["check_fn"]() is False
    assert asks.status(tmp_path) == {"askCards": True, "askCardsShown": False}
    asks.note_cards(tmp_path)
    assert tool["check_fn"]() is True
    (tmp_path / asks.SWITCH).touch()
    assert tool["check_fn"]() is False
    assert asks.status(tmp_path)["askCards"] is False


def test_a_gateway_turn_is_told_to_ask_in_text(tmp_path):
    out = json.loads(registered(tmp_path, serves=False)["handler"](dict(ARGS)))
    assert out["status"] == "failed" and "Ask in your reply instead" in out["error"]


def test_choices():
    assert asks.choices_of({}) == ["Yes", "No"]
    assert asks.choices_of({"choices": [" Captains first ", "Everyone"]}) == ["Captains first", "Everyone"]
    assert isinstance(asks.choices_of({"choices": ["Only one"]}), str)
    assert isinstance(asks.choices_of({"choices": ["a", "b", "c", "d"]}), str)
    assert isinstance(asks.choices_of({"choices": ["Yes", "yes"]}), str)
    assert isinstance(asks.choices_of({"choices": ["x" * 41, "No"]}), str)
    assert isinstance(asks.choices_of({"choices": "Yes, No"}), str)


def test_refusals_say_what_to_fix():
    def error(**change):
        args = dict(ARGS, **change)
        return json.loads(asks.take(args)).get("error", "")
    assert "`question` is empty" in error(question=" ")
    assert "`then` is empty" in error(then="")
    assert "takes 140" in error(question="q" * 141)
    assert "https" in error(link={"url": "http://example.com"})
    assert "https" in error(link={"url": "javascript:alert(1)"})
    assert "attachments" in error(attachments=[{"name": f"f{i}"} for i in range(5)])
    assert "`name`" in error(attachments=[{"note": "no name"}])
    assert "quote" in error(quote="w" * 2001)
    assert json.loads(asks.take({"question": "Go ahead?", "then": "I'll book it."}))["ask"] == "waiting"


def test_the_answer():
    assert asks.answer("[Hermes HQ ask a-1a2b3c4d: 1]\nYes.") == ("a-1a2b3c4d", "1", False)
    assert asks.answer("  [Hermes HQ ask a-1a2b3c4d: other]\nOnly the leads") == ("a-1a2b3c4d", "other", False)
    assert asks.answer("[Hermes HQ ask a-1a2b3c4d: 2 send]\nNo. Send this") == ("a-1a2b3c4d", "2", True)
    assert asks.answer("[Hermes HQ ask a-1a2b3c4d: other send]") is None
    assert asks.answer("[Hermes HQ ask a-1a2b3c4d: 4]") is None
    assert asks.answer("Yes") is None
    assert asks.answer(None) is None


def test_the_ask_cards_note_names_it():
    spec = importlib.util.spec_from_file_location("hermes_hq_push_ask_cards_t", HERE / "ask_cards.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "`ask_to_act`" in module.NOTE


# MARK: v2, a reply ready per choice

YES = "Yes, let's start with the pilot group. Once they're through it we'll open it to everyone."
NO = "Not yet: let's wait until the whole group's checks are done, then go live with everyone at once."

if "hermes_hq_drafts" in sys.modules:
    drafts = sys.modules["hermes_hq_drafts"]
else:
    _dspec = importlib.util.spec_from_file_location("hermes_hq_drafts", HERE / "drafts.py")
    drafts = importlib.util.module_from_spec(_dspec)
    sys.modules["hermes_hq_drafts"] = drafts
    _dspec.loader.exec_module(drafts)


def with_replies(**change):
    return dict(ARGS, replies=[YES, NO], reply_to={"app": "Slack", "to": "Dana Reyes"}, **change)


def test_replies_check():
    assert asks.replies_of({}, ["Yes", "No"]) == ["", ""]
    assert asks.replies_of(with_replies(), ["Yes", "No"]) == [YES, NO]
    assert asks.replies_of({"replies": ["", ""]}, ["Yes", "No"]) == ["", ""]
    assert "one per choice" in asks.replies_of({"replies": [YES]}, ["Yes", "No"])
    assert "`reply_to` is missing" in asks.replies_of({"replies": [YES, ""]}, ["Yes", "No"])
    assert "list of strings" in asks.replies_of({"replies": [YES, 3]}, ["Yes", "No"])
    assert "20000" in asks.replies_of({"replies": ["x" * 20_001, ""], "reply_to": {"app": "Slack"}}, ["Yes", "No"])
    # The id doesn't change with the replies: the app makes it from the card's own words.
    plain = json.loads(asks.take(dict(ARGS)))["id"]
    assert json.loads(asks.take(with_replies()))["id"] == plain


def test_a_ready_reply_is_held_until_its_send():
    drafts._drafts.clear()
    out = json.loads(asks.take(with_replies(), "chat-1"))
    ask = out["id"]
    send = {"channel": "D123", "text": YES}
    assert "hasn't approved" in drafts.blocked_message("slack_send_message", send, "chat-1")
    assert "hasn't approved" in drafts.blocked_message("slack_send_message", {"text": NO}, "chat-1")
    asks.note_answer(f"[Hermes HQ ask {ask}: 1]\nYes.")   # a plain answer lets nothing out
    assert drafts.blocked_message("slack_send_message", send, "chat-1")
    asks.note_answer(f"[Hermes HQ ask {ask}: 1 send]\nYes. Send this to Dana Reyes (Slack) exactly as written:\n\n{YES}")
    assert drafts.blocked_message("slack_send_message", send, "chat-1") is None
    assert drafts.blocked_message("slack_send_message", {"text": NO}, "chat-1")   # the choice not taken stays held
    assert drafts.blocked_message("slack_send_message", {"text": NO}, "another-chat") is None
    drafts._drafts.clear()


def test_a_jobs_report_in_a_bot_chat_is_told_about_the_card(tmp_path):
    report = '[Cronjob "Inbox urgent check" output — scheduled job, not the user. Review it, act on anything...'
    assert asks.job_note(report, data_dir=tmp_path, in_app_chat=True) is None   # no app has said it draws the card
    asks.note_cards(tmp_path)
    assert "`ask_to_act`" in asks.job_note(report, data_dir=tmp_path, in_app_chat=True)
    assert asks.job_note(report, data_dir=tmp_path, in_app_chat=False) is None
    assert asks.job_note("Can you check my Slack?", data_dir=tmp_path, in_app_chat=True) is None
    (tmp_path / asks.SWITCH).touch()
    assert asks.job_note(report, data_dir=tmp_path, in_app_chat=True) is None


@contextlib.contextmanager
def gateway_turn(key, session):
    """A turn the messaging gateway runs, as Hermes marks it: the session key and the turn's session id."""
    from tools import approval_context
    saved = os.environ.get("HERMES_SESSION_ID")
    os.environ["HERMES_SESSION_ID"] = session
    token = approval_context.set_current_session_key(key)
    try:
        yield
    finally:
        approval_context.reset_current_session_key(token)
        if saved is None:
            os.environ.pop("HERMES_SESSION_ID", None)
        else:
            os.environ["HERMES_SESSION_ID"] = saved


def test_a_gateway_turn_in_a_bot_chat_gets_the_card_and_elsewhere_text(tmp_path):
    """Real Hermes turn context (tools.approval_context) read by the add-on's own gateway_logins.turn."""
    agent = os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent"))
    sys.path.insert(0, agent)
    spec = importlib.util.spec_from_file_location("gateway_logins_asks_t", HERE / "gateway_logins.py")
    logins = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(logins)
    ctx = Ctx()
    asks.register_tool(ctx, lambda: tmp_path, lambda: logins.turn() is not None)
    handle = ctx.tools["ask_to_act"]["handler"]
    session = "20261114_090000_c0ffee"
    with gateway_turn(f"agent:otto:local:dm:{session}", session):
        assert json.loads(handle(dict(ARGS), session_id=session))["ask"] == "waiting"
    with gateway_turn("agent:otto:telegram:dm:12345", "20261114_090000_beef00"):
        assert "Ask in your reply instead" in json.loads(handle(dict(ARGS)))["error"]
    assert "Ask in your reply instead" in json.loads(handle(dict(ARGS)))["error"]   # no turn at all
