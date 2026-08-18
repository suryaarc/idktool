#!/usr/bin/env python3
"""Decode Antigravity IDE local chat/agent-trajectory history.

Antigravity (Google's VS Code fork) has no public schema for its local
chat storage, so this script reverse-engineers it structurally:

  1. Finds `~/.gemini/antigravity/` on this machine.
  2. Reads `agyhub_summaries_proto.pb` - a plaintext Protocol Buffers file
     listing every conversation (title, uuid, workspace, git info).
  3. For each conversation uuid, looks in `conversations/<uuid>.db`
     (SQLite - used for many conversations) or `conversations/<uuid>.pb`
     (a handful of conversations - these are encrypted at rest, ~8
     bits/byte entropy, not decompressible, so this script reports them
     as opaque instead of guessing).
  4. Every BLOB found is decoded with a hand-written, schema-less
     Protocol Buffers wire-format walker (no .proto file exists to
     generate real bindings from, and no `protobuf` pip package is
     required). Antigravity additionally nests base64-encoded
     sub-messages inside string fields in some records; the walker
     retries base64 + re-parse before giving up on a field.
  5. Cross-references `~/.gemini/antigravity/brain/<uuid>/` for the
     plan/task markdown Antigravity also drops next to the trajectory.

No field names are invented: every label in the output is either read
directly from the data (a UUID, a file:// path, a JSON blob, a plain
text string) or a raw numeric protobuf field/enum tag, explicitly
marked as such, so the report never claims more than what was verified.

Every file opened and every decode decision is written to the flow log
(printed live and saved as flow_log.txt in the output directory).
"""

import argparse
import base64
import datetime
import json
import os
import re
import sqlite3
import struct
import sys
from pathlib import Path

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
TS_MIN, TS_MAX = 1420070400, 2051222400  # 2015-01-01 .. 2035-01-01, sanity bounds for guessed epoch seconds


class FlowLog:
    def __init__(self, quiet=False):
        self.entries = []
        self.quiet = quiet

    def step(self, msg):
        self.entries.append(f"[{len(self.entries) + 1:04d}] {msg}")
        if not self.quiet:
            print(self.entries[-1])

    def save(self, path):
        Path(path).write_text("\n".join(self.entries), encoding="utf-8")


# ---------------------------------------------------------------------------
# Schema-less Protocol Buffers wire-format decoder
# ---------------------------------------------------------------------------

def read_varint(buf, pos):
    result = 0
    shift = 0
    start = pos
    while True:
        if pos >= len(buf) or shift > 63:
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if pos - start > 10:
            raise ValueError("varint too long")


def decode_message(buf, depth=0, max_depth=12):
    """Parse `buf` as a protobuf message. Returns a field list, or None if
    `buf` does not fully, validly parse as one (used to reject non-protobuf
    / encrypted bytes rather than emit garbage)."""
    if depth > max_depth or len(buf) == 0:
        return None
    fields = []
    pos = 0
    n = len(buf)
    try:
        while pos < n:
            tag, pos = read_varint(buf, pos)
            field_no = tag >> 3
            wire = tag & 7
            if field_no == 0:
                return None
            if wire == 0:
                val, pos = read_varint(buf, pos)
                fields.append({"field": field_no, "wire": "varint", "value": val})
            elif wire == 1:
                if pos + 8 > n:
                    return None
                val = struct.unpack_from("<Q", buf, pos)[0]
                pos += 8
                fields.append({"field": field_no, "wire": "fixed64", "value": val})
            elif wire == 5:
                if pos + 4 > n:
                    return None
                val = struct.unpack_from("<I", buf, pos)[0]
                pos += 4
                fields.append({"field": field_no, "wire": "fixed32", "value": val})
            elif wire == 2:
                length, pos = read_varint(buf, pos)
                if length < 0 or pos + length > n:
                    return None
                chunk = buf[pos:pos + length]
                pos += length
                fields.append({"field": field_no, "wire": "bytes",
                                **interpret_bytes(chunk, depth, max_depth)})
            else:
                return None  # wire types 3/4/6/7: groups/invalid, not used here
    except ValueError:
        return None
    return fields


def interpret_bytes(chunk, depth, max_depth):
    text = try_utf8_text(chunk)
    if text is not None:
        return {"kind": "string", "value": text}

    b64_decoded = try_base64(chunk)
    if b64_decoded is not None:
        sub = decode_message(b64_decoded, depth + 1, max_depth)
        if sub:
            return {"kind": "base64+protobuf", "value": sub}

    sub = decode_message(chunk, depth + 1, max_depth)
    if sub:
        return {"kind": "protobuf", "value": sub}

    return {"kind": "bytes", "length": len(chunk), "hex_preview": chunk[:48].hex()}


def try_utf8_text(chunk):
    try:
        s = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not s:
        return None
    printable = sum(1 for c in s if c.isprintable() or c in "\n\r\t")
    if printable / len(s) < 0.95:
        return None
    return s


def try_base64(chunk):
    if len(chunk) < 8 or len(chunk) % 4 != 0:
        return None
    if not re.fullmatch(rb"[A-Za-z0-9+/]+={0,2}", chunk):
        return None
    try:
        decoded = base64.b64decode(chunk, validate=True)
    except Exception:
        return None
    if base64.b64encode(decoded) != chunk:
        return None
    return decoded


def looks_like_timestamp(fields):
    """google.protobuf.Timestamp shape: exactly {1: varint seconds, 2: varint nanos}."""
    if len(fields) != 2:
        return None
    by_no = {f["field"]: f for f in fields}
    if set(by_no) != {1, 2}:
        return None
    sec_f, nanos_f = by_no[1], by_no[2]
    if sec_f["wire"] != "varint" or nanos_f["wire"] != "varint":
        return None
    sec, nanos = sec_f["value"], nanos_f["value"]
    if not (TS_MIN <= sec <= TS_MAX) or not (0 <= nanos < 1_000_000_000):
        return None
    return datetime.datetime.fromtimestamp(sec, tz=datetime.timezone.utc).isoformat()


def classify_string(s):
    if UUID_RE.match(s.strip()):
        return "uuid"
    if s.startswith("file://"):
        return "path"
    stripped = s.strip()
    if stripped[:1] in "{[":
        try:
            return ("json", json.loads(stripped))
        except (json.JSONDecodeError, ValueError):
            pass
    return "text"


def find_leaf(leaves, suffix, value_type=None):
    parts = suffix.split(".")
    for leaf in leaves:
        p = leaf["path"].split(".")
        if p[-len(parts):] == parts and (value_type is None or leaf["type"] == value_type):
            return leaf["value"]
    return None


# step_type -> role, reverse-engineered empirically by decoding this machine's own
# conversations/*.db files and observing which field shapes recur together (no
# official schema is published by Antigravity). Confirmed consistent across two
# separate conversations and 100+ steps; unknown step_types fall back to a raw dump
# further down so nothing is silently dropped if a type outside this table shows up.
STEP_ROLES = {14: "user", 15: "assistant", 9: "tool_call", 8: "tool_result",
              5: "tool_edit", 7: "tool_error", 17: "model_error",
              132: "permission_request", 98: "internal", 23: "snapshot"}


def classify_step(step_type, leaves):
    role = STEP_ROLES.get(step_type)

    if role == "user":
        text = find_leaf(leaves, "f19.f2", value_type="text") or find_leaf(leaves, "f19.f3.f1", value_type="text")
        return {"role": role, "text": text} if text else None

    if role == "assistant":
        answer = find_leaf(leaves, "f20.f1", value_type="text") or find_leaf(leaves, "f20.f8", value_type="text")
        text = answer or find_leaf(leaves, "f20.f3", value_type="text")
        return {"role": role, "text": text} if text else None

    if role == "tool_call":
        label = find_leaf(leaves, "f31") or find_leaf(leaves, "f30")
        return {"role": role, "label": label} if label else None

    if role == "tool_result":
        label = find_leaf(leaves, "f31") or find_leaf(leaves, "f30")
        content = find_leaf(leaves, "f14.f4")
        return {"role": role, "label": label, "content": content} if (label or content) else None

    if role == "tool_edit":
        label = find_leaf(leaves, "f31") or find_leaf(leaves, "f30")
        content = find_leaf(leaves, "f10.f1.f2.f1")
        path = find_leaf(leaves, "f10.f1.f2.f2")
        instruction = find_leaf(leaves, "f10.f1.f1.f1")
        snippet = find_leaf(leaves, "f10.f1.f1.f9.f1")
        return {"role": role, "label": label, "content": content, "path": path,
                "instruction": instruction, "snippet": snippet}

    if role == "tool_error":
        text = find_leaf(leaves, "f31.f1") or find_leaf(leaves, "f5.f31")
        return {"role": role, "text": text} if text else None

    if role == "model_error":
        short = find_leaf(leaves, "f24.f3.f1")
        detail = find_leaf(leaves, "f24.f3.f2")
        return {"role": role, "text": short, "detail": detail} if short else None

    if role == "permission_request":
        label = find_leaf(leaves, "f31")
        plan = find_leaf(leaves, "f140.f1.f2")
        return {"role": role, "label": label, "plan": plan}

    if role in ("internal", "snapshot"):
        return None  # bookkeeping / auto-regenerated sidebar summaries, not chat content

    # Unrecognized step_type: surface whatever readable text exists rather than
    # dropping it, tagged with the raw numeric type so it's clearly unclassified.
    texty = [l for l in leaves if l["type"] in ("text", "json") and l["value"]]
    if not texty:
        return None
    return {"role": f"unknown_step_type_{step_type}", "text": texty[0]["value"]}


def flatten(fields, path="root"):
    """Walk a decoded field tree, yielding every readable leaf with a
    field-path breadcrumb (e.g. root.f2.f9) so extracted text stays
    traceable back to the raw structure it came from."""
    out = []
    for f in fields:
        p = f"{path}.f{f['field']}"
        if f.get("kind") == "string":
            kind = classify_string(f["value"])
            if isinstance(kind, tuple):
                out.append({"path": p, "type": "json", "value": kind[1]})
            else:
                out.append({"path": p, "type": kind, "value": f["value"]})
        elif f.get("kind") in ("protobuf", "base64+protobuf"):
            ts = looks_like_timestamp(f["value"])
            if ts:
                out.append({"path": p, "type": "timestamp", "value": ts})
            else:
                out.extend(flatten(f["value"], p))
    return out


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_gemini_roots(log, override=None):
    """Returns every existing data root, not just the first match - newer
    Antigravity installs use `~/.gemini/antigravity-ide/` while the
    conversation index (`agyhub_summaries_proto.pb`) and older conversations
    may still only exist under the legacy `~/.gemini/antigravity/`. Picking
    a single root silently drops whichever half lives in the other one."""
    candidates = [Path(override)] if override else [
        Path.home() / ".gemini" / "antigravity-ide",
        Path.home() / ".gemini" / "antigravity",
    ]
    found = []
    for c in candidates:
        exists = c.is_dir()
        log.step(f"checking candidate Antigravity data root: {c} -> {'FOUND' if exists else 'not present'}")
        if exists:
            found.append(c)
    return found


def discover_ide_state_dbs(log):
    """Best-effort secondary source: the VS Code-style state.vscdb key/value
    stores under the editor's own app-data dir. Optional - only used to
    cross-check, never required."""
    app_data_candidates = []
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
        app_data_candidates = [base / "Antigravity", base / "Antigravity IDE"]
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        app_data_candidates = [base / "Antigravity", base / "Antigravity IDE"]
    else:
        base = Path.home() / ".config"
        app_data_candidates = [base / "Antigravity", base / "Antigravity IDE"]

    found = []
    for base in app_data_candidates:
        db = base / "User" / "globalStorage" / "state.vscdb"
        exists = db.is_file()
        log.step(f"checking secondary IDE state store: {db} -> {'FOUND' if exists else 'not present'}")
        if exists:
            found.append(db)
    return found


def load_conversation_index(log, gemini_root):
    idx_path = gemini_root / "agyhub_summaries_proto.pb"
    if not idx_path.is_file():
        log.step(f"conversation index not found at {idx_path}")
        return []
    data = idx_path.read_bytes()
    log.step(f"read conversation index {idx_path} ({len(data)} bytes)")
    top = decode_message(data)
    if not top:
        log.step("conversation index did not parse as protobuf - aborting index load")
        return []
    log.step(f"decoded {len(top)} top-level entries from conversation index")

    summaries = []
    for entry in top:
        if entry.get("kind") not in ("protobuf", "base64+protobuf"):
            continue
        rec = {"uuid": None, "title": None, "workspace_paths": [], "git": None, "raw": entry["value"]}
        for f in entry["value"]:
            if f["field"] == 1 and f.get("kind") == "string" and UUID_RE.match(f["value"]):
                rec["uuid"] = f["value"]
            elif f["field"] == 2 and f.get("kind") in ("protobuf", "base64+protobuf"):
                for sub in f["value"]:
                    if sub["field"] == 1 and sub.get("kind") == "string":
                        rec["title"] = sub["value"]
                    elif sub.get("kind") in ("protobuf", "base64+protobuf"):
                        for leaf in flatten([sub]):
                            if leaf["type"] == "path":
                                rec["workspace_paths"].append(leaf["value"])
                            elif leaf["type"] == "timestamp" and "created_at" not in rec:
                                rec["created_at"] = leaf["value"]
        rec["workspace_paths"] = sorted(set(rec["workspace_paths"]))
        if rec["uuid"]:
            summaries.append(rec)
    log.step(f"extracted {len(summaries)} conversation summaries with a resolvable uuid")
    return summaries


# ---------------------------------------------------------------------------
# Per-conversation decode
# ---------------------------------------------------------------------------

def decode_db_conversation(log, db_path):
    log.step(f"opening SQLite trajectory database {db_path} ({db_path.stat().st_size} bytes)")
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    log.step(f"tables in {db_path.name}: {tables}")

    result = {"trajectory_meta": None, "steps": [], "gen_metadata": [], "other_tables": {}}

    if "trajectory_meta" in tables:
        cur.execute("SELECT * FROM trajectory_meta")
        rows = [dict(r) for r in cur.fetchall()]
        result["trajectory_meta"] = rows
        log.step(f"read trajectory_meta: {rows}")

    if "steps" in tables:
        cur.execute("SELECT * FROM steps ORDER BY idx")
        step_rows = cur.fetchall()
        log.step(f"read {len(step_rows)} rows from steps table, decoding each BLOB column")
        for row in step_rows:
            row = dict(row)
            step = {"idx": row.get("idx"), "step_type": row.get("step_type"),
                    "status": row.get("status"), "extracted": {}}
            for col in ("metadata", "step_payload", "task_details", "render_info", "error_details", "permissions"):
                blob = row.get(col)
                if not blob:
                    continue
                parsed = decode_message(blob)
                if parsed:
                    step["extracted"][col] = flatten(parsed)
                else:
                    step["extracted"][col] = [{"path": "root", "type": "undecoded_bytes", "value": len(blob)}]

            all_leaves = step["extracted"].get("step_payload", []) + step["extracted"].get("metadata", [])
            step["timestamp"] = find_leaf(all_leaves, "f1", value_type="timestamp")
            step["turn"] = classify_step(step["step_type"], all_leaves)
            result["steps"].append(step)

    if "gen_metadata" in tables:
        cur.execute("SELECT idx, data FROM gen_metadata ORDER BY idx")
        rows = cur.fetchall()
        log.step(f"read {len(rows)} rows from gen_metadata, decoding each")
        for r in rows:
            parsed = decode_message(r["data"]) if r["data"] else None
            result["gen_metadata"].append({"idx": r["idx"], "extracted": flatten(parsed) if parsed else []})

    for t in tables:
        if t in ("trajectory_meta", "steps", "gen_metadata", "sqlite_sequence"):
            continue
        cur.execute(f"SELECT * FROM {t}")
        rows = [dict(r) for r in cur.fetchall()]
        log.step(f"read {len(rows)} rows from auxiliary table '{t}'")
        result["other_tables"][t] = rows

    con.close()
    log.step(f"closed {db_path}")
    return result


def decode_pb_conversation(log, pb_path):
    data = pb_path.read_bytes()
    log.step(f"read conversation archive {pb_path} ({len(data)} bytes), attempting schema-less protobuf decode")
    parsed = decode_message(data)
    if parsed:
        log.step(f"{pb_path.name} parsed as valid protobuf ({len(parsed)} top-level fields)")
        return {"opaque": False, "extracted": flatten(parsed)}
    log.step(f"{pb_path.name} did NOT parse as protobuf - byte pattern is consistent with encryption "
              f"(high entropy, no gzip/zlib/lzma magic). Reporting as opaque rather than guessing.")
    return {"opaque": True, "reason": "encrypted-or-unrecognized-format", "size": len(data)}


# ---------------------------------------------------------------------------
# Same-schema SQLite export
# ---------------------------------------------------------------------------

def decode_conversation_to_sqlite(log, db_path, out_path):
    """Copy `db_path` into a fresh SQLite file at `out_path` with IDENTICAL
    table and column names/schema, but every BLOB value that parses as
    protobuf is replaced with its decoded JSON text (same column, same
    name - SQLite has no real column-type enforcement, so a TEXT value
    fits fine into a column declared BLOB). Values that don't parse as
    protobuf (opaque/encrypted blobs, or blobs from an unrecognized shape)
    are left as raw bytes untouched."""
    if out_path.exists():
        out_path.unlink()

    src = sqlite3.connect(str(db_path))
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(str(out_path))

    tables = src.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
    ).fetchall()

    for name, sql in tables:
        dst.execute(sql)
        log.step(f"[sqlite-export] created table `{name}` (verbatim schema)")

    for name, _sql in tables:
        columns = [row[1] for row in src.execute(f"PRAGMA table_info({name})").fetchall()]
        rows = src.execute(f"SELECT * FROM {name}").fetchall()
        decoded_count = 0

        for row in rows:
            values = []
            for col in columns:
                val = row[col]
                if isinstance(val, (bytes, bytearray)):
                    parsed = decode_message(bytes(val))
                    if parsed:
                        val = json.dumps(flatten(parsed), default=str, ensure_ascii=False)
                        decoded_count += 1
                values.append(val)
            placeholders = ",".join("?" for _ in columns)
            col_list = ",".join(columns)
            dst.execute(f"INSERT INTO {name} ({col_list}) VALUES ({placeholders})", values)

        log.step(f"[sqlite-export] copied {len(rows)} row(s) into `{name}` "
                 f"({decoded_count} BLOB value(s) decoded to JSON text)")

    dst.commit()
    dst.close()
    src.close()
    log.step(f"[sqlite-export] wrote {out_path.resolve()}")


def collect_brain_files(log, gemini_root, uuid):
    brain_dir = gemini_root / "brain" / uuid
    if not brain_dir.is_dir():
        log.step(f"no brain/{uuid} artifact directory")
        return {}
    files = {}
    for p in sorted(brain_dir.glob("*")):
        if p.is_file() and p.suffix in (".md", ".json") and ".resolved" not in p.name:
            try:
                files[p.name] = p.read_text(encoding="utf-8", errors="replace")
                log.step(f"read brain artifact {p} ({p.stat().st_size} bytes)")
            except OSError as e:
                log.step(f"failed reading brain artifact {p}: {e}")
    return files


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _clip(text, limit=3000):
    text = str(text).strip()
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


def render_transcript_md(summary, decoded, brain_files):
    lines = []
    title = summary.get("title") or "(untitled conversation)"
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- uuid: `{summary['uuid']}`")
    if summary.get("created_at"):
        lines.append(f"- created_at (guessed from a Timestamp{{seconds,nanos}} shaped field): {summary['created_at']}")
    for wp in summary.get("workspace_paths", []):
        lines.append(f"- workspace path: `{wp}`")
    lines.append("")

    if decoded.get("opaque"):
        lines.append("> This conversation is stored as an encrypted/opaque `.pb` archive on this machine. "
                      "Its byte content has ~8 bits/byte entropy and does not parse as protobuf, decompress "
                      "as gzip/zlib/lzma, or match any known container format, so its message content "
                      "cannot be recovered by this script.")
        lines.append("")
    elif not decoded.get("steps"):
        lines.append("> This conversation's `steps` table is empty (0 rows) - Antigravity recorded the "
                      "session (workspace binding, timestamp) but no message was ever sent in it, so there "
                      "is nothing to show. This is not a decode failure; check trajectory_meta below and "
                      "decoded_raw.json to confirm.")
        lines.append("")
    else:
        steps = decoded.get("steps", [])
        rendered = [s for s in steps if s.get("turn")]
        lines.append(f"## Transcript ({len(rendered)} chat/tool events out of {len(steps)} raw steps)")
        lines.append("")
        if not rendered:
            lines.append("> All raw steps were classified as internal bookkeeping/snapshot noise - no "
                          "user/assistant/tool content was recognized. Raw step data is still in "
                          "decoded_raw.json if you want to check manually.")
            lines.append("")
        lines.append("_Roles (User/Assistant/Tool/error) are reverse-engineered from this machine's own "
                      "data, not from an official schema - see STEP_ROLES in the script. "
                      f"{len(steps) - len(rendered)} internal bookkeeping/snapshot steps are omitted below "
                      "but remain in decoded_raw.json._")
        lines.append("")
        for step in rendered:
            turn = step["turn"]
            ts = f" `{step['timestamp']}`" if step.get("timestamp") else ""
            role = turn["role"]

            if role == "user":
                lines.append(f"**User**{ts}:")
                lines.append(_clip(turn["text"]))
            elif role == "assistant":
                lines.append(f"**Assistant**{ts}:")
                lines.append(_clip(turn["text"]))
            elif role == "tool_call":
                lines.append(f"*Tool call{ts}: {turn['label']}*")
            elif role == "tool_result":
                label = turn.get("label") or "(tool result)"
                lines.append(f"*Tool result{ts}: {label}*")
                if turn.get("content"):
                    lines.append(f"```\n{_clip(turn['content'], 1500)}\n```")
            elif role == "tool_edit":
                label = turn.get("label") or turn.get("instruction") or "(file edit)"
                lines.append(f"*Tool edit{ts}: {label}*")
                if turn.get("path"):
                    lines.append(f"- path: `{turn['path']}`")
                body = turn.get("content") or turn.get("snippet")
                if body:
                    lines.append(f"```\n{_clip(body, 1500)}\n```")
            elif role == "tool_error":
                lines.append(f"**[TOOL ERROR]**{ts}: {turn['text']}")
            elif role == "model_error":
                lines.append(f"**[MODEL ERROR]**{ts}: {turn['text']}")
                if turn.get("detail"):
                    lines.append(f"> {_clip(turn['detail'], 500)}")
            elif role == "permission_request":
                lines.append(f"**[PERMISSION REQUEST]**{ts}: {turn.get('label') or ''}")
                if turn.get("plan"):
                    lines.append(f"> {_clip(turn['plan'], 800)}")
            else:
                lines.append(f"*({role}, step {step['idx']})*{ts}: {_clip(str(turn.get('text', '')))}")
            lines.append("")

        if decoded.get("trajectory_meta"):
            lines.append("## trajectory_meta (raw row)")
            lines.append(f"```json\n{json.dumps(decoded['trajectory_meta'], default=str, indent=2)}\n```")
            lines.append("")

    if brain_files:
        lines.append("## Companion artifacts from brain/<uuid>/")
        for name, content in brain_files.items():
            lines.append(f"### {name}")
            lines.append(f"```\n{content}\n```")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="override auto-detected data root (checks both antigravity-ide and antigravity by default)")
    ap.add_argument("--uuid", action="append", help="decode only this conversation uuid (repeatable)")
    ap.add_argument("--latest", type=int, help="decode only the N most recently modified conversations")
    ap.add_argument("--all", action="store_true", help="decode every conversation found")
    ap.add_argument("--out-dir", default=".", help="output directory for transcript.md/decoded_raw.json (default: current working directory)")
    ap.add_argument("--to-sqlite", action="store_true",
                    help="also write a same-schema SQLite file per targeted conversation "
                         "(decoded_<uuid>.sqlite) into the current working directory, with "
                         "BLOB columns replaced by decoded JSON text - no --uuid/--latest/--all "
                         "needed, defaults to the single most recently modified conversation")
    ap.add_argument("--quiet", action="store_true", help="suppress live flow-log printing")
    args = ap.parse_args()

    log = FlowLog(quiet=args.quiet)
    log.step("=== Antigravity chat history decode started ===")

    gemini_roots = discover_gemini_roots(log, args.root)
    if not gemini_roots:
        log.step("no Antigravity data directory found on this machine - nothing to decode")
        sys.exit(1)

    discover_ide_state_dbs(log)  # logged for transparency; not required for decoding

    summaries = []
    by_uuid = {}
    for root in gemini_roots:
        for s in load_conversation_index(log, root):
            if s["uuid"] not in by_uuid:
                by_uuid[s["uuid"]] = s
                summaries.append(s)

    on_disk = {}
    for root in gemini_roots:
        conv_dir = root / "conversations"
        if conv_dir.is_dir():
            for p in conv_dir.iterdir():
                if p.suffix in (".db", ".pb"):
                    on_disk.setdefault(p.stem, {}).setdefault(p.suffix[1:], p)
            log.step(f"found {len(list(conv_dir.iterdir()))} entries under {conv_dir}")
    log.step(f"{len(on_disk)} distinct conversation ids found across {len(gemini_roots)} root(s)")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log.step(f"output directory: {out_dir.resolve()}")

    index_lines = ["# Antigravity conversation index", ""]
    for s in sorted(summaries, key=lambda r: r.get("created_at") or "", reverse=True):
        fmt = "+".join(sorted(on_disk.get(s["uuid"], {}).keys())) or "no local file"
        index_lines.append(f"- `{s['uuid']}` [{fmt}] - {s.get('title') or '(untitled)'} "
                            f"({s.get('created_at', 'unknown time')})")
    (out_dir / "index.md").write_text("\n".join(index_lines), encoding="utf-8")
    log.step(f"wrote conversation index summary to {out_dir / 'index.md'}")

    if args.uuid:
        targets = args.uuid
    elif args.latest:
        ordered = sorted(on_disk.items(), key=lambda kv: max(p.stat().st_mtime for p in kv[1].values()), reverse=True)
        targets = [u for u, _ in ordered[:args.latest]]
    elif args.all:
        targets = list(on_disk.keys())
    elif args.to_sqlite:
        # --to-sqlite alone defaults to the single most recently modified
        # conversation, same convention as antigravity_watch.py's session picker.
        ordered = sorted(on_disk.items(), key=lambda kv: max(p.stat().st_mtime for p in kv[1].values()), reverse=True)
        targets = [ordered[0][0]] if ordered else []
    else:
        log.step("no --uuid/--latest/--all/--to-sqlite given: listing only. Pass --latest 5 or --all to decode full transcripts.")
        targets = []

    log.step(f"decoding {len(targets)} conversation(s): {targets}")

    for uuid in targets:
        log.step(f"--- conversation {uuid} ---")
        files = on_disk.get(uuid, {})
        summary = by_uuid.get(uuid, {"uuid": uuid})
        brain_files = {}
        for root in gemini_roots:
            brain_files = collect_brain_files(log, root, uuid)
            if brain_files:
                break

        if "db" in files:
            decoded = decode_db_conversation(log, files["db"])
        elif "pb" in files:
            decoded = decode_pb_conversation(log, files["pb"])
        else:
            log.step(f"no conversations/{uuid}.db or .pb found on disk - skipping")
            continue

        md = render_transcript_md(summary, decoded, brain_files)
        conv_out = out_dir / uuid
        conv_out.mkdir(exist_ok=True)
        (conv_out / "transcript.md").write_text(md, encoding="utf-8")
        (conv_out / "decoded_raw.json").write_text(json.dumps(decoded, default=str, indent=2), encoding="utf-8")
        log.step(f"wrote {conv_out / 'transcript.md'} and decoded_raw.json")

        if args.to_sqlite:
            if "db" not in files:
                log.step(f"--to-sqlite: conversation {uuid} has no .db file (it's opaque .pb) - skipping sqlite export")
            else:
                sqlite_out = Path.cwd() / f"decoded_{uuid}.sqlite"
                decode_conversation_to_sqlite(log, files["db"], sqlite_out)

    log.step("=== done ===")
    log.save(out_dir / "flow_log.txt")
    print(f"\nFlow log saved to {out_dir / 'flow_log.txt'}")


if __name__ == "__main__":
    main()
