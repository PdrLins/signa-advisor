-- ============================================================================
-- 023_feedback_reports.sql — "Report a problem" from the apps
-- ============================================================================
-- A signed-in user (iOS or web) sends a problem report or an idea:
-- POST /api/v1/feedback. The owner reads and triages them: GET/PATCH
-- /api/v1/feedback (area.admin). A deleted account keeps its reports,
-- anonymized (user_id -> NULL). Idempotent.
-- ============================================================================

CREATE TABLE IF NOT EXISTS feedback_reports (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID REFERENCES users(id) ON DELETE SET NULL,
    kind          VARCHAR(8)  NOT NULL DEFAULT 'bug' CHECK (kind IN ('bug', 'idea', 'other')),
    message       TEXT        NOT NULL CHECK (char_length(message) BETWEEN 1 AND 4000),
    platform      VARCHAR(8)  NOT NULL DEFAULT 'ios' CHECK (platform IN ('ios', 'web')),
    screen        VARCHAR(80),
    app_version   VARCHAR(32),
    build         VARCHAR(32),
    os_version    VARCHAR(32),
    device_model  VARCHAR(64),
    locale        VARCHAR(16),
    diagnostics   JSONB,
    status        VARCHAR(12) NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'in_progress', 'fixed', 'wont_fix', 'duplicate')),
    owner_note    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_feedback_reports_status ON feedback_reports (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_reports_user ON feedback_reports (user_id, created_at DESC);

DROP TRIGGER IF EXISTS feedback_reports_updated_at ON feedback_reports;
CREATE TRIGGER feedback_reports_updated_at BEFORE UPDATE ON feedback_reports
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

ALTER TABLE public.feedback_reports ENABLE ROW LEVEL SECURITY;
