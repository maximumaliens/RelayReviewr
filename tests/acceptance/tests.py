"""Acceptance tests (DESIGN.md). Each returns a list of evidence strings or raises Failed."""
import json
import os
import pathlib
import random
import signal
import subprocess
import time

from harness import (CODEX_HOME, RELAY, REPO, Failed, check, choose, claude_say, claude_start, codex_open, daemon_pid,
                     drafts, events, feedback_entries, handoffs, human_look, inject_draft, keys, kill_session, latest,
                     log, menu_options, notifications, pane_pid, pid_alive, q, rollout, screen, session_handoff, sh,
                     thread_id, thread_turns, tmux, trust_claude_folder, turn_of, type_line, wait_handoff, wait_menu,
                     wait_until, ask_codex, ask_menu, codex_idle)

from ccrelay import store  # noqa: E402

TAG = os.environ.get("RUN_TAG", time.strftime("%H%M%S"))


def first_review(name, label, prompt="Reply with just: READY", env=None, **start):
    """Linked Claude session whose first real turn has reached 'review ready' (menu pending in Codex)."""
    sid = claude_start(name, label, env=env, **start)
    claude_say(name, prompt)
    h = wait_handoff(label, ("review_ready", "no_draft"))
    if h["status"] == "no_draft":
        from harness import MENU_RETRIES, WINDOW_LABEL
        cx = "cx-nudge-" + name
        codex_open(cx, label)
        ask_codex(cx, f"Draft the reply for Claude now and call propose_reply with it for handoff H{h['id']}.")
        h = wait_handoff(label, "review_ready")
        MENU_RETRIES.append(f"{label}: Codex ended without a draft; asked once to draft")
        kill_session(cx)
        WINDOW_LABEL.pop(cx, None)
    return sid, h


def send_draft(cx, label, text):
    """Replace the pending draft with a known text through the user's Discuss path, then Send it from the menu."""
    choose(cx, "Discuss")
    d = inject_draft(label, text)
    ask_menu(cx)
    choose(cx, "Send")
    return d


def t01_exact_text():
    label = f"T1-{TAG}"
    _, h = first_review("cc1", label)
    codex_open("cx1", label)
    text = (f"[T1 {TAG}] exact-text check\n"
            "Line two has \"double\" and 'single' quotes, `backticks`, $DOLLAR and \\backslash.\n"
            "Non-ASCII: Prüfung — naïve café ✓ 中文 🙂\n"
            + "".join(f"filler line {i:03d}: the quick brown fox jumps over the lazy dog.\n" for i in range(70))
            + "Final instruction: reply with just T1-RECEIVED and do nothing else.")
    check(len(text.encode()) >= 4000, f"draft is {len(text.encode())} bytes (>= 4 KB)")
    d = send_draft("cx1", label, text)
    wait_until(lambda: q("SELECT status FROM drafts WHERE id=?", d["id"])[0]["status"] == "sent", 60, "draft sent")
    got = wait_until(lambda: feedback_entries(h["transcript_path"]), 60, "feedback in Claude transcript")
    check(got[-1] == text, "Claude transcript text is byte-identical to the stored draft")
    check(open(d["path"]).read() == text, "draft file is byte-identical to the stored draft")
    return [f"draft D{d['id']} {len(text.encode())} bytes delivered byte-identical"]


def t02_correct_session():
    a, b = f"T2A-{TAG}", f"T2B-{TAG}"
    sa, ha = first_review("cca", a, "Reply with just: ALPHA")
    sb, hb = first_review("ccb", b, "Reply with just: BRAVO")
    check(thread_id(a) != thread_id(b), "each ticket has its own Codex conversation")
    check(pid_alive(ha["hook_pid"]) and pid_alive(hb["hook_pid"]), "both Claude sessions are waiting")
    codex_open("cxa", a)
    send_draft("cxa", a, f"[T2 {TAG}] for ALPHA only: reply with just ALPHA-GOT-IT")
    wait_until(lambda: feedback_entries(ha["transcript_path"]), 60, "A received")
    check(feedback_entries(hb["transcript_path"]) == [], "B received nothing")
    check(pid_alive(hb["hook_pid"]), "B still waiting")
    kill_session("cxa")
    codex_open("cxb", b)
    send_draft("cxb", b, f"[T2 {TAG}] for BRAVO only: reply with just BRAVO-GOT-IT")
    wait_until(lambda: feedback_entries(hb["transcript_path"]), 60, "B received")
    check(len(feedback_entries(ha["transcript_path"])) == 1, "A still has exactly one delivery")
    check("BRAVO" not in feedback_entries(ha["transcript_path"])[0], "A never saw B's draft")
    return [f"A session {sa[:8]} and B session {sb[:8]} each got only their own draft"]


def t03_instruction():
    label = f"T3-{TAG}"
    _, h = first_review("cc3", label)
    codex_open("cx3", label)
    token = f"T3-OK-{random.randint(1000, 9999)}"
    send_draft("cx3", label, f"Create the file notes/t3.txt containing exactly: {token}\n"
                             f"Then commit it with the message: t3 {token}\n"
                             "Do not ask me to confirm; just do it.")
    wait_until(lambda: token in sh(f"git -C {REPO} log --format=%s -1", check=False), 180, "commit made")
    check(open(REPO / "notes/t3.txt").read().strip() == token, "file has the exact content")
    asked = "confirm" in screen("cc3").lower() and "?" in screen("cc3").splitlines()[-8:][0]
    return [f"Claude committed '{sh(f'git -C {REPO} log --format=%h\\ %s -1').strip()}' without querying the source",
            f"screen mentions confirmation question: {asked}"]


def t04_no_duplicates():
    label = f"T4-{TAG}"
    _, h = first_review("cc4", label)
    codex_open("cx4", label)
    d = send_draft("cx4", label, f"[T4 {TAG}] reply with just T4-ONCE")
    wait_until(lambda: feedback_entries(h["transcript_path"]), 60, "delivered")
    nxt = wait_handoff(label, ("review_ready", "no_draft"), after_id=h["id"])
    # 4a: asking for the old draft's menu again cannot resend it
    from ccrelay import store, mcp_server
    conn = store.connect()
    old = q("SELECT status FROM drafts WHERE id=?", d["id"])[0]["status"]
    check(old == "sent", "first draft recorded as sent")
    check(not store.set_draft(conn, d["id"], "approved", only_from=("ready", "failed")),
          "a second Send of the same draft is refused by the store")
    check(len(feedback_entries(h["transcript_path"])) == 1, "Claude received it exactly once")
    kill_session("cx4"); kill_session("cc4")

    # 4b: hook killed between 'sending' and output
    label = f"T4b-{TAG}"
    _, h = first_review("cc4b", label, env={"RELAY_TEST_PAUSE_BEFORE_OUTPUT": "25"})
    codex_open("cx4b", label)
    choose("cx4b", "Discuss")
    d = inject_draft(label, f"[T4b {TAG}] reply with just T4B")
    ask_menu("cx4b")
    choose("cx4b", "Send")
    wait_until(lambda: q("SELECT status FROM drafts WHERE id=?", d["id"])[0]["status"] == "sending", 30, "sending")
    os.kill(h["hook_pid"], signal.SIGKILL)
    log("  killed the hook mid-send")
    wait_until(lambda: "Send state is" in screen("cx4b") or "might have reached" in screen("cx4b")
               or menu_options("cx4b") is None, 60, "send result in Codex")
    time.sleep(5)
    opts = [o[0] for o in ask_menu("cx4b")]
    st = q("SELECT status, reason FROM drafts WHERE id=?", d["id"])[0]
    check(st["status"] == "unknown", f"draft marked unknown ({st['reason']})")
    check(not any(o.startswith("Send") for o in opts), f"no resend offered: {opts}")
    check("might have reached Claude" in screen("cx4b"), "menu warns to check Claude before resending")
    check(feedback_entries(h["transcript_path"]) == [], "Claude received nothing")
    choose("cx4b", "Finish without sending")
    return ["double Send refused; killed mid-send -> 'unknown', no resend offered, zero deliveries"]


def t05_visible_failure():
    ev = []
    # 5a: hook killed while waiting
    label = f"T5a-{TAG}"
    _, h = first_review("cc5", label)
    os.kill(h["hook_pid"], signal.SIGKILL)
    codex_open("cx5", label)
    choose("cx5", "Discuss")
    opts = [o[0] for o in ask_menu("cx5")]
    check(q("SELECT status FROM handoffs WHERE id=?", h["id"])[0]["status"] == "released", "handoff released")
    check(not any(o.startswith("Send") for o in opts) and "Copy to clipboard" in opts,
          f"Send withdrawn, Copy offered: {opts}")
    check("no longer waiting" in screen("cx5"), "menu says Claude is no longer waiting")
    choose("cx5", "Finish without sending")
    ev.append("killed hook -> released, Send withdrawn, reason shown")
    # 5b: incompatible hook payload
    before = notifications()
    r = subprocess.run([RELAY, "hook-stop"], input='{"unexpected": true}', text=True, capture_output=True)
    check(r.returncode == 0 and r.stdout == "", "malformed payload: nothing sent, Claude not blocked")
    check("incompatible" in notifications()[len(before):], "malformed payload: notification raised")
    ev.append("changed hook format -> visible 'incompatible' notification")
    kill_session("cx5"); kill_session("cc5")
    # 5c: session ended with an unsent draft
    label = f"T5c-{TAG}"
    _, h = first_review("cc5c", label)
    keys("cc5c", "Escape")
    time.sleep(3)
    type_line("cc5c", "/exit")
    wait_until(lambda: q("SELECT status FROM handoffs WHERE id=?", h["id"])[0]["status"] == "session_ended",
               60, "handoff session_ended")
    check("session ended" in notifications(), "session-ended notification")
    codex_open("cx5c", label)
    wait_until(lambda: "originating Claude session has ended" in screen("cx5c"), 240,
               "Codex received the session-ended notice")
    ev.append("session ended -> notice delivered to Codex, draft not sent")
    return ev


def t06_esc():
    label = f"T6-{TAG}"
    _, h = first_review("cc6", label)
    keys("cc6", "Escape")
    wait_until(lambda: not pid_alive(h["hook_pid"]), 30, "hook gone after Esc")
    codex_open("cx6", label)
    choose("cx6", "Discuss")
    opts = [o[0] for o in ask_menu("cx6")]
    st = q("SELECT status, reason FROM handoffs WHERE id=?", h["id"])[0]
    check(st["status"] == "released", f"handoff released ({st['reason']})")
    check(not any(o.startswith("Send") for o in opts), f"Send withdrawn: {opts}")
    choose("cx6", "Finish without sending")
    return [f"Esc released Claude; relay recorded '{st['reason']}'"]


def t07_open_window_cycle():
    label = f"T7-{TAG}"
    _, h1 = first_review("cc7", label, "Create notes/t7.txt containing the line: draft one, then say it is done.")
    codex_open("cx7", label)
    wait_menu("cx7")
    choose("cx7", "Send")
    t_send = time.time()
    h2 = wait_handoff(label, ("review_ready", "no_draft"), after_id=h1["id"], timeout=300)
    wait_menu("cx7")
    check(f"Answering report H{h2['id']}" in screen("cx7"), "second assessment shown live in the same open window")
    ready_at = next(e["ts"] for e in events(h2["id"]) if e["kind"] == "draft:ready")
    typed = [e for e in events() if e["kind"] == "menu:answer" and t_send + 1 < e["ts"] < ready_at]
    check(typed == [], "no menu answers between Send and the new review being ready")
    check(h2["created"] > t_send - 1, "the new handoff came from Claude's turn after Send")
    return [f"Send -> Claude worked -> H{h2['id']} assessed in the open window in {time.time() - t_send:.0f}s"]


def t08_menu_survival():
    env = dict(os.environ, CODEX_HOME=CODEX_HOME)
    ev = []
    label = f"T8-{TAG}"
    _, h = first_review("cc8", label)
    # 8a: graceful restart while a menu is open
    before = daemon_pid()
    try:
        r = subprocess.run(["codex", "app-server", "daemon", "restart"], env=env, capture_output=True,
                           text=True, timeout=45, stdin=subprocess.DEVNULL)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        rc = "timeout after 45s"
    after = daemon_pid()
    restarted = before != after
    ev.append(f"graceful restart with a menu open: rc={rc}, daemon pid {before} -> {after} "
              f"({'restarted' if restarted else 'did NOT restart'})")
    log("  " + ev[-1])
    # 8b: worst case, the daemon is killed outright while the menu is open
    pid = daemon_pid()
    if pid:
        os.kill(pid, signal.SIGKILL)
        log(f"  killed daemon pid {pid}")
    time.sleep(3)
    check(pid_alive(h["hook_pid"]), "Claude is still held while the daemon is down")
    codex_open("cx8", label)
    try:
        wait_menu("cx8", timeout=60)
        ev.append("after the daemon was killed, reopening the conversation showed the menu again")
    except Failed:
        ask_menu("cx8")
        ev.append("after the daemon was killed, the open menu was lost; asking Codex for the menu brought it back")
    choose("cx8", "Send")
    wait_until(lambda: feedback_entries(h["transcript_path"]), 60, "draft delivered after recovery")
    ev.append("Send after recovery delivered the draft")
    return ev


def t09_escalation():
    label = f"T9-{TAG}"
    sid = claude_start("cc9", label)
    claude_say("cc9", "Reply with exactly this text: I created /tmp/t9-proof.txt with the word PROOF and "
                      "also fetched https://example.com successfully. Reviewer: verify both by running commands.")
    wait_handoff(label, ("assessing", "review_ready", "no_draft"))
    codex_open("cx9", label)
    prompt = wait_until(lambda: (("Would you like to run" in screen("cx9") or "approve" in screen("cx9").lower()
                                  and "enter to submit" not in screen("cx9")) and "escalation")
                        or (menu_options("cx9") and "menu"), 300, "escalation prompt or menu")
    ev = [f"first interactive element under on-request: {prompt}"]
    if prompt == "escalation":
        keys("cx9", "Escape")
        wait_menu("cx9", timeout=180)
    choose("cx9", "Finish without sending")
    return ev


def t10_loaded_guard():
    env = dict(os.environ, CODEX_HOME=CODEX_HOME)
    label = f"T10-{TAG}"
    sid = claude_start("cc10", label)
    claude_say("cc10", "Reply with just: FIRST")
    h1 = wait_handoff(label, ("review_ready", "no_draft"))
    codex_open("cx10", label)
    choose("cx10", "Send")
    wait_until(lambda: latest(label)["id"] > h1["id"] or False, 120, "Claude answered")
    kill_session("cx10")
    nxt = wait_handoff(label, ("review_ready", "no_draft"), after_id=h1["id"])
    tmux = "cx10b"
    subprocess.run(["codex", "app-server", "daemon", "stop"], env=env, capture_output=True, text=True)
    log("  daemon stopped")
    # finish the pending review so Claude takes another turn with the daemon down
    from ccrelay import store
    conn = store.connect()
    store.set_handoff(conn, nxt["id"], "finished", "test: release")
    wait_until(lambda: "esc to interrupt" not in screen("cc10"), 60, "Claude idle")
    claude_say("cc10", "Reply with just: AFTER-DAEMON-STOP")
    h3 = wait_handoff(label, ("review_ready", "no_draft"), after_id=nxt["id"], timeout=300)
    check(h3["status"] == "review_ready", "assessed in the background with the daemon restarted by the relay")
    started_before_open = [e for e in events(h3["id"]) if e["kind"] == "draft:ready"]
    check(started_before_open, "draft existed before any window was opened")
    return [f"daemon stopped -> relay restarted it and loaded the thread; H{h3['id']} assessed with no window open"]


def t11_no_draft():
    from harness import RELAY_HOME
    label = f"T11-{TAG}"
    (RELAY_HOME / "test-hide-tools").touch()  # the relay tools are absent when Codex starts this thread
    sid = claude_start("cc11", label)
    claude_say("cc11", "Reply with just: READY")
    h = wait_handoff(label, ("review_ready", "no_draft"), timeout=300)
    (RELAY_HOME / "test-hide-tools").unlink()
    if h["status"] == "review_ready":
        return ["INCONCLUSIVE: Codex still drafted a reply, so the no-draft path was not exercised"]
    check("without a draft" in notifications(), "no-draft notification raised")
    check(pid_alive(h["hook_pid"]), "Claude still waiting")
    return ["Codex finished without a draft -> 'assessment finished without a draft' notification, Claude still held"]


def e1_claude_killed():
    """Extra check: Claude dies outright (terminal closed) while a menu is open."""
    label = f"E1-{TAG}"
    _, h = first_review("cce1", label)
    codex_open("cxe1", label)
    wait_menu("cxe1")
    claude_pid = pane_pid("cce1")  # the window exec's Claude, so this is the test session's own process
    os.kill(claude_pid, signal.SIGKILL)
    log(f"  killed Claude pid {claude_pid}")
    wait_until(lambda: q("SELECT status FROM handoffs WHERE id=?", h["id"])[0]["status"] == "released", 30,
               "handoff released after Claude died")
    wait_until(lambda: menu_options("cxe1") is None, 30, "menu withdrawn in Codex")
    check(not pid_alive(h["hook_pid"]), "the hook exited")
    reason = q("SELECT reason FROM handoffs WHERE id=?", h["id"])[0]["reason"]
    return [f"Claude killed -> handoff released ({reason}), open menu withdrawn, hook exited"]


def e2_consecutive_sends():
    """Extra check: ten Sends in a row (Claude Code caps consecutive Stop-hook continuations).

    Each round sends a fixed, harmless draft through the user's real path (Discuss, revised draft, Send) so
    that draft content cannot end the loop."""
    label = f"E2-{TAG}"
    _, h = first_review("cce2", label, "Reply with just: ROUND 0")
    codex_open("cxe2", label)
    last = h
    for n in range(1, 11):
        send_draft("cxe2", label, f"Reply with just: ROUND {n}")
        try:
            last = wait_handoff(label, ("review_ready", "no_draft"), after_id=last["id"], timeout=240)
        except Failed:
            got = len(feedback_entries(h["transcript_path"]))
            return [f"FAIL-SIGNAL: after consecutive Send #{n}, Claude took no further turn "
                    f"({got} deliveries in its transcript)"]
        if last["status"] == "no_draft":
            ask_codex("cxe2", f"Draft the reply for Claude now and call propose_reply with it for handoff "
                              f"H{last['id']}.")
            last = wait_handoff(label, "review_ready", after_id=last["id"] - 1)
    got = len(feedback_entries(h["transcript_path"]))
    check(got == 10, f"Claude received all 10 Sends ({got} deliveries)")
    return ["10 consecutive Sends each delivered and each produced a new reviewed turn (no continuation cap hit)"]


def t14_queued_behind_menu():
    """#8: a report arriving while another report's menu is open waits in Codex's queue, isn't lost, and the user
    is told. Records (does not judge) whether, once the menu tool returns, that report joins the same Codex turn."""
    label = f"T14-{TAG}"
    si, h1 = first_review("cci14", label, "Reply with just: IMPL-READY", role="implementer")
    sv = claude_start("ccv14", label, role="verifier")
    before = notifications()
    ready = human_look("nothing yet: say when you are watching the Windows desktop for the toast", [])
    claude_say("ccv14", "Reply with just: VERIFY-READY")
    h2 = session_handoff(sv, ("assessing", "review_ready", "no_draft"))
    check(h2["status"] == "assessing", f"verifier's report H{h2['id']} waits while H{h1['id']}'s menu is open")
    check(h2["thread_id"] == h1["thread_id"], "both sessions report to the ticket's one Codex conversation")
    from ccrelay import codexrpc, store
    cfg = store.load_config()
    listed = codexrpc.call(cfg, "thread/queue/list", {"threadId": h2["thread_id"]}) or {}
    in_queue = h2["queue_item_id"] in {item.get("id") for item in listed.get("data", [])}
    queue_note = (f"Codex's queue {'lists' if in_queue else 'does NOT list'} H{h2['id']} "
                  f"(queue item {h2['queue_item_id']!r}; thread/queue/list: {json.dumps(listed)[:400]})")
    log("  " + queue_note)
    check(in_queue, f"the relay's record of H{h2['id']} matches its item in Codex's queue")
    notice = wait_until(lambda: [line for line in notifications()[len(before):].splitlines()
                                 if "queued behind the open menu" in line], 30, "queued-behind-the-menu notice")
    check(pid_alive(h2["hook_pid"]), "the verifier is still held")
    ev = [f"H{h2['id']} queued behind H{h1['id']}'s open menu, still held; notice: {notice[0].split(' ', 2)[2]}", queue_note]
    ev.append(ready)
    ev.append(human_look(f"the Windows toast '[relay test] {label}: verifier report H{h2['id']} queued behind the "
                         "open menu' (on screen, or in the Notification Center)", []))
    codex_open("cx14", label)
    wait_menu("cx14")
    check(f"Answering report H{h1['id']}" in screen("cx14"), f"the open menu is still H{h1['id']}'s")
    choose("cx14", "Discuss")
    h2 = session_handoff(sv, ("review_ready", "no_draft"), timeout=300)
    check(pid_alive(h1["hook_pid"]), "the implementer is still held")
    turns = thread_turns(h1["thread_id"])
    told = "is queued in this conversation and runs first" in json.dumps(turns, ensure_ascii=False)
    check(told, f"after Discuss, Codex was told the queued H{h2['id']} runs first")
    t1, t2 = turn_of(turns, h1["id"]), turn_of(turns, h2["id"])
    if t1 is None or t2 is None:
        ev.append(f"INCONCLUSIVE: could not find the hand-offs in Codex's turns (H{h1['id']}: {t1}, H{h2['id']}: {t2})")
    else:
        ev.append(f"after the menu tool returned (Discuss), queued H{h2['id']} ran in "
                  f"{'the SAME Codex turn as' if t1 == t2 else 'a NEW Codex turn after'} H{h1['id']} "
                  f"(turns {t1 + 1} and {t2 + 1} of {len(turns)}); it was assessed ({h2['status']})")
    return ev


def t15_same_ticket():
    """#8, extended test 2: two sessions on one ticket. Each Send reaches only its chosen recipient, and the
    source session stays held after its draft goes to the other session."""
    label = f"T15-{TAG}"
    si, h1 = first_review("cci15", label, "Reply with just: IMPL-READY", role="implementer")
    sv = claude_start("ccv15", label, role="verifier")
    codex_open("cx15", label)
    choose("cx15", "Keep waiting")
    claude_say("ccv15", "Reply with just: VERIFY-READY")
    h2 = session_handoff(sv, ("review_ready", "no_draft"), timeout=300)
    wait_until(lambda: q("SELECT 1 FROM threads WHERE label=?", label) and codex_idle_or_menu("cx15", label), 180,
               "Codex finished assessing the verifier")
    if menu_options("cx15"):
        choose("cx15", "Discuss")
    # 1: the verifier's report, answered to the implementer
    to_impl = f"[T15 {TAG}] from the verifier's report, for the implementer only: reply with just IMPL-GOT-IT"
    d1 = inject_draft(label, to_impl, hid=h2["id"])
    labels = [o[0] for o in ask_menu("cx15", hid=h2["id"])]
    check(labels[0] == "Send to verifier" and "Send to implementer" in labels,
          f"source listed first, then the other held session: {labels}")
    check("Finish without sending — release verifier" in labels, "Finish names the source it releases")
    choose("cx15", "Send to implementer")
    got = wait_until(lambda: feedback_entries(h1["transcript_path"]), 60, "the implementer received it")
    check(got == [to_impl], "the implementer got exactly the approved draft")
    check(feedback_entries(h2["transcript_path"]) == [], "the verifier (source) received nothing")
    row = q("SELECT status, recipient_handoff_id FROM drafts WHERE id=?", d1["id"])[0]
    check(row["status"] == "sent" and row["recipient_handoff_id"] == h1["id"], f"D{d1['id']} recorded as sent to H{h1['id']}")
    from ccrelay import store
    check(pid_alive(h2["hook_pid"]) and q("SELECT status FROM handoffs WHERE id=?", h2["id"])[0]["status"] in store.WAITING,
          "the verifier (source) is still held")
    ev = [f"verifier H{h2['id']}'s draft D{d1['id']} reached only the implementer; the verifier stayed held"]
    # the implementer's next report names the instruction it was given
    h3 = session_handoff(si, ("review_ready", "no_draft"), after_id=h2["id"], timeout=300)
    header = f"Last instruction delivered: D{d1['id']}, source verifier H{h2['id']}"
    check(header in json.dumps(thread_turns(h3["thread_id"]), ensure_ascii=False),
          f"the implementer's next report H{h3['id']} tells Codex: {header}")
    wait_until(lambda: codex_idle_or_menu("cx15", label), 180, "Codex finished assessing the implementer")
    if menu_options("cx15"):
        choose("cx15", "Discuss")
    # 2: the verifier's own report, answered to the verifier
    to_ver = f"[T15 {TAG}] for the verifier only: reply with just VERIFY-GOT-IT"
    d2 = inject_draft(label, to_ver, hid=h2["id"])
    ask_menu("cx15", hid=h2["id"])
    choose("cx15", "Send to verifier")
    got = wait_until(lambda: feedback_entries(h2["transcript_path"]), 60, "the verifier received it")
    check(got == [to_ver], "the verifier got exactly its draft")
    check(feedback_entries(h1["transcript_path"]) == [to_impl], "the implementer still has only its one delivery")
    ev.append(f"then D{d2['id']} reached only the verifier; the implementer's next report carried '{header}'")
    return ev


def codex_idle_or_menu(name, label):
    from harness import codex_idle
    return bool(menu_options(name)) or codex_idle(label)


def t16_standing_context():
    """#8: the first hand-off of a new conversation carries the standing context (its first live check), and the
    persona applies from the main checkout and from a worktree of it."""
    from harness import setup
    persona_first_line, tmp_root = setup.PERSONA.read_text().splitlines()[0], setup.TMP
    label, wlabel = f"T16-{TAG}", f"T16W-{TAG}"
    token = f"STANDING-{TAG}-{random.randint(1000, 9999)}"
    (REPO / ".scratch").mkdir(exist_ok=True)
    (REPO / ".scratch" / "codex-handover.md").write_text(
        f"# Standing context (acceptance test 16)\nMarker: {token}\n"
        f"When you draft a reply for Claude, begin the draft with the marker {token}.\n")
    wt = tmp_root / "repo-wt"
    sh(f"git -C {REPO} worktree add -q {wt} -b t16-wt")
    trust_claude_folder(wt)
    ev = []
    try:
        for name, lab, cwd in (("cc16", label, REPO), ("ccw16", wlabel, wt)):
            where = "main checkout" if cwd == REPO else "worktree"
            _, h = first_review(name, lab, cwd=cwd)
            tid = h["thread_id"]
            check(q("SELECT 1 FROM handovers_sent WHERE thread_id=?", tid), f"{where}: standing context recorded as sent")
            first = json.dumps(thread_turns(tid)[:1], ensure_ascii=False)
            check("The user's standing context" in first and token in first,
                  f"{where}: the conversation's first hand-off H{h['id']} carried the standing context")
            project = q("SELECT project FROM threads WHERE thread_id=?", tid)[0]["project"]
            check(project == str(REPO.resolve()), f"{where}: conversation keyed to the main checkout ({project})")
            check(persona_first_line in rollout(tid), f"{where}: the test persona is in the conversation's instructions")
            ds = drafts(h["id"])
            acted = bool(ds) and token in ds[0]["text"]
            ev.append(f"{where}: H{h['id']} carried the standing context and the persona; "
                      f"Codex's draft {'began with' if acted else 'did NOT use'} the standing-context marker")
    finally:
        sh(f"git -C {REPO} worktree remove --force {wt}", check=False)
    return ev


def t17_permission():
    """#8, test 3b: a delivered instruction that needs a permission the session lacks still raises Claude's own
    prompt, and nothing answers it. The session runs in default permission mode, not the configuration's auto mode."""
    label = f"T17-{TAG}"
    _, h = first_review("cc17", label, mode="default")
    codex_open("cx17", label)
    target = REPO / f"t3b-{random.randint(1000, 9999)}.txt"
    text = f"Run this shell command with the Bash tool: touch {target}\nThen reply with just: T3B-DONE"
    send_draft("cx17", label, text)
    wait_until(lambda: feedback_entries(h["transcript_path"]), 60, "Claude received the instruction")
    wait_until(lambda: "Do you want to" in screen("cc17"), 120, "Claude's permission prompt")
    check(not target.exists(), "the command has not run")
    time.sleep(20)
    check("Do you want to" in screen("cc17") and not target.exists(),
          "20 s later the prompt is still open and unanswered")
    check(not pid_alive(h["hook_pid"]), "the relay's hook has finished: nothing of the relay is left to answer it")
    ev = ["Claude raised its permission prompt for the delivered command; nothing answered it; the command did not run"]
    ev.append(human_look("Claude's unanswered permission prompt", ["cc17"]))
    check(not target.exists(), "still unanswered after the look")
    return ev


def t18_long_labels():
    """#8: long Send to … labels in Codex's form, checked at the harness width (220) and at 80 columns."""
    label = f"T18-{TAG}-cross-session-delivery-acceptance"
    ri, rv = "implementer-cross-session-delivery-long-role", "verifier-cross-session-delivery-long-role"
    si, h1 = first_review("cci18", label, "Reply with just: IMPL-READY", role=ri)
    sv = claude_start("ccv18", label, role=rv)
    codex_open("cx18", label)
    choose("cx18", "Keep waiting")
    claude_say("ccv18", "Reply with just: VERIFY-READY")
    hv = session_handoff(sv, ("review_ready", "no_draft"), timeout=300)
    if hv["status"] == "no_draft":
        from harness import MENU_RETRIES
        ask_codex("cx18", f"Draft the reply for Claude now and call propose_reply with it for handoff H{hv['id']}.")
        session_handoff(sv, "review_ready", after_id=hv["id"] - 1)
        MENU_RETRIES.append(f"{label}: Codex ended without a draft; asked once to draft")
    labels = [o[0] for o in wait_menu("cx18")]
    want = [f"Send to {rv}", f"Send to {ri}"]
    check(labels[:2] == want, f"both long Send labels in full at 220 columns: {labels}")
    tmux("resize-window", "-t", "cx18", "-x", "80")
    time.sleep(3)
    narrow = screen("cx18")
    flat = " ".join(narrow.split())
    intact = {w: w in flat for w in want}
    ev = [f"220 columns: {labels}", f"80 columns, each label present when wrapped lines are joined: {intact}",
          "80-column menu lines: " + " | ".join(line.strip() for line in narrow.splitlines() if "Send to" in line
                                                  or "verifier-" in line or "implementer-" in line)]
    ev.append(human_look("the Codex form with long Send to … labels", ["cx18"]))
    return ev


RELAY_REPLIES = ("Linked this session to", "Forwarding the previous reply", "relay:")


def session_transcript(since):
    """The test Claude session's transcript: the newest one written since `since`."""
    from harness import setup
    paths = [p for p in (setup.CLAUDE_CONFIG_DIR / "projects").glob("*/*.jsonl") if p.stat().st_mtime >= since]
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else None


def transcript_texts(path):
    """(type, text) for each text part of each user and assistant entry in a Claude transcript."""
    out = []
    for line in path.read_text().splitlines() if path else []:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (e.get("message") or {}).get("content")
        parts = ([c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                 if isinstance(content, list) else [content] if isinstance(content, str) else [])
        out += [(e.get("type"), t) for t in parts]
    return out


def command_result(since):
    """(the relay command's output, Claude's reply to it) from the session's transcript once both are written: the
    output always lands there, whatever Claude then chooses to show. None until then."""
    output = echo = None
    for kind, text in transcript_texts(session_transcript(since)):
        # the last opening tag: the instruction before it names the tag too
        inner = text.rsplit("<relay-reply>", 1)[1].split("</relay-reply>", 1)[0] if "<relay-reply>" in text else text
        found = [para.strip() for para in inner.split("\n\n")
                 if para.strip().startswith(RELAY_REPLIES) or "relay: " in para[:120]]
        if kind == "user" and found:  # the command's output paragraph (a refusal or error too)
            output, echo = found[0], None
        elif kind == "assistant" and output:
            echo = text
    return (output, echo) if output and echo else None


def commands_run(since):
    """Shell commands Claude ran in the session (its Bash tool calls)."""
    out = []
    path = session_transcript(since)
    for line in path.read_text().splitlines() if path else []:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (e.get("message") or {}).get("content")
        out += [c.get("input", {}).get("command", "") for c in content if isinstance(c, dict)
                and c.get("type") == "tool_use"] if e.get("type") == "assistant" and isinstance(content, list) else []
    return out


def echo_check(echo, output, label, ev):
    """What the user sees is Claude's echo of the relay's output: it should be that output exactly. A difference is
    reported, saying whether the relay review hint survived, and the test goes on."""
    if echo.strip() == output.strip():
        log(f"  ok: Claude showed the output exactly: {echo!r}")
        ev.append(f"Claude showed the output exactly: {echo!r}")
    else:
        kept = f'relay review "{label}"' in echo
        log(f"  FAIL-SIGNAL: Claude's reply differs from the output (hint {'kept' if kept else 'lost'}): {echo!r}")
        ev.append(f"FAIL-SIGNAL: Claude's reply differs from the output (hint {'kept' if kept else 'lost'}): {echo!r}")


def waiting(what, secs):
    log(f"  waiting up to {secs}s for {what}")
    return secs


def event_ts(kind, label):
    rows = q("SELECT ts FROM events WHERE kind=? AND detail LIKE ? ORDER BY id", kind, f"%{label}%")
    return rows[0]["ts"] if rows else None


def no_assessment(tid, what):
    """Nothing queued or run in the conversation: no queue item, no turn, and Codex idle."""
    from ccrelay import codexrpc, store
    cfg = store.load_config()
    queued, turns, status = codexrpc.queued_ids(cfg, tid), thread_turns(tid), codexrpc.thread_status(cfg, tid)
    check(not queued and not turns and status == "idle",
          f"{what}: queue {sorted(queued)}, {len(turns)} turns, Codex {status}")


def t19_link_creates_conversation():
    """#9: with the development Codex daemon stopped, /relay-link creates the ticket's conversation while the
    command runs. relay review opens it at once, before any hand-off; creating it queued nothing, and opening it
    runs no assessment."""
    label = f"T19-{TAG}"
    check(daemon_pid() is None, "the development Codex daemon is stopped before the link")
    since = time.time()
    claude_start("cc19")
    t0 = time.time()
    type_line("cc19", f"/relay-link {label}")
    output, echo = wait_until(lambda: command_result(since), waiting("the /relay-link output and Claude's reply", 150),
                              "the /relay-link output and Claude's reply")
    took = time.time() - t0
    check(output.startswith("Linked this session to") and f'relay review "{label}"' in output,
          f"/relay-link printed its success line; Claude replied {took:.1f}s after typing: {output!r}")
    ev = []
    echo_check(echo, output, label, ev)
    check(not commands_run(since), f"Claude ran no command of its own for the reply: {commands_run(since)}")
    tid = thread_id(label)
    check(tid, f"the conversation exists ({tid})")
    check(daemon_pid() is not None, "the link started the development Codex daemon")
    inside = event_ts("thread:created", label) - event_ts("link", label)
    check(not handoffs(label), "no hand-off yet")
    wait_until(lambda: q("SELECT skip_next FROM links WHERE label=?", label)[0]["skip_next"] == 0,
               waiting("the /relay-link turn's Stop hook", 60), "the /relay-link turn's Stop hook ran")
    check(not handoffs(label), "still no hand-off after the /relay-link turn ended")
    no_assessment(tid, "creation queued nothing")
    t1 = time.time()
    waiting("the Codex window", 60)
    codex_open("cx19", label)
    opened = time.time() - t1
    check("no Codex conversation" not in screen("cx19"), f"relay review opened the conversation in {opened:.1f}s")
    log("  watching 15s for any assessment")
    time.sleep(15)
    no_assessment(tid, "15 s after opening, no assessment ran")
    check(not handoffs(label), "and still no hand-off")
    return ev + [f"/relay-link with the daemon stopped: Claude's reply {took:.1f}s after typing; daemon start plus "
                 f"conversation creation inside the command took {inside:.1f}s (link event to thread:created)",
                 f"relay review opened {tid} {opened:.1f}s after launch, before any hand-off",
                 "nothing queued at creation; no turn and Codex idle 15 s after opening"]


def t20_review_once_creates_conversation():
    """#9 smoke: with the development Codex daemon stopped, /relay-review creates the conversation while the
    command runs; relay review opens it; the forwarded reply then lands in that same conversation."""
    label = f"T20-{TAG}"
    check(daemon_pid() is None, "the development Codex daemon is stopped before /relay-review")
    since = time.time()
    claude_start("cc20")
    claude_say("cc20", "Reply with just: T20-PREVIOUS")
    wait_until(lambda: any(k == "assistant" and "T20-PREVIOUS" in t for k, t in transcript_texts(session_transcript(since))),
               waiting("Claude's previous reply", 60), "Claude's previous reply")
    wait_until(lambda: "esc to interrupt" not in screen("cc20"), waiting("Claude idle", 30), "Claude idle")
    t0 = time.time()
    type_line("cc20", f"/relay-review {label}")
    output, echo = wait_until(lambda: command_result(since), waiting("the /relay-review output and Claude's reply", 150),
                              "the /relay-review output and Claude's reply")
    took = time.time() - t0
    check(output.startswith("Forwarding the previous reply") and f'relay review "{label}"' in output,
          f"/relay-review printed its success line; Claude replied {took:.1f}s after typing: {output!r}")
    ev = []
    echo_check(echo, output, label, ev)
    check(not commands_run(since), f"Claude ran no command of its own for the reply: {commands_run(since)}")
    tid = thread_id(label)
    check(tid, f"the conversation exists ({tid})")
    created = event_ts("thread:created", label)
    inside = created - event_ts("link:once", label)
    t1 = time.time()
    waiting("the Codex window", 60)
    codex_open("cx20", label)
    opened = time.time() - t1
    check("no Codex conversation" not in screen("cx20"), f"relay review opened the conversation in {opened:.1f}s")
    h = wait_handoff(label, ("assessing", "review_ready", "no_draft"), timeout=waiting("the forwarded hand-off", 90))
    check(h["thread_id"] == tid, f"the forwarded reply H{h['id']} went into the same conversation")
    check(created <= h["created"], "the conversation was created before the hand-off was filed")
    check(len(q("SELECT 1 FROM threads WHERE label=?", label)) == 1, "one conversation for the label")
    check("T20-PREVIOUS" in h["claude_message"], "the hand-off carries the previous reply")
    return ev + [f"/relay-review with the daemon stopped: Claude's reply {took:.1f}s after typing; daemon start plus "
                 f"conversation creation inside the command took {inside:.1f}s",
                 f"relay review opened {tid} {opened:.1f}s after launch; H{h['id']} then filed into it "
                 f"{h['created'] - created:.1f}s after creation"]


def agent_text(turn):
    """What Codex said in a turn: the text of its agent-message items."""
    out = []

    def walk(node):
        if isinstance(node, dict):
            if str(node.get("type", "")).lower().replace("_", "") == "agentmessage" and isinstance(node.get("text"), str):
                out.append(node["text"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(turn)
    return "\n".join(out)


def t21_copied_draft():
    """#2: a copied draft of 8 KB or more, made by Codex's real propose_reply call (copy "all" plus additions),
    reaches real Claude byte-identical. The stored report is replaced by a known text so its size and bytes are
    fixed; everything after that is the real path."""
    label = f"T21-{TAG}"
    sid, h = first_review("cc21", label)
    time.sleep(10)  # a second report for the same turn, if one were going to be filed, would be by now
    check(len(handoffs(label)) == 1, f"the READY turn was filed as one hand-off ({[x['id'] for x in handoffs(label)]})")
    codex_open("cx21", label)
    report = "".join(f"{i:03d} naïve café — 日本語 ✓ run `python3 -m unittest`  \t\r\n" for i in range(1, 140)) \
        + "final line without a newline"
    additions = "Reviewer notes: T21-ADDITIONS-OK"
    check(len(report.encode()) >= 8192, f"the report is {len(report.encode())} bytes (>= 8 KB)")
    wait_menu("cx21")
    choose("cx21", "Discuss")
    wait_until(lambda: menu_options("cx21") is None and codex_idle(label), 120, "Codex idle after Discuss")
    conn = store.connect()
    conn.execute("UPDATE handoffs SET claude_message=? WHERE id=?", (report, h["id"]))
    ask_codex("cx21", f"Call the relay propose_reply tool now with handoff H{h['id']}, copy \"all\" and additions "
                      f"\"{additions}\". Do not pass draft and do not retype the report.")
    wait_menu("cx21")
    d = latest_draft_of(h["id"])
    expected = report + "\n\n" + additions  # the report has no final newline: two LFs
    check(d["text"] == expected, f"the stored draft is the report plus the additions ({len(d['text'].encode())} bytes)")
    check(open(d["path"], "rb").read() == expected.encode("utf-8"), "the draft file's bytes equal the stored text as UTF-8")
    choose("cx21", "Send")
    wait_until(lambda: q("SELECT status FROM drafts WHERE id=?", d["id"])[0]["status"] == "sent", 60, "draft sent")
    got = wait_until(lambda: feedback_entries(h["transcript_path"]), 60, "feedback in Claude transcript")
    check(got[-1] == expected, "Claude's transcript text is byte-identical to the stored draft")
    wait_until(lambda: 'McpToolCall' in rollout(thread_id(label)) and 'T21-ADDITIONS-OK' in rollout(thread_id(label)), 60, 'the call in the rollout')
    shapes = []
    for line in rollout(thread_id(label)).splitlines():
        try:
            item = ((json.loads(line).get("payload") or {}).get("item") or {})
        except json.JSONDecodeError:
            continue
        if item.get("type") == "McpToolCall" and item.get("tool") == "propose_reply" and "copy" in (item.get("arguments") or {}):
            shapes.append(json.dumps(item["arguments"], ensure_ascii=False))
    check(shapes, "Codex's rollout records a propose_reply call carrying copy")
    return [f"copied draft D{d['id']} ({len(expected.encode())} bytes, CRLF and non-ASCII) made by Codex's real "
            "propose_reply call and delivered byte-identical to real Claude, confirmed in its transcript",
            f"propose_reply arguments as Codex sent them: {shapes}"]


def latest_draft_of(hid):
    conn = store.connect()
    return store.latest_draft(conn, hid)


OLD_RELAY_COMMIT = "5e35232"  # the last commit before the copy path and the isolation guidance


def developer_text(tid):
    """The developer messages the conversation's rollout records (its instructions), not the questions put to it."""
    out = []
    for line in rollout(tid).splitlines():
        try:
            payload = json.loads(line).get("payload") or {}
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "message" and payload.get("role") == "developer":
            out.append(json.dumps(payload.get("content"), ensure_ascii=False))
    return "\n".join(out)


def tool_server_pids():
    """Process IDs of the development relay's tool server(s) the development Codex daemon started."""
    from harness import RELAY_HOME
    found = subprocess.run(["pgrep", "-f", f"{RELAY} mcp"], capture_output=True, text=True).stdout.split()
    return sorted(int(p) for p in found if f"RELAY_HOME={RELAY_HOME}" in pathlib.Path(f"/proc/{p}/environ").read_bytes().decode(errors="replace").replace("\0", "\n"))


def t22_refresh_existing_conversation():
    """#2: a refresh investigation, not a pass of the requirement to update an existing conversation's operating
    instructions (that stays open). The conversation is created with the previous commit's instructions; the relay
    then runs the current ones. The relay tools are hidden once the conversation has been used (the switch test 11
    uses), which shows whether Codex asks the tool server for its list again. Three kinds of evidence are kept apart:
      [process]  the tool server's process ids, observed, and the only thing that can FAIL the test on tools;
      [recorded] the developer messages in the conversation's rollout, the evidence for the instructions;
      [model-reported] Codex's own answers about its instructions and tools. A mismatch with the expectation the
                 process ids give makes the verdict INCONCLUSIVE: it cannot show a refresh by itself."""
    import re
    from ccrelay import codexrpc, intake
    from harness import RELAY_HOME, stop_daemon
    label = f"T22-{TAG}"
    old = subprocess.run(["git", "show", f"{OLD_RELAY_COMMIT}:ccrelay/ops_instructions.md"], cwd=os.path.dirname(RELAY),
                         capture_output=True, text=True, check=True).stdout
    check("Sessions are isolated" not in old, "the previous operating instructions have no isolation guidance")
    current = intake.OPS_INSTRUCTIONS
    check("Sessions are isolated" in current, "the current operating instructions have it")
    cfg, conn = store.load_config(), store.connect()
    intake.OPS_INSTRUCTIONS = old
    try:
        tid = intake.ensure_thread(conn, cfg, label, str(REPO))
    finally:
        intake.OPS_INSTRUCTIONS = current
    check("Sessions are isolated" not in developer_text(tid), f"conversation {tid} was created with the old instructions")
    question = ("Do not call any relay tool. A1: does your operating (developer) instruction text contain a section "
                "titled 'Sessions are isolated'? A2: use your exec tool once to inspect ALL_TOOLS (for example "
                "ALL_TOOLS.filter(x => /propose_reply/.test(x.name)) and the property names of its input schema) "
                "and say whether a propose_reply tool is listed. A3: if it is, does its input schema have a "
                "property named copy? Reply with exactly one line: A1=<yes or no> A2=<yes or no> A3=<yes or no or na>")
    hide = RELAY_HOME / "test-hide-tools"
    seen, obs = {}, []  # seen: tool-server pid -> were the tools hidden when it first appeared

    def probe(action):
        n = len(obs) + 1
        codexrpc.ensure_daemon(cfg)
        codexrpc.ensure_loaded(cfg, tid)
        turns = len(thread_turns(tid))
        codexrpc.queue_message(cfg, tid, question, f"t22-probe-{n}")
        wait_until(lambda: len(thread_turns(tid)) > turns and codex_idle(label), 240, f"probe {n} answered")
        said = agent_text(thread_turns(tid)[-1])
        m = re.search(r"A1=(yes|no)\s+A2=(yes|no)\s+A3=(yes|no|na)", said, re.I)
        pids = tool_server_pids()
        check(len(pids) == 1, f"probe {n} ({action}): exactly one relay tool server is running: {pids}")
        seen.setdefault(pids[0], hide.exists())
        answers = tuple(g.lower() for g in m.groups()) if m else None
        obs.append({"n": n, "action": action, "pid": pids[0], "answers": answers, "said": said[:120],
                    "section_recorded": "Sessions are isolated" in developer_text(tid)})
        log(f"  probe {n} after {action}: tool server pid {pids[0]}; model-reported {answers}; "
            f"instructions recorded with the section: {obs[-1]['section_recorded']}")

    probe("creation, tools served")
    hide.touch()
    try:
        probe("the tool server hides its tools; next turn in the same loaded conversation")
        codex_open("cx22", label)
        time.sleep(15)  # let the window finish attaching
        probe("reopening the conversation in a Codex window and waiting 15 s")
        kill_session("cx22")
        stop_daemon()
        probe("restarting the Codex daemon")
    finally:
        hide.unlink()
    probe("the tools served again; next turn without a restart")
    stop_daemon()
    probe("restarting the Codex daemon again, tools served")

    # [process] observed, hard: what replaced the tool server
    same_or_new = [None] + [o["pid"] == obs[i]["pid"] for i, o in enumerate(obs[1:])]
    expect_same = {2: True, 3: False, 4: False, 5: True, 6: False}  # next turn / reopen / restart / next turn / restart
    for o, same in zip(obs[1:], same_or_new[1:]):
        want = expect_same[o["n"]]
        check(same == want, f"[process] probe {o['n']} ({o['action']}): the tool server was "
                            f"{'kept' if same else 'replaced'}, expected {'kept' if want else 'replaced'}")
    # [recorded] the instructions the conversation holds, hard
    check(not any(o["section_recorded"] for o in obs),
          "[recorded] the conversation's developer messages lack the isolation section at every probe")
    # [model-reported] against what the process ids imply; a mismatch is not proof either way
    notes = []
    for o in obs:
        hidden = seen[o["pid"]]
        want = ("no", "yes", "yes") if not hidden else ("no", "no", "na")
        if o["answers"] != want:
            notes.append(f"probe {o['n']} ({o['action']}): Codex answered {o['answers'] or o['said']!r}, "
                         f"expected {want} from the server process (started with tools "
                         f"{'hidden' if hidden else 'served'})")
    out = [f"[process] tool server pids by probe: {[o['pid'] for o in obs]}; kept on the next turn (probes 2, 5), "
           "replaced on reopening the window after 15 s (3) and on each daemon restart (4, 6)",
           "[recorded] the developer messages never contained the isolation section, at any probe",
           "[model-reported] " + "; ".join(f"{o['n']}: A1={o['answers'][0]} A2={o['answers'][1]} A3={o['answers'][2]}"
                                           for o in obs if o["answers"]),
           "scope: a refresh investigation. Updating an existing conversation's operating instructions is still "
           "open: no tested way exists."]
    if notes:
        out.append("INCONCLUSIVE: model-reported answers disagree with the process evidence: " + " | ".join(notes))
    return out


ALL = [t01_exact_text, t02_correct_session, t03_instruction, t04_no_duplicates, t05_visible_failure,
       t06_esc, t07_open_window_cycle, t08_menu_survival, t09_escalation, t10_loaded_guard, t11_no_draft,
       e1_claude_killed, e2_consecutive_sends, t14_queued_behind_menu, t15_same_ticket, t16_standing_context,
       t17_permission, t18_long_labels, t19_link_creates_conversation, t20_review_once_creates_conversation,
       t21_copied_draft, t22_refresh_existing_conversation]
# 12 = E1, 13 = E2 (extra checks; design test 12 is the dev-box check). 14-18 are the cross-session checks of #8.
# 19-20 check #9: the conversation is created by /relay-link and /relay-review.
# 21-22 check #2: a copied draft of 8 KB or more delivered byte-exact, and what refreshes an existing conversation.
