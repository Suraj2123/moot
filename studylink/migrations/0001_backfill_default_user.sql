-- Assign pre-multi-user rows to a single local user.
--
-- Databases created before user ownership existed have NULL user_id everywhere.
-- A NULL owner is worse than useless: SQLite treats NULLs as distinct in a
-- UNIQUE index, so `UNIQUE(user_id, canvas_id)` stops preventing duplicate
-- courses on re-sync, and every scoped query silently returns nothing.
--
-- This migration is idempotent: each statement only touches rows that are still
-- unowned, so re-running it after a partial failure is safe.

INSERT INTO users (email, display_name, created_at)
SELECT 'local@studylink', 'Local user', datetime('now')
WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = 'local@studylink');

UPDATE courses
   SET user_id = (SELECT id FROM users WHERE email = 'local@studylink')
 WHERE user_id IS NULL;

UPDATE assignments
   SET user_id = (SELECT id FROM users WHERE email = 'local@studylink')
 WHERE user_id IS NULL;

UPDATE notes
   SET user_id = (SELECT id FROM users WHERE email = 'local@studylink')
 WHERE user_id IS NULL;

-- Chunks take their owner from the note they belong to rather than the default
-- user, so this stays correct if a future migration runs after real accounts
-- exist and notes are already spread across several owners.
UPDATE chunks
   SET user_id = (SELECT n.user_id FROM notes n WHERE n.id = chunks.note_id)
 WHERE user_id IS NULL;

UPDATE eval_labels
   SET user_id = (SELECT a.user_id FROM assignments a WHERE a.id = eval_labels.assignment_id)
 WHERE user_id IS NULL;

UPDATE work_sessions
   SET user_id = (SELECT a.user_id FROM assignments a WHERE a.id = work_sessions.assignment_id)
 WHERE user_id IS NULL;
