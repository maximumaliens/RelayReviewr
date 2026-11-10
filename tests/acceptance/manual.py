"""Isolation check around steps a person does by hand in the acceptance environment (design test 12).

  manual.py start     snapshot the installed relay's store (read-only) before the manual steps
  manual.py finish    compare it, attribute changes as run.py does, and write results/<run-id>-manual/summary.md

Run nothing else in the acceptance environment between the two: `finish` takes its test marks from the
development store and from the test Claude configuration's transcripts changed since `start`.
Exit status as run.py: 0 valid, 1 INVALID (the installed store gained a row the manual steps wrote), 2 error.
"""
import pathlib
import pickle
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import harness  # noqa: E402  (pins the isolated environment)
import isolation  # noqa: E402
from harness import setup  # noqa: E402

STATE = setup.TMP / "manual-baseline.pickle"


def start():
    if STATE.exists():
        print(f"manual: a baseline from an unfinished check exists ({STATE}); run finish first", file=sys.stderr)
        return 2
    STATE.write_bytes(pickle.dumps({"started": time.time(), "baseline": isolation.snapshot(),
                                 "code": isolation.code_state(harness.ROOT)}))
    print(f"baseline taken: {isolation.INSTALLED_STORE}")
    return 0


def finish():
    saved = pickle.loads(STATE.read_bytes())
    sids, labels = harness.marks(saved["started"])
    marks = {"session_ids": sids, "labels": labels, "roots": [setup.ACCEPT, setup.TMP]}
    mine, other = isolation.attribute(saved["baseline"], isolation.snapshot(), marks)
    guard, code = isolation.guard(mine, other)
    out = harness.ROOT / "results" / (time.strftime("%Y%m%d-%H%M%S", time.localtime(saved["started"])) + "-manual")
    out.mkdir(parents=True, exist_ok=True)
    code_line = isolation.describe_code(saved.get("code", (None, None)), isolation.code_state(harness.ROOT))
    (out / "summary.md").write_text(guard + code_line + f"\nTest sessions: {sorted(sids)}\nTest labels: {sorted(labels)}\n")
    print((out / "summary.md").read_text())
    STATE.unlink()
    return code


if __name__ == "__main__":
    step = {"start": start, "finish": finish}.get(sys.argv[1] if len(sys.argv) == 2 else None)
    if step is None:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    try:
        sys.exit(step())
    except Exception as e:  # noqa: BLE001  a runner error, as in run.py
        print(f"manual: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
