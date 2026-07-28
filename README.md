# CalVerify

> ⚠️ This project is **private and not intended for public or self-use**. It is open-sourced solely for **transparency, education, and portfolio display purposes**. The codebase is tightly coupled to UC Berkeley's Discord server infrastructure, Google Cloud configuration, and internal workflows. Running your own instance would require significant adaptation and is not supported.
>
> If you discover a security vulnerability, please report it privately by emailing **iancheung@berkeley.edu**.

A full-stack Discord verification dashboard built for the UC Berkeley Discord server. Combines a FastAPI backend, a Discord bot, and a single-page vanilla JavaScript frontend to streamline the verification workflow for thousands of students.

## Features

### Verification Workflow
- **Google OAuth 2.0 login**: restricted to an email allowlist; session tokens stored in HTTP-only, secure, same-site cookies with 1-hour expiry
- **Live Google Sheets integration**: reads form submissions directly from a Google Sheet (including cell background colors) and writes back color-coded statuses, notes, Discord usernames, and Discord IDs in real time via the Sheets API
- **Two-column bento layout**: left panel for entry navigation and actions, right panel for searching linked student databases
- **Multi-directional entry navigation**: next/previous, jump to next/previous unverified (with wrap-around), direct index input, and full-text search across all entry fields
- **One-click color-coding**: mark entries as Verified (green), Not Found (yellow), or Banned (red) with a single click; colors sync instantly to the Google Sheet and are preserved across page loads
- **Annotation system**: add and edit notes on any entry; edit Discord usernames inline via prompt dialogs
- **Auto-search on entry change**: navigating to a submission automatically searches the student database by email
- **Auto-advance mode**: after marking an entry, automatically advances to the next unverified submission
- **Keyboard shortcut system**: 8 default shortcuts for navigation and actions, fully configurable per-verifier with click-to-rebind and localStorage persistence
- **Smart role pre-selection**: the verification modal parses the entry's graduating class field, extracts the year, detects transfer status, and pre-selects the matching Discord role in the dropdown

### Student Database Search
- **Cross-file search**: searches across all linked Google Sheets and CSV files simultaneously; all columns searched via case-insensitive substring match
- **Syntax-highlighted results**: matches highlighted with yellow `<mark>` tags in results tables
- **Collapsible file groups**: results grouped by source file with accordion-style toggles; each group shows row count and last-synced timestamp

### Live Google Sheet Management
- **Link new sheets**: add new Google Sheets via URL; downloads and converts to CSV using Google Drive API (supports both native Sheets and XLSX); automatically reloads all databases
- **Rename and unlink**: manage linked sheets directly from the dashboard with confirmation dialogs
- **Sheet manifest**: tracks all linked sheets with metadata (sheet ID, tab name, upload timestamp); persisted in `manifest.json`

### Discord Integration
- **Profile lookup**: fetches high-resolution avatar (with decoration overlay), display name, canonical username, account creation date, server join date, and existing class roles
- **Role assignment**: assigns the main verified role plus a class-specific role (Class of 2023–2030, Transfer classes, Grad & Professional, UCEAP); automatically removes any previous class role to prevent duplicates
- **Canonical username auto-correction**: if the submitted Discord username differs from the actual one, silently updates the Google Sheet
- **Welcome automation**: sends students an embed containing server links and three action links; optional public welcome message in `#all-class-chat`

### Voice Channel Rule Enforcement
- **Automated consent flow**: when a user joins the designated voice channel, the bot posts an interactive rules embed with a 5-minute timed "I agree" button
- **Persistent whitelist**: consent stored in a local SQLite database so approved users don't see the prompt again
- **Auto-kick on timeout**: users who don't agree within 5 minutes are moved out of the voice channel

### Security
- **Role management**: add or remove authorized admin emails from the dashboard; master admin is permanent and cannot be removed
- **Self-removal prevention**: users cannot remove their own email from the allowlist
- **Immediate session invalidation**: removing a user's email instantly invalidates all their active sessions
- **Google OAuth 2.0 token verification**: ID tokens verified via `google.oauth2.id_token.verify_oauth2_token`
- **IP-based rate limiting**: 120 requests/minute for search and updates; 5 login attempts per 5 minutes with a 15-minute lockout
- **Referer header validation**: login endpoint restricted to requests from the legitimate domain
- **Security headers**: HSTS (2-year preload), CSP (restrictive policy), X-Frame-Options DENY, X-Content-Type-Options nosniff, strict Referrer-Policy, Permissions-Policy
- **Request body size limit**: 10 MB maximum; enforced via middleware
- **Path traversal prevention**: filename sanitization on sheet delete/rename
- **Role name whitelist**: Discord role assignment restricted to a hardcoded set of 15 valid roles
- **Session cleanup**: expired sessions removed in-memory every 5 minutes