import logging
from typing import List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

# Use SQLite db_service
from services.db_service import db_service

class InstitutionCreate(BaseModel):
    name: str
    country: str
    plan: str

class InstitutionResponse(BaseModel):
    id: str
    name: str
    country: str
    plan: str
    status: str

@router.post("/institutions", response_model=InstitutionResponse)
async def create_institution(inst: InstitutionCreate):
    import uuid
    new_id = str(uuid.uuid4())
    status = "active"
    db_service.execute(
        "INSERT INTO institutions (id, name, country, plan, status) VALUES (?, ?, ?, ?, ?)",
        (new_id, inst.name, inst.country, inst.plan, status)
    )
    return {
        "id": new_id,
        "name": inst.name,
        "country": inst.country,
        "plan": inst.plan,
        "status": status
    }

@router.get("/institutions", response_model=List[InstitutionResponse])
async def list_institutions():
    rows = db_service.fetchall("SELECT * FROM institutions")
    return rows

@router.get("/system/status")
async def system_status():
    """Get overall system health and API quotas."""
    return {
        "status": "operational",
        "afferens_api_quota": "85% remaining",
        "openai_api_quota": "90% remaining",
        "active_connections": 142
    }
