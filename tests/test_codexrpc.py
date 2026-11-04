"""Codex app-server replies as the relay reads them. The reply shapes are from Codex's own protocol schema
(`codex app-server generate-json-schema --experimental`), not from the fakes in tests/support.py."""
import unittest
from unittest import mock

from ccrelay import codexrpc


class QueueMessageTest(unittest.TestCase):
    def test_returns_the_queued_submission_id_codex_replies_with(self):
        # ThreadQueueAddResponse: {"queuedSubmission": QueuedSubmission}, QueuedSubmission requires id
        reply = {"queuedSubmission": {"id": "01a19283-d8fc-7473-96a3-085d84b0be0f", "clientUserMessageId": "relay-H2",
                                      "input": [{"type": "text", "text": "report"}]}}
        with mock.patch.object(codexrpc, "call", return_value=reply) as call:
            item = codexrpc.queue_message({}, "thread-1", "report", "relay-H2")
        self.assertEqual(item, "01a19283-d8fc-7473-96a3-085d84b0be0f")  # the id, not clientUserMessageId
        self.assertEqual(call.call_args.args[1], "thread/queue/add")

    def test_a_reply_without_an_id_gives_none(self):
        with mock.patch.object(codexrpc, "call", return_value={}):
            self.assertIsNone(codexrpc.queue_message({}, "thread-1", "report", "relay-H2"))


if __name__ == "__main__":
    unittest.main()
