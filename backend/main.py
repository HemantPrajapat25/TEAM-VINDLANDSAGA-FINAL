import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from dotenv import load_dotenv

from routers import monitoring, alerts, reports, analytics, exams, sessions, admin, auth

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("ExamGuard AI Backend starting up...")
    # Initialize SQLite database
    from services.db_service import db_service
    db_service.init_db()
    
    # Import and close afferens service on shutdown
    from services.afferens_service import afferens_service
    yield
    await afferens_service.close()
    logger.info("ExamGuard AI Backend shut down.")


app = FastAPI(
    title="ExamGuard AI API",
    description="AI-powered online exam invigilation backend — Afferens Vision API + GPT-4o",
    version="1.0.0",
    lifespan=lifespan,
)

# ─── Middleware ──────────────────────────────────────────────────────
frontend_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
if os.getenv("FRONTEND_URL"):
    frontend_origins.append(os.getenv("FRONTEND_URL"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# ─── Routers ────────────────────────────────────────────────────────
app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(exams.router, prefix="/api/exams", tags=["Exams"])
app.include_router(sessions.router, prefix="/api/sessions", tags=["Sessions"])
app.include_router(monitoring.router, prefix="/api/monitoring", tags=["AI Monitoring"])
app.include_router(alerts.router, prefix="/api/alerts", tags=["Alerts"])
app.include_router(reports.router, prefix="/api/reports", tags=["Reports"])
app.include_router(analytics.router, prefix="/api/analytics", tags=["Analytics"])
app.include_router(admin.router, prefix="/api/admin", tags=["Admin"])


@app.get("/health", tags=["Health"])
async def health_check():
    return {
        "status": "healthy",
        "service": "ExamGuard AI API",
        "version": "1.0.0",
        "afferens_configured": bool(os.getenv("AFFRENS_API_KEY")),
        "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
    }


@app.get("/", tags=["Root"])
async def root():
    return {"message": "ExamGuard AI API. See /docs for API reference."}
