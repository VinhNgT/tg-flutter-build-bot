# 🤖 Telegram Flutter Build Bot

A self-hosted Telegram bot that **clones any Flutter project**, **builds an APK**, **uploads it to Google Drive**, and **sends the download link** — all from a single `/build` command.

## Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Obtaining API Keys](#obtaining-api-keys)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Telegram Bot Commands](#telegram-bot-commands)
- [Web Admin UI](#web-admin-ui)
- [Project Structure](#project-structure)
- [Development Workflow](#development-workflow)
- [Troubleshooting](#troubleshooting)

---

## Features

- **Generic Flutter builder** — point it at any Flutter repo, it handles the rest
- **Telegram interface** — trigger builds, check status, and get download links via chat
- **Google Drive upload** — APKs auto-upload with shareable links
- **Web Admin UI** — configure everything from a browser at `http://localhost:8080`
- **Commit deduplication** — re-requesting the same commit returns the cached link
- **Auto-recovery** — if a Drive file is deleted, the bot re-uploads from a local copy
- **Build concurrency lock** — one build at a time, clear status feedback
- **Cooldown system** — prevents accidental build spam
- **Build history pruning** — keeps only the last N builds (local + Drive)
- **Configuration precedence** — Web UI > Environment Variables > `.env` file > Defaults
- **Chat whitelist** — restrict bot access to specific Telegram chat IDs

---

## Prerequisites

| Tool        | Version | Purpose                            |
| ----------- | ------- | ---------------------------------- |
| **Python**  | ≥ 3.11  | Runtime                            |
| **uv**      | latest  | Package & virtualenv manager       |
| **Git**     | any     | Cloning target repositories        |
| **Flutter** | stable  | Building APKs (must be on `$PATH`) |

> [!NOTE]
> The machine running the bot must have the full Flutter SDK and Android SDK configured.
> Run `flutter doctor` to verify your setup before starting the bot.

---

## Obtaining API Keys

You need credentials from **two** external services: Telegram and Google Cloud.

### 1. Telegram Bot Token

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Follow the prompts to choose a **name** and **username** for your bot
4. BotFather replies with a token like `123456789:ABCdefGhI-jklMNOpqrSTUvwxYZ`
5. Copy the token — this is your `TELEGRAM_BOT_TOKEN`

> [!TIP]
> While chatting with @BotFather, send `/setcommands` and select your bot, then paste:
> ```
> start - Show help and available commands
> build - Build latest commit (or specify branch/hash)
> status - Current build status and config
> recent - Recent build history
> ```
> This gives your bot a nice command menu in Telegram.

### 2. Finding Your Telegram Chat ID

The bot uses chat IDs to restrict who can trigger builds.

1. Add your bot to a group, or start a private chat with it
2. Send any message to the bot
3. Open this URL in your browser (replace `<TOKEN>` with your bot token):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
4. Look for `"chat":{"id":123456789}` in the JSON response — that number is the chat ID
5. For groups, the ID will be a **negative** number (e.g., `-1001234567890`)

> [!NOTE]
> If you leave `ALLOWED_CHAT_IDS` empty, the bot accepts commands from **any** chat.
> This is useful for initial setup, but you should lock it down afterward.

### 3. Google Cloud OAuth2 Credentials (for Drive Upload)

This step is **optional** — the bot can build APKs without Drive. But without it, there's no download link.

#### a. Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click the project dropdown (top-left) → **New Project**
3. Name it (e.g., `flutter-build-bot`) → **Create**
4. Make sure the new project is selected in the dropdown

#### b. Enable the Google Drive API

1. Go to **APIs & Services** → **Library**
2. Search for **Google Drive API**
3. Click it → **Enable**

#### c. Configure the OAuth Consent Screen

1. Go to **APIs & Services** → **OAuth consent screen**
2. Select **External** user type → **Create**
3. Fill in the required fields:
   - **App name**: e.g., `Flutter Build Bot`
   - **User support email**: your email
   - **Developer contact email**: your email
4. Click **Save and Continue** through the remaining steps (Scopes, Test users, Summary)
5. On the **Test users** step, **add your own Google email** as a test user

> [!IMPORTANT]
> While the app is in "Testing" status, **only test users you explicitly add** can complete the OAuth flow. Add every Google account that will authorize the bot.

#### d. Create OAuth Client Credentials

1. Go to **APIs & Services** → **Credentials**
2. Click **Create Credentials** → **OAuth 2.0 Client ID**
3. Application type: **Web application**
4. Name: anything (e.g., `Build Bot Web`)
5. Under **Authorized redirect URIs**, add:
   ```
   http://localhost:8080/oauth/callback
   ```
   > If you're running on a remote server, use its public URL instead.
6. Click **Create**
7. Copy the **Client ID** and **Client Secret** — these are your `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`

#### e. Complete the OAuth Flow

After starting the bot:

1. Navigate to `http://localhost:8080/oauth`
2. Click **Connect Google Drive**
3. Sign in with a Google account that is listed as a **test user**
4. Grant the requested permission (`drive.file` scope — the bot can only access files it creates)
5. You'll be redirected back and see a success message

The bot stores the refresh token in `data/config.json` and auto-refreshes access tokens.

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/VinhNgT/tg-flutter-build-bot.git
cd tg-flutter-build-bot
```

### 2. Install dependencies

```bash
uv sync
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Required
TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
REPO_URL=https://github.com/user/my-flutter-app.git
ALLOWED_CHAT_IDS=123456789,987654321

# Required for Google Drive upload
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
```

### 4. Run the bot

```bash
uv run tg-flutter-build-bot
```

The bot starts two services concurrently:
- **Telegram bot** — polls for commands
- **Web UI** — `http://localhost:8080`

---

## Configuration

Configuration follows a strict **precedence chain**:

```
Web UI (saved)  →  Environment Variable  →  .env File  →  Hardcoded Default
```

### All Settings

| Setting             | Env Variable         | Default                                         | Description                       |
| ------------------- | -------------------- | ----------------------------------------------- | --------------------------------- |
| `telegram_token`    | `TELEGRAM_BOT_TOKEN` | *(required)*                                    | Bot token from @BotFather         |
| `repo_url`          | `REPO_URL`           | *(required)*                                    | Git URL of the Flutter project    |
| `build_command`     | `BUILD_COMMAND`      | `flutter build apk --release`                   | Build command to execute          |
| `build_output_path` | `BUILD_OUTPUT_PATH`  | `build/app/outputs/flutter-apk/app-release.apk` | Relative path to built artifact   |
| `allowed_chat_ids`  | `ALLOWED_CHAT_IDS`   | *(empty = allow all)*                           | Comma-separated Telegram chat IDs |
| `cooldown_seconds`  | `COOLDOWN_SECONDS`   | `300`                                           | Seconds between builds            |
| `max_builds`        | `MAX_BUILDS`         | `3`                                             | Max build records to keep         |
| `drive_folder_name` | `DRIVE_FOLDER_NAME`  | `{projectName}-tg-flutter-build-bot`            | Google Drive folder name          |
| `web_port`          | `WEB_PORT`           | `8080`                                          | Port for the Web Admin UI         |

---

## Telegram Bot Commands

| Command           | Description                              |
| ----------------- | ---------------------------------------- |
| `/start`          | Show welcome message and command list    |
| `/build`          | Build latest commit on `main`            |
| `/build <branch>` | Build latest commit on a specific branch |
| `/build <hash>`   | Build a specific commit (≥ 7 hex chars)  |
| `/status`         | Show build status, cooldown, and config  |
| `/recent`         | List the 5 most recent builds with links |

---

## Web Admin UI

Access at `http://localhost:8080` (or your configured port).

| Page          | Path      | Description                                           |
| ------------- | --------- | ----------------------------------------------------- |
| **Dashboard** | `/`       | Overview: config summary, recent builds, Drive status |
| **Config**    | `/config` | Edit all settings with source badges                  |
| **OAuth**     | `/oauth`  | Connect/disconnect Google Drive                       |
| **Builds**    | `/builds` | Full build history with delete option                 |

---

## Project Structure

```
tg-flutter-build-bot/
├── pyproject.toml                   # Metadata, dependencies, CLI entry point
├── uv.lock                         # Locked dependency versions
├── .env.example                    # Template for environment variables
│
├── src/tg_flutter_build_bot/
│   ├── main.py                     # Entry point — boots bot + web server
│   ├── config.py                   # Pydantic models, env mapping, precedence logic
│   ├── store.py                    # JSON persistence (config, builds, artifacts)
│   │
│   ├── bot/
│   │   ├── handlers.py             # /start, /build, /status, /recent
│   │   └── filters.py              # Chat ID whitelist
│   │
│   ├── builder/
│   │   └── service.py              # Git clone, Flutter build, temp dir lifecycle
│   │
│   ├── drive/
│   │   └── uploader.py             # OAuth2 flow, Drive upload/delete/folder
│   │
│   └── web/
│       ├── app.py                  # FastAPI factory
│       ├── routes.py               # Dashboard, config, OAuth, build routes
│       └── templates/              # Jinja2 HTML templates
│
└── data/                           # Runtime data (gitignored)
    ├── config.json                 # Saved config + OAuth tokens
    ├── builds.json                 # Build history
    └── builds/*.apk                # Local APK copies
```

---

## Development Workflow

### Running Locally

```bash
# Run the bot
uv run tg-flutter-build-bot

# Or run the module directly
uv run python -m tg_flutter_build_bot.main
```

### Code Quality

```bash
# Type checking
uv run mypy src/

# Linting
uv run ruff check src/

# Formatting
uv run ruff format src/
```

### Adding Dependencies

```bash
uv add <package>          # runtime dependency
uv add --dev <package>    # development dependency
```

### Resetting State

All runtime state lives in `data/` (gitignored). To start fresh:

```bash
rm -rf data/
```

---

## Troubleshooting

### Bot doesn't respond to commands

- Verify `TELEGRAM_BOT_TOKEN` is correct
- Check that your chat ID is in `ALLOWED_CHAT_IDS` (or leave it empty to allow all)
- Look for errors in the terminal output

### Build fails immediately

- Ensure `flutter` is on `$PATH` — run `which flutter`
- Verify the repo URL is accessible — run `git ls-remote <REPO_URL>`
- Check `BUILD_COMMAND` — the default assumes a standard Flutter Android project

### Google Drive upload fails

- Complete the OAuth flow via `http://localhost:8080/oauth`
- Ensure your Google Cloud project has the **Drive API** enabled
- Check that the redirect URI matches exactly: `http://localhost:8080/oauth/callback`
- Make sure your Google account is added as a **test user** in the OAuth consent screen

### "Cooldown active" message

- Default cooldown is 300 seconds (5 minutes)
- Reduce it via `COOLDOWN_SECONDS` or the Web UI config page
- Use `/status` to see remaining cooldown time

---

## License

This project is private. All rights reserved.
