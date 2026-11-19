"""Ask-to-act cards (``actCards`` in /status; ``askCards`` is the older clarify note, ask_cards.py): before a bot does something for the person (answers a message for them,
replies in a thread, books, files, changes something), it asks with ``ask_to_act`` and the Hermes HQ app draws the
call as one card: where it came from (app, who, where, when), their words, the files they sent, a link to open the
conversation, the question, a button per choice (Yes / No unless the bot names others, its pick first), "Something
else…" and a line saying what happens next. Nothing is done until the person answers.

Like ``draft_message`` (drafts.py): Hermes gives a plugin tool no way to hold a turn open for an answer, so the tool
answers at once (``{"ask": "waiting", "id"}``) and tells the bot to end its reply. The person's answer comes back as an
ordinary message in the chat, written by the app, whose first line names the card:

    [Hermes HQ ask a-1a2b3c4d: 1]       the first choice ("Yes."), then the question and what the bot said comes next
    [Hermes HQ ask a-1a2b3c4d: other]   what the person typed instead
    [Hermes HQ ask a-1a2b3c4d: 1 send]  the first choice, and the reply the bot had ready for it, to send as written

A reply ready per choice (``replies``, ``reply_to``) cuts the round trip: the card shows it under the choice with Send
and Edit, so the person needn't wait for the bot to write it. While a reply waits (or its choice wasn't the one sent)
the drafts guard holds its text: a sending tool carrying it is refused, as for a waiting draft card.

The tool shows only once an app that draws the card has said so on this computer (PUT /asks/cards writes
``ask-cards.on`` in the add-on's data folder), so a bot never asks where nobody sees a card. Off for every bot while
``asks.off`` exists there. It works in turns ``hermes serve`` runs and in a turn the messaging gateway runs in one of
the app's own chats (a scheduled job's report handed to a bot's Bot Chat): that turn's calls are kept in the chat, and
the app draws the card from them. Anywhere else (Telegram, a cron run's own session) the tool's answer tells the bot to
ask in its reply instead. Nothing the card holds is logged.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

TOOL = "ask_to_act"
SWITCH = "asks.off"
CARDS = "ask-cards.on"
DEFAULT_CHOICES = ("Yes", "No")
MAX_QUESTION = 140
MAX_THEN = 160
MAX_CHOICE = 40
MAX_APP = 40
MAX_PLACE = 120
MAX_WHEN = 60
MAX_QUOTE = 2000
MAX_FILES = 4
MAX_FILE_NAME = 80
MAX_FILE_NOTE = 160
MAX_LINK_TITLE = 60
MAX_REPLY = 20_000
MAX_REPLY_APP = 40
MAX_REPLY_PLACE = 300

MARK = re.compile(r"^\s*\[Hermes HQ ask (a-[0-9a-f]{8}): ([1-3]( send)?|other)\]")

SCHEMA = {
    "name": TOOL,
    "description": (
        "Ask the user for a go-ahead before you act for them, as one card in the Hermes HQ app they answer with a "
        "tap: where it came from, the other person's words, the files they sent, a link to the conversation, your "
        "question and a button per choice. Use it when someone asked the user something and you'd answer, reply, book, "
        "file or change something on their behalf once they decide (\"Dana asked whether to start with the pilot "
        "group: yes or no, and I'll draft the reply\"). Don't write that ask in your reply, and don't do the thing "
        "yet. This tool does nothing itself: it returns {ask: \"waiting\", id} at once. End your reply with one short "
        "line and stop. Their answer arrives as a message starting \"[Hermes HQ ask <id>: 1]\" (the first choice; 2 "
        "and 3 the others) or \"[Hermes HQ ask <id>: other]\" with their own words: carry on with what you said in "
        "`then`. When a choice means a message in their name (a reply to the one who asked), write it now and pass it in "
        "`replies` (one per choice, \"\" for a choice with none) with `reply_to`: the card shows it under that choice "
        "with Send and Edit. Then \"[Hermes HQ ask <id>: 1 send]\" carries the final text: send exactly that, to that "
        "place, with the tool you'd normally use, and confirm in a few words. Without a ready reply, once they answer "
        "show the message with draft_message; never send it straight away. For a question that only decides how you "
        "do your own work, use clarify. One call per ask."),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string",
                         "description": f"One short question, as you'd ask it (≤ {MAX_QUESTION} characters): "
                                        "\"Start with just the pilot group?\""},
            "then": {"type": "string",
                     "description": f"What you do once they answer (≤ {MAX_THEN}): \"I'll draft the reply. Nothing "
                                    "gets sent.\""},
            "choices": {"type": "array", "items": {"type": "string"},
                        "description": f"2 or 3 short answers (≤ {MAX_CHOICE} each), your pick first. Leave out for "
                                       "Yes / No."},
            "app": {"type": "string", "description": "Where it came from, the app's name alone: Slack, Email, Text…"},
            "from": {"type": "string", "description": "Who wrote it."},
            "where": {"type": "string", "description": "The channel, DM, thread or email subject."},
            "when": {"type": "string", "description": "When they wrote it, as you'd say it: \"11:03 AM\", \"Tue\"."},
            "quote": {"type": "string",
                      "description": f"Their own words, exactly (≤ {MAX_QUOTE}). Not a summary."},
            "attachments": {
                "type": "array",
                "description": f"Files they sent with it (≤ {MAX_FILES}).",
                "items": {"type": "object",
                          "properties": {"name": {"type": "string", "description": "The file's name."},
                                         "note": {"type": "string", "description": "What they said with it."}},
                          "required": ["name"]},
            },
            "replies": {"type": "array", "items": {"type": "string"},
                        "description": "The reply you'd send for each choice, in the same order as `choices` (Yes, No "
                                       "when you left them out), exactly as it would be sent; \"\" for a choice with "
                                       "no reply. Leave out when there is nothing to send."},
            "reply_to": {"type": "object",
                         "description": "Where `replies` go. Needed with `replies`.",
                         "properties": {"app": {"type": "string", "description": "Slack, Email, Text…"},
                                        "to": {"type": "string", "description": "The person, channel or thread."},
                                        "subject": {"type": "string", "description": "Email only."},
                                        "from": {"type": "string",
                                                 "description": "The account it's sent from, when they have more "
                                                                "than one."}},
                         "required": ["app"]},
            "link": {"type": "object",
                     "description": "Where they can open the conversation itself (an https link).",
                     "properties": {"title": {"type": "string", "description": "\"Open Dana's DM\""},
                                    "url": {"type": "string"}},
                     "required": ["url"]},
        },
        "required": ["question", "then"],
    },
}

WAITING_NOTE = (
    "The user now sees this as a card with a button per choice. Nothing has been done. Say in one short line that "
    "you've asked (don't repeat the card's details) and end your reply. If they answer in their own words instead of "
    "the card, follow that.")

UNSERVED = (
    "An ask card shows only in a Hermes HQ app chat, and this turn isn't one. Ask in your reply instead: where it "
    "came from and their words first, then a link to the conversation, then one short question with its choices and "
    "what you'll do once they answer.")

JOB_NOTE = (
    "[Hermes HQ app note, not from the person: if this report has something that needs their go-ahead before you act "
    "for them (someone asked them a question you'd answer, a reply you'd send, something you'd book or change), ask "
    "it with `ask_to_act` instead of writing the ask in your reply: it shows who asked and their words, and a reply "
    "ready for each choice in `replies`. Don't mention this note.]")


def switched_on(data_dir: Path) -> bool:
    try:
        return not (Path(data_dir) / SWITCH).exists()
    except OSError:
        return True


def cards_shown(data_dir: Path) -> bool:
    """Whether an app that draws the card has said so on this computer."""
    try:
        return (Path(data_dir) / CARDS).exists()
    except OSError:
        return False


def note_cards(data_dir: Path) -> None:
    """PUT /asks/cards: an app that draws ask cards uses this computer."""
    (Path(data_dir) / CARDS).touch(exist_ok=True)


def status(data_dir: Path) -> dict:
    """`actCards`: ask_to_act exists here (false while asks.off turns it off). `actCardsShown`: an app has said it
    draws the card, so bots have the tool; an app that draws it and reads false answers with PUT /asks/cards."""
    return {"actCards": switched_on(data_dir), "actCardsShown": cards_shown(data_dir)}


def _text(args, key: str) -> str:
    value = args.get(key) if isinstance(args, dict) else None
    return value.strip() if isinstance(value, str) else ""


def _reply(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def _failed(error: str) -> str:
    return _reply({"status": "failed", "error": error})


def choices_of(args) -> list[str] | str:
    """The card's choices (Yes / No when the bot gave none), or what is wrong with them."""
    raw = args.get("choices") if isinstance(args, dict) else None
    if raw is None or raw == []:
        return list(DEFAULT_CHOICES)
    if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
        return "`choices` is a list of 2 or 3 short answers. Leave it out for Yes / No."
    out = [c.strip() for c in raw]
    if not 2 <= len(out) <= 3:
        return f"`choices` holds {len(out)}; a card has 2 or 3 buttons."
    if any(not c or len(c) > MAX_CHOICE for c in out):
        return f"Each choice is 1 to {MAX_CHOICE} characters."
    if len({c.casefold() for c in out}) != len(out):
        return "Two choices are the same; make each one different."
    return out


def ask_id(question: str, sender: str, quote: str, choices: list[str]) -> str:
    """An ask's id is made from its own words, so the app makes the same one from the call it draws (its AskCards.id)
    without reading this tool's result: "a-" and 8 hex digits of SHA-256 over question, from and quote (stripped)
    and the choices, joined by newlines."""
    return "a-" + hashlib.sha256("\n".join([question, sender, quote, *choices]).encode("utf-8")).hexdigest()[:8]


def _https(url: str) -> bool:
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.netloc)


def _error(args: dict) -> str | None:
    question, then = _text(args, "question"), _text(args, "then")
    if not question:
        return "`question` is empty: one short question, as you'd ask it."
    if len(question) > MAX_QUESTION:
        return f"`question` is {len(question)} characters; the card takes {MAX_QUESTION}. Put the detail in `quote`."
    if not then:
        return "`then` is empty: say what you'll do once they answer (\"I'll draft the reply. Nothing gets sent.\")."
    if len(then) > MAX_THEN:
        return f"`then` takes {MAX_THEN} characters."
    if (len(_text(args, "app")) > MAX_APP or len(_text(args, "from")) > MAX_PLACE
            or len(_text(args, "where")) > MAX_PLACE or len(_text(args, "when")) > MAX_WHEN):
        return f"`app` takes {MAX_APP} characters, `from` and `where` {MAX_PLACE} each, `when` {MAX_WHEN}."
    if len(_text(args, "quote")) > MAX_QUOTE:
        return f"`quote` takes {MAX_QUOTE} characters; keep the part the question is about."
    files = args.get("attachments")
    if files not in (None, []):
        if not isinstance(files, list) or len(files) > MAX_FILES:
            return f"`attachments` is a list of up to {MAX_FILES} files ({{name, note}})."
        for item in files:
            name = _text(item, "name")
            if not name or len(name) > MAX_FILE_NAME or len(_text(item, "note")) > MAX_FILE_NOTE:
                return f"Each attachment has a `name` (≤ {MAX_FILE_NAME}) and an optional `note` (≤ {MAX_FILE_NOTE})."
    link = args.get("link")
    if link not in (None, {}):
        if not isinstance(link, dict) or not _https(_text(link, "url")):
            return "`link` is {title, url} with an https url. Leave it out when there is none."
        if len(_text(link, "title")) > MAX_LINK_TITLE:
            return f"The link's `title` takes {MAX_LINK_TITLE} characters."
    return None


def replies_of(args, choices: list[str]) -> list[str] | str:
    """The reply ready for each choice ("" for none; all "" when the bot gave none), or what is wrong with them."""
    raw = args.get("replies") if isinstance(args, dict) else None
    if raw is None or raw == []:
        return [""] * len(choices)
    if not isinstance(raw, list) or not all(isinstance(r, str) for r in raw):
        return "`replies` is a list of strings, one per choice (\"\" for a choice with no reply)."
    if len(raw) != len(choices):
        return (f"`replies` has {len(raw)} and the card has {len(choices)} choices ({', '.join(choices)}): give one "
                "per choice, in the same order, \"\" for a choice with no reply.")
    out = [r.strip() for r in raw]
    if any(len(r) > MAX_REPLY for r in out):
        return f"A reply takes {MAX_REPLY} characters."
    if not any(out):
        return [""] * len(choices)
    target = args.get("reply_to")
    if not isinstance(target, dict) or not _text(target, "app"):
        return "`reply_to` is missing: say where the replies go ({app, to}), e.g. {\"app\": \"Slack\", \"to\": \"Dana\"}."
    if len(_text(target, "app")) > MAX_REPLY_APP or any(len(_text(target, k)) > MAX_REPLY_PLACE
                                                          for k in ("to", "subject", "from")):
        return f"In `reply_to`, `app` takes {MAX_REPLY_APP} characters, `to`, `subject` and `from` {MAX_REPLY_PLACE}."
    return out


def _drafts():
    """The drafts module this process loaded (its guard holds a ready reply's text until the person's Send)."""
    return sys.modules.get("hermes_hq_drafts")


def reply_key(ask: str, index: int) -> str:
    return f"{ask}/{index}"


def take(args, session_id: str = "") -> str:
    """The tool call: check it, hold its ready replies for the guard, and say it waits."""
    args = args if isinstance(args, dict) else {}
    wrong = _error(args)
    if wrong:
        return _failed(wrong)
    choices = choices_of(args)
    if isinstance(choices, str):
        return _failed(choices)
    replies = replies_of(args, choices)
    if isinstance(replies, str):
        return _failed(replies)
    new_id = ask_id(_text(args, "question"), _text(args, "from"), _text(args, "quote"), choices)
    drafts = _drafts()
    if drafts is not None:
        for index, reply in enumerate(replies, start=1):
            if reply:
                drafts.remember(session_id, reply_key(new_id, index), reply)
    return _reply({"ask": "waiting", "id": new_id, "note": WAITING_NOTE})


def answer(user_message) -> tuple[str, str, bool] | None:
    """(ask id, "1" | "2" | "3" | "other", whether the person sent that choice's reply) for an answer to an ask card."""
    if not isinstance(user_message, str):
        return None
    found = MARK.match(user_message)
    if not found:
        return None
    return found.group(1), found.group(2).split()[0], bool(found.group(3))


def note_answer(user_message) -> None:
    """pre_llm_call: Send on a ready reply lets that reply's text out; every other reply of the ask stays held."""
    found = answer(user_message)
    drafts = _drafts()
    if not found or not found[2] or drafts is None:
        return
    with drafts._lock:
        held = drafts._drafts.get(reply_key(found[0], int(found[1])))
        if held is not None:
            held["state"] = "send"


def job_note(user_message, *, data_dir: Path, in_app_chat: bool) -> str | None:
    """The note for a scheduled job's report handed to a bot's Bot Chat, while the tool is on there."""
    if not in_app_chat or not isinstance(user_message, str) or not user_message.lstrip().startswith("[Cronjob "):
        return None
    try:
        return JOB_NOTE if switched_on(data_dir) and cards_shown(data_dir) else None
    except Exception:
        return None


def register_tool(ctx, data_dir: Callable[[], Path], serves: Callable[[], bool]) -> None:
    """`serves`: this turn's chat is one the app shows (a `hermes serve` turn, or a gateway turn in a Bot Chat)."""
    def handle(args, session_id="", **kwargs):
        if not serves():
            return _failed(UNSERVED)
        return take(args, str(session_id or ""))

    def on() -> bool:
        try:
            root = data_dir()
            return switched_on(root) and cards_shown(root)
        except Exception:
            return False

    ctx.register_tool(name=TOOL, toolset="hq_asks", check_fn=on, schema=SCHEMA, handler=handle,
                      description=SCHEMA["description"], emoji="🙋")
