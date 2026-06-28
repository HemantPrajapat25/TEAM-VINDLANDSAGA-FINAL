"""
monitoring.py — The Core AI Pipeline Router
============================================

Physical Perception  →  AI Reasoning  →  Intelligent Action

Pipeline:
  1. Receive base64 webcam frame from student browser
  2. Run vision analysis (GPT-4o Vision if key available, else rule-based fallback)
  3. AI Reasoning Engine computes risk delta + student message
  4. Dynamic Risk Score updated (with gradual decay on clean frames)
  5. Persist: monitoring_log, alert (if high/critical), evidence (screenshot) in SQLite
  6. Sync live status to sessions._session_live_status for teacher dashboard
  7. Return AnalysisResult with student_message → browser warning overlay
"""

import os
import json
import uuid
import logging
from typing import Optional
from datetime import datetime
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel

from services.afferens_service import afferens_service, AfferensAPIError
from services.ai_reasoning_engine import ai_reasoning_engine
from services.db_service import db_service

logger = logging.getLogger(__name__)
router = APIRouter()

# ─── In-memory rolling caches (lightweight, no persistence needed) ─────────────
_detection_history: dict[str, list] = {}   # session_id → last 50 detection dicts
_risk_scores: dict[str, float] = {}        # session_id → current live risk score


# ─── Request / Response Schemas ───────────────────────────────────────────────

class AnalyzeFrameRequest(BaseModel):
    session_id: str
    frame_base64: str
    frame_number: int
    student_context: Optional[dict] = None   # {id, name, exam_id, exam_code}


class AnalyzeImageRequest(BaseModel):
    image_base64: str
    context: Optional[dict] = None


class AnalysisResult(BaseModel):
    session_id: str
    frame_number: int
    timestamp: str
    risk_score: float
    risk_delta: int
    is_suspicious: bool
    severity: str
    action: str
    reasons: list[str]
    student_message: Optional[str]
    teacher_summary: str
    detected_violations: list[str]
    detections: dict
    reasoned_by: str
    event_log: list[dict]


# ─── Primary Monitoring Endpoint ──────────────────────────────────────────────

@router.post("/analyze-frame", response_model=AnalysisResult)
async def analyze_frame(
    request: AnalyzeFrameRequest,
    background_tasks: BackgroundTasks,
):
    """
    PRIMARY monitoring endpoint.
    Camera → Backend → Vision API → AI Reasoning → Risk Score → Warnings → Teacher Alerts → Timeline
    """
    session_id = request.session_id
    frame_number = request.frame_number
    now = datetime.utcnow().isoformat()

    # ── Verify session exists before continuing ───────────────────────────────
    session_row = db_service.fetchone("SELECT id FROM exam_sessions WHERE id = ?", (session_id,))
    if not session_row:
        raise HTTPException(status_code=404, detail="Session not found. Please register your exam session before starting monitoring.")

    # ── Init in-memory session caches ──────────────────────────────────────────
    if session_id not in _detection_history:
        _detection_history[session_id] = []
    if session_id not in _risk_scores:
        # Load persisted risk score from DB if session exists
        db_row = db_service.fetchone(
            "SELECT final_risk_score FROM exam_sessions WHERE id = ?", (session_id,)
        )
        _risk_scores[session_id] = db_row["final_risk_score"] if db_row else 0.0

    try:
        # ── STEP 1: Vision API (Afferens or GPT-4o fallback) ──────────────────
        detection_result = await afferens_service.detectSuspiciousActivity(
            image_base64=request.frame_base64,
            history=_detection_history[session_id][-5:],
        )

        # ── STEP 2: AI Reasoning Engine ───────────────────────────────────────
        reasoning = await ai_reasoning_engine.reason(
            current_detection=detection_result,
            detection_history=_detection_history[session_id],
            student_context=request.student_context,
        )

        # ── STEP 3: Dynamic Risk Score Update ─────────────────────────────────
        risk_before = _risk_scores[session_id]
        risk_delta = reasoning.get("risk_delta", 0)
        new_risk = max(0.0, min(100.0, risk_before + risk_delta))

        # Gradual natural decay when frame is clear
        if not reasoning.get("is_suspicious") and risk_before > 0:
            decay = 2.0 if risk_before < 30 else 1.0
            new_risk = max(0.0, new_risk - decay)

        _risk_scores[session_id] = new_risk

        # ── STEP 4: Update detection history ──────────────────────────────────
        detection_result["timestamp"] = now
        detection_result["reasoning"] = reasoning
        _detection_history[session_id].append(detection_result)
        if len(_detection_history[session_id]) > 50:
            _detection_history[session_id] = _detection_history[session_id][-50:]

        # ── STEP 5: Persist monitoring log to DB ──────────────────────────────
        violations_json = json.dumps(reasoning.get("detected_violations", []))
        log_id = str(uuid.uuid4())
        db_service.execute(
            """INSERT INTO monitoring_logs
               (id, session_id, frame_timestamp, frame_number, risk_score, risk_delta,
                is_suspicious, severity, action_taken, detected_violations, ai_reasoning_summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                log_id,
                session_id,
                now,
                frame_number,
                round(new_risk, 1),
                risk_delta,
                1 if reasoning.get("is_suspicious") else 0,
                reasoning.get("severity", "low"),
                reasoning.get("action", "ignore"),
                violations_json,
                reasoning.get("teacher_summary", ""),
            ),
        )

        # ── STEP 6: Background sync + alerts + evidence ───────────────────────
        background_tasks.add_task(
            _sync_session_status,
            session_id=session_id,
            risk_score=new_risk,
            risk_before=risk_before,
            reasoning=reasoning,
            detection=detection_result,
            frame_base64=request.frame_base64,
            student_context=request.student_context or {},
        )

        # ── STEP 7: Fetch recent timeline events from DB ───────────────────────
        recent_events = _get_recent_timeline(session_id, limit=20)

        return AnalysisResult(
            session_id=session_id,
            frame_number=frame_number,
            timestamp=now,
            risk_score=round(new_risk, 1),
            risk_delta=risk_delta,
            is_suspicious=reasoning.get("is_suspicious", False),
            severity=reasoning.get("severity", "low"),
            action=reasoning.get("action", "ignore"),
            reasons=reasoning.get("reasons", []),
            student_message=reasoning.get("student_message"),
            teacher_summary=reasoning.get("teacher_summary", ""),
            detected_violations=reasoning.get("detected_violations", []),
            detections=detection_result.get("detections", {}),
            reasoned_by=reasoning.get("reasoned_by", "unknown"),
            event_log=recent_events,
        )

    except AfferensAPIError as e:
        logger.error(f"Vision API error in session {session_id}: {e}")
        raise HTTPException(
            status_code=503,
            detail={
                "error": "vision_api_unavailable",
                "message": str(e),
                "fallback": "Teacher has been notified of monitoring interruption.",
            },
        )
    except Exception as e:
        logger.error(f"Unexpected error in analyze_frame: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Secondary Endpoints ──────────────────────────────────────────────────────

@router.post("/analyze-image")
async def analyze_image(request: AnalyzeImageRequest):
    """Analyze a single image (for identity verification, environment scan, etc.)."""
    try:
        result = await afferens_service.analyzeImage(
            image_base64=request.image_base64,
            context=request.context,
        )
        return {"status": "success", "result": result}
    except AfferensAPIError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/detect-environment")
async def detect_environment(request: AnalyzeImageRequest):
    """Analyze room environment before exam starts."""
    try:
        result = await afferens_service.analyzeEnvironment(
            image_base64=request.image_base64,
        )
        return {"status": "success", "environment": result}
    except AfferensAPIError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/session/{session_id}/risk")
async def get_session_risk(session_id: str):
    """Get current risk score for a session (live in-memory + DB fallback)."""
    live = _risk_scores.get(session_id)
    if live is None:
        row = db_service.fetchone(
            "SELECT final_risk_score FROM exam_sessions WHERE id = ?", (session_id,)
        )
        live = row["final_risk_score"] if row else 0.0
    return {
        "session_id": session_id,
        "risk_score": round(live, 1),
        "frame_count": len(_detection_history.get(session_id, [])),
    }


@router.get("/session/{session_id}/history")
async def get_session_history(session_id: str, limit: int = 20):
    """Get recent detection history for a session."""
    history = _detection_history.get(session_id, [])
    return {
        "session_id": session_id,
        "history": history[-limit:],
        "total_frames": len(history),
    }


@router.get("/session/{session_id}/timeline")
async def get_session_timeline(session_id: str, limit: int = 100):
    """Get the full event timeline for a session (from DB)."""
    events = _get_recent_timeline(session_id, limit=limit)
    return {
        "session_id": session_id,
        "events": events,
        "total": len(events),
    }


# ─── Helper: Pull timeline events from monitoring_logs ────────────────────────

def _get_recent_timeline(session_id: str, limit: int = 20) -> list[dict]:
    """Build a structured event log from monitoring_logs DB entries."""
    rows = db_service.fetchall(
        """SELECT id, frame_timestamp AS timestamp, severity, detected_violations,
                  risk_score, risk_delta, ai_reasoning_summary AS explanation, action_taken
           FROM monitoring_logs
           WHERE session_id = ?
           ORDER BY frame_timestamp DESC
           LIMIT ?""",
        (session_id, limit),
    )
    events = []
    for r in rows:
        violations = []
        try:
            violations = json.loads(r.get("detected_violations") or "[]")
        except Exception:
            pass
        event_label = violations[0] if violations else (
            "Risk decayed" if (r.get("risk_delta") or 0) < 0 else "Frame analyzed"
        )
        events.append({
            "id": r["id"],
            "timestamp": r["timestamp"],
            "event": event_label,
            "type": r.get("severity", "info"),
            "risk_before": round((r.get("risk_score") or 0) - (r.get("risk_delta") or 0), 1),
            "risk_after": round(r.get("risk_score") or 0, 1),
            "explanation": r.get("explanation", ""),
        })
    # Return in chronological order
    return list(reversed(events))


# ─── Background Tasks ─────────────────────────────────────────────────────────

async def _sync_session_status(
    session_id: str,
    risk_score: float,
    risk_before: float,
    reasoning: dict,
    detection: dict,
    frame_base64: str,
    student_context: dict,
):
    """
    Background: updates DB session record, live status cache, creates alert, stores evidence.
    """
    try:
        from routers.sessions import _session_live_status

        det = detection.get("detections", {})
        face_detected = det.get("face", {}).get("count", 1) >= 1
        violations = reasoning.get("detected_violations", [])
        is_suspicious = reasoning.get("is_suspicious", False)
        severity = reasoning.get("severity", "low")

        # ── Update exam_sessions row ───────────────────────────────────────────
        last_detection = violations[0] if violations else "All clear"
        db_service.execute(
            """UPDATE exam_sessions
               SET final_risk_score = ?,
                   face_detected = ?,
                   last_detection = ?,
                   violations = violations + ?
               WHERE id = ?""",
            (
                round(risk_score, 1),
                1 if face_detected else 0,
                last_detection,
                1 if (is_suspicious and violations) else 0,
                session_id,
            ),
        )

        # Append violation to flagged_events JSON column
        if is_suspicious and violations:
            row = db_service.fetchone(
                "SELECT flagged_events FROM exam_sessions WHERE id = ?", (session_id,)
            )
            if row:
                try:
                    flagged = json.loads(row.get("flagged_events") or "[]")
                except Exception:
                    flagged = []
                flagged.append({
                    "timestamp": datetime.utcnow().isoformat(),
                    "violation": violations[0],
                    "risk": round(risk_score, 1),
                })
                db_service.execute(
                    "UPDATE exam_sessions SET flagged_events = ? WHERE id = ?",
                    (json.dumps(flagged[-50:]), session_id),
                )

        # ── Write to shared live status (teacher dashboard polling) ────────────
        live_snapshot = {
            "risk_score": round(risk_score, 1),
            "is_suspicious": is_suspicious,
            "severity": severity,
            "action": reasoning.get("action", "ignore"),
            "student_message": reasoning.get("student_message"),
            "teacher_summary": reasoning.get("teacher_summary", ""),
            "detected_violations": violations,
            "face_detected": face_detected,
            "detections": det,
            "updated_at": datetime.utcnow().isoformat(),
            "frame_base64": frame_base64,  # Latest frame for teacher grid view
        }
        _session_live_status[session_id] = live_snapshot

        # ── Create alert for high/critical events ──────────────────────────────
        if severity in ("high", "critical"):
            await _create_alert(
                session_id=session_id,
                reasoning=reasoning,
                risk_score=risk_score,
                student_context=student_context,
            )

        # ── Store screenshot evidence ───────────────────────────────────────────
        if severity in ("high", "critical") and frame_base64:
            await _store_evidence(
                session_id=session_id,
                frame_base64=frame_base64,
                reasoning=reasoning,
                risk_before=risk_before,
                risk_score=risk_score,
                student_context=student_context,
            )

    except Exception as e:
        logger.error(f"Background sync error for session {session_id}: {e}", exc_info=True)


async def _create_alert(
    session_id: str,
    reasoning: dict,
    risk_score: float,
    student_context: dict,
):
    """Persist an alert to DB and push via WebSocket to connected teachers."""
    try:
        from routers.alerts import _teacher_connections

        alert_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        event = reasoning.get("detected_violations", ["Suspicious activity"])[0]
        severity = reasoning.get("severity", "medium")

        # Resolve exam metadata
        session_row = db_service.fetchone(
            "SELECT exam_id, student_name FROM exam_sessions WHERE id = ?", (session_id,)
        )
        exam_id = session_row["exam_id"] if session_row else "unknown"
        student_name = student_context.get("name") or (session_row["student_name"] if session_row else "Unknown")
        exam_row = db_service.fetchone("SELECT title FROM exams WHERE id = ?", (exam_id,))
        exam_name = exam_row["title"] if exam_row else exam_id

        db_service.execute(
            """INSERT INTO alerts
               (id, session_id, exam_id, student_id, student_name, exam_name,
                event, severity, risk_score, explanation, has_evidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                alert_id,
                session_id,
                exam_id,
                student_context.get("id", "unknown"),
                student_name,
                exam_name,
                event,
                severity,
                round(risk_score, 1),
                reasoning.get("teacher_summary", ""),
                1,  # has_evidence = True (screenshot stored separately)
                now,
            ),
        )

        alert_payload = {
            "id": alert_id,
            "created_at": now,
            "session_id": session_id,
            "exam_id": exam_id,
            "student_name": student_name,
            "exam_name": exam_name,
            "event": event,
            "severity": severity,
            "risk_score": round(risk_score, 1),
            "explanation": reasoning.get("teacher_summary", ""),
            "has_evidence": True,
            "read": False,
            "resolved": False,
        }

        # Push via WebSocket to any connected teachers
        for key in [exam_id, "all"]:
            if key in _teacher_connections:
                dead = []
                for ws in _teacher_connections[key]:
                    try:
                        await ws.send_json({"type": "alert", "data": alert_payload})
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    _teacher_connections[key].remove(ws)

        logger.info(f"[Alert] {severity.upper()} — {event} | Session: {session_id}")

    except Exception as e:
        logger.error(f"Failed to create alert for session {session_id}: {e}", exc_info=True)


async def _store_evidence(
    session_id: str,
    frame_base64: str,
    reasoning: dict,
    risk_before: float,
    risk_score: float,
    student_context: dict,
):
    """Persist base64 screenshot evidence to the evidence table."""
    try:
        evidence_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        session_row = db_service.fetchone(
            "SELECT exam_id, student_id FROM exam_sessions WHERE id = ?", (session_id,)
        )
        exam_id = session_row["exam_id"] if session_row else "unknown"
        student_id = session_row["student_id"] if session_row else student_context.get("id", "unknown")
        violations = reasoning.get("detected_violations", [])
        detection_type = violations[0] if violations else "Suspicious activity"

        db_service.execute(
            """INSERT INTO evidence
               (id, session_id, student_id, exam_id, timestamp, screenshot,
                detection_type, confidence, risk_before, risk_after, ai_explanation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evidence_id,
                session_id,
                student_id,
                exam_id,
                now,
                frame_base64,
                detection_type,
                reasoning.get("confidence", 0.85),
                round(risk_before, 1),
                round(risk_score, 1),
                reasoning.get("teacher_summary", ""),
            ),
        )
        logger.info(f"[Evidence] Stored screenshot for session {session_id}: {detection_type}")

    except Exception as e:
        logger.error(f"Failed to store evidence for session {session_id}: {e}", exc_info=True)
