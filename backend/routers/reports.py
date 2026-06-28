from __future__ import annotations
import io
import logging
import os
import time
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.ai_reasoning_engine import ai_reasoning_engine

logger = logging.getLogger(__name__)
router = APIRouter()


class ReportRequest(BaseModel):
    session_id: str
    student_name: str
    student_email: str
    exam_name: str
    exam_date: str
    exam_duration: str
    institute_name: str
    final_risk_score: float
    violations: list[dict]
    timeline: list[dict]
    teacher_notes: Optional[str] = None


@router.post("/generate")
async def generate_report(request: ReportRequest):
    """
    Generate an AI-powered PDF examination report.
    Returns the PDF as a downloadable file.
    """
    try:
        # Generate AI summary
        session_data = {
            "student": request.student_name,
            "exam": request.exam_name,
            "date": request.exam_date,
            "duration": request.exam_duration,
            "final_risk": request.final_risk_score,
            "violations_count": len(request.violations),
            "violations": request.violations[:10],  # limit for token efficiency
            "timeline_events": len(request.timeline),
        }
        ai_summary = await ai_reasoning_engine.generate_report_summary(session_data)

        # Generate PDF using ReportLab
        pdf_buffer = _generate_pdf(request, ai_summary)

        filename = f"ExamGuard_Report_{request.student_name.replace(' ', '_')}_{request.exam_date}.pdf"
        return StreamingResponse(
            pdf_buffer,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except Exception as e:
        logger.error(f"Report generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Report generation failed: {e}")


@router.post("/explain")
async def explain_session(session_id: str, violation_history: list[dict]):
    """Generate AI explanation for why a student was marked suspicious."""
    explanation = await ai_reasoning_engine.explain_student(session_id, violation_history)
    return {"session_id": session_id, "explanation": explanation}


def _generate_pdf(request: ReportRequest, ai_summary: str) -> io.BytesIO:
    """Generate a professional PDF report using ReportLab."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.colors import Color, HexColor
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            HRFlowable, KeepTogether,
        )
        from reportlab.lib.units import cm
        from reportlab.lib import colors

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=2*cm, leftMargin=2*cm,
            topMargin=2*cm, bottomMargin=2*cm,
        )

        styles = getSampleStyleSheet()
        brand = HexColor("#6366f1")
        risk_color = (
            HexColor("#ef4444") if request.final_risk_score >= 80 else
            HexColor("#f97316") if request.final_risk_score >= 60 else
            HexColor("#eab308") if request.final_risk_score >= 40 else
            HexColor("#22c55e")
        )

        title_style = ParagraphStyle("Title", parent=styles["Title"], textColor=brand, fontSize=20, spaceAfter=6)
        heading_style = ParagraphStyle("Heading", parent=styles["Heading2"], textColor=brand, fontSize=13, spaceAfter=4)
        normal_style = styles["Normal"]
        normal_style.fontSize = 10

        story = []

        # Header
        story.append(Paragraph("ExamGuard AI", title_style))
        story.append(Paragraph("Examination Integrity Report", ParagraphStyle("Sub", parent=styles["Normal"], textColor=colors.grey, fontSize=12)))
        story.append(Spacer(1, 0.3*cm))
        story.append(HRFlowable(width="100%", thickness=2, color=brand))
        story.append(Spacer(1, 0.5*cm))

        # Student Info Table
        info_data = [
            ["Student Name", request.student_name, "Session ID", request.session_id[:8] + "..."],
            ["Email", request.student_email, "Institute", request.institute_name],
            ["Exam", request.exam_name, "Date", request.exam_date],
            ["Duration", request.exam_duration, "Generated", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")],
        ]
        info_table = Table(info_data, colWidths=[3.5*cm, 7*cm, 3.5*cm, 4*cm])
        info_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), HexColor("#eef2ff")),
            ("BACKGROUND", (2, 0), (2, -1), HexColor("#eef2ff")),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(info_table)
        story.append(Spacer(1, 0.5*cm))

        # Risk Score
        story.append(Paragraph("Risk Assessment", heading_style))
        risk_label = "HIGH RISK" if request.final_risk_score >= 80 else \
                     "SUSPICIOUS" if request.final_risk_score >= 60 else \
                     "NEEDS ATTENTION" if request.final_risk_score >= 40 else "LOW RISK"
        risk_data = [["Final Risk Score", f"{request.final_risk_score:.0f}/100", "Verdict", risk_label]]
        risk_table = Table(risk_data, colWidths=[5*cm, 5*cm, 5*cm, 5*cm])
        risk_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), HexColor("#fafafa")),
            ("TEXTCOLOR", (1, 0), (1, 0), risk_color),
            ("TEXTCOLOR", (3, 0), (3, 0), risk_color),
            ("FONTSIZE", (0, 0), (-1, -1), 11),
            ("FONTNAME", (1, 0), (1, 0), "Helvetica-Bold"),
            ("FONTNAME", (3, 0), (3, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("PADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(risk_table)
        story.append(Spacer(1, 0.5*cm))

        # AI Summary
        story.append(Paragraph("AI Analysis Summary", heading_style))
        story.append(Paragraph(ai_summary, normal_style))
        story.append(Spacer(1, 0.5*cm))

        # Violations
        if request.violations:
            story.append(Paragraph("Detected Violations", heading_style))
            viol_data = [["#", "Timestamp", "Event", "Severity", "Risk"]]
            for i, v in enumerate(request.violations[:20], 1):
                viol_data.append([
                    str(i),
                    v.get("timestamp", "—"),
                    v.get("event", "—")[:60],
                    v.get("severity", "—").upper(),
                    str(v.get("risk_score", "—")),
                ])
            viol_table = Table(viol_data, colWidths=[1*cm, 3.5*cm, 8*cm, 2.5*cm, 2*cm])
            viol_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), brand),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, HexColor("#f8f9ff")]),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ("PADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(viol_table)
            story.append(Spacer(1, 0.5*cm))

        # Teacher Notes
        if request.teacher_notes:
            story.append(Paragraph("Teacher Notes", heading_style))
            story.append(Paragraph(request.teacher_notes, normal_style))
            story.append(Spacer(1, 0.5*cm))

        # Footer
        story.append(HRFlowable(width="100%", thickness=1, color=colors.lightgrey))
        story.append(Spacer(1, 0.2*cm))
        story.append(Paragraph(
            f"Report generated by ExamGuard AI · {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} · Powered by Afferens Vision API & GPT-4o",
            ParagraphStyle("Footer", parent=styles["Normal"], fontSize=8, textColor=colors.grey),
        ))

        doc.build(story)
        buffer.seek(0)
        return buffer

    except ImportError:
        # If ReportLab not available, return a simple text report
        buffer = io.BytesIO()
        buffer.write(f"ExamGuard AI Report\n{'='*40}\n".encode())
        buffer.write(f"Student: {request.student_name}\n".encode())
        buffer.write(f"Exam: {request.exam_name}\n".encode())
        buffer.write(f"Risk Score: {request.final_risk_score:.0f}/100\n\n".encode())
        buffer.write(f"AI Summary:\n{ai_summary}\n".encode())
        buffer.seek(0)
        return buffer
