#!/usr/bin/env python3
"""Tail a live Antigravity IDE conversation and print messages as they land.

Antigravity stores each conversation as a SQLite database that it writes to
while you chat. To notice those writes we do NOT hook the process, watch file
mtimes, or scan the table on a timer - we ask SQLite directly:

    PRAGMA data_version

That counter changes whenever *another* connection commits, which is exactly
what Antigravity is. It is O(1) - measured at ~6.7us on a 22 MB / 1276-row
database, identical to a 41-row one - so polling it several times a second is
free, and the steps table is only read after something has actually committed.

Two behaviours of this storage drive the design:

  * Rows are updated in place, not only appended. A pending command sits at
    status 9 and flips to 3 when you approve it, with no new row inserted, so
    tracking the highest idx alone would miss approvals resolving.

  * The databases run in WAL mode, so the .db file's mtime only moves on
    checkpoint - on this machine 85 of 87 -wal files were newer than their
    .db, one by roughly a month. Anything that ranks or polls by .db mtime is
    therefore looking at a stale clock.

The connection is held open across polls (data_version is compared against
what that connection last saw) but no transaction is ever left open, since a
long-lived reader on a WAL database blocks checkpointing and lets the -wal
grow without bound. Everything is opened read-only, so Antigravity is never
disturbed.

Message decoding is reused wholesale from antigravity_chat_decoder.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from antigravity_chat_decoder import (
    STEP_ROLES,
    UUID_RE,
    classify_step,
    decode_message,
    find_leaf,
    flatten,
)

DEFAULT_ROOTS = [
    Path.home() / ".gemini" / "antigravity-ide" / "conversations",
    Path.home() / ".gemini" / "antigravity" / "conversations",
]

# How far back to re-check rows for in-place status changes. Approvals resolve
# within a step or two of being raised; 30 is generous without being a scan.
STATUS_WINDOW = 30

# Antigravity's own step statuses. These are transient: a command observed here
# went 9 -> 2 -> 3 across an approval, and once finished every row in the file
# reads 3 - so a post-hoc decoder can never see 9 or 2, only a live watcher can.
# Only the two endpoints are named, since those are the ones actually confirmed;
# anything else is printed as a raw number rather than guessed at.
STATUS_AWAITING_APPROVAL = 9
STATUS_DONE = 3
STATUS_NAMES = {STATUS_AWAITING_APPROVAL: "awaiting approval", STATUS_DONE: "done"}

# step_type carrying "Allow running this command?" requests. Not in the decoder's
# STEP_ROLES table, so it is handled here rather than editing the shared decoder.
STEP_TYPE_COMMAND_APPROVAL = 21


def status_label(status: int) -> str:
    return STATUS_NAMES.get(status, f"status {status}")

ROLE_COLORS = {
    "user": "\033[96m",
    "assistant": "\033[92m",
    "tool_call": "\033[93m",
    "tool_result": "\033[90m",
    "tool_edit": "\033[95m",
    "tool_error": "\033[91m",
    "model_error": "\033[91m",
    "permission_request": "\033[93m",
    "command_approval": "\033[93m",
}
DIM = "\033[90m"
BOLD = "\033[1m"
RESET = "\033[0m"


class Palette:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.enabled and code else text


class Telemetry:
    """Counters for what the watcher itself is doing.

    The interesting number is `quiet_ratio`: the fraction of polls where
    data_version was unchanged and no table read happened at all. That is the
    whole justification for this design - if it isn't close to 1.0 on an idle
    session, the cheap-probe premise is wrong.
    """

    def __init__(self):
        self.started = time.monotonic()
        self.polls = 0
        self.commits = 0            # data_version actually moved
        self.rows_seen = 0
        self.status_changes = 0
        self.session_switches = 0
        self.decode_calls = 0
        self.decode_seconds = 0.0
        self.decode_max = 0.0
        self.read_seconds = 0.0     # time inside emit_changes/flush_settled
        self.rendered: dict[str, int] = {}

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.started

    def count_render(self, role: str) -> None:
        base = role.replace(" (updated)", "")
        self.rendered[base] = self.rendered.get(base, 0) + 1

    def time_decode(self, seconds: float) -> None:
        self.decode_calls += 1
        self.decode_seconds += seconds
        self.decode_max = max(self.decode_max, seconds)

    def snapshot(self) -> dict:
        up = max(self.uptime, 1e-9)
        quiet = self.polls - self.commits
        return {
            "uptime_s": round(self.uptime, 1),
            "polls": self.polls,
            "polls_per_s": round(self.polls / up, 2),
            "commits_detected": self.commits,
            "quiet_ratio": round(quiet / self.polls, 4) if self.polls else None,
            "rows_seen": self.rows_seen,
            "status_changes": self.status_changes,
            "session_switches": self.session_switches,
            "rendered": dict(sorted(self.rendered.items())),
            "decode_calls": self.decode_calls,
            "decode_ms_avg": round(self.decode_seconds / self.decode_calls * 1000, 2)
            if self.decode_calls else 0.0,
            "decode_ms_max": round(self.decode_max * 1000, 2),
            "db_read_ms_total": round(self.read_seconds * 1000, 1),
            "busy_ratio": round((self.read_seconds + self.decode_seconds) / up, 6),
        }

    def line(self) -> str:
        s = self.snapshot()
        rendered = " ".join(f"{k}={v}" for k, v in s["rendered"].items()) or "none"
        return (
            f"[telemetry] up {s['uptime_s']}s | polls {s['polls']} ({s['polls_per_s']}/s) | "
            f"commits {s['commits_detected']} | quiet {s['quiet_ratio']} | "
            f"rows {s['rows_seen']} | decode avg {s['decode_ms_avg']}ms max {s['decode_ms_max']}ms | "
            f"busy {s['busy_ratio']} | {rendered}"
        )


def session_activity(db: Path) -> float:
    """Most recent *write* time for a session.

    Uses .db and -wal, deliberately NOT -shm. In WAL mode commits land in -wal
    and the .db is only touched on checkpoint, so the .db alone lags by hours or
    days. But -shm is the shared-memory index: it is touched merely by a
    connection being open - Antigravity keeps many conversations open, and our
    own read-only connections touch it too. Measured here: 19 sessions had -shm
    touched within 120s while only 1 had a real -wal write, so including -shm
    makes nearly every idle session look live and the watcher flip-flops
    between them forever.
    """
    newest = 0.0
    for p in (db, Path(str(db) + "-wal")):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    return newest


def find_sessions() -> list[Path]:
    found: list[Path] = []
    for root in DEFAULT_ROOTS:
        if root.is_dir():
            found.extend(p for p in root.glob("*.db") if UUID_RE.match(p.stem))
    found.sort(key=session_activity, reverse=True)
    return found


def resolve_session(uuid: str | None) -> Path | None:
    sessions = find_sessions()
    if not sessions:
        return None
    if uuid:
        for p in sessions:
            if p.stem == uuid:
                return p
        return None
    return sessions[0]


def open_ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def clean_prompt(s: str) -> str:
    # 1. Remove <USER_REQUEST> tags
    s = re.sub(r"</?USER_REQUEST>", "", s, flags=re.IGNORECASE)
    # 2. Convert markdown links [text](url) -> text
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    # 3. Remove @[mention] tags if other text exists
    cleaned = re.sub(r"@\[[^\]]*\]", "", s)
    cleaned = re.sub(r"@\S+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned:
        return cleaned
    # If removing @[...] left nothing, extract basename or tag content
    m = re.search(r"@\[(.*?)\]", s)
    if m:
        val = m.group(1).strip()
        p = Path(val)
        return p.name if p.name else val
    return re.sub(r"\s+", " ", s).strip()


def get_session_title(db_path: Path) -> str | None:
    try:
        conn = open_ro(db_path)
        rows = conn.execute(
            "SELECT step_payload FROM steps WHERE step_type = 14 AND step_payload IS NOT NULL ORDER BY idx ASC LIMIT 5"
        ).fetchall()
        conn.close()
        for r in rows:
            parsed = decode_message(r["step_payload"])
            if not parsed:
                continue
            leaves = flatten(parsed)
            text = find_leaf(leaves, "f19.f2") or find_leaf(leaves, "f19.f3.f1")
            if text and isinstance(text, str):
                cleaned = clean_prompt(text)
                if cleaned:
                    return cleaned
    except Exception:
        pass
    return None



def data_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA data_version").fetchone()[0]


def step_time(leaves) -> str:
    """Best-effort clock for a step, from whichever Timestamp-shaped field the
    payload carries, converted to local time.

    The decoder yields those timestamps as UTC ISO strings. Printing them raw
    next to wall-clock lines makes a session look like it happened hours away
    from when it did (here UTC+5:30, so 19:47 UTC printed beside a 01:17 local
    line for the same moment), so normalize everything to local.
    """
    for leaf in leaves:
        if leaf["type"] == "timestamp":
            try:
                dt = datetime.fromisoformat(str(leaf["value"]))
                return dt.astimezone().strftime("%H:%M:%S")
            except ValueError:
                return str(leaf["value"])[11:19]
    return time.strftime("%H:%M:%S")


def describe(step_type: int, status: int, leaves, raw: bool) -> tuple[str, list[str]] | None:
    # Command-approval steps carry the command inside a JSON leaf rather than in
    # any of the text fields classify_step knows about, so pull it out directly.
    if step_type == STEP_TYPE_COMMAND_APPROVAL:
        body = []
        label = find_leaf(leaves, "f31") or find_leaf(leaves, "f30")
        if label:
            body.append(str(label))
        for leaf in leaves:
            if leaf["type"] == "json" and isinstance(leaf["value"], dict):
                cmd = leaf["value"].get("CommandLine")
                if cmd:
                    body.append(f"$ {cmd}")
                    break
        if status == STATUS_AWAITING_APPROVAL:
            body.append(">> AWAITING APPROVAL")
        return ("command_approval", body) if body else None

    # Assistant steps carry the reply in f20.f1 (mirrored in f20.f8) and the model's
    # internal reasoning in f20.f3. classify_step prefers f20.f3, which is right for a
    # full transcript dump but wrong for a live chat tail - it shows the thinking and
    # never the answer. Measured on one step: f20.f3 = 342 chars of "Generating the
    # Report", f20.f1 = the actual 5543-char report. So prefer the answer here, and
    # fall back to the reasoning only while the answer is still empty (mid-stream).
    if STEP_ROLES.get(step_type) == "assistant":
        answer = find_leaf(leaves, "f20.f1", value_type="text") or find_leaf(leaves, "f20.f8", value_type="text")
        thinking = find_leaf(leaves, "f20.f3", value_type="text")
        if answer:
            return "assistant", [str(answer)]
        if thinking:
            return "assistant_thinking", [str(thinking)]
        return None

    info = classify_step(step_type, leaves)
    if not info:
        if not raw:
            return None
        texty = [l for l in leaves if l["type"] in ("text", "json") and l["value"]]
        if not texty:
            return None
        return f"step_type_{step_type}", [f"{l['path']}: {str(l['value'])[:300]}" for l in texty[:6]]

    role = info["role"]

    # classify_step deliberately surfaces unrecognized step types rather than
    # dropping them, but several (e.g. 90) are pure bookkeeping whose only text
    # is the internal "<trajectory_id><cascade_id>" header. Keep them behind
    # --raw so the default view stays readable without hiding anything outright.
    if role.startswith("unknown_step_type_") and not raw:
        return None
    body: list[str] = []

    label = info.get("label")
    if label:
        body.append(str(label))
    for key in ("text", "instruction", "path", "content", "snippet", "plan", "detail"):
        val = info.get(key)
        if val:
            text = str(val).strip()
            if text and text not in body:
                body.append(text)

    if status == STATUS_AWAITING_APPROVAL:
        body.append(">> AWAITING APPROVAL")

    return role, body


def render(pal: Palette, when: str, role: str, body: list[str], args) -> None:
    color = ROLE_COLORS.get(role, "")
    # Tool steps carry whole files (a single read can be 700+ lines), so they get
    # a tighter cap than what someone actually said.
    cap = args.max_lines if role in ("user", "assistant") else args.tool_lines
    print(f"{pal(f'[{when}]', DIM)} {pal(role.upper(), BOLD + color)}")
    for chunk in body:
        lines = str(chunk).splitlines() or [""]
        for line in lines[:cap]:
            print(f"  {line}")
        if len(lines) > cap:
            print(pal(f"  ... +{len(lines) - cap} more lines", DIM))
    print()


def write_json_record(args, record: dict) -> None:
    """Append one full, untruncated record to the JSONL sink.

    The console view deliberately clips long messages (a single file read can be
    700+ lines), so this is where the complete text lives. JSONL rather than one
    big JSON array so it can be appended to a running file and still be valid.
    """
    if not args.json_out:
        return
    try:
        with open(args.json_out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        print(f"[warn] could not write {args.json_out}: {exc}", file=sys.stderr)


def decode_timed(payload, tel: Telemetry):
    t0 = time.perf_counter()
    parsed = decode_message(payload)
    tel.time_decode(time.perf_counter() - t0)
    return parsed


def emit_changes(conn, state: dict, pal: Palette, args, tel: Telemetry) -> None:
    """Print rows added since last poll, plus any status transition on recent
    rows. Both are needed: approvals resolve by mutating an existing row."""
    started = time.perf_counter()
    cur = conn.cursor()

    # 1. New rows.
    rows = cur.execute(
        "SELECT idx, step_type, status, step_payload FROM steps WHERE idx > ? ORDER BY idx ASC",
        (state["last_idx"],),
    ).fetchall()
    tel.rows_seen += len(rows)

    for row in rows:
        state["last_idx"] = max(state["last_idx"], row["idx"])
        state["status"][row["idx"]] = row["status"]
        payload = row["step_payload"]
        if not payload:
            continue
        parsed = decode_timed(payload, tel)
        if not parsed:
            continue
        leaves = flatten(parsed)
        described = describe(row["step_type"], row["status"], leaves, args.raw)
        if not described:
            continue
        role, body = described
        tel.count_render(role)
        when = step_time(leaves)
        render(pal, when, role, body, args)
        write_json_record(args, {
            "time": when, "session": state.get("session"), "idx": row["idx"],
            "step_type": row["step_type"], "status": row["status"], "role": role,
            "updated": False, "body": body,
        })

    # 2. Rows that changed in place. An assistant reply is written incrementally into
    # an existing row while it streams, so a row printed the moment it appeared holds
    # only a fragment; without this it would never show the finished answer.
    floor = max(0, state["last_idx"] - STATUS_WINDOW)
    # LENGTH() only, never the payload itself: this runs on every commit and only
    # needs to spot that a row's size moved. Selecting step_payload here pulled
    # ~296 KB of blobs per tick on a large session for nothing (8x slower) - the
    # bytes are fetched once in flush_settled, and only for rows that settled.
    for row in cur.execute(
        "SELECT idx, step_type, status, LENGTH(step_payload) AS size "
        "FROM steps WHERE idx >= ? ORDER BY idx ASC",
        (floor,),
    ).fetchall():
        prev_size = state["size"].get(row["idx"])
        state["size"][row["idx"]] = row["size"]
        if prev_size is not None and row["size"] != prev_size:
            # Don't print yet. A streaming reply grows on nearly every poll, so
            # rendering on each change would reprint the whole message dozens of
            # times. Mark it and let flush_settled() print once it stops growing.
            state["pending"][row["idx"]] = time.monotonic()

        before = state["status"].get(row["idx"])
        if before is not None and before != row["status"]:
            state["status"][row["idx"]] = row["status"]
            tel.status_changes += 1
            # Report every transition, not only those leaving "awaiting approval":
            # an approved command moves 9 -> 2 -> 3, so watching only for departures
            # from 9 catches the approval but silently drops the completion.
            print(pal(f"  -> step {row['idx']}: {status_label(before)} -> {status_label(row['status'])}", DIM))
            print()
            write_json_record(args, {
                "time": datetime.now().astimezone().strftime("%H:%M:%S"),
                "session": state.get("session"), "idx": row["idx"],
                "step_type": row["step_type"], "role": "status_change",
                "status_from": before, "status_to": row["status"],
                "status_from_label": status_label(before),
                "status_to_label": status_label(row["status"]),
            })
        elif before is None:
            state["status"][row["idx"]] = row["status"]

    # Keep the status map from growing forever on long sessions.
    if len(state["status"]) > 4 * STATUS_WINDOW:
        for idx in sorted(state["status"])[:-2 * STATUS_WINDOW]:
            del state["status"][idx]
            state["size"].pop(idx, None)
            state["pending"].pop(idx, None)

    tel.read_seconds += time.perf_counter() - started


def flush_settled(conn, state: dict, pal: Palette, args, tel: Telemetry) -> None:
    """Render rows that changed in place and have since stopped changing.

    Called every poll, not only when data_version moves: the whole point is to
    fire once a row has been quiet for a moment, which is the absence of change.
    """
    if not state["pending"]:
        return
    now = time.monotonic()
    ready = [idx for idx, changed_at in state["pending"].items() if now - changed_at >= args.settle]
    for idx in sorted(ready):
        del state["pending"][idx]
        row = conn.execute(
            "SELECT idx, step_type, status, step_payload FROM steps WHERE idx = ?", (idx,)
        ).fetchone()
        if not row or not row["step_payload"]:
            continue
        parsed = decode_timed(row["step_payload"], tel)
        if not parsed:
            continue
        leaves = flatten(parsed)
        described = describe(row["step_type"], row["status"], leaves, args.raw)
        if not described:
            continue
        role, body = described
        tel.count_render(role)
        # Use the step's own clock, not wall time - the two disagree by the UTC
        # offset and printing both styles makes one conversation look like two.
        when = step_time(leaves)
        render(pal, when, f"{role} (updated)", body, args)
        write_json_record(args, {
            "time": when, "session": state.get("session"), "idx": row["idx"],
            "step_type": row["step_type"], "status": row["status"], "role": role,
            "updated": True, "body": body,
        })


def prime(conn, state: dict, tail: int, pal: Palette, args, tel: Telemetry) -> None:
    """Seed state from the existing conversation, optionally showing the tail."""
    cur = conn.cursor()
    row = cur.execute("SELECT COALESCE(MAX(idx), -1) AS m FROM steps").fetchone()
    highest = row["m"]

    start = -1 if tail <= 0 else max(-1, highest - tail)
    state["last_idx"] = start
    for r in cur.execute("SELECT idx, status FROM steps").fetchall():
        state["status"][r["idx"]] = r["status"]

    if tail > 0:
        emit_changes(conn, state, pal, args, tel)
    state["last_idx"] = max(state["last_idx"], highest)


def watch(args) -> int:
    pal = Palette(enabled=not args.no_color and sys.stdout.isatty())

    db = resolve_session(args.session)
    if db is None:
        print("No Antigravity conversation databases found." if not args.session
              else f"No session matching {args.session!r}.", file=sys.stderr)
        return 1

    conn = open_ro(db)
    state = {"last_idx": -1, "status": {}, "size": {}, "pending": {}, "session": db.stem}
    tel = Telemetry()

    print(pal(f"watching {db.stem}", BOLD))
    print(pal(f"  {db}", DIM))
    print(pal(f"  polling PRAGMA data_version every {args.interval}s - Ctrl+C to stop", DIM))
    if args.json_out:
        print(pal(f"  full untruncated messages -> {Path(args.json_out).resolve()}", DIM))
    print()

    prime(conn, state, args.tail, pal, args, tel)
    last_dv = data_version(conn)
    last_scan = time.monotonic()
    last_stats = time.monotonic()

    try:
        while True:
            time.sleep(args.interval)
            tel.polls += 1

            dv = data_version(conn)
            if dv != last_dv:
                last_dv = dv
                tel.commits += 1
                emit_changes(conn, state, pal, args, tel)

            # Runs every poll, including quiet ones - a row is "settled" precisely
            # when nothing has changed for a while, which no dv event can signal.
            flush_settled(conn, state, pal, args, tel)

            if args.stats and time.monotonic() - last_stats >= args.stats:
                last_stats = time.monotonic()
                print(pal(tel.line(), DIM))
                print()

            # A brand-new chat writes to a different file, which this connection
            # can never see - rescan occasionally and follow the newest session.
            if not args.session and time.monotonic() - last_scan >= args.rescan:
                last_scan = time.monotonic()
                newest = resolve_session(None)
                # Only follow a session that is decisively more recent. Without
                # this margin, two sessions written in the same second trade
                # places on every rescan and the watcher never settles.
                if (newest and newest != db
                        and session_activity(newest) > session_activity(db) + args.switch_margin):
                    print(pal(f"--- switching to new session {newest.stem} ---", BOLD))
                    print()
                    conn.close()
                    db = newest
                    conn = open_ro(db)
                    state = {"last_idx": -1, "status": {}, "size": {}, "pending": {}, "session": db.stem}
                    tel.session_switches += 1
                    prime(conn, state, 0, pal, args, tel)
                    last_dv = data_version(conn)
    except KeyboardInterrupt:
        print(pal("\nstopped", DIM))
    finally:
        conn.close()
        print(pal(tel.line(), DIM))
        if args.stats_file:
            snap = tel.snapshot()
            snap["session"] = db.stem
            snap["ended_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            Path(args.stats_file).write_text(json.dumps(snap, indent=2), encoding="utf-8")
            print(pal(f"telemetry written to {args.stats_file}", DIM))
    return 0


def list_sessions(args) -> int:
    sessions = find_sessions()
    if not sessions:
        print("No Antigravity conversation databases found.", file=sys.stderr)
        return 1
    now = time.time()
    for p in sessions[: args.limit]:
        age_min = (now - session_activity(p)) / 60
        try:
            conn = open_ro(p)
            rows = conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
            conn.close()
        except sqlite3.Error:
            rows = -1
        title = get_session_title(p)
        title_str = ""
        if title:
            max_len = 45
            if len(title) > max_len:
                title_str = f'  "{title[:max_len - 3]}..."'
            else:
                title_str = f'  "{title}"'
        print(f"{p.stem}  {rows:>5} steps  {age_min:7.1f}m ago{title_str}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--session", help="conversation uuid to follow (default: most recently active)")
    ap.add_argument("--list", action="store_true", help="list sessions and exit")
    ap.add_argument("--limit", type=int, default=20, help="how many sessions --list shows")
    ap.add_argument("--interval", type=float, default=0.25, help="seconds between data_version polls")
    ap.add_argument("--rescan", type=float, default=5.0, help="seconds between checks for a newer session")
    ap.add_argument("--switch-margin", type=float, default=10.0,
                    help="seconds a rival session must be newer by before following it")
    ap.add_argument("--tail", type=int, default=5, help="steps of existing history to show on start (0 = none)")
    ap.add_argument("--max-lines", type=int, default=20, help="max lines per user/assistant chunk")
    ap.add_argument("--tool-lines", type=int, default=6, help="max lines per tool chunk (file dumps)")
    ap.add_argument("--settle", type=float, default=1.5,
                    help="seconds a streaming message must be quiet before it is reprinted")
    ap.add_argument("--json-out", default="antigravity_watch.jsonl",
                    help="append full untruncated messages here as JSONL "
                         "(default: antigravity_watch.jsonl in the working dir; '' to disable)")
    ap.add_argument("--stats", type=float, default=0.0,
                    help="print a telemetry line every N seconds (0 = only on exit)")
    ap.add_argument("--stats-file", help="write telemetry JSON here on exit")
    ap.add_argument("--raw", action="store_true", help="also dump steps whose type has no known shape")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI color")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        # line_buffering so output still appears promptly when piped or redirected -
        # without it Python block-buffers a non-tty and a tail-style tool looks dead.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    return list_sessions(args) if args.list else watch(args)


if __name__ == "__main__":
    sys.exit(main())
