"""
ASTRA PERSONAL — Backend FastAPI
Repository: astra-personalday
Deploy: Render (Web Service + Cron Jobs)
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging

from config import get_settings
from routers.auth import SupabaseAuthMiddleware
from routers import auth, profiles, horoscope, luna, webhooks, admin, scheduler, telegram

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Astra Personal backend avviato")
    yield
    logger.info("Backend spento")

app = FastAPI(
    title="Astra Personal API",
    version="1.0.0",
    lifespan=lifespan
)

settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url, "http://localhost:3000", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# JWT verificato su ogni richiesta (route pubbliche escluse automaticamente)
app.add_middleware(SupabaseAuthMiddleware)

app.include_router(auth.router,       prefix="/api/auth",      tags=["Auth"])
app.include_router(profiles.router,   prefix="/api/profiles",  tags=["Profiles"])
app.include_router(horoscope.router,  prefix="/api/horoscope", tags=["Horoscope"])
app.include_router(luna.router,       prefix="/api/luna",      tags=["Luna"])
app.include_router(webhooks.router,   prefix="/api/webhooks",  tags=["Webhooks"])
app.include_router(telegram.router,   prefix="/api/telegram",  tags=["Telegram"])
app.include_router(admin.router,      prefix="/api/admin",     tags=["Admin"])
app.include_router(scheduler.router,  prefix="/api/scheduler", tags=["Scheduler"])

@app.get("/health")
async def health():
    return {"status": "ok", "service": "astra-personal-api"}
