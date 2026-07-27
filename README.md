# CalVerify

A Discord verification dashboard for the UC Berkeley Discord server. Authenticates users via Google OAuth, searches student records from linked Google Sheets, assigns class roles via a Discord bot, and manages voice channel rule consent.

## Features

- **Google OAuth login**: restricted to authorized email addresses
- **Student record search**: searches across linked Google Sheets/CSVs for student ID, name, email, etc.
- **Verification form management**: view, color-code, and annotate form entries from a Google Sheet
- **Discord bot integration**: look up Discord profiles and assign verified roles (class year, transfer, grad, UCEAP)
- **Sheet linking**: link additional Google Sheets to dynamically expand the search database
- **Admin management**: add/remove authorized admin emails via the dashboard
- **Voice channel rules**: Discord bot prompts users to agree to VC rules with a timed consent flow
- **Rate limiting & session management**: IP-based rate limiting and expiring HTTP-only cookie sessions

## Architecture

- **`main.py`**: FastAPI server serving the web frontend and REST API
- **`bot.py`**: Discord bot (discord.py) for role assignment and VC rule enforcement
- **`templates/index.html`**: Single-page frontend
- **`data/`**: Directory of CSV files (gitignored) loaded as searchable DataFrames via Polars
- **`manifest.json`**: Tracks linked sheets and authorized emails (gitignored)
- **`service-account-key.json`**: Google service account credentials (gitignored)

## Setup

### Prerequisites

- Python 3.10+
- A Google Cloud project with Sheets API & Drive API enabled
- A Discord application

### Configuration

Create a `.env` file in the project root:

```env
GOOGLE_CLIENT_ID=your_google_oauth_client_id
VERIFICATION_FORM_SHEET_ID=your_google_sheet_id
BOT_TOKEN=your_discord_bot_token
DISCORD_GUILD_ID=your_discord_guild_id
DISCORD_MAIN_ROLE_ID=the_main_verified_role_id
DISCORD_VC_CHANNEL_ID=voice_channel_id_for_rules
```

A Google service account key file at `service-account-key.json` with access to the verification sheet and Drive/Sheets APIs is needed.
