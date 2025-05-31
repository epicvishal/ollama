import logging
from uuid import uuid4
import asyncio
import aiohttp
import json
import time
import redis.asyncio as redis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse

from fastapi.middleware.cors import CORSMiddleware

# app = FastAPI()
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["http://127.0.0.1:8000"],  # Or specify ["http://127.0.0.1:8000"]
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )
# # ...existing code...


logging.basicConfig(
    filename="my_app.log",
    filemode="a",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://0.0.0.0:8001",   # <-- Add this line
        "http://127.0.0.1:8001", # (optional, for other dev setups)
        "http://localhost:8001"  # (optional, for other dev setups)
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")

REDIS_URL = "redis://localhost:6379/0"
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}
        self.generation_tasks: dict[str, asyncio.Task] = {}

    async def connect(self, websocket: WebSocket, session_id: str):
        await websocket.accept()
        self.active_connections[session_id] = websocket
        await websocket.send_json({
            "type": "session_id",
            "session_id": session_id
        })

    def disconnect(self, session_id: str):
        if session_id in self.active_connections:
            del self.active_connections[session_id]
        task = self.generation_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()

    async def send_message(self, message: dict, session_id: str):
        if session_id in self.active_connections:
            await self.active_connections[session_id].send_json(message)

    def set_task(self, session_id: str, task: asyncio.Task):
        self.generation_tasks[session_id] = task

    def stop_task(self, session_id: str):
        task = self.generation_tasks.get(session_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

manager = ConnectionManager()

OLLAMA_URL = "http://10.38.25.128:11434/api/chat"
MODEL_NAME = "mistral"

def session_key(uuid, session_id):
    return f"chat:{uuid}:{session_id}"

def session_meta_key(uuid, session_id):
    return f"chatmeta:{uuid}:{session_id}"

async def append_history(uuid, session_id, role, content):
    entry = {
        "role": role,
        "content": content,
        "timestamp": int(time.time())
    }
    await redis_client.rpush(session_key(uuid, session_id), json.dumps(entry))
    meta = await redis_client.hgetall(session_meta_key(uuid, session_id))
    if role == "user":
        await redis_client.hset(session_meta_key(uuid, session_id), mapping={
            "session_id": session_id,
            "title": content[:32] if not meta.get("title") else meta["title"],
            "preview": content[:64],
            "updated_at": str(int(time.time()))
        })
    elif role == "assistant":
        await redis_client.hset(session_meta_key(uuid, session_id), mapping={
            "session_id": session_id,
            "preview": content[:64],
            "updated_at": str(int(time.time()))
        })

async def get_history(uuid, session_id):
    entries = await redis_client.lrange(session_key(uuid, session_id), 0, -1)
    return [json.loads(e) for e in entries]

async def ensure_system_message(uuid, session_id):
    if await redis_client.llen(session_key(uuid, session_id)) == 0:
        await append_history(uuid, session_id, "system", "You are a helpful assistant.")

async def get_all_sessions(uuid):
    keys = await redis_client.keys(f"chatmeta:{uuid}:*")
    sessions = []
    for key in keys:
        meta = await redis_client.hgetall(key)
        if meta:
            sessions.append(meta)
    sessions.sort(key=lambda x: int(x.get("updated_at", "0")), reverse=True)
    return sessions

async def generate_with_ollama(uuid, session_id, websocket: WebSocket):
    await ensure_system_message(uuid, session_id)
    history = await get_history(uuid, session_id)
    payload = {
        "model": MODEL_NAME,
        "messages": history,
        "stream": True
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(OLLAMA_URL, json=payload) as resp:
                full_response = ""
                async for line in resp.content:
                    if not line or line == b"\n":
                        continue
                    try:
                        data = json.loads(line.decode("utf-8"))
                        chunk = data.get("message", {}).get("content", "")
                        full_response += chunk
                        await websocket.send_json({
                            "type": "response_chunk",
                            "content": chunk
                        })
                    except Exception:
                        pass
                await append_history(uuid, session_id, "assistant", full_response)
                await websocket.send_json({"type": "response_end"})
    except asyncio.CancelledError:
        await websocket.send_json({"type": "stopped"})
    except Exception as e:
        await websocket.send_json({"type": "error", "content": str(e)})

@app.websocket("/api/chat")
async def websocket_endpoint(websocket: WebSocket, uuid: str = Query(...)):
    session_id = str(uuid4())
    await ensure_system_message(uuid, session_id)
    try:
        await manager.connect(websocket, session_id)
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                if message.get("type") == "user_message":
                    manager.stop_task(session_id)
                    await append_history(uuid, session_id, "user", message["content"])
                    task = asyncio.create_task(
                        generate_with_ollama(uuid, session_id, websocket)
                    )
                    manager.set_task(session_id, task)
                elif message.get("type") == "stop_generation":
                    stopped = manager.stop_task(session_id)
                    if stopped:
                        await manager.send_message({"type": "stopped"}, session_id)
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(session_id)
    except Exception:
        manager.disconnect(session_id)

@app.get("/")
async def get():
    return HTMLResponse(open("static/index.html").read())

@app.get("/history_sessions")
async def history_sessions(uuid: str):
    sessions = await get_all_sessions(uuid)
    return sessions

@app.get("/history/{session_id}")
async def get_history_api(session_id: str, uuid: str):
    history = await get_history(uuid, session_id)
    return JSONResponse(content={"session_id": session_id, "history": history})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
