"""Run acceptance tests in the isolated dev-box environment: run.py [test-number ...].

Results go to results/<run-id>/. The run snapshots the installed relay's store first and compares it row by
row at the end. A changed row carrying one of the run's test session IDs, test labels or acceptance paths
marks the run INVALID in summary.md; every other changed row is listed as not attributed to it.

Exit status: 0 for a valid run (whatever the tests' verdicts), 1 only for an INVALID run, 2 when the run couldn't
be made or finished (missing sign-ins, a refused path, a runner error), with the reason on stderr.
"""
import json
import pathlib
import sys
import time
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).parent))


def main():
    # imported here, so a failure while loading them exits 2 like any other runner error
    import harness
    import isolation
    import tests
    from harness import setup

    wanted = {int(a) for a in sys.argv[1:]} or set(range(1, len(tests.ALL) + 1))
    setup.setup()
    todo = setup.missing_sign_ins()
    if todo:
        print("acceptance: sign-ins needed first (run each yourself, in a terminal):\n  " + "\n  ".join(todo), file=sys.stderr)
        return 2
    baseline = isolation.snapshot()
    harness.log(f"installed store baseline: {isolation.INSTALLED_STORE} "
                f"({'absent' if baseline is None else sum(len(rows) for rows in baseline.values())} rows)")
    run_id = time.strftime("%Y%m%d-%H%M%S")
    out = harness.ROOT / "results" / run_id
    out.mkdir(parents=True)
    summary = []
    started = time.time()
    marks = {"session_ids": set(), "labels": set(), "roots": [setup.ACCEPT, setup.TMP]}
    for n, fn in enumerate(tests.ALL, start=1):
        if n not in wanted:
            continue
        harness.reset()
        harness.log(f"=== test {n}: {fn.__name__}")
        t0 = time.time()
        try:
            evidence = fn()
            verdict = ("FAIL" if any("FAIL-SIGNAL" in e for e in evidence)
                       else "INCONCLUSIVE" if any("INCONCLUSIVE" in e for e in evidence) else "PASS")
            err = None
        except Exception as e:
            verdict, evidence, err = "FAIL", [], f"{type(e).__name__}: {e}"
            harness.log(traceback.format_exc())
        if harness.MENU_RETRIES:
            evidence = evidence + [f"model needed re-asking: {', '.join(harness.MENU_RETRIES)}"]
            harness.MENU_RETRIES.clear()
        if harness.RATE_LIMITS:
            evidence = evidence + [f"waited out Codex rate limits: {'; '.join(harness.RATE_LIMITS)}"]
            harness.RATE_LIMITS.clear()
            if verdict == "FAIL" and ("timed out" in (err or "") or any("FAIL-SIGNAL" in e for e in evidence)):
                verdict = "INCONCLUSIVE"  # it ran out of time while Codex was rate-limited; that says nothing of the relay
        screens = {s: harness.screen(s) for s in harness.sessions()}
        sids, labels = harness.marks(started)
        marks["session_ids"] |= sids
        marks["labels"] |= labels
        mine, other = isolation.attribute(baseline, isolation.snapshot(), marks)  # traces a change to a test
        record = {"test": n, "name": fn.__name__, "verdict": verdict, "seconds": round(time.time() - t0),
                  "evidence": evidence, "error": err, "events": harness.events(),
                  "notifications": harness.notifications(), "screens": screens,
                  "installed_store_changes": {"attributed": mine, "not_attributed": other}}
        (out / f"t{n:02d}.json").write_text(json.dumps(record, indent=2, default=str))
        summary.append(f"| {n} | {fn.__name__} | {verdict} | {'; '.join(evidence + ([err] if err else []))} |")
        harness.log(f"=== test {n}: {verdict} {err or ''}")
    harness.tmux("kill-server", check=False)
    guard, code = isolation.guard(*isolation.attribute(baseline, isolation.snapshot(), marks))
    (out / "summary.md").write_text(guard + "\n| # | test | verdict | evidence |\n|---|---|---|---|\n"
                                    + "\n".join(summary) + "\n")
    print((out / "summary.md").read_text())
    return code


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit as e:
        if e.code is None or isinstance(e.code, int):
            raise
        print(e.code, file=sys.stderr)  # a refusal, such as setup's for a path inside the installed setup
        code = 2
    except Exception:
        traceback.print_exc()
        print("acceptance: the runner failed; this is not an INVALID run (exit 1 means only that)", file=sys.stderr)
        code = 2
    sys.exit(code)
