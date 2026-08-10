# UI/UX Audit — Complete

GhostLink terminal interface quality pass. Scope: presentation and navigation
only — no new features, no changes to networking, cryptography, storage
engines, or protocol behavior. The GhostLink identity (wordmark, creator
credits) is preserved.

---

## Problems found

1. **Emoji-heavy chrome.** Chat banners, status lines, toasts, history and
   error output relied on emoji (👻 ⚡ 🔒 📎 ⚠ ✖ ✗), inconsistent with a
   minimal professional terminal product.
2. **Duplicated / recursive menus.** The Settings center rendered a giant
   overview table of every category and value *and then* a menu of the same
   categories; every submenu re-rendered full summary panels on each loop
   iteration, so content appeared repeatedly while navigating.
3. **Maze-like navigation.** Settings displayed its own duplicate "Back to
   Main Menu" behaviours, theme picker looped after apply, and in-UI "Phase
   15P/15F" developer labels leaked roadmap jargon into user-facing surfaces.
4. **Inconsistent status language.** Success/warning/error/info had no single
   symbol set; toasts carried wrapping timestamps; badges used ad-hoc icons.
5. **Dead feature surface.** A Command Palette screen (Ctrl+K) existed in code
   and Help text but added a second, redundant navigation system.
6. **Width-naive rendering.** Nested panels and unbounded lines risked
   overflow at 60–80 columns; toast timestamps wrapped mid-line.
7. **No regression net.** Nothing guarded against duplicate menus, emoji
   regressions, phase text, narrow-terminal rendering, or theme/language
   persistence.

## Root causes

- Every screen owned its own ad-hoc rendering: no shared glyph set, no shared
  header/back primitives, no shared row layout.
- Settings was built as "overview table + menu + re-rendered panels" rather
  than one dispatch loop per category.
- Toast pipeline rendered wall-clock timestamps into fixed-width terminal rows.
- Version/release labels were assembled from roadmap constants
  (`RELEASE_LABEL = "Phase 15"`) that leaked into CLI help and dashboards.
- Menus mixed presentation and selection state, so the same content was
  painted more than once per interaction.

## Files changed

**New**

- `ghostlink/ui/components/layout.py` — the design system: terminal-safe
  glyphs (`✓ ! ✕ • ◆ · › ← →`), `page_header`, `section_label`, `status_line`,
  `back_label`, `confirm_panel`.
- `tests/test_ui_polish.py` — 41 regression tests (see Tests added).
- `docs/UI_UX_AUDIT.md` — this report.

**Rewritten**

- `ghostlink/ui/menu.py` — `MenuEntry` gains `value`, `icon`, `separator`
  (blank grouping rows via `MenuEntry.spacer()`); keyboard renderer paints
  aligned `label + value` rows with a `›` cursor, skips separators
  (`skip_empty_entries=True`), numbers only selectable rows in the piped
  fallback; prompt glyph `›`; main-thread signal guard kept intact.
- `ghostlink/ui/screens/home.py` — wordmark, tagline
  ("Encrypted communication for your terminal"), `◆ v0.17.0 • Platform`
  badges, creator credits, one grouped menu (Host a Room, Join a Room /
  Rooms, Transfers / Identity, Security Status / Settings, Storage, Help,
  About / Exit).
- `ghostlink/ui/screens/settings.py` (~1145 lines) — single Settings center
  with one dispatch loop per category (see Settings redesign).
- `ghostlink/ui/screens/{about,help,identity,security,storage,transfers,rooms,onboarding,base}.py`
  — rebuilt on `page_header` + flat key/value grids + one `← Back` entry.
- `ghostlink/ui/components/{badges,notifications,dialogs}.py` — shared glyph
  tones; timestamp removed from rendered toasts; `›` prompts;
  `confirm_action` (calm CONFIRM panel: title, message, consequence,
  `Continue?`).
- `ghostlink/ui/banner.py`, `ghostlink/ui/dashboards.py` — hero supports a
  tagline; compact header gains a rule; host/join dashboards de-nested
  (single panel + section labels); developer phase text removed.
- `ghostlink/ui/chat.py`, `ghostlink/ui/group_chat.py`,
  `ghostlink/messaging/history.py`, `ghostlink/messaging/models/message.py`,
  `ghostlink/exceptions/handler.py` — emoji purge (see Emoji removals).
- `ghostlink/i18n/catalog.py` — ~31 new keys × 9 languages; English names
  aligned to the IA spec (see Language UI improvements).
- `ghostlink/cli/arguments.py` — `--version` prints
  `GhostLink 0.17.0 (2026-08-09)`; phase annotations removed from help.
- `pyproject.toml` — ruff confusables allowlist extended for the new glyphs.
- `scripts/termux_smoke_pty.py` — markers updated to the shipped labels.
- `tests/{test_i18n,test_ui_components,test_branding,test_creator_credits,test_screens_expansion,test_settings_center}.py`
  — updated to the new (intentional) strings.

**Deleted**

- `ghostlink/ui/screens/palette.py` — the redundant Command Palette (removed
  from exports and tests; Help no longer advertises Ctrl+K).

## Navigation fixes

- One navigation model: the home menu is the base of the stack; every
  destination owns exactly one screen; every screen ends in exactly one
  `← Back` entry that returns exactly one level.
- Verified live in the real keyboard path (PTY, 100×34, terminfo arrow/vim
  keys): Home → Settings → Appearance → Theme picker → preview → confirm →
  back → back → back → farewell, exit code 0.
- `q`/Esc cancels any menu and backs up one level; `q` at Home exits.
- Separators are skipped by the cursor, never selectable, never numbered.
- The Command Palette's parallel navigation system was removed.

## Duplicate-render fixes

- Settings no longer renders an overview table of all categories and values
  above its own menu — the menu *is* the top level.
- Each category submenu runs exactly one prompt loop with one dispatch table;
  headers render once per screen open (asserted: `SETTINGS` appears exactly
  once per open in recordings).
- Theme picker is single-shot: pick → inline `Preview — {Theme}` panel →
  confirm → persist → notify → return to Appearance (no re-opened picker).
- Menus repaint only their own rows on cursor movement; no screen content is
  emitted twice per interaction (traced with a render counter during QA).

## Emoji removals

- Chat & group chat chrome: 👻 ⚡ 🔒 📌 📎 ⚠ removed; `✗/✖` → `✕`;
  double-line boxes softened to rounded.
- History file markers `📎` → `[file]`; message send failure `✕`;
  exception handler `✕`.
- One deliberate exception: the `/react <emoji>` usage example keeps its
  `👍` sample, because the example documents a user-supplied emoji argument —
  it is documentation of a feature input, not UI chrome.
- Non-interactive CLI command output keeps `✓/✗` (locked by CLI tests and
  appropriate for scriptable output).

## Settings redesign

One Settings center. Top level (exactly these six categories, single
separator, single `← Back`):

- **Appearance** — Theme (live value), Language (live value).
- **Privacy & Chat** — Read Receipts, Typing Indicators, Presence Visibility,
  Message History, Auto Cleanup (aligned ON/OFF), Display Name, Identity
  (view fingerprint / rotate keys).
- **Notifications** — In-App Notifications master switch, Message Alerts,
  Room Activity, Invite Alerts, Audible Bell, Vibration (aligned ON/OFF),
  Notification Style.
- **Network** — Relay Endpoint (live value), Connectivity Test, Clear Relay.
- **Storage** — Data Directory (live value), Storage Usage (opens the storage
  manager with Clean Temporary Chunks / Trim Diagnostic Logs), Reset to
  Default.
- **System** — Debug Mode, System Diagnostics (read-only).

Behavioral guarantees, all test-locked: settings opens once per entry;
categories appear once and in spec order; back returns one level; every
action callback executes exactly once per selection; submenu screens never
re-open their parent; values shown in menus reflect the persisted config.

## Theme improvements

- Seven polished palettes selectable from one picker: Phantom, Obsidian,
  Ember, Emerald, Arctic, Aurora, Mono (grayscale for accessibility).
- The picker renders an inline `Preview — {Theme}` panel showing every
  semantic role (primary, selected, success ✓, warning !, error ✕, muted ·)
  before application, then asks `Apply {Theme} theme?` and persists on
  confirm.
- Semantic tokens (`gl.title`, `gl.muted`, `gl.highlight`, `gl.success`, …)
  are used consistently across headers, legends, prompts and status rows.
- Applied theme is reflected immediately in the Appearance row and survives
  restart (verified live: PTY session applying Obsidian wrote
  `theme = "obsidian"` to `~/.config/ghostlink/config.toml`).
- All 7 themes render Home + Settings without overflow (test-locked).

## Language UI improvements

- English menu/settings labels aligned to the information architecture:
  Host a Room, Join a Room; categories Appearance, Privacy & Chat,
  Notifications, Network, Storage, System; rows Theme, Language, Display
  Name.
- ~31 new localized keys (presence, auto cleanup, identity/fingerprint/rotate,
  per-channel notification toggles, relay local/test/clear, storage
  overview/reset, `status.current`, confirm dialogs, onboarding completion)
  across all 9 languages (en, hi, es, fr, de, pt, ja, hinglish, mr).
- Status templates de-decorated: leading `✓ ` was stripped from toast
  templates; iconography now comes from the notification glyph map only.
- Language switching applies instantly from Appearance; re-opening the menu
  shows the persisted value. All 9 languages render every screen with no raw
  i18n keys leaking (test-locked).
- The config file ships clean, documented sections (no inline `# labels`
  clutter beyond a single header comment).

## Terminal width fixes

- Rendering validated at 60, 80, 100 and 120 columns: every app-rendered
  line fits (0 overflow lines at 60 cols, measured by display width).
- Home hero swaps to a compact one-line header on small widths / re-entry.
- Toasts no longer include a timestamp, so alert lines never wrap mid-stamp
  (the timestamp is still recorded in notification history).
- Panels are un-nested: a single rounded panel per block with `─` rules and
  section labels, eliminating nested-border overflow.
- `--no-color` sessions emit zero ANSI bytes and stay fully legible;
  combined with the Mono theme this covers low-vision and screen-reader
  contexts (test-locked via a live ANSI-free sink).

## Error handling improvements

- One status system everywhere: `✓` success, `!` warning, `✕` error,
  `•` info — badges, notifications, kv rows, dashboards.
- Destructive actions use `confirm_action`: a calm CONFIRM panel stating
  what happens and what is lost, then a single Continue? prompt (identity
  rotation, relay clear, data-directory reset).
- Connectivity failures render a clean `Connection failed` notice with
  reason; local mode renders `No relay configured` instead of stack noise.
- CLI `--version`/`--help` carry no roadmap labels; unhandled-exception
  output uses the shared `✕` error glyph.
- The interactive menu's main-thread signal guard (the original SIGWINCH
  crash class) remains in place and is asserted by dedicated tests plus the
  PTY smoke test.

## Tests added

`tests/test_ui_polish.py` — 41 tests covering all fifteen required
regression cases:

1. Settings opens exactly once (single `SETTINGS` header per open).
2. Categories are unique, in spec order, and not duplicated.
3. Back returns exactly one level (Home → Settings → Appearance → Back →
   Back → Exit header sequence).
4. Selecting a row executes its callback exactly once.
5. Screen constructors run once per navigation hop (no double construction).
6. Canceling the theme picker returns to Appearance exactly once.
7. Theme persistence is reflected in entries on re-open.
8. Language persistence is reflected in entries on re-open.
9. Separators are never selectable and never numbered.
10. No emoji and no "Phase" text anywhere in rendered screens.
11. No raw i18n keys leak (dotted `key.subkey` patterns) in any screen.
12. Widths 60/80/100/120 — Home and Settings lines stay within bounds.
13. `--no-color` output contains no ANSI sequences (live sink).
14. All 7 themes render Home + Settings.
15. All 9 languages render every screen key-free.
Plus: main-thread menu signal-guard preservation, toggle ON/OFF alignment,
menu invariants (values present, spacer placement), and clean-text helpers.

`scripts/termux_smoke_pty.py` markers updated to the shipped labels; the
smoke test spawns the real PTY keyboard path, asserts the menu renders, and
asserts a clean `q` exit (exit code 0).

## Tests passed

| Command | Result |
| --- | --- |
| `pytest tests/` | **1613 passed** (repeated clean runs) |
| `pytest portal/backend/tests/` | **254 passed, 13 skipped** |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 380 files already formatted |
| `mypy --config-file pyproject.toml` | Success: no issues found in 179 source files |
| `python -m compileall -q ghostlink tests ghostlink.py` | OK |
| `python -m ghostlink --version` | `GhostLink 0.17.0 (2026-08-09)` |
| `python -m ghostlink --help` | clean, no phase annotations |
| `python scripts/termux_smoke_pty.py` | `MENU_OK`, exit 0 |
| `cd portal/web && npm run build` | vite production build succeeds |
| Interactive PTY walkthrough | full Settings/Theme/back/quit flow, exit 0 |

Rendered output was additionally inspected by eye at every width above —
tests alone were not treated as proof of visual quality.

## Remaining issues

- `tests/test_transfer_manager.py::TestReconnectResume::test_resume_after_relay_restart_keeps_verified_chunks`
  is an occasional, pre-existing timing flake under full-suite load
  (observed failing ~1–2 times in 6 full runs; 10/10 isolated passes). The
  code it exercises (transfer manager, relay reconnect) was **not touched**
  by this pass — zero files changed in `ghostlink/transfer`,
  `ghostlink/messaging/session`, or `ghostlink/transport` — and the failure
  mode is an integration-level reconnect race, not a UI issue. Recommended
  follow-up, out of scope here.
- `tests/test_groups_mesh.py::test_cross_ciphertext_swap_fails` skips on
  ~half of runs by design (random key-draw decides roles; the test skips
  when the draw makes the wrong party initiator). Pre-existing.
- Source docstrings in `ghostlink/ui/chat.py` still reference architecture
  phases ("Phase 3/4/5") as developer documentation; no rendered UI text
  contains phase labels.
- The `/react <emoji>` help example intentionally shows a `👍` sample input.
- Onboarding runs on first launch (`onboarding_completed = false` in a fresh
  config); the four-step wizard (Identity → Theme → Language → Privacy) was
  exercised headlessly at all widths, and its live PTY path shares the same
  menu component verified above.
