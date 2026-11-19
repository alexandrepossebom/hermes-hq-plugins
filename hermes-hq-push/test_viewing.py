"""[viewing] 0.9.66: a reply written in a chat that is on a screen in front of its person alerts no device.

A tester: "With a bot conversation open in Hermes HQ on Mac, a reply from the bot also triggers a new-message push
notification on iPhone." Needs only Python: `python3 -m unittest test_viewing.py` from the plugin folder.
"""
import secrets
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import push  # noqa: E402


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, device, item):
        self.sent.append((device.device_id, item.kind))


class FakeLineage:
    def __init__(self, ancestors):
        self.ancestors = ancestors

    def info(self, profile, session):
        return ("", frozenset(self.ancestors.get((profile, session), ())))


class Viewing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.store = push.Store(self.root / "devices.db")
        self.sender = FakeSender()
        self.dispatcher = push.Dispatcher(self.store, sender_factory=lambda: self.sender)
        self.dispatcher.lineage = FakeLineage({})
        for name in ("phone-0001", "mac-00001"):
            self.store.upsert(push.Device(name, secrets.token_hex(32), "production", push.TOPIC, "*", {}, True, ""))

    def tearDown(self):
        self.dir.cleanup()

    def reply(self, session="chat-a", profile="otto"):
        return push.Push("turnDone", session=session, profile=profile, sender="Otto", text="Done.")

    def sent(self):
        return sorted(device for device, _ in self.sender.sent)

    def test_a_reply_in_the_chat_on_screen_alerts_nobody(self):
        push.set_viewing(self.root, "mac-00001", "chat-a", "otto")
        self.assertEqual(self.dispatcher.deliver(self.reply()), 0)
        self.assertEqual(self.sent(), [])

    def test_a_reply_in_another_chat_or_another_bots_alerts_as_before(self):
        push.set_viewing(self.root, "mac-00001", "chat-a", "otto")
        self.assertEqual(self.dispatcher.deliver(self.reply(session="chat-b")), 2)
        self.assertEqual(self.dispatcher.deliver(self.reply(profile="ivy")), 2)
        self.assertEqual(self.sent(), ["mac-00001", "mac-00001", "phone-0001", "phone-0001"])

    def test_nobody_looking_alerts_as_before(self):
        self.assertEqual(self.dispatcher.deliver(self.reply()), 2)

    def test_the_report_runs_out_by_itself(self):
        push.set_viewing(self.root, "mac-00001", "chat-a", "otto", ttl=45, now=1_000)
        self.assertEqual(self.store.viewed(1_044), [("mac-00001", "chat-a", "otto")])
        self.assertEqual(self.store.viewed(1_046), [])
        # The row above is long past by the real clock: the Mac went to sleep with the chat open.
        self.assertEqual(self.dispatcher.deliver(self.reply()), 2)

    def test_leaving_the_chat_brings_the_alerts_back(self):
        push.set_viewing(self.root, "mac-00001", "chat-a", "otto")
        push.clear_viewing(self.root, "mac-00001")
        self.assertEqual(self.dispatcher.deliver(self.reply()), 2)

    def test_a_device_shows_one_chat_at_a_time(self):
        push.set_viewing(self.root, "mac-00001", "chat-a", "otto")
        push.set_viewing(self.root, "mac-00001", "chat-b", "otto")
        self.assertEqual(self.dispatcher.deliver(self.reply(session="chat-a")), 2)
        self.assertEqual(self.dispatcher.deliver(self.reply(session="chat-b")), 0)

    def test_approvals_and_questions_still_alert_everywhere(self):
        push.set_viewing(self.root, "mac-00001", "chat-a", "otto")
        for kind in ("approval", "input"):
            self.sender.sent.clear()
            self.assertEqual(self.dispatcher.deliver(push.Push(kind, session="chat-a", profile="otto", request="r1")), 2, kind)

    def test_a_chat_that_changed_id_mid_turn_still_counts(self):
        push.set_viewing(self.root, "mac-00001", "chat-a", "otto")
        self.dispatcher.lineage = FakeLineage({("otto", "chat-a2"): {"chat-a"}})
        self.assertEqual(self.dispatcher.deliver(self.reply(session="chat-a2")), 0)

    def test_a_device_that_never_registered_for_alerts_can_report(self):
        push.set_viewing(self.root, "mac-quiet-1", "chat-a", "otto")
        self.assertEqual(self.dispatcher.deliver(self.reply()), 0)

    def test_how_long_a_report_may_live_is_bounded(self):
        self.assertEqual(push.set_viewing(self.root, "mac-00001", "chat-a", "otto", ttl=99_999)["ttl"], 120)
        self.assertEqual(push.set_viewing(self.root, "mac-00001", "chat-a", "otto", ttl=0)["ttl"], 5)

    def test_bad_reports_are_refused(self):
        for device, session, profile, ttl in (("x", "chat-a", "otto", 45), ("mac-00001", "", "otto", 45),
                                              ("mac-00001", "chat a", "otto", 45), ("mac-00001", "chat-a", "ot to", 45),
                                              ("mac-00001", "chat-a", "otto", "soon"), ("mac-00001", "chat-a", "otto", True)):
            with self.assertRaises(push.RouteError):
                push.set_viewing(self.root, device, session, profile, ttl)

    def test_forgetting_a_device_forgets_what_it_showed(self):
        push.set_viewing(self.root, "mac-00001", "chat-a", "otto")
        self.store.remove("mac-00001")
        self.assertEqual(self.dispatcher.deliver(self.reply()), 1)

    def test_status_says_so(self):
        self.assertTrue(push.status_body(self.root, None)["viewing"])


if __name__ == "__main__":
    unittest.main()
