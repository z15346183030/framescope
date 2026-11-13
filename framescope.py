#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""framescope - show the local source behind each frame of a pasted traceback.

Read-only, standard library only, one job: given a traceback captured on another
machine or from another build, find the matching file under a local source root,
print the lines around each frame, verify those lines against the source text the
interpreter printed, and state exactly why any frame cannot be shown.
"""

import argparse
import json
import os
import re
import sys

__version__ = "1.0.0"

FRAME_RE = re.compile(r'File "([^"]+)", line (\d+)(?:, in (.*?))?\s*$')
HEADER_RE = re.compile(r"^\s*Traceback \(most recent call last\):\s*$")
CHAIN_RE = re.compile(
    r"^\s*(?:The above exception was the direct cause of the following exception:"
    r"|During handling of the above exception, another exception occurred:)\s*$"
)
REPEAT_RE = re.compile(r"^\s*\[Previous line repeated (\d+) more times\]\s*$")
EXC_RE = re.compile(r"^\s*[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt)\b[^:]*:")
FRAME_START_RE = re.compile(r'^\s*File "')
CARET_RE = re.compile(r"^\s*[\s~^]*\^[\s~^]*$")
DRIVE_RE = re.compile(r"^[A-Za-z]:")

SKIP_DIRS = frozenset(
    (".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
     "env", "dist", "build", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
     ".idea", ".vscode", "target")
)
MAX_FILES = 50000

STATUS_OK = "ok"
STATUS_NO_ECHO = "no-echo"
STATUS_STALE = "stale"
STATUS_MISSING = "missing"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_BAD_LINE = "bad-line"
STATUS_SKIP = "skip"
BAD_STATUSES = frozenset((STATUS_STALE, STATUS_MISSING, STATUS_AMBIGUOUS, STATUS_BAD_LINE))
ALL_STATUSES = (
    STATUS_OK, STATUS_NO_ECHO, STATUS_STALE, STATUS_MISSING,
    STATUS_AMBIGUOUS, STATUS_BAD_LINE, STATUS_SKIP,
)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def _indent(line):
    return len(line) - len(line.lstrip(" \t"))


def parse_text(text):
    """Split traceback text into stacks of frames.

    A frame keeps the file, the line, the function, the source line the
    interpreter echoed under it, and the caret line when there is one. Anything
    that is not a frame becomes a note on the stack it belongs to.
    """
    raw = text.splitlines()
    stacks = []
    cur = None
    pending_notes = []
    n = len(raw)

    def ensure_stack():
        nonlocal cur, pending_notes
        if cur is None:
            cur = {"index": len(stacks) + 1, "frames": [], "notes": pending_notes}
            stacks.append(cur)
            pending_notes = []
        return cur

    i = 0
    while i < n:
        line = raw[i]

        if HEADER_RE.match(line):
            cur = {"index": len(stacks) + 1, "frames": [], "notes": pending_notes}
            stacks.append(cur)
            pending_notes = []
            i += 1
            continue

        m = FRAME_RE.search(line)
        if m:
            stack = ensure_stack()
            frame = {
                "file": m.group(1),
                "line": int(m.group(2)),
                "function": (m.group(3) or "").strip(),
                "echo": None,
                "caret": None,
            }
            base = _indent(line)
            j = i + 1
            if j < n:
                nxt = raw[j]
                if (nxt.strip()
                        and _indent(nxt) > base
                        and not FRAME_START_RE.match(nxt)
                        and not HEADER_RE.match(nxt)
                        and not CHAIN_RE.match(nxt)
                        and not REPEAT_RE.match(nxt)
                        and not EXC_RE.match(nxt)):
                    frame["echo"] = nxt
                    k = j + 1
                    if k < n and CARET_RE.match(raw[k]):
                        frame["caret"] = raw[k]
            stack["frames"].append(frame)
            i += 1
            continue

        if CHAIN_RE.match(line):
            # A chain sentence introduces the stack that follows it.
            pending_notes.append(line.strip())
            i += 1
            continue

        rm = REPEAT_RE.match(line)
        if rm:
            ensure_stack()["notes"].append(
                "previous line repeated %s more times" % rm.group(1)
            )
            i += 1
            continue

        i += 1

    if pending_notes:
        if stacks:
            stacks[-1]["notes"].extend(pending_notes)
        else:
            stacks.append({"index": 1, "frames": [], "notes": pending_notes})
    return stacks


# --------------------------------------------------------------------------- #
# source index and path resolution
# --------------------------------------------------------------------------- #

def _parts(path):
    """Turn any log path into clean components used only as lookup keys."""
    s = path.replace("\\", "/")
    s = DRIVE_RE.sub("", s)
    return [p for p in s.split("/") if p and p != "."]


def _is_inside(root, absolute):
    try:
        return os.path.commonpath((root, absolute)) == root
    except (ValueError, TypeError):
        return False


def build_index(root, cap=MAX_FILES):
    """Index every file under root by each of its path suffixes.

    Only real files are indexed, and every lookup key is a suffix of a real
    relative path. A path from the log is never joined onto root directly, so a
    log entry cannot point outside the root.
    """
    abs_root = os.path.abspath(root)
    by_suffix = {}
    files = []
    truncated = False
    if not os.path.isdir(abs_root):
        raise ValueError("source root is not a directory: %s" % root)

    # A log path normally carries the checkout directory name as its last
    # component before the file, e.g. "/work/cpython/Lib/glob.py" for a root
    # named "Lib". Local paths are relative to that root, so registering each
    # suffix once more with the root name in front lets the longer suffix win
    # instead of falling back to a bare basename that may be shared.
    root_name = os.path.basename(abs_root.rstrip(os.sep)).lower()

    def register(key, rel):
        bucket = by_suffix.get(key)
        if bucket is None:
            by_suffix[key] = [rel]
        elif rel not in bucket:
            bucket.append(rel)

    for dirpath, dirnames, filenames in os.walk(abs_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, abs_root).replace(os.sep, "/")
            parts = rel.split("/")
            files.append(rel)
            if len(files) >= cap:
                truncated = True
            for k in range(1, len(parts) + 1):
                register("/".join(parts[-k:]).lower(), rel)
            if root_name:
                register(root_name + "/" + rel.lower(), rel)
        if truncated:
            break

    return {"root": abs_root, "by_suffix": by_suffix, "files": files,
            "truncated": truncated, "cap": cap}


def resolve(frame, index):
    """Find the local file for a frame, longest path suffix first."""
    path = frame["file"]
    if path.startswith("<") and path.endswith(">"):
        return {"resolved": None, "status": STATUS_SKIP, "dropped": 0,
                "candidates": [], "hits": [], "via": "none",
                "reason": "virtual filename, no file on disk"}

    root = index["root"]
    exact = path if os.path.isabs(path) else os.path.join(root, path)
    exact_abs = os.path.abspath(os.path.normpath(exact))
    if os.path.isfile(exact_abs) and _is_inside(root, exact_abs):
        rel = os.path.relpath(exact_abs, root).replace(os.sep, "/")
        return {"resolved": rel, "status": STATUS_OK, "dropped": 0,
                "candidates": [{"path": path, "hits": 1}], "hits": [rel],
                "via": "exact path under root",
                "reason": "exact path under root"}

    parts = _parts(path)
    candidates = []
    for k in range(len(parts), 0, -1):
        suffix = "/".join(parts[-k:])
        hits = index["by_suffix"].get(suffix.lower(), ())
        candidates.append({"path": suffix, "hits": len(hits)})
        if len(hits) == 1:
            return {"resolved": hits[0], "status": STATUS_OK,
                    "dropped": len(parts) - k, "candidates": candidates,
                    "hits": list(hits),
                    "via": "suffix, %d leading part(s) dropped" % (len(parts) - k),
                    "reason": "suffix match, %d leading part(s) dropped"
                              % (len(parts) - k)}
        if len(hits) > 1:
            return {"resolved": None, "status": STATUS_AMBIGUOUS,
                    "dropped": len(parts) - k, "candidates": candidates,
                    "hits": sorted(hits)[:8], "via": "ambiguous",
                    "reason": "suffix %r matches %d files" % (suffix, len(hits))}

    return {"resolved": None, "status": STATUS_MISSING, "dropped": 0,
            "candidates": candidates, "hits": [], "via": "none",
            "reason": "no file under root ends with any suffix of the log path"}


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #

def _read_lines(path):
    with open(path, "rb") as handle:
        data = handle.read()
    return data.decode("utf-8-sig", errors="replace").splitlines()


def analyze(stacks, index, context=2):
    """Attach resolution, verification and surrounding lines to every frame."""
    root = index["root"]
    for stack in stacks:
        for frame in stack["frames"]:
            res = resolve(frame, index)
            frame.update(res)
            frame["echo_text"] = frame["echo"].strip() if frame["echo"] else None
            frame["local"] = None
            frame["relocations"] = []
            frame["context"] = []
            frame["caret_text"] = None
            frame["caret_body"] = None

            if frame["status"] in (STATUS_SKIP, STATUS_MISSING, STATUS_AMBIGUOUS):
                continue

            full = os.path.join(root, frame["resolved"])
            try:
                lines = _read_lines(full)
            except OSError as exc:
                frame["status"] = STATUS_MISSING
                frame["reason"] = "file could not be read: %s" % exc.strerror
                continue

            target = frame["line"]
            if target < 1 or target > len(lines):
                frame["status"] = STATUS_BAD_LINE
                frame["reason"] = ("line %d is outside the local file (%d lines)"
                                   % (target, len(lines)))
                continue

            local = lines[target - 1]
            frame["local"] = local
            echo = frame["echo_text"]

            if echo is None:
                frame["status"] = STATUS_NO_ECHO
                frame["reason"] = "log carried no source line; local lines shown as-is"
            elif echo == local.strip():
                frame["status"] = STATUS_OK
                frame["reason"] = "local line %d matches the line printed in the log" % target
            else:
                frame["status"] = STATUS_STALE
                reloc = [i + 1 for i, text in enumerate(lines)
                         if i + 1 != target and text.strip() == echo]
                frame["relocations"] = reloc[:5]
                if reloc:
                    frame["reason"] = ("local line %d differs from the log; "
                                       "the printed line appears at line %s"
                                       % (target, ", ".join(str(x) for x in reloc[:5])))
                else:
                    frame["reason"] = ("local line %d differs from the log; "
                                       "the printed line is not in this file" % target)

            caret = frame["caret"]
            if caret:
                off = _indent(frame["echo"])
                piece = caret[off:] if len(caret) > off else caret.lstrip()
                frame["caret_body"] = piece
                # The caret belongs to the line printed in the log. It only
                # belongs under the local line when both lines are identical.
                if frame["status"] == STATUS_OK:
                    shift = len(local) - len(local.lstrip())
                    frame["caret_text"] = " " * shift + piece

            lo = max(1, target - context)
            hi = min(len(lines), target + context)
            frame["context"] = [
                {"n": i, "text": lines[i - 1], "target": i == target}
                for i in range(lo, hi + 1)
            ]
    return stacks


def summarise(stacks):
    counts = dict((s, 0) for s in ALL_STATUSES)
    total = 0
    for stack in stacks:
        for frame in stack["frames"]:
            counts[frame["status"]] += 1
            total += 1
    return {"stacks": len(stacks), "frames": total, "by_status": counts}


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def _short_reason(frame):
    return frame["reason"]


def format_human(stacks, index, context, explain=False):
    out = []
    out.append("framescope  root=%s  context=%d" % (index["root"], context))
    if index["truncated"]:
        out.append("note: index capped at %d files; results may be incomplete"
                   % index["cap"])
    out.append("")

    for stack in stacks:
        if len(stacks) > 1:
            out.append("stack %d" % stack["index"])
        for note in stack["notes"]:
            out.append("  note: %s" % note)

        pos = 0
        for frame in stack["frames"]:
            pos += 1
            head = "[%d] %s:%d" % (pos, frame["file"], frame["line"])
            if frame["function"]:
                head += " in %s" % frame["function"]
            out.append("  " + head)

            if frame["status"] in (STATUS_SKIP, STATUS_MISSING,
                                   STATUS_AMBIGUOUS, STATUS_BAD_LINE):
                out.append("      %-9s %s" % (frame["status"], _short_reason(frame)))
                if frame["status"] == STATUS_AMBIGUOUS:
                    for hit in frame["hits"]:
                        out.append("        candidate  %s" % hit)
                if explain:
                    _append_candidates(out, frame)
                out.append("")
                continue

            out.append("      -> %-28s %s" % (frame["resolved"], frame["via"]))
            ctx = frame["context"]
            width = max((len(str(c["n"])) for c in ctx), default=1)
            for item in ctx:
                marker = ">" if item["target"] else " "
                out.append("  %s %s | %s"
                           % (marker, str(item["n"]).rjust(width + 2), item["text"]))
                if item["target"] and frame["caret_text"]:
                    out.append("    %s | %s"
                               % (" " * (width + 2), frame["caret_text"]))
            out.append("      %-9s %s" % (frame["status"], _short_reason(frame)))

            if explain:
                out.append("      echo    %s" % (frame["echo_text"]
                                                 if frame["echo_text"] is not None
                                                 else "(not present)"))
                if frame["status"] == STATUS_STALE and frame["caret_body"]:
                    out.append("      caret   %s" % frame["caret_body"])
                out.append("      local   %s" % (frame["local"].strip()
                                                 if frame["local"] is not None
                                                 else "(not present)"))
                if frame["relocations"]:
                    out.append("      also at lines %s"
                               % ", ".join(str(x) for x in frame["relocations"]))
                _append_candidates(out, frame)
            out.append("")

    counts = summarise(stacks)["by_status"]
    out.append("summary  %d stack(s) · %d frame(s) · %d ok · %d no-echo · "
               "%d stale · %d skip · %d missing · %d ambiguous · %d bad-line"
               % (len(stacks), sum(counts.values()), counts[STATUS_OK],
                  counts[STATUS_NO_ECHO], counts[STATUS_STALE], counts[STATUS_SKIP],
                  counts[STATUS_MISSING], counts[STATUS_AMBIGUOUS],
                  counts[STATUS_BAD_LINE]))
    return "\n".join(out)


def _append_candidates(out, frame):
    cands = frame["candidates"]
    if not cands:
        return
    out.append("      candidates (longest suffix first):")
    for c in cands:
        out.append("        %-46s %d hit(s)" % (c["path"], c["hits"]))


def format_json(stacks, index, context):
    s = summarise(stacks)
    payload = {
        "tool": "framescope",
        "version": __version__,
        "root": index["root"],
        "context": context,
        "index": {"files": len(index["files"]), "truncated": index["truncated"]},
        "stacks": [
            {
                "index": stack["index"],
                "notes": stack["notes"],
                "frames": [
                    {
                        "file": f["file"],
                        "line": f["line"],
                        "function": f["function"],
                        "status": f["status"],
                        "resolved": f["resolved"],
                        "reason": f["reason"],
                        "echo": f["echo_text"],
                        "local": f["local"],
                        "caret": f["caret_text"],
                        "relocations": f["relocations"],
                        "candidates": f["candidates"],
                        "hits": f["hits"],
                        "context": f["context"],
                    }
                    for f in stack["frames"]
                ],
            }
            for stack in stacks
        ],
        "summary": {"stacks": s["stacks"], "frames": s["frames"],
                    "by_status": s["by_status"]},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #

def _decode(data):
    return data.decode("utf-8-sig", errors="replace")


def _setup_streams():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="framescope",
        description="Show the local source behind each frame of a pasted traceback.",
    )
    parser.add_argument("log", nargs="?", default="-",
                        help="traceback file, or - for stdin (default)")
    parser.add_argument("--root", default=".",
                        help="local source root (default: current directory)")
    parser.add_argument("--context", "-C", type=int, default=2, metavar="N",
                        help="lines of context on each side (default: 2)")
    parser.add_argument("--explain", action="store_true",
                        help="print the candidate ladder and the line check")
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON document instead of the text report")
    parser.add_argument("--version", action="version",
                        version="framescope " + __version__)
    args = parser.parse_args(argv)

    if not 0 <= args.context <= 20:
        parser.error("--context must be between 0 and 20")

    try:
        if args.log == "-":
            data = sys.stdin.buffer.read()
        else:
            with open(args.log, "rb") as handle:
                data = handle.read()
    except OSError as exc:
        print("framescope: cannot read %s: %s" % (args.log, exc.strerror),
              file=sys.stderr)
        return 2
    text = _decode(data)

    try:
        index = build_index(args.root)
    except (ValueError, OSError) as exc:
        print("framescope: %s" % exc, file=sys.stderr)
        return 2

    stacks = parse_text(text)
    if not stacks or not any(s["frames"] for s in stacks):
        print("framescope: no traceback frames found in the input", file=sys.stderr)
        return 2

    analyze(stacks, index, args.context)
    if args.json:
        print(format_json(stacks, index, args.context))
    else:
        print(format_human(stacks, index, args.context, args.explain))

    counts = summarise(stacks)["by_status"]
    return 1 if any(counts[s] for s in BAD_STATUSES) else 0


if __name__ == "__main__":
    _setup_streams()
    sys.exit(main())
