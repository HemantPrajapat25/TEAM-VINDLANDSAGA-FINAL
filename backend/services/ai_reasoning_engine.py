from __future__ import annotations
import os
import json
import logging
from typing import Optional
from openai import AsyncOpenAI
from datetime import datetime

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o")


class AIReasoningEngine:
    """
    Context-aware AI reasoning engine powered by GPT-4o.
    Does NOT make simple rule-based decisions.
    Reasons over a sequence of observations to understand context.
    """

    SYSTEM_PROMPT = """You are ExamGuard AI, an expert online examination invigilation AI.
Your job is to analyze a sequence of observations from a student's webcam and determine:
1. Whether suspicious behavior is occurring
2. WHY it is suspicious (with specific evidence)
3. A risk score delta (how much to change the current score)
4. Recommended action (warn_student, alert_teacher, capture_evidence, escalate, ignore)
5. Severity level (low, medium, high, critical)

IMPORTANT RULES:
- Do NOT flag brief, innocent behaviors (drinking water, adjusting glasses, brief head turn)
- DO flag sustained suspicious behavior (phone visible 30+ seconds, person leaving room, repeated looking away)
- Consider sequences: a single glance left is innocent; 10 glances left in 5 minutes is suspicious
- Return ONLY valid JSON, no markdown
- Be concise in reasons (max 2 sentences per reason)

Respond with this exact JSON structure:
{
  "is_suspicious": boolean,
  "confidence": float (0.0-1.0),
  "risk_delta": int (-10 to +30),
  "severity": "low" | "medium" | "high" | "critical",
  "action": "ignore" | "warn_student" | "alert_teacher" | "capture_evidence" | "escalate",
  "reasons": [string],
  "student_message": string | null,
  "teacher_summary": string,
  "detected_violations": [string]
}"""

    def __init__(self):
        if not OPENAI_API_KEY:
            logger.warning("OPENAI_API_KEY not set. AIReasoningEngine will use rule-based fallback.")
        self.client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

    async def reason(
        self,
        current_detection: dict,
        detection_history: list[dict],
        student_context: Optional[dict] = None,
    ) -> dict:
        """
        Reason over the current detection + last N detections to produce
        a contextual decision. This is the core of the non-rule-based engine.
        """
        if not self.client:
            return self._rule_based_fallback(current_detection, detection_history)

        # Build the observation sequence for the model
        history_summary = self._summarize_history(detection_history[-20:])
        current_summary = self._summarize_detection(current_detection)

        prompt = f"""CURRENT OBSERVATION (just captured):
{json.dumps(current_summary, indent=2)}

RECENT HISTORY (last {len(detection_history[-20:])} observations, chronological):
{json.dumps(history_summary, indent=2)}

STUDENT CONTEXT:
{json.dumps(student_context or {}, indent=2)}

Analyze the complete picture and decide if action is required."""

        try:
            response = await self.client.chat.completions.create(
                model=GPT_MODEL,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)
            result["reasoned_by"] = "gpt-4o"
            return result
        except Exception as e:
            logger.error(f"GPT reasoning failed: {e}. Falling back to rule-based.")
            return self._rule_based_fallback(current_detection)

    async def explain_student(self, session_id: str, violation_history: list[dict]) -> str:
        """
        Answer teacher queries like "Why was Student X marked suspicious?"
        Returns a natural language explanation with evidence.
        """
        if not self.client:
            return "AI explanation unavailable (API key not configured)."

        prompt = f"""A teacher is asking: "Why was this student marked suspicious?"

Exam Session ID: {session_id}
Violation History:
{json.dumps(violation_history, indent=2)}

Write a clear, professional explanation (3-5 sentences) that:
1. Summarizes what happened
2. Cites specific timestamps and evidence
3. Explains why the AI flagged it
4. States the confidence level
Do NOT be accusatory. Present facts only."""

        try:
            response = await self.client.chat.completions.create(
                model=GPT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=400,
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"GPT explanation failed: {e}")
            return "Unable to generate AI explanation at this time."

    async def generate_report_summary(self, session_data: dict) -> str:
        """Generate the AI summary section for the final PDF report."""
        if not self.client:
            return "AI summary unavailable."

        prompt = f"""Generate a professional examination integrity report summary.

Session Data:
{json.dumps(session_data, indent=2)}

Write a concise (150-200 word) professional summary that:
1. States the overall risk assessment
2. Lists the most significant violations
3. Notes the timeline of key events
4. Provides a recommended action (pass/review/flag)
Use formal, neutral language suitable for an academic report."""

        try:
            response = await self.client.chat.completions.create(
                model=GPT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=300,
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"GPT report summary failed: {e}")
            return "Unable to generate AI summary."

    def _summarize_history(self, history: list[dict]) -> list[dict]:
        """Compress detection history to key signals for the prompt."""
        summary = []
        for item in history:
            det = item.get("detections", {})
            summary.append({
                "timestamp": item.get("timestamp", ""),
                "face_count": det.get("face", {}).get("count", 1),
                "phone": det.get("phone", {}).get("detected", False),
                "head_yaw": det.get("head_pose", {}).get("yaw", 0),
                "eyes_forward": det.get("eyes", {}).get("gaze_direction", "forward") == "forward",
                "objects": [o.get("type") for o in det.get("objects", [])],
                "suspicious_score": item.get("suspicious_activity", {}).get("score", 0),
            })
        return summary

    def _summarize_detection(self, detection: dict) -> dict:
        det = detection.get("detections", {})
        return {
            "face_count": det.get("face", {}).get("count", 0),
            "face_confidence": det.get("face", {}).get("confidence", 0),
            "phone_detected": det.get("phone", {}).get("detected", False),
            "head_yaw": det.get("head_pose", {}).get("yaw", 0),
            "eyes_forward": det.get("eyes", {}).get("gaze_direction", "forward") == "forward",
            "objects_detected": [o.get("type") for o in det.get("objects", [])],
            "camera_blocked": det.get("environment", {}).get("camera_blocked", False),
            "suspicious_score": detection.get("suspicious_activity", {}).get("score", 0),
        }

    def _phone_detected(self, detection: dict) -> bool:
        det = detection.get("detections", {})
        if det.get("phone", {}).get("detected", False):
            return True

        if detection.get("phone_visible") or detection.get("phone_detected"):
            return True

        raw_objects = det.get("objects") if isinstance(det.get("objects"), list) else detection.get("objects", [])
        if isinstance(raw_objects, list):
            for obj in raw_objects:
                if isinstance(obj, dict):
                    obj_type = str(obj.get("type", "")).lower()
                else:
                    obj_type = str(obj).lower()
                if "phone" in obj_type or "mobile" in obj_type:
                    return True

        suspicious_activity = detection.get("suspicious_activity", {})
        violations = suspicious_activity.get("reasons") if isinstance(suspicious_activity.get("reasons"), list) else detection.get("violations", [])
        if isinstance(violations, list):
            for reason in violations:
                if isinstance(reason, str) and "phone" in reason.lower():
                    return True

        reason_text = str(suspicious_activity.get("reasoning", "") or detection.get("reasoning", ""))
        if "phone" in reason_text.lower() or "mobile" in reason_text.lower():
            return True

        return False

    def _rule_based_fallback(self, detection: dict, detection_history: list[dict] = None) -> dict:
        """Real rule-based fallback when GPT is unavailable.
        Matches exact risk scoring from the production spec.
        Tracks persistence across frames for continuous violations.
        """
        det = detection.get("detections", {})
        reasons = []
        risk_delta = 0
        severity = "low"
        action = "ignore"
        is_suspicious = False
        student_message = None

        # Use recent history for persistence detection (last 10 frames)
        history = detection_history or []
        recent = history[-10:] if len(history) > 10 else history

        # Helper: count consecutive frames with a condition
        def count_consecutive_frames(check_fn):
            count = 0
            for item in reversed(recent + [detection]):
                if check_fn(item):
                    count += 1
                else:
                    break
            return count

        # 1. Phone detection
        phone_now = self._phone_detected(detection)
        phone_consecutive = count_consecutive_frames(lambda item: self._phone_detected(item))
        if phone_now:
            if phone_consecutive >= 3:
                reasons.append("Mobile phone visible continuously.")
                risk_delta += 40
                severity = "critical"
                action = "escalate"
                is_suspicious = True
                student_message = "Please remove your phone immediately."
            else:
                reasons.append("Mobile phone detected.")
                risk_delta += 20
                severity = "high"
                action = "alert_teacher"
                is_suspicious = True
                student_message = "Mobile phone detected. Please remove it."

        # 2. Multiple faces
        face_count = det.get("face", {}).get("count", 1)
        if face_count > 1:
            reasons.append("Multiple people detected.")
            risk_delta += 30
            severity = "high"
            action = "alert_teacher"
            is_suspicious = True
            student_message = "Multiple people detected. Only one person is allowed."

        # 3. Face missing / student left camera
        no_face = face_count == 0 or not det.get("face", {}).get("detected", True)
        no_face_consecutive = count_consecutive_frames(
            lambda d: d.get("face", {}).get("count", 1) == 0 or not d.get("face", {}).get("detected", True)
        )
        if no_face:
            if no_face_consecutive >= 2:
                reasons.append("Student has left the camera view.")
                risk_delta += 30
                severity = "high"
                action = "alert_teacher"
                is_suspicious = True
                student_message = "Please return to your seat and face the camera."
            else:
                reasons.append("Face not detected.")
                risk_delta += 15
                severity = "medium"
                action = "warn_student"
                is_suspicious = True
                student_message = "Face not detected. Please face the camera."

        # 4. Unknown person (face count > 0 but person confidence low or identity mismatch)
        person_count = det.get("person", {}).get("count", 1)
        if person_count > 1 or (det.get("person", {}).get("confidence", 1.0) < 0.5 and face_count > 0):
            reasons.append("Unknown person detected.")
            risk_delta += 35
            severity = "critical"
            action = "escalate"
            is_suspicious = True
            student_message = "Unknown person detected. Only the registered student is allowed."

        # 5. Camera blocked
        if det.get("environment", {}).get("camera_blocked", False):
            reasons.append("Camera is blocked.")
            risk_delta += 25
            severity = "critical"
            action = "escalate"
            is_suspicious = True
            student_message = "Camera is blocked. Please unblock it immediately."

        # 6. Looking away continuously
        gaze = det.get("eyes", {}).get("gaze_direction", "forward")
        head_yaw = abs(det.get("head_pose", {}).get("yaw", 0))
        looking_away = gaze in ("away", "side", "downward") or head_yaw > 45
        looking_away_consecutive = count_consecutive_frames(
            lambda d: (
                d.get("eyes", {}).get("gaze_direction", "forward") in ("away", "side", "downward")
                or abs(d.get("head_pose", {}).get("yaw", 0)) > 45
            )
        )
        if looking_away:
            if looking_away_consecutive >= 3:
                reasons.append("Looking away continuously.")
                risk_delta += 10
                if severity == "low":
                    severity = "medium"
                if action == "ignore":
                    action = "warn_student"
                is_suspicious = True
                if not student_message:
                    student_message = "Please keep your eyes on the screen."
            else:
                # Brief look - small warning only, no risk increase
                if not student_message:
                    student_message = "Please focus on your exam."

        # 7. Poor lighting
        lighting = det.get("environment", {}).get("lighting", "good")
        if lighting in ("poor", "dark"):
            reasons.append("Poor lighting detected.")
            if severity == "low":
                severity = "low"  # small warning only, minimal risk
            if action == "ignore":
                action = "warn_student"
            if not student_message:
                student_message = "Improve room lighting for better monitoring."

        # Cap risk_delta
        risk_delta = max(-10, min(50, risk_delta))

        # Build teacher summary
        if reasons:
            teacher_summary = "; ".join(reasons)
        elif lighting in ("poor", "dark"):
            teacher_summary = "Poor lighting detected."
        else:
            teacher_summary = "No suspicious activity detected."

        return {
            "is_suspicious": is_suspicious,
            "confidence": 0.85 if is_suspicious else 0.7,
            "risk_delta": risk_delta,
            "severity": severity,
            "action": action,
            "reasons": reasons,
            "student_message": student_message,
            "teacher_summary": teacher_summary,
            "detected_violations": reasons,
            "reasoned_by": "rule-based-fallback",
        }


ai_reasoning_engine = AIReasoningEngine()
