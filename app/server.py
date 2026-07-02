import os
import uuid
import json
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.engine import HardenedAgentEngine

app = FastAPI(title="Hardened Agent")
templates = Jinja2Templates(directory="templates")

active_sessions = {}

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.websocket("/ws/stream")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    session_id = str(uuid.uuid4())
    active_sessions[session_id] = {"socket": websocket, "worker_task": None}

    try:
        while True:
            # Non‑blocking read loop – never awaits the worker task.
            raw = await websocket.receive_text()
            payload = json.loads(raw)

            if payload.get("action") == "PRODUCE_WORKFLOW":
                goal = payload.get("goal", "")

                # Cancel previous task if any
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
                worker_task = asyncio.create_task(engine.orchestrate())
                active_sessions[session_id]["worker_task"] = worker_task
                # Do NOT await – keep loop alive for commands.

            elif payload.get("action") == "STOP_WORKFLOW":
                task = active_sessions[session_id].get("worker_task")
                if task and not task.done():
                    task.cancel()
                    await websocket.send_json({"stage": "SYSTEM", "message": "Stop signal sent."})

    except WebSocketDisconnect:
        print(f"[WS] Session {session_id} disconnected.")
    finally:
        # Cleanup
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
