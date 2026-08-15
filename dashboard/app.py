from __future__ import annotations

from datetime import datetime, timezone
import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dashboard.antigravity import (
    get_antigravity_targets,
    get_conversation,
    get_diff_telemetry,
    get_latest_plan_and_tasks,
    get_recent_activity,
    get_workspace_file_changes,
    is_agent_busy,
    list_conversations,
    record_diff_snapshot,
    respond_to_permission_prompt,
    send_chat_prompt,
    start_new_chat,
    stop_current_prompt,
)

app = FastAPI(title="Antigravity Connector Dashboard")

@app.middleware("http")
async def log_requests(request, call_next):
    print(f"[{request.method}] {request.url.path}", flush=True)
    response = await call_next(request)
    print(f"[{request.method}] {request.url.path} -> {response.status_code}", flush=True)
    return response

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


class ChatRequest(BaseModel):
    prompt: str
    mode: str = "agent"
    target_id: str | None = None


@app.get("/api/antigravity/targets")
def api_antigravity_targets():
    return get_antigravity_targets()


@app.get("/api/antigravity/activity")
def api_antigravity_activity(hours: int = 2):
    return get_recent_activity(hours=hours)


@app.get("/api/antigravity/conversations")
def api_antigravity_conversations(limit: int = 30):
    return list_conversations(limit=limit)


@app.get("/api/antigravity/conversation")
def api_antigravity_conversation(session_id: str | None = None, limit: int | None = None):
    return get_conversation(session_id=session_id, limit=limit)


@app.post("/api/antigravity/chat")
def api_antigravity_chat(req: ChatRequest):
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt cannot be empty")
    try:
        result = send_chat_prompt(req.prompt, req.mode, req.target_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not result["ok"]:
        err_msg = result.get("error") or f"Prompt did not land in target '{result.get('target_title')}'."
        raise HTTPException(status_code=502, detail=err_msg)
    return {"status": "sent", "target_title": result.get("target_title", "Antigravity IDE")}


class PermissionResponse(BaseModel):
    decision: str  # "approve" | "reject"
    target_id: str | None = None


@app.post("/api/antigravity/permission")
def api_antigravity_permission(req: PermissionResponse):
    if req.decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")
    try:
        return respond_to_permission_prompt(req.decision, req.target_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class StopRequest(BaseModel):
    target_id: str | None = None


@app.post("/api/antigravity/stop")
def api_antigravity_stop(req: StopRequest):
    try:
        return stop_current_prompt(req.target_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class NewChatRequest(BaseModel):
    target_id: str | None = None


@app.post("/api/antigravity/new-chat")
def api_antigravity_new_chat(req: NewChatRequest):
    try:
        return start_new_chat(req.target_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/antigravity/plan")
def api_antigravity_plan():
    return get_latest_plan_and_tasks()


@app.get("/api/antigravity/agent-diff")
def api_antigravity_agent_diff():
    return get_workspace_file_changes()


@app.get("/api/antigravity/telemetry")
def api_antigravity_telemetry(limit: int = 100):
    return get_diff_telemetry(limit=limit)


@app.get("/api/health")
def api_health():
    targets = get_antigravity_targets()
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bridge_connected": len(targets) > 0,
        "targets_count": len(targets),
    }


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

ws_manager = ConnectionManager()


@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        # Push initial state immediately
        # All four are synchronous and hit disk / the network; awaiting them on a
        # worker thread keeps the initial push from stalling the event loop.
        await websocket.send_json({"type": "conversation", "data": await asyncio.to_thread(get_conversation)})
        await websocket.send_json({"type": "agent_diff", "data": await asyncio.to_thread(get_workspace_file_changes)})
        await websocket.send_json({"type": "targets", "data": await asyncio.to_thread(get_antigravity_targets)})
        await websocket.send_json({"type": "busy", "data": await asyncio.to_thread(is_agent_busy)})

        last_conv_hash = ""
        last_diff_hash = ""
        last_busy = None

        while True:
            await asyncio.sleep(1.0)

            # Check conversation updates. Runs in a worker thread: the parse is
            # synchronous and, uncapped, takes seconds on a large session - doing it
            # inline blocks the whole event loop (and every HTTP route with it).
            # The data_version cache inside get_conversation makes the common case a
            # ~6us probe, but the first parse after a change is still real work.
            conv_data = await asyncio.to_thread(get_conversation)
            # Fingerprint per message rather than the full text: this ran 1x/second
            # over the entire conversation, and with history uncapped that string is
            # megabytes. Includes tool_calls/status/pending_approval, which the old
            # hash omitted - so tool cards and approval flips now actually get pushed.
            conv_hash = str([
                (m.get("idx"), m.get("role"), m.get("status"), m.get("pending_approval"),
                 sum(len(t) for t in m.get("texts", [])), len(m.get("tool_calls", [])))
                for m in conv_data.get("messages", [])
            ])
            if conv_hash != last_conv_hash:
                await websocket.send_json({"type": "conversation", "data": conv_data})
                last_conv_hash = conv_hash

            # Check workspace git status updates
            diff_data = await asyncio.to_thread(get_workspace_file_changes)
            diff_hash = str((diff_data.get("file_count"), diff_data.get("additions"), diff_data.get("deletions"), diff_data.get("status")))
            if diff_hash != last_diff_hash:
                await websocket.send_json({"type": "agent_diff", "data": diff_data})
                last_diff_hash = diff_hash
                point = record_diff_snapshot(diff_data)
                if point:
                    await websocket.send_json({"type": "telemetry_point", "data": point})

            # Check whether Antigravity is currently generating
            busy = await asyncio.to_thread(is_agent_busy)
            if busy != last_busy:
                await websocket.send_json({"type": "busy", "data": busy})
                last_busy = busy

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
