"""
AgentOmega server -- FastAPI + WebSocket driver with SHACKLE governance APIs.

Adds to the base non-blocking WS loop:
  - WS HITL_RESPONSE handling (operator APPROVE / SKIP / ABORT)
  - Auth-gated governance REST endpoints:
      GET  /api/audit                  -> recent audit records
      GET  /api/audit/verify           -> hash-chain + signature verification
      GET  /api/sessions               -> active session snapshots
      POST /api/sessions/{sid}/kill    -> trip circuit + cancel worker

Auth: all /api/* endpoints require `Authorization: Bearer <SHACKLE_API_TOKEN>`
when SHACKLE_API_TOKEN is set. If unset, the API refuses (fails closed) rather
than exposing audit/kill unauthenticated.
"""

import asyncio
import json
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.engine import HardenedAgentEngine, get_ledger

app = FastAPI(title="AgentOmega -- SHACKLE-governed browser agent")
templates = Jinja2Templates(directory="templates")

active_sessions = {}


def _require_auth(authorization: str) -> None:
    """Fail closed: require a bearer token that matches SHACKLE_API_TOKEN."""
    token = settings.SHACKLE_API_TOKEN
    if not token:
        raise HTTPException(status_code=503,
                            detail="Governance API disabled: SHACKLE_API_TOKEN not configured")
    expected = f"Bearer {token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ----------------------------------------------------------------------
# Governance REST API (auth-gated)
# ----------------------------------------------------------------------
@app.get("/api/audit")
async def api_audit(limit: int = 100, authorization: str = Header(default="")):
    _require_auth(authorization)
    records = get_ledger().read_all()
    return JSONResponse({"count": len(records), "records": records[-limit:]})


@app.get("/api/audit/verify")
async def api_audit_verify(authorization: str = Header(default="")):
    _require_auth(authorization)
    return JSONResponse(get_ledger().verify_chain())


@app.get("/api/sessions")
async def api_sessions(authorization: str = Header(default="")):
    _require_auth(authorization)
    out = []
    for sid, data in active_sessions.items():
        engine = data.get("engine")
        snap = engine.governor.snapshot() if engine else {"session_id": sid}
        task = data.get("worker_task")
        snap["running"] = bool(task and not task.done())
        out.append(snap)
    return JSONResponse({"count": len(out), "sessions": out})


@app.post("/api/sessions/{sid}/kill")
async def api_kill_session(sid: str, authorization: str = Header(default="")):
    _require_auth(authorization)
    data = active_sessions.get(sid)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    engine = data.get("engine")
    if engine:
        engine.governor.trip("killed via governance API")
        await engine.stop()
    task = data.get("worker_task")
    if task and not task.done():
        task.cancel()
    return JSONResponse({"killed": True, "session_id": sid})


# ----------------------------------------------------------------------
# WebSocket driver loop (non-blocking; commands never await the worker)
# ----------------------------------------------------------------------
@app.websocket("/ws/stream")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    session_id = str(uuid.uuid4())
    active_sessions[session_id] = {"socket": websocket, "worker_task": None, "engine": None}

    try:
        while True:
            raw = await websocket.receive_text()
            payload = json.loads(raw)
            action = payload.get("action")

            if action == "PRODUCE_WORKFLOW":
                goal = payload.get("goal", "")
                old_task = active_sessions[session_id].get("worker_task")
                if old_task and not old_task.done():
                    old_task.cancel()
                    try:
                        await old_task
                    except asyncio.CancelledError:
                        pass

                async def telemetry_cb(data):
                    try:
                        await websocket.send_json(data)
                    except Exception:
                        pass

                engine = HardenedAgentEngine(goal, session_id, telemetry_cb)
                active_sessions[session_id]["engine"] = engine
                worker_task = asyncio.create_task(engine.orchestrate())
                active_sessions[session_id]["worker_task"] = worker_task

            elif action == "HITL_RESPONSE":
                engine = active_sessions[session_id].get("engine")
                if engine:
                    engine.resolve_hitl(payload.get("decision", "ABORT"))
                    await websocket.send_json(
                        {"stage": "HITL_ACK", "message": f"HITL: {payload.get('decision')}"})

            elif action == "STOP_WORKFLOW":
                engine = active_sessions[session_id].get("engine")
                if engine:
                    await engine.stop()
                task = active_sessions[session_id].get("worker_task")
                if task and not task.done():
                    task.cancel()
                    await websocket.send_json({"stage": "SYSTEM", "message": "Stop signal sent."})

    except WebSocketDisconnect:
        pass
    finally:
        session_data = active_sessions.pop(session_id, None)
        if session_data and session_data.get("worker_task"):
            task = session_data["worker_task"]
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
