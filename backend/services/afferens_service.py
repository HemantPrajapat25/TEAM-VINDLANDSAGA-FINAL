import os
import json
import asyncio
import httpx
import base64
import logging
from typing import Optional, Any
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

AFFERENS_BASE_URL = os.getenv("AFFERENS_BASE_URL", "https://api.afferens.ai/v1")
AFFERENS_API_KEY = os.getenv("AFFRENS_API_KEY", "")
AFFERENS_TIMEOUT = int(os.getenv("AFFERENS_TIMEOUT", "30"))
AFFERENS_MAX_RETRIES = int(os.getenv("AFFERENS_MAX_RETRIES", "3"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


class AfferensAPIError(Exception):
    """Raised when the Afferens Vision API returns an error."""
    pass


class AfferensService:
    """
    Reusable service for communicating with the Afferens Vision API.
    The frontend NEVER touches this service directly.
    All vision analysis must go through this server-side service.
    """

    def __init__(self):
        self.headers = {
            "X-API-KEY": AFFERENS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=AFFERENS_BASE_URL,
                headers=self.headers,
                timeout=AFFERENS_TIMEOUT,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @retry(
        stop=stop_after_attempt(AFFERENS_MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        reraise=True,
    )
    async def _post(self, endpoint: str, payload: dict) -> dict:
        """Core POST request with retry logic.
        Priority: Afferens API -> GPT-4o Vision -> local conservative fallback.
        """
        if not AFFERENS_API_KEY:
            # Try GPT-4o Vision if OpenAI key is configured
            if OPENAI_API_KEY and OPENAI_API_KEY not in ("your_openai_api_key", ""):
                image = payload.get("image") or payload.get("frame", "")
                if image:
                    return await self._gpt4o_vision_analyze(image)
            logger.warning("Afferens API key not configured and no fallback AI available. Returning conservative local fallback.")
            return self._simulator_response()

        try:
            client = await self._get_client()
            response = await client.post(endpoint, json=payload)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"Afferens API HTTP error {e.response.status_code}: {e.response.text}")
            if e.response.status_code == 429:
                await asyncio.sleep(5)
                return self._simulator_response()
            # On HTTP error, try GPT-4o if available
            if OPENAI_API_KEY and OPENAI_API_KEY not in ("your_openai_api_key", ""):
                image = payload.get("image") or payload.get("frame", "")
                if image:
                    return await self._gpt4o_vision_analyze(image)
            return self._simulator_response()
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            logger.error(f"Afferens API network error: {e} — trying GPT-4o fallback")
            # Network unreachable — try GPT-4o if available
            if OPENAI_API_KEY and OPENAI_API_KEY not in ("your_openai_api_key", ""):
                image = payload.get("image") or payload.get("frame", "")
                if image:
                    return await self._gpt4o_vision_analyze(image)
            return self._simulator_response()
        except Exception as e:
            logger.error(f"Afferens API unexpected error: {e}")
            return self._simulator_response()

    def _simulator_response(self) -> dict:
        """Return a conservative, structured fallback when vision services are unavailable."""
        return {
            "status": "simulator",
            "detections": {
                "face": {"detected": True, "count": 1, "confidence": 0.7},
                "phone": {"detected": False, "confidence": 0.0},
                "multiple_faces": {"detected": False, "count": 1, "confidence": 0.0},
                "person": {"detected": True, "count": 1, "confidence": 0.75},
                "eyes": {"open": True, "gaze_direction": "forward", "confidence": 0.72},
                "head_pose": {"yaw": 0, "pitch": 0, "confidence": 0.7},
                "objects": [],
                "environment": {"lighting": "good", "camera_blocked": False, "confidence": 0.7},
            },
            "suspicious_activity": {
                "detected": False,
                "score": 0.0,
                "reasons": [],
                "reasoning": "Vision service unavailable; monitoring continued with conservative fallback.",
            },
            "violations": [],
            "timestamp": asyncio.get_event_loop().time(),
        }

    def _normalize_suspicious_activity_response(self, raw_response: dict) -> dict:
        """Normalize suspicious activity output into the internal detection schema."""
        if not isinstance(raw_response, dict):
            return self._simulator_response()

        normalized = dict(raw_response)
        detections = normalized.get("detections", {}) if isinstance(normalized.get("detections"), dict) else {}

        # Normalize phone detection
        phone_detected = bool(
            detections.get("phone", {}).get("detected")
            or normalized.get("phone_visible")
            or normalized.get("phone_detected")
        )
        object_list = detections.get("objects") if isinstance(detections.get("objects"), list) else normalized.get("objects", [])
        if isinstance(object_list, list):
            for obj in object_list:
                if isinstance(obj, dict) and "type" in obj:
                    obj_type = str(obj["type"]).lower()
                    if "phone" in obj_type or "mobile" in obj_type:
                        phone_detected = True
                        break
                elif isinstance(obj, str) and ("phone" in obj.lower() or "mobile" in obj.lower()):
                    phone_detected = True
                    break

        if "phone" not in detections:
            detections["phone"] = {
                "detected": phone_detected,
                "confidence": float(normalized.get("phone_confidence", 1.0 if phone_detected else 0.0)),
            }
        else:
            detections["phone"]["detected"] = phone_detected or detections["phone"].get("detected", False)

        # Normalize environment
        environment = detections.get("environment") if isinstance(detections.get("environment"), dict) else {}
        if "lighting" not in environment:
            environment["lighting"] = normalized.get("lighting", "good")
        if "camera_blocked" not in environment:
            environment["camera_blocked"] = normalized.get("camera_blocked", False)
        environment["confidence"] = float(environment.get("confidence", normalized.get("environment_confidence", 0.0)))
        detections["environment"] = environment

        # Ensure face info exists
        face = detections.get("face") if isinstance(detections.get("face"), dict) else {}
        face_count = face.get("count", normalized.get("face_count", face.get("count", 0)))
        face_detected = face.get("detected", normalized.get("face_detected", face_count > 0))
        face_confidence = float(face.get("confidence", normalized.get("face_confidence", 0.0)))
        detections["face"] = {
            "detected": bool(face_detected),
            "count": int(face_count),
            "confidence": face_confidence,
        }

        # Preserve object list
        detections["objects"] = list(object_list) if isinstance(object_list, list) else []

        # Ensure suspicious_activity exists and maps violations/reasons
        suspicious_activity = normalized.get("suspicious_activity") if isinstance(normalized.get("suspicious_activity"), dict) else {}
        if not suspicious_activity:
            suspicious_activity = {
                "detected": bool(normalized.get("suspicious") or normalized.get("phone_visible") or normalized.get("violations")),
                "score": float(normalized.get("suspicious_score", 0.0)),
                "reasons": normalized.get("violations") if isinstance(normalized.get("violations"), list) else [],
                "reasoning": str(normalized.get("reasoning", "")),
            }
        else:
            suspicious_activity["detected"] = bool(
                suspicious_activity.get("detected")
                or normalized.get("suspicious")
                or normalized.get("phone_visible")
                or normalized.get("phone_detected")
            )
            suspicious_activity["reasons"] = suspicious_activity.get("reasons") or normalized.get("violations") or []
            suspicious_activity["reasoning"] = suspicious_activity.get("reasoning", normalized.get("reasoning", ""))

        normalized["detections"] = detections
        normalized["suspicious_activity"] = suspicious_activity
        normalized["violations"] = normalized.get("violations", suspicious_activity.get("reasons", []))
        return normalized

    async def _gpt4o_vision_analyze(self, image_base64: str) -> dict:
        """Use GPT-4o Vision to analyze a webcam frame for exam violations."""
        import httpx as _httpx
        prompt = (
            "You are an AI proctoring system analyzing a student webcam frame during an online exam. "
            "Carefully examine the image and respond ONLY with a valid JSON object containing exactly these keys:\n"
            "{\n"
            '  "face_count": <integer, how many faces visible>,\n'
            '  "face_detected": <true/false>,\n'
            '  "phone_detected": <true/false>,\n'
            '  "multiple_faces": <true/false>,\n'
            '  "person_leaving": <true/false - is the student moving away from camera>,\n'
            '  "looking_away": <true/false - student looking away from screen>,\n'
            '  "camera_blocked": <true/false>,\n'
            '  "lighting": <"good"|"poor"|"dark">,\n'
            '  "suspicious": <true/false>,\n'
            '  "violations": <array of string violation names, e.g. ["Phone visible"]>,\n'
            '  "confidence": <float 0-1>,\n'
            '  "reasoning": <one sentence summary>\n'
            "}"
        )
        try:
            async with _httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": "gpt-4o",
                        "max_tokens": 400,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}", "detail": "low"}},
                                ],
                            }
                        ],
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"].strip()
                # Strip markdown code fences if present
                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                data = json.loads(content)

                violations = data.get("violations", [])
                face_count = data.get("face_count", 1)
                return {
                    "status": "gpt4o_vision",
                    "detections": {
                        "face": {"detected": data.get("face_detected", True), "count": face_count, "confidence": data.get("confidence", 0.9)},
                        "phone": {"detected": data.get("phone_detected", False), "confidence": 0.92 if data.get("phone_detected") else 0.0},
                        "multiple_faces": {"detected": data.get("multiple_faces", False), "count": face_count, "confidence": 0.9 if data.get("multiple_faces") else 0.0},
                        "person": {"detected": data.get("face_detected", True), "count": 1, "confidence": 0.95},
                        "eyes": {"open": not data.get("looking_away", False), "gaze_direction": "away" if data.get("looking_away") else "forward", "confidence": 0.88},
                        "head_pose": {"yaw": 0, "pitch": 0, "confidence": 0.85},
                        "objects": [],
                        "environment": {"lighting": data.get("lighting", "good"), "camera_blocked": data.get("camera_blocked", False), "confidence": 0.99},
                    },
                    "suspicious_activity": {
                        "detected": data.get("suspicious", False),
                        "score": 85.0 if data.get("suspicious") else 5.0,
                        "reasons": violations,
                        "reasoning": data.get("reasoning", ""),
                    },
                    "violations": violations,
                    "timestamp": asyncio.get_event_loop().time(),
                }
        except Exception as e:
            logger.warning(f"GPT-4o vision call failed: {e} — falling back to simulator")
            return self._simulator_response()



    # ─────────────────────────────────────────────
    # Public API Methods
    # ─────────────────────────────────────────────

    async def analyzeImage(self, image_base64: str, context: Optional[dict] = None) -> dict:
        """Analyze a static image for all detection types."""
        payload = {
            "image": image_base64,
            "analysis_types": ["all"],
            "context": context or {},
        }
        return await self._post("/analyze/image", payload)

    async def analyzeVideoFrame(self, frame_base64: str, session_id: str, frame_number: int, context: Optional[dict] = None) -> dict:
        """Analyze a video frame within an ongoing session context."""
        payload = {
            "frame": frame_base64,
            "session_id": session_id,
            "frame_number": frame_number,
            "analysis_types": ["face", "person", "objects", "activity", "environment"],
            "context": context or {},
        }
        return await self._post("/analyze/video-frame", payload)

    async def analyzeEnvironment(self, image_base64: str) -> dict:
        """Analyze room environment: lighting, background, desk area."""
        payload = {
            "image": image_base64,
            "analysis_types": ["environment", "lighting", "background", "desk"],
        }
        return await self._post("/analyze/environment", payload)

    async def detectObjects(self, image_base64: str, object_types: Optional[list] = None) -> dict:
        """Detect specific objects in the frame (phone, book, calculator, etc.)."""
        payload = {
            "image": image_base64,
            "object_types": object_types or ["phone", "book", "notebook", "calculator", "tablet", "monitor", "earphone"],
        }
        return await self._post("/detect/objects", payload)

    async def detectPeople(self, image_base64: str) -> dict:
        """Count and locate people in the frame."""
        payload = {
            "image": image_base64,
            "analysis_types": ["person_count", "person_location", "identity"],
        }
        return await self._post("/detect/people", payload)

    async def detectPhone(self, image_base64: str) -> dict:
        """Specifically detect mobile phones and smart devices."""
        payload = {
            "image": image_base64,
            "object_types": ["mobile_phone", "smartphone", "tablet"],
            "confidence_threshold": 0.65,
        }
        return await self._post("/detect/phone", payload)

    async def detectMultipleFaces(self, image_base64: str) -> dict:
        """Detect if more than one face is present."""
        payload = {
            "image": image_base64,
            "analysis_types": ["face_detection", "face_count", "face_identity"],
        }
        return await self._post("/detect/faces", payload)

    async def detectDeskObjects(self, image_base64: str) -> dict:
        """Analyze desk area for unauthorized materials."""
        payload = {
            "image": image_base64,
            "region": "desk",
            "object_types": ["book", "notebook", "paper", "calculator", "phone", "tablet", "cheatsheet"],
        }
        return await self._post("/detect/desk-objects", payload)

    async def detectSuspiciousActivity(self, image_base64: str, history: Optional[list] = None) -> dict:
        """
        High-level suspicious activity detection using frame history context.
        This is the primary call for the AI monitoring pipeline.
        """
        payload = {
            "image": image_base64,
            "detection_types": [
                "face_missing", "multiple_faces", "phone_visible", "book_visible",
                "looking_away", "head_turning", "person_leaving", "unknown_person",
                "camera_blocked", "hand_movement", "suspicious_gesture",
                "empty_chair", "background_change",
            ],
            "history": history or [],
            "return_confidence": True,
            "return_bounding_boxes": True,
        }
        result = await self._post("/detect/suspicious-activity", payload)
        return self._normalize_suspicious_activity_response(result)


# Singleton instance
afferens_service = AfferensService()
