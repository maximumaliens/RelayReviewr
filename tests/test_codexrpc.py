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
        with mock.patch.object(codexrpc, "call", side_effect=[reply, {}, {}]) as call:
            thread_id = codexrpc.start_thread(cfg, "/tmp/repo", "instructions", "ABC-42")
        self.assertEqual(thread_id, THREAD_ID)
        self.assertEqual([c.args[1] for c in call.call_args_list],
                         ["thread/start", "thread/name/set", "thread/settings/update"])
        self.assertEqual(call.call_args_list[1].args[2], {"threadId": THREAD_ID, "name": "ABC-42"})

    def test_has_codex_record_read_only_in_the_shape_a_settings_update_takes(self):
        # ThreadSettingsUpdateParams: sandboxPolicy is a SandboxPolicy object (tagged by "type"), not the
        # SandboxMode string that thread/start and thread/resume take; ThreadSettingsUpdateResponse is empty
        reply = {"thread": thread({"type": "idle"}), "sandbox": {"type": "readOnly", "networkAccess": False}}
        with mock.patch.object(codexrpc, "call", side_effect=[reply, {}, {}]) as call:
            codexrpc.start_thread({"approval_policy": "on-request"}, "/tmp/repo", "instructions", "ABC-42")
        self.assertEqual(call.call_args_list[0].args[2]["sandbox"], "read-only")
        self.assertEqual(call.call_args_list[2].args[2],
                         {"threadId": THREAD_ID, "approvalPolicy": "on-request",
                          "sandboxPolicy": {"type": "readOnly", "networkAccess": False}})


class SetReadOnlyTest(unittest.TestCase):
    """ThreadResumeParams takes `sandbox` as a SandboxMode string; ThreadResumeResponse reports it as a
    SandboxPolicy object tagged by "type". The replies are the development daemon's, as #13's probes saw them."""
    CFG = {"approval_policy": "on-request"}

    def resume_reply(self, sandbox, profile=None):
        return {"thread": thread({"type": "idle"}), "approvalPolicy": "on-request", "approvalsReviewer": "user",
                "cwd": "/tmp/repo", "model": "gpt-6.1-sol", "modelProvider": "openai", "sandbox": sandbox,
                "activePermissionProfile": profile}

    def test_resumes_naming_read_only_and_confirms_the_thread_is_loaded(self):
        replies = [self.resume_reply({"type": "readOnly", "networkAccess": False}), {"data": [THREAD_ID]}]
        with mock.patch.object(codexrpc, "call", side_effect=replies) as call:
            codexrpc.set_read_only(self.CFG, THREAD_ID)
        self.assertEqual(call.call_args_list[0].args[1:],
                         ("thread/resume", {"threadId": THREAD_ID, "excludeTurns": True, "sandbox": "read-only",
                                            "approvalPolicy": "on-request"}))
        self.assertEqual(call.call_args_list[1].args[1], "thread/loaded/list")

    WRITE = {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
             "excludeTmpdirEnvVar": False, "excludeSlashTmp": False}
    UPDATE = {"threadId": THREAD_ID, "approvalPolicy": "on-request",
              "sandboxPolicy": {"type": "readOnly", "networkAccess": False}}

    def test_a_workspace_write_reply_gets_the_settings_update_and_a_second_check(self):
        replies = [self.resume_reply(self.WRITE, {"id": ":workspace", "extends": None}), {},
                   self.resume_reply({"type": "readOnly", "networkAccess": False}), {"data": [THREAD_ID]}]
        with mock.patch.object(codexrpc, "call", side_effect=replies) as call:
            codexrpc.set_read_only(self.CFG, THREAD_ID)
        self.assertEqual([c.args[1] for c in call.call_args_list],
                         ["thread/resume", "thread/settings/update", "thread/resume", "thread/loaded/list"])
        self.assertEqual(call.call_args_list[1].args[2], self.UPDATE)
        self.assertEqual(call.call_args_list[2].args[2]["sandbox"], "read-only")

    def test_a_workspace_write_reply_after_the_settings_update_is_refused(self):
        reply = self.resume_reply(self.WRITE, {"id": ":workspace", "extends": None})
        with mock.patch.object(codexrpc, "call", side_effect=[reply, {}, reply]):
            with self.assertRaises(codexrpc.NotReadOnly) as refused:
                codexrpc.set_read_only(self.CFG, THREAD_ID)
        self.assertEqual(str(refused.exception), "this conversation is not read-only (Codex reports workspaceWrite)")

    def test_a_settings_update_that_fails_is_refused_as_not_read_only(self):
        reply = self.resume_reply(self.WRITE)
        failed = codexrpc.CodexError("thread/settings/update: method not found")
        with mock.patch.object(codexrpc, "call", side_effect=[reply, failed]):
            with self.assertRaises(codexrpc.NotReadOnly) as refused:
                codexrpc.set_read_only(self.CFG, THREAD_ID)
        self.assertIn("this conversation is not read-only (Codex reports workspaceWrite", str(refused.exception))
        self.assertIn("method not found", str(refused.exception))

    def test_a_reply_that_reports_no_sandbox_is_refused(self):
        bare = {"thread": thread({"type": "idle"})}
        with mock.patch.object(codexrpc, "call", side_effect=[bare, {}, bare]):
            with self.assertRaises(codexrpc.NotReadOnly):
                codexrpc.set_read_only(self.CFG, THREAD_ID)

    def test_a_thread_still_not_loaded_is_an_error(self):
        replies = [self.resume_reply({"type": "readOnly", "networkAccess": False}), {"data": []}]
        with mock.patch.object(codexrpc, "call", side_effect=replies):
            with self.assertRaises(codexrpc.CodexError):
                codexrpc.set_read_only(self.CFG, THREAD_ID)

    def test_ensure_loaded_resumes_read_only_only_a_thread_that_is_not_loaded(self):
        with mock.patch.object(codexrpc, "call", side_effect=[{"data": [THREAD_ID]}]) as call:
            codexrpc.ensure_loaded(self.CFG, THREAD_ID)
        self.assertEqual([c.args[1] for c in call.call_args_list], ["thread/loaded/list"])
        replies = [{"data": []}, self.resume_reply({"type": "readOnly", "networkAccess": False}), {"data": [THREAD_ID]}]
        with mock.patch.object(codexrpc, "call", side_effect=replies) as call:
            codexrpc.ensure_loaded(self.CFG, THREAD_ID)
        self.assertEqual(call.call_args_list[1].args[2]["sandbox"], "read-only")


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
