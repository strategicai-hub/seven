import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.webhook import router
from app.api import router as api_router
from app.api_sai import router as sai_router
from app.db import init_db_sync
from app.services import sai_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db_sync()
    sai_task = asyncio.create_task(sai_sync.start_polling())
    try:
        yield
    finally:
        sai_task.cancel()
        try:
            await sai_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(title="Seven Academia - API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Imagens da academia servidas localmente da pasta app/media:
# https://webhook-whatsapp.strategicai.com.br/{slug}/media/<arquivo>
app.mount(
    f"/{settings.CLIENT_SLUG}/media",
    StaticFiles(directory="app/media"),
    name="media",
)

app.include_router(router)
app.include_router(api_router)
app.include_router(sai_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
