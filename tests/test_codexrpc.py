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


THREAD_ID = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"


def thread(status):
    """A Thread as Codex sends it, with every required field; `status` is a ThreadStatus."""
    return {"id": THREAD_ID, "sessionId": "019a2b3c-4d5e-7f60-8a9b-ffffffffffff", "cliVersion": "0.166.0",
            "createdAt": 1762500000, "updatedAt": 1762500060, "cwd": "/tmp/repo", "ephemeral": False,
            "modelProvider": "openai", "preview": "", "projectId": None, "source": "appServer", "status": status,
            "turns": []}


class StartThreadTest(unittest.TestCase):
    def test_returns_the_new_threads_id_and_names_that_thread(self):
        # ThreadStartResponse: the thread's id is under "thread"; ThreadSetNameResponse is an empty object
        reply = {"thread": thread({"type": "idle"}), "approvalPolicy": "on-request", "approvalsReviewer": "user",
                 "cwd": "/tmp/repo", "model": "gpt-6.1-sol", "modelProvider": "openai",
                 "sandbox": {"type": "readOnly", "networkAccess": False}}
        cfg = {"approval_policy": "on-request"}
        with mock.patch.object(codexrpc, "call", side_effect=[reply, {}]) as call:
            thread_id = codexrpc.start_thread(cfg, "/tmp/repo", "instructions", "ABC-42")
        self.assertEqual(thread_id, THREAD_ID)
        self.assertEqual([c.args[1] for c in call.call_args_list], ["thread/start", "thread/name/set"])
        self.assertEqual(call.call_args_list[1].args[2], {"threadId": THREAD_ID, "name": "ABC-42"})


class QueuedIdsTest(unittest.TestCase):
    def test_returns_the_ids_of_the_queued_submissions(self):
        # ThreadQueueListResponse: {"data": [QueuedSubmission], "nextCursor": ...}
        reply = {"data": [{"id": "item-a", "clientUserMessageId": "relay-H2", "input": [{"type": "text", "text": "r"}]},
                          {"id": "item-b", "clientUserMessageId": "relay-H3", "input": [{"type": "text", "text": "r"}]}],
                 "nextCursor": None}
        with mock.patch.object(codexrpc, "call", return_value=reply) as call:
            ids = codexrpc.queued_ids({}, THREAD_ID)
        self.assertEqual(ids, {"item-a", "item-b"})  # the ids, never the client message ids
        self.assertEqual(call.call_args.args[1:], ("thread/queue/list", {"threadId": THREAD_ID}))

    def test_an_empty_queue(self):
        with mock.patch.object(codexrpc, "call", return_value={"data": [], "nextCursor": None}):
            self.assertEqual(codexrpc.queued_ids({}, THREAD_ID), set())


class ThreadStatusTest(unittest.TestCase):
    def status(self, status):
        # ThreadReadResponse: {"thread": Thread}; Thread.status is a ThreadStatus object tagged by "type"
        with mock.patch.object(codexrpc, "call", return_value={"thread": thread(status)}) as call:
            result = codexrpc.thread_status({}, THREAD_ID)
        self.assertEqual(call.call_args.args[1], "thread/read")
        return result

    def test_idle(self):
        self.assertEqual(self.status({"type": "idle"}), "idle")

    def test_an_open_menu_is_a_running_turn(self):
        # a menu is a form inside a tool call, so the turn is active, waiting on the user's input
        self.assertEqual(self.status({"type": "active", "activeFlags": ["waitingOnUserInput"]}), "active")
        self.assertEqual(self.status({"type": "active", "activeFlags": []}), "active")

    def test_not_loaded_and_system_error(self):
        self.assertEqual(self.status({"type": "notLoaded"}), "notLoaded")
        self.assertEqual(self.status({"type": "systemError"}), "systemError")


if __name__ == "__main__":
    unittest.main()
