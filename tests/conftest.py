import asyncio

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse


def _build_upstream() -> FastAPI:
    """A real upstream, served over a real socket. The gateway dispatches with
    httpx to an absolute URL, so an in-process ASGI transport can't stand in
    for it -- the dispatch has to actually leave the process."""
    app = FastAPI()

    @app.get("/accounts/ok")
    async def ok():
        return {"account_id": "acc_1", "balance": 42.5}

    @app.get("/accounts/drifted")
    async def drifted():
        # Renamed keys -- fails validation against the expected schema.
        return {"id": "acc_1", "current_balance": 42.5}

    @app.post("/accounts/transfer/fails")
    async def transfer_fails():
        return JSONResponse({"error": "insufficient_funds"}, status_code=500)

    return app


@pytest.fixture
async def upstream_url():
    config = uvicorn.Config(_build_upstream(), host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
