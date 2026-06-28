"""
alerts.py — Alert Management & Real-time WebSocket Teacher Notifications
=========================================================================

Routes:
  POST /                   Create a manual alert (teacher or admin use)
  GET  /                   List alerts with filters (severity, unread, session)
  GET  /evidence           Get all evidence records (Real Evidence Vault)
  GET  /evidence/{id}      Get single evidence record with base64 screenshot
  PATCH/{alert_id}         Mark alert read/resolved or add teacher note
  WS   /ws/{exam_id}       Teacher WebSocket for real-time push notifications
"""

import uuid
import json
import logging
from typing import Optional, List
from datetime import datetime
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from services.db_service import db_service

logger = logging.getLogger(__name__)
router = APIRouter()

# ─── WebSocket connections kept in memory (one entry per exam the teacher monitors)
_teacher_connections: dict[str, list[WebSocket]] = {}


# ─── Schemas ─────────────────────────────────────────────────────────────────

class AlertCreate(BaseModel):
    session_id: str
    student_id: str
    student_name: str
    exam_name: str
    event: str
    severity: str          # low | medium | high | critical
    risk_score: float
    explanation: str
    has_evidence: bool = False


class AlertUpdate(BaseModel):
    read: Optional[bool] = None
    resolved: Optional[bool] = None
    teacher_note: Optional[str] = None


# ─── Alert Routes ─────────────────────────────────────────────────────────────

@router.post("/")
async def create_alert(alert: AlertCreate):
    """Create a new alert (saved to DB) and push to all connected teacher WebSockets."""
    alert_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    # Resolve exam_id from session
    session_row = db_service.fetchone(
        "SELECT exam_id FROM exam_sessions WHERE id = ?", (alert.session_id,)
    )
    exam_id = session_row["exam_id"] if session_row else "unknown"

    db_service.execute(
        """INSERT INTO alerts
           (id, session_id, exam_id, student_id, student_name, exam_name,
            event, severity, risk_score, explanation, has_evidence, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            alert_id,
            alert.session_id,
            exam_id,
            alert.student_id,
            alert.student_name,
            alert.exam_name,
            alert.event,
            alert.severity,
            alert.risk_score,
            alert.explanation,
            1 if alert.has_evidence else 0,
            now,
        ),
    )

    alert_payload = {
        "id": alert_id,
        "created_at": now,
        "session_id": alert.session_id,
        "exam_id": exam_id,
        "student_name": alert.student_name,
        "exam_name": alert.exam_name,
        "event": alert.event,
        "severity": alert.severity,
        "risk_score": alert.risk_score,
        "explanation": alert.explanation,
        "has_evidence": alert.has_evidence,
        "read": False,
        "resolved": False,
    }

    # Push to teacher WebSockets (exam-specific and "all")
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

    logger.info(f"Alert created: [{alert.severity}] {alert.event} for {alert.student_name}")
    return {"status": "created", "alert_id": alert_id, "alert": alert_payload}


@router.get("/")
async def list_alerts(
    severity: Optional[str] = None,
    unread_only: bool = False,
    session_id: Optional[str] = None,
    limit: int = 50,
):
    """List alerts from DB with optional filters."""
    query = "SELECT * FROM alerts"
    params = []
    conditions = []

    if severity:
        conditions.append("severity = ?")
        params.append(severity)
    if unread_only:
        conditions.append("is_read = 0")
    if session_id:
        conditions.append("session_id = ?")
        params.append(session_id)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = db_service.fetchall(query, tuple(params))
    alerts = []
    for r in rows:
        alerts.append({
            "id": r["id"],
            "created_at": r["created_at"],
            "session_id": r["session_id"],
            "exam_id": r.get("exam_id"),
            "student_name": r.get("student_name"),
            "exam_name": r.get("exam_name"),
            "event": r["event"],
            "severity": r["severity"],
            "risk_score": r["risk_score"],
            "explanation": r.get("explanation", ""),
            "has_evidence": bool(r.get("has_evidence", 0)),
            "read": bool(r.get("is_read", 0)),
            "resolved": bool(r.get("is_resolved", 0)),
            "teacher_note": r.get("teacher_note"),
        })

    return {"alerts": alerts, "total": len(alerts)}


@router.patch("/{alert_id}")
async def update_alert(alert_id: str, update: AlertUpdate):
    """Mark alert as read/resolved or add teacher note."""
    fields = []
    values = []

    if update.read is not None:
        fields.append("is_read = ?")
        values.append(1 if update.read else 0)
    if update.resolved is not None:
        fields.append("is_resolved = ?")
        values.append(1 if update.resolved else 0)
    if update.teacher_note is not None:
        fields.append("teacher_note = ?")
        values.append(update.teacher_note)

    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    values.append(alert_id)
    rows_affected = db_service.execute(
        f"UPDATE alerts SET {', '.join(fields)} WHERE id = ?",
        tuple(values),
    )
    if not rows_affected:
        raise HTTPException(status_code=404, detail="Alert not found")

    row = db_service.fetchone("SELECT * FROM alerts WHERE id = ?", (alert_id,))
    return {
        "status": "updated",
        "alert": {
            "id": row["id"],
            "read": bool(row.get("is_read", 0)),
            "resolved": bool(row.get("is_resolved", 0)),
            "teacher_note": row.get("teacher_note"),
        },
    }


# ─── Evidence Vault Routes ────────────────────────────────────────────────────

@router.get("/evidence")
async def list_evidence(
    session_id: Optional[str] = None,
    exam_id: Optional[str] = None,
    limit: int = 50,
    include_screenshot: bool = False,
):
    """
    Real Evidence Vault — fetch violation records from DB.
    By default excludes large base64 screenshots for performance;
    use include_screenshot=true or GET /evidence/{id} for the full image.
    """
    query = """
        SELECT e.id, e.session_id, e.student_id, e.exam_id, e.timestamp,
               e.detection_type, e.confidence, e.risk_before, e.risk_after,
               e.ai_explanation, e.created_at,
               es.student_name, ex.title AS exam_title, ex.code AS exam_code
               {screenshot_col}
        FROM evidence e
        LEFT JOIN exam_sessions es ON e.session_id = es.id
        LEFT JOIN exams ex ON e.exam_id = ex.id
    """
    screenshot_col = ", e.screenshot" if include_screenshot else ", NULL AS screenshot"
    query = query.format(screenshot_col=screenshot_col)

    params = []
    conditions = []
    if session_id:
        conditions.append("e.session_id = ?")
        params.append(session_id)
    if exam_id:
        conditions.append("e.exam_id = ?")
        params.append(exam_id)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY e.timestamp DESC LIMIT ?"
    params.append(limit)

    rows = db_service.fetchall(query, tuple(params))
    evidence_list = []
    for r in rows:
        item = {
            "id": r["id"],
            "session_id": r["session_id"],
            "student_id": r.get("student_id"),
            "student_name": r.get("student_name", "Unknown"),
            "exam_id": r.get("exam_id"),
            "exam_title": r.get("exam_title", "Unknown Exam"),
            "exam_code": r.get("exam_code"),
            "timestamp": r["timestamp"],
            "detection_type": r.get("detection_type", "Violation"),
            "confidence": r.get("confidence", 0.0),
            "risk_before": r.get("risk_before", 0.0),
            "risk_after": r.get("risk_after", 0.0),
            "ai_explanation": r.get("ai_explanation", ""),
            "has_screenshot": True,
        }
        if include_screenshot and r.get("screenshot"):
            item["screenshot"] = r["screenshot"]
        evidence_list.append(item)

    return {"evidence": evidence_list, "total": len(evidence_list)}


@router.get("/evidence/{evidence_id}")
async def get_evidence(evidence_id: str):
    """Get a single evidence record including the base64 screenshot."""
    row = db_service.fetchone(
        """SELECT e.*, es.student_name, ex.title AS exam_title, ex.code AS exam_code
           FROM evidence e
           LEFT JOIN exam_sessions es ON e.session_id = es.id
           LEFT JOIN exams ex ON e.exam_id = ex.id
           WHERE e.id = ?""",
        (evidence_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "student_id": row.get("student_id"),
        "student_name": row.get("student_name", "Unknown"),
        "exam_id": row.get("exam_id"),
        "exam_title": row.get("exam_title", "Unknown Exam"),
        "exam_code": row.get("exam_code"),
        "timestamp": row["timestamp"],
        "screenshot": row.get("screenshot"),
        "detection_type": row.get("detection_type", "Violation"),
        "confidence": row.get("confidence", 0.0),
        "risk_before": row.get("risk_before", 0.0),
        "risk_after": row.get("risk_after", 0.0),
        "ai_explanation": row.get("ai_explanation", ""),
    }


# ─── WebSocket for Real-time Teacher Alerts ───────────────────────────────────

@router.websocket("/ws/{exam_id}")
async def alert_websocket(websocket: WebSocket, exam_id: str):
    """
    Teacher WebSocket: connects to this endpoint and receives live alert pushes.
    Use exam_id='all' to subscribe to alerts for all exams.
    """
    await websocket.accept()
    if exam_id not in _teacher_connections:
        _teacher_connections[exam_id] = []
    _teacher_connections[exam_id].append(websocket)
    logger.info(f"Teacher connected to exam {exam_id} WebSocket")

    try:
        # Immediately send the last 10 unread alerts for this exam
        recent_rows = db_service.fetchall(
            "SELECT * FROM alerts WHERE (exam_id = ? OR ? = 'all') AND is_read = 0 ORDER BY created_at DESC LIMIT 10",
            (exam_id, exam_id),
        )
        for row in reversed(recent_rows):
            await websocket.send_json({
                "type": "alert",
                "data": {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "session_id": row.get("session_id"),
                    "student_name": row.get("student_name"),
                    "exam_name": row.get("exam_name"),
                    "event": row["event"],
                    "severity": row["severity"],
                    "risk_score": row["risk_score"],
                    "explanation": row.get("explanation", ""),
                    "has_evidence": bool(row.get("has_evidence", 0)),
                    "read": False,
                },
            })

        await websocket.send_json({
            "type": "connected",
            "exam_id": exam_id,
            "message": "Real-time alert monitoring active",
        })

        while True:
            data = await websocket.receive_json()
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
            elif data.get("type") == "mark_read":
                alert_id = data.get("alert_id")
                if alert_id:
                    db_service.execute("UPDATE alerts SET is_read = 1 WHERE id = ?", (alert_id,))

    except WebSocketDisconnect:
        if exam_id in _teacher_connections:
            try:
                _teacher_connections[exam_id].remove(websocket)
            except ValueError:
                pass
        logger.info(f"Teacher disconnected from exam {exam_id}")
