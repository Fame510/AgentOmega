import uuid
import json
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.engine import HardenedAgentEngine
from app.agents import AgentSpec, registry
from app.shackle import ledger

app = FastAPI(title="AgentOmega")
templates = Jinja2Templates(directory="templates")

active_sessions = {}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Agent Builder API ──────────────────────────────

@app.get("/api/agents")
async def list_agents():
    return [a.model_dump() for a in registry.list()]


@app.post("/api/agents")
async def create_agent(spec: AgentSpec):
    return registry.create(spec).model_dump()


@app.put("/api/agents/{agent_id}")
async def update_agent(agent_id: str, spec: AgentSpec):
    updated = registry.update(agent_id, spec)
    if not updated:
        raise HTTPException(404, "Agent not found")
    return updated.model_dump()


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str):
    if not registry.delete(agent_id):
        raise HTTPException(404, "Agent not found")
    return {"deleted": agent_id}


# ── Governance API ─────────────────────────────────

@app.get("/api/audit")
async def audit_log(limit: int = 200):
    return ledger.read_all(limit=limit)


@app.get("/api/audit/verify")
async def audit_verify():
    return ledger.verify_chain()


@app.get("/api/sessions")
async def sessions():
    out = []
    for sid, data in active_sessions.items():
        engine = data.get("engine")
        task = data.get("worker_task")
        out.append({
            "session_id": sid,
            "running": bool(task and not task.done()),
            "governance": engine.governance_snapshot() if engine else None,
        })
    return out


@app.post("/api/sessions/{session_id}/kill")
async def kill_session(session_id: str):
    data = active_sessions.get(session_id)
    if not data:
        raise HTTPException(404, "Session not found")
    task = data.get("worker_task")
    if task and not task.done():
        task.cancel()
    return {"killed": session_id}


# ── Runtime WebSocket ──────────────────────────────

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
                agent_id = payload.get("agent_id")
                agent_spec = registry.get(agent_id) if agent_id else None

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

                engine = HardenedAgentEngine(goal, session_id, telemetry_cb, agent_spec=agent_spec)
                worker_task = asyncio.create_task(engine.orchestrate())
                active_sessions[session_id]["worker_task"] = worker_task
                active_sessions[session_id]["engine"] = engine

            elif action == "HITL_RESPONSE":
                engine = active_sessions[session_id].get("engine")
                if engine:
                    engine.submit_hitl_response(payload.get("choice", "abort"))

            elif action == "STOP_WORKFLOW":
                task = active_sessions[session_id].get("worker_task")
                if task and not task.done():
                    task.cancel()
                    await websocket.send_json({"stage": "SYSTEM", "message": "Stop signal sent."})

    except WebSocketDisconnect:
        print(f"[WS] Session {session_id} disconnected.")
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
        print(f"[Session] {session_id} cleaned.")
