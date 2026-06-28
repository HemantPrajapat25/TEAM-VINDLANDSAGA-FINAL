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

# ─── Shared state that monitoring.py can import ────────────────────────
# This dict holds the very latest analysis result per session_id so that
# the teacher dashboard can poll it via GET /api/sessions/{id}/status
_session_live_status: dict = {}


class StudentIdentity(BaseModel):
    name: str
    email: str
    student_id: Optional[str] = None


class SessionJoinRequest(BaseModel):
    exam_code: str
    student: StudentIdentity
    environment_scan_passed: bool = True
    device_check_passed: bool = True


class SessionJoinResponse(BaseModel):
    session_id: str
    student_id: str
    exam_code: str
    student_name: Optional[str] = None
    started_at: str
    message: str


class SessionStart(BaseModel):
    exam_id: str
    student_id: str


class SessionResponse(BaseModel):
    id: str
    exam_id: Optional[str] = None
    exam_code: Optional[str] = None
    student_id: Optional[str] = None
    student_name: Optional[str] = None
    student_email: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    status: str
    risk_score: float
    violations: int
    last_detection: str
    camera_active: bool
    mic_active: bool
    face_detected: bool
    flagged_events: List[dict] = Field(default_factory=list)


def _get_or_create_student(identity: StudentIdentity) -> dict:
    email_lower = identity.email.lower().strip()
    row = db_service.fetchone("SELECT * FROM users WHERE LOWER(email) = ? AND role = 'student'", (email_lower,))
    if row:
        return row
    
    # Create a new student user
    new_id = f"student-{str(uuid.uuid4())[:8]}"
    db_service.execute(
        "INSERT INTO users (id, institution_id, email, full_name, role) VALUES (?, ?, ?, ?, ?)",
        (new_id, "inst-default", email_lower, identity.name, "student")
    )
    return {
        "id": new_id,
        "full_name": identity.name,
        "email": email_lower,
        "student_id": identity.student_id
    }


def _map_session_row(row: dict) -> dict:
    import json
    flagged_events = []
    if row.get("flagged_events"):
        try:
            flagged_events = json.loads(row["flagged_events"])
        except Exception:
            flagged_events = [row["flagged_events"]]
            
    return {
        "id": row["id"],
        "exam_id": row["exam_id"],
        "student_id": row["student_id"],
        "student_name": row.get("student_name"),
        "student_email": row.get("student_email"),
        "started_at": row["started_at"],
        "ended_at": row.get("ended_at"),
        "status": row["status"],
        "risk_score": row.get("final_risk_score", 0.0),
        "violations": row.get("violations", 0),
        "last_detection": row.get("last_detection", "Joined"),
        "camera_active": bool(row.get("camera_active", 1)),
        "mic_active": bool(row.get("mic_active", 1)),
        "face_detected": bool(row.get("face_detected", 1)),
        "flagged_events": flagged_events
    }


# ─── Routes ────────────────────────────────────────────────────────────────────

@router.post("/join", response_model=SessionJoinResponse, summary="Student joins exam via onboarding flow")
async def join_exam(req: SessionJoinRequest):
    if not req.environment_scan_passed:
        raise HTTPException(status_code=400, detail="Environment scan must pass before joining.")
    if not req.device_check_passed:
        raise HTTPException(status_code=400, detail="Device check must pass before joining.")

    student = _get_or_create_student(req.student)

    # Resolve exam join code to DB exam ID
    exam = db_service.fetchone("SELECT id FROM exams WHERE code = ?", (req.exam_code.lower().strip(),))
    if not exam:
        raise HTTPException(status_code=404, detail="Invalid exam code. Please check the exam access code and try again.")
    exam_id = exam["id"]

    session_id = str(uuid.uuid4())
    started_at = datetime.utcnow().isoformat()
    status = "active"

    # Upsert exam session
    existing_session = db_service.fetchone(
        "SELECT id FROM exam_sessions WHERE exam_id = ? AND student_id = ?",
        (exam_id, student["id"])
    )
    if existing_session:
        session_id = existing_session["id"]
        db_service.execute(
            """UPDATE exam_sessions 
               SET started_at = ?, ended_at = NULL, status = ?, final_risk_score = 0.0, violations = 0, last_detection = 'Joined', flagged_events = '[]'
               WHERE id = ?""",
            (started_at, status, session_id)
        )
    else:
        db_service.execute(
            """INSERT INTO exam_sessions (id, exam_id, student_id, student_name, student_email, started_at, status, final_risk_score, violations, last_detection)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, 0, 'Joined')""",
            (session_id, exam_id, student["id"], student.get("full_name"), student["email"], started_at, status)
        )

    logger.info(f"Session started: {session_id} for student {student['id']} in exam {req.exam_code}")

    return SessionJoinResponse(
        session_id=session_id,
        student_id=student["id"],
        exam_code=req.exam_code,
        student_name=student.get("full_name"),
        started_at=started_at,
        message=f"Welcome {student.get('full_name')}! Your session is active.",
    )


@router.post("/start", response_model=SessionResponse, summary="Start a session (teacher/internal use)")
async def start_session(req: SessionStart):
    session_id = str(uuid.uuid4())
    started_at = datetime.utcnow().isoformat()
    status = "active"

    # Fetch student name/email
    student = db_service.fetchone("SELECT full_name, email FROM users WHERE id = ?", (req.student_id,))
    s_name = student["full_name"] if student else "Unknown"
    s_email = student["email"] if student else "unknown@examguard.ai"

    db_service.execute(
        """INSERT INTO exam_sessions (id, exam_id, student_id, student_name, student_email, started_at, status, final_risk_score, violations, last_detection)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, 0, 'Started')""",
        (session_id, req.exam_id, req.student_id, s_name, s_email, started_at, status)
    )

    row = db_service.fetchone("SELECT * FROM exam_sessions WHERE id = ?", (session_id,))
    return _map_session_row(row)


@router.post("/{session_id}/end", summary="End an active session")
async def end_session(session_id: str):
    ended_at = datetime.utcnow().isoformat()
    rows_affected = db_service.execute(
        "UPDATE exam_sessions SET ended_at = ?, status = 'completed' WHERE id = ?",
        (ended_at, session_id)
    )
    if not rows_affected:
        raise HTTPException(status_code=404, detail="Session not found")
        
    # Remove from live cache
    if session_id in _session_live_status:
        _session_live_status.pop(session_id, None)

    row = db_service.fetchone("SELECT * FROM exam_sessions WHERE id = ?", (session_id,))
    return _map_session_row(row)


@router.get("/", response_model=List[SessionResponse], summary="List all sessions")
async def list_sessions():
    rows = db_service.fetchall("SELECT * FROM exam_sessions")
    return [_map_session_row(r) for r in rows]


@router.get("/{session_id}", response_model=SessionResponse, summary="Get session by ID")
async def get_session(session_id: str):
    row = db_service.fetchone("SELECT * FROM exam_sessions WHERE id = ?", (session_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    return _map_session_row(row)


@router.get("/{session_id}/live-status", summary="Get live AI monitoring status for this session")
async def get_session_live_status(session_id: str):
    """Returns the latest AI analysis result for real-time teacher view."""
    status = _session_live_status.get(session_id)
    if not status:
        row = db_service.fetchone("SELECT * FROM exam_sessions WHERE id = ?", (session_id,))
        if not row:
            return {"session_id": session_id, "available": False}
        return {
            "session_id": session_id,
            "available": True,
            "risk_score": row.get("final_risk_score", 0.0),
            "is_suspicious": row.get("final_risk_score", 0.0) >= 40,
            "severity": "high" if row.get("final_risk_score", 0.0) >= 60 else "low",
            "student_message": None,
            "teacher_summary": row.get("last_detection", "Joined"),
            "detected_violations": [],
            "face_detected": bool(row.get("face_detected", 1)),
            "detections": {},
            "updated_at": row.get("created_at") or datetime.utcnow().isoformat(),
            "frame_base64": None
        }
    return {"session_id": session_id, "available": True, **status}


@router.patch("/{session_id}/risk", summary="Update session risk score from monitoring")
async def update_risk_score(session_id: str, risk_score: float):
    if not 0 <= risk_score <= 100:
        raise HTTPException(status_code=422, detail="risk_score must be between 0 and 100")
        
    rows_affected = db_service.execute(
        "UPDATE exam_sessions SET final_risk_score = ? WHERE id = ?",
        (risk_score, session_id)
    )
    if not rows_affected:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session_id, "risk_score": risk_score}
