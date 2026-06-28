-- ==============================================================================
-- ExamGuard AI - Database Schema (PostgreSQL for Supabase)
-- ==============================================================================

-- ─── EXTENSIONS ─────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ─── ENUMS ────────────────────────────────────────────────────────────────
CREATE TYPE user_role AS ENUM ('superadmin', 'admin', 'teacher', 'student');
CREATE TYPE exam_status AS ENUM ('draft', 'scheduled', 'active', 'completed', 'cancelled');
CREATE TYPE session_status AS ENUM ('pending', 'active', 'completed', 'flagged', 'voided');
CREATE TYPE alert_severity AS ENUM ('low', 'medium', 'high', 'critical');
CREATE TYPE subscription_tier AS ENUM ('free', 'starter', 'business', 'enterprise');


-- ─── 1. INSTITUTIONS ────────────────────────────────────────────────────────
CREATE TABLE institutions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    country TEXT,
    plan subscription_tier DEFAULT 'starter',
    status TEXT DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);


-- ─── 2. USERS ───────────────────────────────────────────────────────────────
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    auth_id UUID UNIQUE, -- References auth.users(id) in Supabase
    institution_id UUID REFERENCES institutions(id) ON DELETE CASCADE,
    email TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    role user_role NOT NULL,
    department TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);


-- ─── 3. EXAMS ───────────────────────────────────────────────────────────────
CREATE TABLE exams (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    institution_id UUID REFERENCES institutions(id) ON DELETE CASCADE,
    teacher_id UUID REFERENCES users(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    description TEXT,
    duration_minutes INTEGER NOT NULL,
    scheduled_start TIMESTAMPTZ,
    scheduled_end TIMESTAMPTZ,
    status exam_status DEFAULT 'draft',
    ai_strictness_level INTEGER DEFAULT 50, -- 0 to 100
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);


-- ─── 4. EXAM ENROLLMENTS ────────────────────────────────────────────────────
CREATE TABLE exam_enrollments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    exam_id UUID REFERENCES exams(id) ON DELETE CASCADE,
    student_id UUID REFERENCES users(id) ON DELETE CASCADE,
    enrolled_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(exam_id, student_id)
);


-- ─── 5. EXAM SESSIONS ───────────────────────────────────────────────────────
CREATE TABLE exam_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    exam_id UUID REFERENCES exams(id) ON DELETE CASCADE,
    student_id UUID REFERENCES users(id) ON DELETE CASCADE,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    status session_status DEFAULT 'pending',
    final_risk_score NUMERIC(5, 2) DEFAULT 0.0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(exam_id, student_id)
);


-- ─── 6. AI MONITORING LOGS ──────────────────────────────────────────────────
CREATE TABLE monitoring_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID REFERENCES exam_sessions(id) ON DELETE CASCADE,
    frame_timestamp TIMESTAMPTZ NOT NULL,
    frame_number INTEGER,
    risk_score NUMERIC(5, 2) NOT NULL,
    risk_delta NUMERIC(5, 2) NOT NULL,
    is_suspicious BOOLEAN DEFAULT FALSE,
    severity alert_severity DEFAULT 'low',
    action_taken TEXT,
    detected_violations JSONB DEFAULT '[]', -- Array of violation strings
    ai_reasoning_summary TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);


-- ─── 7. ALERTS ──────────────────────────────────────────────────────────────
CREATE TABLE alerts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID REFERENCES exam_sessions(id) ON DELETE CASCADE,
    exam_id UUID REFERENCES exams(id) ON DELETE CASCADE,
    student_id UUID REFERENCES users(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    severity alert_severity NOT NULL,
    risk_score NUMERIC(5, 2) NOT NULL,
    explanation TEXT NOT NULL,
    has_evidence BOOLEAN DEFAULT FALSE,
    evidence_url TEXT,
    is_read BOOLEAN DEFAULT FALSE,
    is_resolved BOOLEAN DEFAULT FALSE,
    teacher_note TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    resolved_by UUID REFERENCES users(id) ON DELETE SET NULL
);


-- ─── 8. EVIDENCE ────────────────────────────────────────────────────────────
CREATE TABLE evidence (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID REFERENCES exam_sessions(id) ON DELETE CASCADE,
    alert_id UUID REFERENCES alerts(id) ON DELETE SET NULL,
    file_path TEXT NOT NULL, -- Path in Supabase Storage
    file_type TEXT DEFAULT 'image/jpeg',
    timestamp TIMESTAMPTZ NOT NULL,
    ai_confidence NUMERIC(5, 2),
    created_at TIMESTAMPTZ DEFAULT NOW()
);


-- ─── 9. REPORTS ─────────────────────────────────────────────────────────────
CREATE TABLE reports (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID REFERENCES exam_sessions(id) ON DELETE CASCADE,
    generated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    pdf_url TEXT,
    ai_summary TEXT,
    final_verdict TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);


-- ─── 10. SYSTEM SETTINGS ────────────────────────────────────────────────────
CREATE TABLE system_settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);


-- ==============================================================================
-- ROW LEVEL SECURITY (RLS) POLICIES
-- ==============================================================================

ALTER TABLE institutions ENABLE ROW LEVEL SECURITY;
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE exams ENABLE ROW LEVEL SECURITY;
ALTER TABLE exam_enrollments ENABLE ROW LEVEL SECURITY;
ALTER TABLE exam_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE monitoring_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE reports ENABLE ROW LEVEL SECURITY;

-- Utility function to get current user's role
CREATE OR REPLACE FUNCTION get_auth_role() RETURNS user_role AS $$
  SELECT role FROM users WHERE auth_id = auth.uid();
$$ LANGUAGE sql SECURITY DEFINER;

-- Utility function to get current user's institution
CREATE OR REPLACE FUNCTION get_auth_institution() RETURNS UUID AS $$
  SELECT institution_id FROM users WHERE auth_id = auth.uid();
$$ LANGUAGE sql SECURITY DEFINER;

-- Users can see their own profile, or admins/teachers can see users in their institution
CREATE POLICY user_read_policy ON users FOR SELECT
USING (
    auth_id = auth.uid() OR 
    (get_auth_role() IN ('admin', 'teacher') AND institution_id = get_auth_institution()) OR
    get_auth_role() = 'superadmin'
);

-- Exams: Teachers can see exams in their institution; Students can see exams they are enrolled in
CREATE POLICY exam_read_policy ON exams FOR SELECT
USING (
    (get_auth_role() IN ('admin', 'teacher') AND institution_id = get_auth_institution()) OR
    id IN (SELECT exam_id FROM exam_enrollments WHERE student_id = (SELECT id FROM users WHERE auth_id = auth.uid())) OR
    get_auth_role() = 'superadmin'
);

-- Sessions: Teachers can see all in institution; Students can see their own
CREATE POLICY session_read_policy ON exam_sessions FOR SELECT
USING (
    student_id = (SELECT id FROM users WHERE auth_id = auth.uid()) OR
    (get_auth_role() IN ('admin', 'teacher') AND exam_id IN (SELECT id FROM exams WHERE institution_id = get_auth_institution())) OR
    get_auth_role() = 'superadmin'
);

-- Alerts: Teachers can read alerts for exams in their institution
CREATE POLICY alert_read_policy ON alerts FOR SELECT
USING (
    (get_auth_role() IN ('admin', 'teacher') AND exam_id IN (SELECT id FROM exams WHERE institution_id = get_auth_institution())) OR
    get_auth_role() = 'superadmin'
);

-- Alerts: Teachers can update alerts for exams in their institution
CREATE POLICY alert_update_policy ON alerts FOR UPDATE
USING (
    (get_auth_role() IN ('admin', 'teacher') AND exam_id IN (SELECT id FROM exams WHERE institution_id = get_auth_institution())) OR
    get_auth_role() = 'superadmin'
);

-- Monitoring Logs: Read-only for teachers
CREATE POLICY logs_read_policy ON monitoring_logs FOR SELECT
USING (
    (get_auth_role() IN ('admin', 'teacher') AND session_id IN (
        SELECT s.id FROM exam_sessions s JOIN exams e ON s.exam_id = e.id WHERE e.institution_id = get_auth_institution()
    )) OR
    get_auth_role() = 'superadmin'
);
