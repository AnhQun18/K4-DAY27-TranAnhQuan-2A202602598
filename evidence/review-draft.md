### Lab review after escalation (LLM confidence: 40%)

This PR adds login, token storage, cloud sync, and task ownership. The reviewer answers clarify required changes; they do not establish that those changes have been implemented. The following issues remain unresolved:

- **[blocker] `tasktrack/storage.py` — `Storage.add`:** SQL is built with an f-string containing `title`. Restore parameterized SQL for both values; test quotes and injection-like input.
- **[blocker] `tasktrack/auth.py` — `hash_password` / `issue_token`:** Unsalted MD5 and a locally derived SHA-1 token do not establish secure password storage or server-verified authentication. Use a suitable password hashing scheme and authenticated, expiring sessions; protect the saved token.
- **[blocker] `tasktrack/sync.py` — `SYNC_URL`:** The endpoint uses HTTP while sending a bearer token. Require HTTPS before transmitting credentials or task data.
- **[issue] `tasktrack/sync.py` — `_current_user_id`:** Returning user 1 for every session does not enforce ownership. Derive identity from a verified session and test per-user isolation.
- **[issue] `tasktrack/storage.py` — schema migration:** CREATE TABLE IF NOT EXISTS does not add `user_id` to existing databases. Provide a tested migration, explicit backfill policy, backup, and rollback procedure.
- **[issue] `tasktrack/sync.py` — `push_all`:** A 10-second timeout already exists. Add handling for network/HTTP failures and guaranteed database cleanup; preserve local tasks and define safe retry behavior.

Before production use, add focused tests for authentication, SQL parameters, existing-database migration, cross-user access, and failed synchronization. This lab comment is review feedback, not approval to merge.
