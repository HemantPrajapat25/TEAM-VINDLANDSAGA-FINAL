import uuid
import logging
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter()

# Use SQLite db
from services.db_service import db_service
import uuid

# ─── Schemas ──────────────────────────────────────────────────────────────────

class ExamCreate(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    description: Optional[str] = None
    code: str = Field(..., min_length=2, max_length=30, description="Short join code, e.g. CS-101")
    duration_minutes: int = Field(default=90, ge=5, le=480)
    scheduled_for: Optional[str] = None
    strictness: int = Field(default=70, ge=10, le=100)


class ExamResponse(BaseModel):
    id: str
    code: str
    title: str
    description: Optional[str] = None
    duration_minutes: int
    scheduled_for: Optional[str] = None
    created_at: str
    status: str
    strictness: int
    candidates: int = 0


# ─── Routes ────────────────────────────────────────────────────────────────────

@router.post("/", response_model=ExamResponse, summary="Create a new exam (Teacher)")
async def create_exam(exam: ExamCreate):
    code_lower = exam.code.lower().strip()
    
    # Check if code already in use
    existing = db_service.fetchone("SELECT id FROM exams WHERE code = ?", (code_lower,))
    if existing:
        raise HTTPException(status_code=409, detail=f"Exam code '{code_lower}' is already in use.")

    new_id = f"exam-{str(uuid.uuid4())[:8]}"
    created_at = datetime.utcnow().isoformat()
    status = "scheduled"

    db_service.execute(
        """INSERT INTO exams (id, institution_id, teacher_id, title, description, code, duration_minutes, scheduled_start, status, ai_strictness_level, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_id, "inst-default", "usr-teacher", exam.title, exam.description, code_lower, exam.duration_minutes, exam.scheduled_for, status, exam.strictness, created_at)
    )

    return {
        "id": new_id,
        "code": code_lower,
        "title": exam.title,
        "description": exam.description,
        "duration_minutes": exam.duration_minutes,
        "scheduled_for": exam.scheduled_for,
        "created_at": created_at,
        "status": status,
        "strictness": exam.strictness,
        "candidates": 0
    }


@router.get("/", response_model=List[ExamResponse], summary="List all exams")
async def list_exams():
    rows = db_service.fetchall(
        """SELECT e.id, e.code, e.title, e.description, e.duration_minutes, e.scheduled_start AS scheduled_for, e.created_at, e.status, e.ai_strictness_level AS strictness, 
                  (SELECT COUNT(*) FROM exam_sessions WHERE exam_id = e.id) AS candidates
           FROM exams e"""
    )
    return rows


@router.get("/code/{code}", response_model=ExamResponse, summary="Lookup exam by join code (Student onboarding)")
async def get_exam_by_code(code: str):
    """Used by the student onboarding flow to resolve an exam code to exam details."""
    code_lower = code.lower().strip()
    row = db_service.fetchone(
        """SELECT e.id, e.code, e.title, e.description, e.duration_minutes, e.scheduled_start AS scheduled_for, e.created_at, e.status, e.ai_strictness_level AS strictness, 
                  (SELECT COUNT(*) FROM exam_sessions WHERE exam_id = e.id) AS candidates
           FROM exams e WHERE e.code = ?""",
        (code_lower,)
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"No exam found with code '{code_lower}'. Please check with your supervisor.")
    return row


@router.get("/{exam_id}", response_model=ExamResponse, summary="Get exam by ID")
async def get_exam(exam_id: str):
    row = db_service.fetchone(
        """SELECT e.id, e.code, e.title, e.description, e.duration_minutes, e.scheduled_start AS scheduled_for, e.created_at, e.status, e.ai_strictness_level AS strictness, 
                  (SELECT COUNT(*) FROM exam_sessions WHERE exam_id = e.id) AS candidates
           FROM exams e WHERE e.id = ?""",
        (exam_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Exam not found")
    return row


@router.patch("/{exam_id}/status", summary="Update exam status")
async def update_exam_status(exam_id: str, status: str):
    valid = {"draft", "scheduled", "active", "completed", "cancelled"}
    if status not in valid:
        raise HTTPException(status_code=422, detail=f"Invalid status. Must be one of: {valid}")
    
    rows_affected = db_service.execute(
        "UPDATE exams SET status = ? WHERE id = ?",
        (status, exam_id)
    )
    if not rows_affected:
        raise HTTPException(status_code=404, detail="Exam not found")
    return await get_exam(exam_id)
