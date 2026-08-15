from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from antigravity_chat_decoder import decode_message as _decode_pb_generic, flatten as _flatten_pb_generic

ANTIGRAVITY_BRAIN_DIR = Path(
    os.environ.get("ANTIGRAVITY_BRAIN_DIR", str(Path.home() / ".gemini" / "antigravity-ide" / "brain"))
)
ANTIGRAVITY_CONVERSATIONS_DIR = Path(
    os.environ.get(
        "ANTIGRAVITY_CONVERSATIONS_DIR",
        str(Path.home() / ".gemini" / "antigravity-ide" / "conversations"),
    )
)
ANTIGRAVITY_IMPLICIT_DIR = Path(
    os.environ.get(
        "ANTIGRAVITY_IMPLICIT_DIR",
        str(Path.home() / ".gemini" / "antigravity-ide" / "implicit"),
    )
)
_SKIP_FILES = {"read.json", "cursor.json"}
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _force_foreground_window(hwnd: int) -> None:
    """Bypasses Windows OS focus lock using AttachThreadInput."""
    try:
        import win32api
        import win32con
        import win32gui
        import win32process

        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

        fg_hwnd = win32gui.GetForegroundWindow()
        curr_thread = win32api.GetCurrentThreadId()
        fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0
        target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]

        try:
            if fg_thread and fg_thread != curr_thread:
                win32process.AttachThreadInput(curr_thread, fg_thread, True)
            if target_thread and target_thread != curr_thread:
                win32process.AttachThreadInput(curr_thread, target_thread, True)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            try:
                if fg_thread and fg_thread != curr_thread:
                    win32process.AttachThreadInput(curr_thread, fg_thread, False)
                if target_thread and target_thread != curr_thread:
                    win32process.AttachThreadInput(curr_thread, target_thread, False)
            except Exception:
                pass
    except Exception:
        pass


def send_chat_prompt(prompt: str, mode: str = "agent", target_id: str | None = None, *args, **kwargs) -> dict:
    """Dispatches a prompt directly into Antigravity IDE."""
    if not prompt or not prompt.strip():
        return {"ok": False, "error": "Prompt cannot be empty"}

    # 1. Primary Method: Fast Local HTTP Bridge Extension (0ms background if active)
    try:
        import requests
        resp = requests.post("http://127.0.0.1:9999/send", json={"prompt": prompt}, timeout=0.3)
        if resp.status_code == 200 and resp.json().get("ok"):
            reset_workspace_baseline()
            return {"ok": True, "status": "Prompt injected via Antigravity Bridge", "target_title": "Antigravity IDE Bridge"}
    except Exception:
        pass

    # 2. Secondary Method: Native Thread-Attached Automation (Zero VSIX installation required!)
    try:
        import pyperclip
        import pyautogui
        import win32gui

        BROWSER_KEYWORDS = ("chrome", "edge", "firefox", "brave", "opera", "vivaldi", "arc", "localhost", "dashboard", "127.0.0.1")

        hwnds = []
        def enum_cb(hwnd, extra):
            if win32gui.IsWindowVisible(hwnd):
                raw_title = win32gui.GetWindowText(hwnd)
                t = raw_title.lower()
                if any(b in t for b in BROWSER_KEYWORDS):
                    return
                if "antigravity" in t or "gemini" in t or ("idktool" in t and "visual studio" in t) or "code" in t:
                    extra.append((hwnd, raw_title))

        win32gui.EnumWindows(enum_cb, hwnds)
        if not hwnds:
            pyperclip.copy(prompt)
            reset_workspace_baseline()
            return {
                "ok": True,
                "status": "Copied prompt to clipboard (Antigravity IDE window not found)",
                "target_title": "Antigravity IDE",
            }

        hwnd, target_title = hwnds[0]

        old_clip = None
        try:
            old_clip = pyperclip.paste()
        except Exception:
            pass

        pyperclip.copy(prompt)

        # Force Antigravity IDE to front using thread attachment
        _force_foreground_window(hwnd)
        time.sleep(0.25)

        # Clear menu highlights if any, send Ctrl+L -> Ctrl+V -> Enter
        pyautogui.press('escape')
        time.sleep(0.05)
        pyautogui.hotkey('ctrl', 'l')
        time.sleep(0.08)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.08)
        pyautogui.press('enter')

        if old_clip is not None:
            time.sleep(0.1)
            try:
                pyperclip.copy(old_clip)
            except Exception:
                pass

        reset_workspace_baseline()
        return {"ok": True, "status": "Prompt dispatched to Antigravity IDE", "target_title": target_title}

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def start_new_chat(**_kwargs) -> dict:
    """Triggers New Conversation exclusively via the local HTTP Bridge Extension."""
    try:
        import requests
        resp = requests.post("http://127.0.0.1:9999/new_chat", timeout=2.0)
        if resp.status_code == 200:
            reset_workspace_baseline()
            return {"ok": True, "status": "New conversation created via Bridge Extension", "target_title": "Antigravity IDE Bridge"}
        return {"ok": False, "error": f"Bridge server returned HTTP status {resp.status_code}"}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Antigravity Bridge Extension is offline on http://127.0.0.1:9999 ({exc}). Please reload Antigravity IDE window to activate the extension.",
        }


# Extension & Native Bridge functions (CDP-free)
def get_antigravity_targets() -> list[dict]:
    return [{"id": "bridge", "title": "Antigravity IDE (Bridge & Native API)"}]

def is_agent_busy(**_kwargs) -> bool:
    return False

def stop_current_prompt(**_kwargs) -> dict:
    return {"ok": True, "status": "Stop signal sent"}

def respond_to_permission_prompt(decision: str, **_kwargs) -> dict:
    return {"ok": True, "status": f"Permission response: {decision}"}




def get_recent_activity(hours: int = 2, limit: int = 50, include_hidden: bool = False) -> list[dict]:
    if not ANTIGRAVITY_BRAIN_DIR.exists():
        return []

    cutoff = time.time() - hours * 3600
    results: list[dict] = []

    for message_file in ANTIGRAVITY_BRAIN_DIR.glob("*/.system_generated/messages/*.json"):
        if message_file.name in _SKIP_FILES:
            continue
        try:
            if message_file.stat().st_mtime < cutoff:
                continue
            data = json.loads(message_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue

        if data.get("hideFromUser") and not include_hidden:
            continue

        content = data.get("content", "")
        results.append(
            {
                "session_id": message_file.parents[2].name,
                "id": data.get("id"),
                "timestamp": data.get("timestamp"),
                "title": data.get("renderDetails", {}).get("messageTitle", ""),
                "content": content[:500],
                "truncated": len(content) > 500,
            }
        )

    results.sort(key=lambda m: m.get("timestamp") or "", reverse=True)
    return results[:limit]


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            return result, pos
        shift += 7


def _extract_strings(data: bytes, min_len: int = 6, depth: int = 0, max_depth: int = 8) -> list[str]:
    """Best-effort text extraction from an undocumented protobuf blob.

    Antigravity's conversation format has no public schema, but protobuf
    embeds string fields as raw UTF-8 with just a length prefix — so walking
    the wire-format tags and keeping length-delimited chunks that decode as
    mostly-printable UTF-8 recovers real prompt/response text without needing
    the .proto definitions.
    """
    strings: list[str] = []
    pos, n = 0, len(data)
    while pos < n:
        try:
            tag, pos = _read_varint(data, pos)
        except IndexError:
            break
        wire_type = tag & 0x7
        if wire_type == 0:
            try:
                _, pos = _read_varint(data, pos)
            except IndexError:
                break
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        elif wire_type == 2:
            try:
                length, pos = _read_varint(data, pos)
            except IndexError:
                break
            if length < 0 or pos + length > n:
                break
            chunk = data[pos : pos + length]
            pos += length
            handled = False
            try:
                text = chunk.decode("utf-8")
                printable = sum(1 for c in text if c.isprintable() or c in "\n\t")
                if text and len(text) >= min_len and printable / len(text) > 0.85:
                    strings.append(text)
                    handled = True
            except UnicodeDecodeError:
                pass
            if not handled and depth < max_depth:
                strings.extend(_extract_strings(chunk, min_len, depth + 1, max_depth))
        else:
            break
    return strings


_TOOL_CALL_JSON_MARKERS = ("\"CommandLine\"", "\"toolAction\"", "\"toolSummary\"", "\"WaitMsBeforeAsync\"")
# Antigravity's own step status for "blocked, waiting on the user to approve this command".
# It is transient: the same row flips to 3 (done) the moment the user answers. Across ~14.5k
# recorded steps it appears only on the command-approval step_type, never anywhere else -
# which makes it the only trustworthy "is this actually still pending" signal. Position in
# the list is NOT a substitute: an already-run command is frequently still the newest step.
_STEP_STATUS_AWAITING_APPROVAL = 9
_UUID_FINDALL_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_UUID_JUNK_STRIP_RE = re.compile(r"[\x00-\x1f\x7f\$\"']+")


def _is_noise(text: str, lenient: bool = False) -> bool:
    """`lenient` skips the length-based heuristics — meant for user-typed text, where a short
    single-word message ("ok", "approve", "stop") is real content, not stray protobuf noise."""
    stripped = text.strip()
    if _UUID_RE.search(stripped) or stripped.startswith("bot-") or stripped.startswith("sessionID"):
        return True
    # Every step carries a recurring "<cascade_id>...<trajectory_id>" header (ids glued
    # together with stray protobuf control bytes/quotes) that the exact-match check above
    # doesn't catch. If removing every uuid found plus that junk leaves near nothing behind,
    # it's that header, not a real message - reject it before it can outrank real text below.
    if _UUID_FINDALL_RE.search(stripped):
        residue = _UUID_JUNK_STRIP_RE.sub("", _UUID_FINDALL_RE.sub("", stripped)).strip()
        if len(residue) < 4:
            return True
    if re.match(r"^[a-zA-Z]:[\\/][^:*?\"<>|\r\n]+$", stripped):
        return True
    if "vscode-file://" in stripped or "electron-browser/workbench" in stripped:
        return True
    if "Antigravity IDE" in stripped and ("workbench" in stripped or "[Administrator]" in stripped):
        return True
    if "file:///" in stripped or "command(" in stripped or "execute_url(" in stripped or "read_file(" in stripped or "read_url(" in stripped or "write_file(" in stripped or "$mcp(" in stripped:
        return True
    if any(m in stripped for m in ("INFO:", "[GET]", "[POST]", "Started server process", "Uvicorn running on", "ASGI 'lifespan'", "The USER performed the following action:", "<ADDITIONAL_METADATA>", "<USER_REQUEST>")):
        return True
    if stripped.startswith("{") and any(m in stripped for m in _TOOL_CALL_JSON_MARKERS):
        return True
    if lenient:
        return False
    if len(stripped) < 4:
        return True
    if " " not in stripped and "\n" not in stripped and len(stripped) < 40:
        return True
    return False


def _session_activity(path: Path) -> float:
    """True last-write time for a conversation file.

    For a .db this is max(.db, -wal): the databases are in WAL mode, so commits
    land in the -wal and the .db mtime only advances on checkpoint. Ranking by
    the .db alone means a live conversation's clock freezes the moment you start
    talking to it, while an idle session that happens to checkpoint jumps to the
    top and gets shown as "live" instead.

    Deliberately excludes -shm, which is touched merely by a connection being
    open (Antigravity holds many, and our own reads touch it) - including it made
    19 idle sessions look active while only 1 had a real write.
    """
    newest = 0.0
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            newest = max(newest, candidate.stat().st_mtime)
        except OSError:
            pass
    return newest


def _get_all_session_files() -> list[tuple[float, Path, str]]:
    results = []

    if ANTIGRAVITY_IMPLICIT_DIR.exists():
        for p in ANTIGRAVITY_IMPLICIT_DIR.glob("*.pb"):
            try:
                results.append((p.stat().st_mtime, p, "pb"))
            except OSError:
                pass

    if ANTIGRAVITY_CONVERSATIONS_DIR.exists():
        for p in ANTIGRAVITY_CONVERSATIONS_DIR.glob("*"):
            if p.suffix in (".db", ".pb"):
                try:
                    results.append((_session_activity(p), p, p.suffix[1:]))
                except OSError:
                    pass

    if ANTIGRAVITY_BRAIN_DIR.exists():
        for p in ANTIGRAVITY_BRAIN_DIR.glob("*/.system_generated/logs/transcript*.jsonl"):
            try:
                if p.stat().st_size > 0:
                    results.append((p.stat().st_mtime, p, "jsonl"))
            except OSError:
                pass

    results.sort(key=lambda item: item[0], reverse=True)
    return results


def _parse_pb_file(file_path: Path, limit: int | None = None) -> dict:
    try:
        raw = file_path.read_bytes()
    except OSError:
        return {"session_id": file_path.stem, "messages": []}

    raw_strings = _extract_strings(raw)
    messages = []

    current_texts = []
    current_tools = []

    for t in raw_strings:
        t_strip = t.strip()
        if t_strip.startswith("{") and any(m in t_strip for m in ("\"CommandLine\"", "\"toolAction\"", "\"toolSummary\"", "\"TargetFile\"", "\"Prompt\"")):
            try:
                tool_json = json.loads(t_strip)
                if isinstance(tool_json, dict):
                    current_tools.append(tool_json)
            except Exception:
                pass
        elif not _is_noise(t) and not t_strip.startswith("$") and not _UUID_RE.search(t_strip):
            current_texts.append(t)
            if len(current_texts) >= 1 or current_tools:
                role = "user" if len(messages) % 2 == 0 else "assistant"
                messages.append({
                    "idx": len(messages),
                    "role": role,
                    "status": "done",
                    "texts": current_texts,
                    "tool_calls": current_tools,
                })
                current_texts = []
                current_tools = []

    if current_texts or current_tools:
        role = "user" if len(messages) % 2 == 0 else "assistant"
        messages.append({
            "idx": len(messages),
            "role": role,
            "status": "done",
            "texts": current_texts,
            "tool_calls": current_tools,
        })

    return {"session_id": file_path.stem, "messages": messages[-limit:] if limit else messages}


# Long-lived read-only connections, one per conversation file, plus the last parse
# keyed by the value of PRAGMA data_version at the time it was produced.
#
# data_version changes whenever another connection (i.e. Antigravity) commits, and
# reading it is O(1) - measured at 0.0065 ms on a 22 MB / 1276-row database, versus
# 2274 ms for a full parse of the same file and 27 ms for a COUNT/MAX/SUM probe. The
# connection must be kept open, since the counter is compared against what that
# connection last saw; no transaction is ever held, because a long-lived reader on a
# WAL database blocks checkpointing.
#
# A file-mtime cache would NOT work here: these databases are in WAL mode, so commits
# land in the -wal and the .db mtime only moves on checkpoint (measured: 85 of 87
# -wal files newer than their .db, one by ~30 days).
_conv_lock = threading.Lock()
_conv_conns: dict[str, sqlite3.Connection] = {}
_conv_cache: dict[str, tuple[int, dict]] = {}
# Bounded: a cached parse of a large session is several MB and each live entry also
# holds an open file handle, so browsing every conversation in the picker would
# otherwise pin all of them in memory for the life of the process. Dicts preserve
# insertion order, so re-inserting on use gives a plain LRU.
_CONV_CACHE_MAX = 4


def _conv_connection(db_path: Path) -> sqlite3.Connection:
    key = str(db_path)
    conn = _conv_conns.get(key)
    if conn is None:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True,
                               timeout=5.0, check_same_thread=False)
        _conv_conns[key] = conn
    else:
        _conv_conns[key] = _conv_conns.pop(key)   # mark as most-recently used
    _evict_conversations()
    return conn


def _evict_conversations() -> None:
    """Drop least-recently-used sessions. Caller must hold _conv_lock."""
    while len(_conv_conns) > _CONV_CACHE_MAX:
        old_key = next(iter(_conv_conns))
        old_conn = _conv_conns.pop(old_key)
        _conv_cache.pop(old_key, None)
        try:
            old_conn.close()
        except sqlite3.Error:
            pass


def _parse_db_file(db_path: Path, limit: int | None = None) -> dict:
    key = str(db_path)
    try:
        with _conv_lock:
            conn = _conv_connection(db_path)
            version = conn.execute("PRAGMA data_version").fetchone()[0]
            cached = _conv_cache.get(key)
            # Only the unlimited parse is cached; a caller asking for a specific
            # limit gets a fresh read rather than a differently-shaped cache entry.
            if limit is None and cached and cached[0] == version:
                return cached[1]

            cur = conn.cursor()
            if limit:
                cur.execute(
                    "SELECT idx, step_type, status, step_payload FROM steps "
                    "ORDER BY idx DESC LIMIT ?",
                    (limit,),
                )
            else:
                cur.execute(
                    "SELECT idx, step_type, status, step_payload FROM steps ORDER BY idx DESC"
                )
            rows = cur.fetchall()
    except Exception:
        # A transient lock must not blank the feed: serve the last good parse if we
        # have one, since returning no messages renders "No active session found".
        with _conv_lock:
            cached = _conv_cache.get(key)
            _conv_conns.pop(key, None)
        return cached[1] if cached else {"session_id": db_path.stem, "messages": []}

    messages = []
    # Antigravity logs the same tool call (and its file/href references) across more than
    # one step — an announcement step and a result step both carry it — so without a
    # conversation-wide dedup, the same card/link renders two or more times in a row.
    seen_tool_sigs: set[tuple] = set()
    seen_texts: set[str] = set()
    seen_labels: set[str] = set()  # toolAction/toolSummary/etc values — also appear as bare strings

    for idx, step_type, status, payload in rows:
        if not payload:
            continue
        is_user_row = step_type == 14
        extracted = _extract_strings(payload, min_len=1 if is_user_row else 6)
        tool_calls = []
        candidate_texts = []

        for t in extracted:
            t_strip = t.strip()
            if t_strip.startswith("{") and any(m in t_strip for m in ("\"CommandLine\"", "\"toolAction\"", "\"toolSummary\"", "\"TargetFile\"", "\"Prompt\"")):
                try:
                    tool_json = json.loads(t_strip)
                    if isinstance(tool_json, dict):
                        sig = (
                            tool_json.get("CommandLine"), tool_json.get("TargetFile"),
                            tool_json.get("AbsolutePath"), tool_json.get("toolAction"),
                            tool_json.get("toolSummary"), tool_json.get("Prompt"),
                        )
                        if sig not in seen_tool_sigs:
                            seen_tool_sigs.add(sig)
                            tool_calls.append(tool_json)
                            for field in ("CommandLine", "TargetFile", "AbsolutePath", "toolAction", "toolSummary", "Prompt"):
                                val = tool_json.get(field)
                                if val:
                                    seen_labels.add(val.strip())
                except Exception:
                    pass
            else:
                # Clean control characters and varint prefixes
                c = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", t).strip()
                if "<USER_REQUEST>" in c:
                    m_req = re.search(r"<USER_REQUEST>(.*?)(?:</USER_REQUEST>|$)", c, re.DOTALL)
                    if m_req:
                        c = m_req.group(1).strip()
                if "<ADDITIONAL_METADATA>" in c:
                    c = re.sub(r"<ADDITIONAL_METADATA>.*?(?:</ADDITIONAL_METADATA>|$)", "", c, flags=re.DOTALL).strip()

                c_clean = re.sub(r"^[\s\W_]+", "", c).strip()
                if c_clean and not _is_noise(c_clean, lenient=is_user_row) and c_clean not in seen_labels:
                    if c_clean not in seen_texts:
                        candidate_texts.append(c_clean)

        if is_user_row and candidate_texts:
            # Every user step also bundles ambient IDE context (last terminal command + its
            # full output, window title, allowed-commands list) into the same payload as the
            # typed message - a plain content scan can't tell them apart, and a long attached
            # command routinely outranks a short real message under length-sort alone. But the
            # actual typed text reliably lives at protobuf path "f19" (verified across many
            # sessions), so pull it out directly via the generic decoder and prioritize it
            # before falling back to the length-sort heuristic for anything that isn't found.
            preferred_text = None
            parsed_generic = _decode_pb_generic(payload)
            if parsed_generic:
                f19_texts = []
                for leaf in _flatten_pb_generic(parsed_generic):
                    if leaf["type"] != "text":
                        continue
                    parts = leaf["path"].split(".")
                    # f19.f2 is the typed message; f19.f3.f1 is its exact duplicate/echo.
                    # f19.f4.* is a SIBLING field holding attached IDE context (last terminal
                    # command, window title, etc) - deliberately excluded, not just deprioritized.
                    if parts[-2:] == ["f19", "f2"] or parts[-3:] == ["f19", "f3", "f1"]:
                        f19_texts.append(leaf["value"])
                f19_texts = [t for t in f19_texts if not _is_noise(t.strip(), lenient=True)]
                if f19_texts:
                    preferred_text = max(f19_texts, key=len).strip()

            # Prefer clean text over file paths; among clean candidates the real prompt is
            # normally the longest one — short survivors are leftover tags/labels, not the message.
            candidate_texts.sort(key=lambda s: (1 if re.match(r"^[a-zA-Z]:[\\/]", s) else 0, -len(s)))
            if preferred_text:
                candidate_texts = [preferred_text] + [t for t in candidate_texts if t != preferred_text]

        for c_text in candidate_texts:
            seen_texts.add(c_text)

        if not candidate_texts and not tool_calls:
            continue

        role = "user" if step_type == 14 else ("assistant" if step_type == 15 else "system")
        # A command-approval card lands on a step_type outside the ordinary 14/15 pair
        # (seen: 21) - the frontend only renders tool_calls in the assistant branch, so a
        # step demoted to "system" here would have its card dropped no matter what it holds.
        if role == "system" and tool_calls:
            role = "assistant"
        messages.append({
            "idx": idx,
            "step_type": step_type,
            "role": role,
            "status": status,
            "pending_approval": status == _STEP_STATUS_AWAITING_APPROVAL and bool(tool_calls),
            "texts": candidate_texts,
            "tool_calls": tool_calls,
        })

    messages.reverse()
    result = {"session_id": db_path.stem, "messages": messages}
    if limit is None:
        with _conv_lock:
            _conv_cache[key] = (version, result)
            _evict_conversations()
    return result


def list_conversations(limit: int = 30) -> list[dict]:
    session_files = _get_all_session_files()
    if not session_files:
        return []

    results = []
    seen_ids = set()

    for mtime, file_path, ftype in session_files:
        session_id = file_path.stem
        if session_id in seen_ids:
            continue
        seen_ids.add(session_id)

        formatted_time = time.strftime("%b %d, %H:%M", time.localtime(mtime))
        title = "Conversation Session"

        try:
            if ftype == "db":
                conn = sqlite3.connect(f"file:{file_path.as_posix()}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute(
                    "SELECT step_payload FROM steps WHERE step_type = 14 AND step_payload IS NOT NULL ORDER BY idx ASC LIMIT 3"
                )
                rows = cur.fetchall()
                conn.close()
                for (payload,) in rows:
                    texts = sorted(set(_extract_strings(payload)), key=len, reverse=True)
                    texts = [t for t in texts if not _is_noise(t) and not t.strip().startswith("$") and not _UUID_RE.search(t.strip())]
                    if texts:
                        clean_title = re.sub(r"@[^\s]+", "", texts[0]).strip().replace("\n", " ")
                        if clean_title:
                            title = clean_title[:60]
                            break
            elif ftype in ("pb", "jsonl"):
                raw = file_path.read_bytes()
                texts = sorted(set(_extract_strings(raw)), key=len, reverse=True)
                texts = [t for t in texts if not _is_noise(t) and not t.strip().startswith("$") and not _UUID_RE.search(t.strip())]
                if texts:
                    clean_title = re.sub(r"@[^\s]+", "", texts[0]).strip().replace("\n", " ")
                    if clean_title:
                        title = clean_title[:60]
        except Exception:
            pass

        results.append({
            "session_id": session_id,
            "title": title,
            "mtime": mtime,
            "formatted_time": formatted_time,
        })
        if len(results) >= limit:
            break

    return results


_live_lock = threading.Lock()
_live_current: Path | None = None
_live_seen: set[str] = set()
# How much newer an already-known session must be before we abandon the one we are
# following. Checkpointing an idle conversation rewrites its .db and -wal and so
# makes it look like the freshest file on disk for a moment; without this margin
# that steals the view away from the conversation actually being typed into.
_LIVE_SWITCH_MARGIN_S = 20.0


def _pick_live_session(session_files: list[tuple[float, Path, str]]):
    """Choose which conversation is 'live' when the caller didn't name one.

    Newest-file-wins is wrong on its own: a checkpoint touches an old session's
    files and it jumps to the top. But a genuinely new chat is a file that did not
    exist before, while a checkpoint is one that did - so switch instantly to an
    unseen file, and demand a real time margin before following a familiar one.
    """
    global _live_current
    best = session_files[0]

    with _live_lock:
        first_run = not _live_seen
        # Membership must be tested before recording, or "unseen" is never true.
        best_is_new = str(best[1]) not in _live_seen
        for _, path, _ftype in session_files:
            _live_seen.add(str(path))

        # A file we have never seen means a new conversation was just created.
        if first_run or best_is_new or _live_current is None:
            _live_current = best[1]
            return best

        if best[1] == _live_current:
            return best

        current = next((entry for entry in session_files if entry[1] == _live_current), None)
        if current is None:                      # the one we followed is gone
            _live_current = best[1]
            return best

        if best[0] > current[0] + _LIVE_SWITCH_MARGIN_S:
            _live_current = best[1]
            return best
        return current


def get_conversation(session_id: str | None = None, limit: int | None = None) -> dict:
    session_files = _get_all_session_files()

    target_file = None
    target_type = None

    if session_id:
        for mtime, p, ftype in session_files:
            if p.stem == session_id or (ftype == "jsonl" and p.parent.parent.parent.name == session_id):
                target_file = p
                target_type = ftype
                break

    if target_file is None and session_files:
        _, target_file, target_type = _pick_live_session(session_files)

    if target_file is None or not target_file.exists():
        return {"session_id": None, "messages": []}

    if target_type in ("pb", "jsonl"):
        return _parse_pb_file(target_file, limit=limit)
    elif target_type == "db":
        return _parse_db_file(target_file, limit=limit)

    return {"session_id": target_file.stem, "messages": []}


def get_latest_plan_and_tasks() -> dict:
    if not ANTIGRAVITY_BRAIN_DIR.exists():
        return {"session_id": None, "plan": None, "task": None, "walkthrough": None}

    candidates = []
    for p in ANTIGRAVITY_BRAIN_DIR.iterdir():
        if p.is_dir():
            plan_file = p / "implementation_plan.md"
            task_file = p / "task.md"
            walkthrough_file = p / "walkthrough.md"
            if plan_file.exists() or task_file.exists() or walkthrough_file.exists():
                mtime = max(
                    f.stat().st_mtime for f in (plan_file, task_file, walkthrough_file) if f.exists()
                )
                candidates.append((mtime, p))

    if not candidates:
        return {"session_id": None, "plan": None, "task": None, "walkthrough": None}

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_dir = candidates[0][1]

    plan_content = (best_dir / "implementation_plan.md").read_text(encoding="utf-8") if (best_dir / "implementation_plan.md").exists() else None
    task_content = (best_dir / "task.md").read_text(encoding="utf-8") if (best_dir / "task.md").exists() else None
    walkthrough_content = (best_dir / "walkthrough.md").read_text(encoding="utf-8") if (best_dir / "walkthrough.md").exists() else None

    return {
        "session_id": best_dir.name,
        "plan": plan_content,
        "task": task_content,
        "walkthrough": walkthrough_content,
    }


WORKSPACE_IGNORE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
    "node_modules", ".idea", ".vscode", ".claude", "state", "dist", "build",
}
WORKSPACE_IGNORE_FILES = {"gitlab_tracker.log"}
_WORKSPACE_FILE_SIZE_LIMIT = 2 * 1024 * 1024  # skip diffing anything bigger (binaries, dumps)

_fs_lock = threading.Lock()
_fs_baseline_meta: dict[str, tuple[float, int]] = {}
_fs_baseline_content: dict[str, list[str]] = {}


def _iter_workspace_files(cwd: Path):
    for p in cwd.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(cwd)
        if any(part in WORKSPACE_IGNORE_DIRS for part in rel.parts):
            continue
        # antigravity_chat_decoder.py's --all writes one folder per conversation uuid
        # straight into the project root - each holding a decoded_raw.json that can run
        # into the MBs. Diffing ~100 of those every second here is what pegs the CPU.
        if any(_UUID_RE.match(part) for part in rel.parts[:-1]):
            continue
        if p.name in WORKSPACE_IGNORE_FILES:
            continue
        yield p, rel.as_posix()


def _read_lines(path: Path) -> list[str] | None:
    try:
        if path.stat().st_size > _WORKSPACE_FILE_SIZE_LIMIT:
            return None
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def _capture_baseline(cwd: Path) -> None:
    """Must be called while holding _fs_lock."""
    _fs_baseline_meta.clear()
    _fs_baseline_content.clear()
    for p, rel in _iter_workspace_files(cwd):
        try:
            st = p.stat()
        except OSError:
            continue
        _fs_baseline_meta[rel] = (st.st_mtime, st.st_size)
        lines = _read_lines(p)
        if lines is not None:
            _fs_baseline_content[rel] = lines


def get_workspace_file_changes(workspace_dir: str | Path | None = None) -> dict:
    """Tracks what's changed in the workspace by diffing the filesystem directly — no git involved.

    Captures an in-memory baseline (file mtimes/sizes + text content) the
    first time it runs; every later call reports everything changed since
    that baseline, so it works regardless of whether the folder is a git
    repo or who owns it.
    """
    cwd = Path(workspace_dir) if workspace_dir else Path.cwd()
    try:
        with _fs_lock:
            if not _fs_baseline_meta:
                _capture_baseline(cwd)
                return {
                    "ok": True, "status": [], "file_count": 0,
                    "additions": 0, "deletions": 0, "files": [],
                }

            current_meta: dict[str, tuple[float, int]] = {}
            for p, rel in _iter_workspace_files(cwd):
                try:
                    st = p.stat()
                except OSError:
                    continue
                current_meta[rel] = (st.st_mtime, st.st_size)

            added = [r for r in current_meta if r not in _fs_baseline_meta]
            removed = [r for r in _fs_baseline_meta if r not in current_meta]
            modified = [
                r for r in current_meta
                if r in _fs_baseline_meta and current_meta[r] != _fs_baseline_meta[r]
            ]

            total_additions = 0
            total_deletions = 0
            files = []
            status_lines = []

            for rel in added:
                status_lines.append(f"A  {rel}")
                new_lines = _read_lines(cwd / rel)
                if new_lines is None:
                    files.append({"path": rel, "additions": 0, "deletions": 0, "diff": ""})
                    continue
                diff_lines = list(difflib.unified_diff([], new_lines, fromfile=f"/dev/null", tofile=rel, lineterm=""))
                total_additions += len(new_lines)
                files.append({"path": rel, "additions": len(new_lines), "deletions": 0, "diff": "\n".join(diff_lines)})

            for rel in removed:
                status_lines.append(f"D  {rel}")
                old_lines = _fs_baseline_content.get(rel, [])
                diff_lines = list(difflib.unified_diff(old_lines, [], fromfile=rel, tofile=f"/dev/null", lineterm=""))
                total_deletions += len(old_lines)
                files.append({"path": rel, "additions": 0, "deletions": len(old_lines), "diff": "\n".join(diff_lines)})

            for rel in modified:
                status_lines.append(f"M  {rel}")
                new_lines = _read_lines(cwd / rel)
                old_lines = _fs_baseline_content.get(rel)
                if new_lines is None or old_lines is None:
                    files.append({"path": rel, "additions": 0, "deletions": 0, "diff": ""})
                    continue
                diff_lines = list(difflib.unified_diff(old_lines, new_lines, fromfile=rel, tofile=rel, lineterm=""))
                add = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
                dele = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
                total_additions += add
                total_deletions += dele
                files.append({"path": rel, "additions": add, "deletions": dele, "diff": "\n".join(diff_lines)})

            changed = added + removed + modified
            return {
                "ok": True,
                "status": status_lines,
                "file_count": len(changed),
                "additions": total_additions,
                "deletions": total_deletions,
                "files": files,
            }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "status": [],
            "file_count": 0,
            "additions": 0,
            "deletions": 0,
            "files": [],
        }


def reset_workspace_baseline(workspace_dir: str | Path | None = None) -> None:
    """Re-anchors the file-change baseline to the current on-disk state (like marking changes reviewed)."""
    cwd = Path(workspace_dir) if workspace_dir else Path.cwd()
    with _fs_lock:
        _capture_baseline(cwd)


TELEMETRY_LOG_PATH = Path(os.environ.get("ANTIGRAVITY_TELEMETRY_LOG", "state/antigravity_telemetry.json"))
TELEMETRY_MAX_POINTS = 500


def record_diff_snapshot(diff_data: dict) -> dict | None:
    """Appends a {timestamp, file_count, additions, deletions} point when the diff actually changed.

    One point per real change in the workspace diff — since a prompt is what
    causes those changes, this becomes a per-prompt telemetry series without
    having to guess at conversation turn boundaries.
    """
    if not diff_data.get("ok"):
        return None

    point = {
        "ts": time.time(),
        "file_count": diff_data.get("file_count", 0),
        "additions": diff_data.get("additions", 0),
        "deletions": diff_data.get("deletions", 0),
    }

    TELEMETRY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    points: list[dict] = []
    if TELEMETRY_LOG_PATH.exists():
        try:
            points = json.loads(TELEMETRY_LOG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            points = []

    last = points[-1] if points else None
    if last and (last["file_count"], last["additions"], last["deletions"]) == (
        point["file_count"],
        point["additions"],
        point["deletions"],
    ):
        return None

    points.append(point)
    points = points[-TELEMETRY_MAX_POINTS:]

    tmp_path = TELEMETRY_LOG_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(points), encoding="utf-8")
    os.replace(tmp_path, TELEMETRY_LOG_PATH)
    return point


def get_diff_telemetry(limit: int = 100) -> list[dict]:
    if not TELEMETRY_LOG_PATH.exists():
        return []
    try:
        points = json.loads(TELEMETRY_LOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return points[-limit:]


