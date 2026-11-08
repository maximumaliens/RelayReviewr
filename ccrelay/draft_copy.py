"""Builds a draft from a stored report: the relay copies the text itself, so the reviewer never retypes it.

A draft is made of parts: copied parts (raw slices of the report), separators the relay inserts between
consecutive parts, and the reviewer's additions. Nothing is stripped or normalised in a copied part.
"""
import re

SELECTION_KEYS = {"from", "to", "from_line", "to_line"}
CONTEXT_CHARS = 120


class CopyError(ValueError):
    """The request is invalid; the message is what the reviewer is told."""


def split_lines(report):
    """(start, end) of each line, cut after every LF only (never at CR or Unicode separators). `end` includes the
    LF. An empty piece after a final LF is not a line."""
    return [(m.start(), m.end()) for m in re.finditer(r"[^\n]*\n|[^\n]+\Z", report)]


def content(report, line):
    """What boundaries are matched against: the line without its LF or CRLF, stripped at both ends."""
    return report[line[0]:line[1]].strip()


def shown(text):
    return text if len(text) <= CONTEXT_CHARS else text[:CONTEXT_CHARS] + "…"


def match_boundary(report, lines, which, boundary, given, first):
    """Index of the line `boundary` names, searching lines from index `first`."""
    if not isinstance(boundary, str) or not boundary.strip():
        raise CopyError(f"Rejected: '{which}' must be non-empty text of a line in the report.")
    if given is not None and (isinstance(given, bool) or not isinstance(given, int)):
        raise CopyError(f"Rejected: '{which}_line' must be a line number.")
    want = boundary.strip()
    matches = [i for i in range(first, len(lines)) if content(report, lines[i]) == want]
    if given is not None:
        if not 1 <= given <= len(lines):
            raise CopyError(f"Rejected: '{which}_line' {given} is not a line of the report (it has {len(lines)}).")
        if given - 1 not in matches:
            at = f"line {given}: {shown(content(report, lines[given - 1]))}"
            raise CopyError(f"Rejected: '{which}' does not match {at}.")
        return given - 1
    if not matches:
        where = "" if first == 0 else " at or after the start of this selection"
        raise CopyError(f"Rejected: no line{where} matches '{which}' {want!r}. Nothing was saved or shown.")
    if len(matches) > 1:
        raise CopyError(ambiguous(report, lines, which, want, matches))
    return matches[0]


def ambiguous(report, lines, which, want, matches):
    out = [f"Rejected: '{which}' {want!r} matches {len(matches)} lines. Retry with {which}_line set to the "
           "line you mean, exactly as listed here. Nothing was saved or shown."]
    for i in matches:
        out.append(f"line {i + 1}: {shown(content(report, lines[i]))}")
        if i > 0:
            out.append(f"    line {i}: {shown(content(report, lines[i - 1]))}")
        if i + 1 < len(lines):
            out.append(f"    line {i + 2}: {shown(content(report, lines[i + 1]))}")
    return "\n".join(out)


def selections(report, copy):
    """The (first line, last line) index pairs `copy` asks for, with the lines of the report."""
    lines = split_lines(report)
    if copy == "all":
        return lines, [(0, len(lines) - 1)]
    if not isinstance(copy, list) or not copy:
        raise CopyError("Rejected: copy must be \"all\" or a non-empty list of {from, to} selections.")
    picked = []
    for n, sel in enumerate(copy, 1):
        if not isinstance(sel, dict) or not {"from", "to"} <= set(sel) or set(sel) - SELECTION_KEYS:
            raise CopyError(f"Rejected: selection {n} must be an object with 'from' and 'to' and optionally "
                            "'from_line' and 'to_line'.")
        a = match_boundary(report, lines, "from", sel["from"], sel.get("from_line"), 0)
        b = match_boundary(report, lines, "to", sel["to"], sel.get("to_line"), a)
        if picked and a <= picked[-1][1]:
            raise CopyError(f"Rejected: selection {n} starts at line {a + 1}, but the previous one ends at line "
                            f"{picked[-1][1] + 1}. Selections must ascend and not overlap.")
        picked.append((a, b))
    return lines, picked


def assemble(report, copy, additions=None):
    """(text, parts): the draft built from `report`. Each part is a dict with `kind` ("copied", "separator" or
    "additions") and character offsets `start`/`end` into the text; a copied part also has the 1-based
    `from_line`/`to_line` it came from. Raises CopyError, having built nothing."""
    if not isinstance(report, str) or not report:
        raise CopyError("Rejected: the report is empty, so there is nothing to copy. Nothing was saved or shown.")
    if additions is not None and (not isinstance(additions, str) or not additions.strip()):
        raise CopyError("Rejected: additions must be text that is not only whitespace. Nothing was saved or shown.")
    lines, picked = selections(report, copy)
    spans = [("copied", report[lines[a][0]:lines[b][1]], a + 1, b + 1) for a, b in picked]
    if additions is not None:
        spans.append(("additions", additions, None, None))
    text, parts = "", []
    for kind, span, first, last in spans:
        if text:
            sep = "\n" if text.endswith("\n") else "\n\n"
            parts.append({"kind": "separator", "start": len(text), "end": len(text) + len(sep)})
            text += sep
        part = {"kind": kind, "start": len(text), "end": len(text) + len(span)}
        if kind == "copied":
            part.update(from_line=first, to_line=last)
        parts.append(part)
        text += span
    return text, parts
