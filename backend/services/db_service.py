from __future__ import annotations
import os
import sqlite3
import logging

logger = logging.getLogger(__name__)

# Resolve db directory and file path
SERVICES_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SERVICES_DIR)
DATABASE_DIR = os.path.join(BACKEND_DIR, "..", "database")
DB_PATH = os.path.abspath(os.path.join(DATABASE_DIR, "examguard.db"))

class DatabaseService:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        logger.info(f"Database service initialized at {self.db_path}")

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Enable foreign keys
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def init_db(self):
        """Create tables if they do not exist."""
        logger.info("Initializing SQLite database tables...")
        conn = self._get_connection()
        cursor = conn.cursor()

        # 1. Institutions
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS institutions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            country TEXT,
            plan TEXT DEFAULT 'starter',
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 2. Users
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            auth_id TEXT UNIQUE,
            institution_id TEXT REFERENCES institutions(id) ON DELETE CASCADE,
            email TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL,
            department TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 3. Exams
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS exams (
            id TEXT PRIMARY KEY,
            institution_id TEXT REFERENCES institutions(id) ON DELETE CASCADE,
            teacher_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            title TEXT NOT NULL,
            description TEXT,
            code TEXT UNIQUE NOT NULL,
            duration_minutes INTEGER NOT NULL,
            scheduled_start TEXT,
            scheduled_end TEXT,
            status TEXT DEFAULT 'draft',
            ai_strictness_level INTEGER DEFAULT 50,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 4. Exam Sessions
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS exam_sessions (
            id TEXT PRIMARY KEY,
            exam_id TEXT REFERENCES exams(id) ON DELETE CASCADE,
            student_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            student_name TEXT,
            student_email TEXT,
            started_at TEXT,
            ended_at TEXT,
            status TEXT DEFAULT 'pending',
            final_risk_score REAL DEFAULT 0.0,
            violations INTEGER DEFAULT 0,
            last_detection TEXT DEFAULT 'Joined',
            camera_active INTEGER DEFAULT 1,
            mic_active INTEGER DEFAULT 1,
            face_detected INTEGER DEFAULT 1,
            flagged_events TEXT DEFAULT '[]',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(exam_id, student_id)
        );
        """)

        # 5. Monitoring Logs
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS monitoring_logs (
            id TEXT PRIMARY KEY,
            session_id TEXT REFERENCES exam_sessions(id) ON DELETE CASCADE,
            frame_timestamp TEXT NOT NULL,
            frame_number INTEGER,
            risk_score REAL NOT NULL,
            risk_delta REAL NOT NULL,
            is_suspicious INTEGER DEFAULT 0,
            severity TEXT DEFAULT 'low',
            action_taken TEXT,
            detected_violations TEXT DEFAULT '[]',
            ai_reasoning_summary TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 6. Alerts
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            session_id TEXT REFERENCES exam_sessions(id) ON DELETE CASCADE,
            exam_id TEXT REFERENCES exams(id) ON DELETE CASCADE,
            student_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            student_name TEXT,
            exam_name TEXT,
            event TEXT NOT NULL,
            severity TEXT NOT NULL,
            risk_score REAL NOT NULL,
            explanation TEXT NOT NULL,
            has_evidence INTEGER DEFAULT 0,
            evidence_url TEXT,
            is_read INTEGER DEFAULT 0,
            is_resolved INTEGER DEFAULT 0,
            teacher_note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 7. Evidence
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS evidence (
            id TEXT PRIMARY KEY,
            session_id TEXT REFERENCES exam_sessions(id) ON DELETE CASCADE,
            alert_id TEXT REFERENCES alerts(id) ON DELETE SET NULL,
            student_id TEXT,
            exam_id TEXT,
            timestamp TEXT NOT NULL,
            screenshot TEXT NOT NULL, -- base64 screenshot data
            detection_type TEXT,
            confidence REAL,
            risk_before REAL,
            risk_after REAL,
            ai_explanation TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 8. Reports
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY,
            session_id TEXT REFERENCES exam_sessions(id) ON DELETE CASCADE,
            generated_by TEXT REFERENCES users(id) ON DELETE SET NULL,
            pdf_url TEXT,
            ai_summary TEXT,
            final_verdict TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 9. System Settings
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        conn.commit()

        # ─── SEED DEFAULT DATA IF EMPTY ───────────────────────────────────────
        # Seed an institution
        cursor.execute("SELECT COUNT(*) FROM institutions")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
            INSERT INTO institutions (id, name, country, plan, status)
            VALUES ('inst-default', 'ExamGuard Academy', 'USA', 'enterprise', 'active')
            """)

        # Seed a teacher user
        cursor.execute("SELECT COUNT(*) FROM users")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
            INSERT INTO users (id, auth_id, institution_id, email, full_name, role, department)
            VALUES ('usr-teacher', 'auth-teacher', 'inst-default', 'teacher@examguard.ai', 'Professor Smith', 'teacher', 'Computer Science')
            """)

        # Seed default exams
        cursor.execute("SELECT COUNT(*) FROM exams")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
            INSERT INTO exams (id, institution_id, teacher_id, title, description, code, duration_minutes, scheduled_start, scheduled_end, status, ai_strictness_level)
            VALUES ('exam-cs-101', 'inst-default', 'usr-teacher', 'Data Structures Final Exam', 'Final evaluation of BSTs, sorting, and space complexities.', 'cs-101', 90, '2026-06-28T10:00:00', '2026-06-28T11:30:00', 'scheduled', 75)
            """)
            cursor.execute("""
            INSERT INTO exams (id, institution_id, teacher_id, title, description, code, duration_minutes, scheduled_start, scheduled_end, status, ai_strictness_level)
            VALUES ('exam-algo-202', 'inst-default', 'usr-teacher', 'Algorithms Analysis Quiz', 'Asymptotic notation, recurrence relations, and graph searches.', 'algo-202', 45, '2026-07-02T14:00:00', '2026-07-02T14:45:00', 'draft', 50)
            """)
            cursor.execute("""
            INSERT INTO exams (id, institution_id, teacher_id, title, description, code, duration_minutes, scheduled_start, scheduled_end, status, ai_strictness_level)
            VALUES ('exam-net-305', 'inst-default', 'usr-teacher', 'Computer Networks Midterm', 'TCP/IP layers, routing algorithms, DNS protocol.', 'net-305', 120, '2026-06-25T09:00:00', '2026-06-25T11:00:00', 'completed', 85)
            """)

        conn.commit()
        conn.close()
        logger.info("SQLite database tables initialized and seeded successfully.")

    def execute(self, query: str, params: tuple = None) -> int:
        """Execute a write query (INSERT, UPDATE, DELETE) and return changes row count."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            conn.commit()
            changes = cursor.rowcount
            return changes
        finally:
            conn.close()

    def fetchall(self, query: str, params: tuple = None) -> list[dict]:
        """Execute a SELECT query and return all matching rows as dict list."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def fetchone(self, query: str, params: tuple = None) -> dict | None:
        """Execute a SELECT query and return the first matching row as dict or None."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

# Singleton instance
db_service = DatabaseService()
