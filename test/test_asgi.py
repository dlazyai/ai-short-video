import asyncio

from fastapi import FastAPI

from app.asgi import application_lifespan


def test_application_lifespan_starts_and_stops_cleanly():
    async def run_lifespan():
        async with application_lifespan(FastAPI()):
            pass

    asyncio.run(run_lifespan())
