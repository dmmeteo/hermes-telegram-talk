# 0016 Main owns the release and static contains only served assets

Status: accepted, 2026-11-17. User requested consolidation of outstanding worktrees into main and permanent removal
of obsolete static files.

The installed plugin had current Pulse/thread packages and the current UI, while main still pointed to the initial
public preview and its working directory contained an older uncommitted integration. Older worktrees retained the
retired static root bundle. Updating directories from those trees could restore files already deleted elsewhere.

Main is the release source. Integration preserves substantive pending fixes and records superseded branch content
explicitly; scratch work remains recoverable. Runtime uses an installed artifact, not an editable feature checkout.
`tests/release/package.py` always exports committed main through git archive into a fresh directory, with exact
source hashes. A dirty checkout or an old feature branch cannot influence the artifact. Activation must still use
the shared rollout lock, source preimage checks, an idle-call fence and health verification.

`static/talk/` holds the current index.html, app.js and app.css. The server's STATIC_FILES map defines the served file
set. `tests/release/assets.py` rejects physical files outside that set, missing files and symlinks, and this check is
part of the frontend/integrated gate and artifact construction. Browser fixtures, old UIs, evidence and scratch
files belong elsewhere. Adding a new asset requires adding its serving route and keeping that contract consistent.

Both current themes and all current controls remain. The retired voice-test UI is not restored by consolidation;
its signaling/security checks remain under tests/e2e/run-signaling.py. The actual old static tree is retained as
red evidence: app.css, app.js and index.html at the static root are rejected as extra files.
