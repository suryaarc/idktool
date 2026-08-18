# How `antigravity_watch.py` Works

## Goal
Live-tail Antigravity IDE conversation while it happen — print message as they land, no polling table on timer, no hooking process.

## Data source
Each conversation = one SQLite DB file:
```
~/.gemini/antigravity-ide/conversations/<uuid>.db
~/.gemini/antigravity/conversations/<uuid>.db
```
([antigravity_watch.py:55-58](antigravity_watch.py:55)). Table `steps`, column `step_payload` hold raw protobuf blob per message/tool-event/turn.

## Core trick: `PRAGMA data_version`
Instead watch file mtime or scan table repeatedly, script open read-only SQLite connection and ask:
```sql
PRAGMA data_version
```
Counter bump whenever ANOTHER connection commit to that DB — exactly what Antigravity IDE itself do while writing conversation. Measured cost: ~6.7 microsecond, same whether DB 41 rows or 1276 rows. So polling this 4x/second (`--interval 0.25`) essentially free ([antigravity_watch.py:1-13](antigravity_watch.py:1)).

Flow each poll ([antigravity_watch.py:558-570](antigravity_watch.py:558)):
1. Sleep `interval` sec
2. Check `data_version` — changed since last poll?
3. If yes → `emit_changes()` (real table read + decode)
4. If no → skip entirely, no table touch
5. Always run `flush_settled()` (cheap, in-memory dict check only)

Why not `.db` file mtime: DB run WAL mode, so `.db` only touched on checkpoint — can lag hours/days behind real writes. `-wal` file is real signal, `-shm` is NOT (touched merely by connection opening, gives false positive) — [antigravity_watch.py:172-190](antigravity_watch.py:172).

## Two kinds of change tracked

**1. New rows** (`emit_changes`, [antigravity_watch.py:392-426](antigravity_watch.py:392))
Query `WHERE idx > last_idx` — straightforward append detection.

**2. In-place row mutation** (same function, part 2, [antigravity_watch.py:428-465](antigravity_watch.py:428))
Antigravity update existing row, not always insert new one:
- Command approval: row start `status=9` (awaiting approval), flip to `status=3` (done) in place, no new row
- Streaming assistant reply: `step_payload` grow bigger across multiple commits as answer stream in

Script track `LENGTH(step_payload)` cheaply (not full blob — pulling blob every commit cost 8x slower, ~296KB/tick measured) per row within last `STATUS_WINDOW=30` rows. Size change → mark "pending" with timestamp, don't print yet.

## Settle-then-print (avoid spam)
`flush_settled()` run every poll regardless of `data_version` ([antigravity_watch.py:479-513](antigravity_watch.py:479)). For each pending row: has it been quiet (`args.settle`, default 1.5s) since last size change? If yes → NOW fetch full payload, decode, print as "(updated)". This stop reprinting same growing message dozens of time while streaming.

## Decode path
Reuse `antigravity_chat_decoder.py` wholesale — no separate implementation:

- `decode_message()` — schema-less protobuf wire-format walker. No `.proto` file exist for Antigravity (private/undocumented format), so decoder parse raw varint/wire-type tags blind, recursively try: UTF-8 text → base64-then-reparse → nested protobuf → else raw bytes ([antigravity_chat_decoder.py:83-144](antigravity_chat_decoder.py:83))
- `flatten()` — walk decoded tree into flat leaf list with path breadcrumb (`root.f20.f1` etc), auto-detect UUID/file-path/JSON/timestamp shape ([antigravity_chat_decoder.py:278-297](antigravity_chat_decoder.py:278))
- `classify_step()` / `STEP_ROLES` — map numeric `step_type` (14=user, 15=assistant, 9=tool_call, etc) to role, extracted via known field-path per role (`f19.f2` for user text, `f20.f1` for assistant answer) — all reverse-engineered empirically, no official schema, confirmed by cross-checking two real conversations ([antigravity_chat_decoder.py:213-220](antigravity_chat_decoder.py:213))

`antigravity_watch.py` adds own `describe()` wrapper on top ([antigravity_watch.py:286-351](antigravity_watch.py:286)) for two case decoder doesn't handle:
- Command-approval steps (`step_type=21`) — pull command string from JSON leaf, tag "AWAITING APPROVAL" if status=9
- Assistant reply — prefer real answer field (`f20.f1`) over internal reasoning (`f20.f3`); decoder's own `classify_step` prefer reasoning (right for full dump, wrong for live tail)

## Session discovery / switching
- `find_sessions()` glob `*.db` matching UUID pattern, rank by real activity (`.db` + `-wal` mtime, not `-shm`) ([antigravity_watch.py:172-199](antigravity_watch.py:172))
- Every `--rescan` sec (default 5s), re-check for newer session. Switch only if candidate decisively newer by `--switch-margin` (default 10s) — prevent flip-flop when two session write same second ([antigravity_watch.py:577-595](antigravity_watch.py:577))

## Output
- Console: colored role-tagged lines, capped length (`--max-lines` for chat, `--tool-lines` for tool dumps — file reads can be 700+ lines)
- `--json-out` (default `antigravity_watch.jsonl`): full untruncated record per event, JSONL so append-safe
- `--stats` / exit: `Telemetry` class track poll count, quiet-ratio (fraction of polls where nothing happened — validates the whole "cheap probe" premise), decode timing, per-role render count

## Safety properties
- Read-only connection only (`mode=ro` URI)
- No open transaction held across polls — long-lived reader would block WAL checkpoint, grow `-wal` unbounded
- Antigravity IDE itself never touched/disturbed — pure observer
