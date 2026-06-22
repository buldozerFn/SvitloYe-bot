# SvitloYeBot 💡

Telegram bot for tracking power outage schedules across Ukraine (DTEK regions).
Open to all Telegram users — each person selects their own region and group.

## ✨ Key Features

- 🌍 **Multi-region support:** Kyiv region, Kyiv city, Dnipropetrovsk region, Odesa region, Zaporizhzhia region, Donetsk region.
- 🎯 **Per-user group selection:** Every user picks their own region and queue group (1.1 – 6.2) via inline buttons.
- 🔄 **Automatic Updates:** The bot checks for new data every 20 minutes.
- 📅 **Today & Tomorrow Schedules:** Get the full day schedule with a single command.
- 🔔 **Smart Notifications:**
    - Warning 10 minutes before a planned outage or restoration.
    - Instant notification upon status change.
    - Automatic delivery of tomorrow's schedule as soon as it becomes available.
- 🕒 **Real-time Status:** `/now` shows the current state and the next upcoming change.

## 🚀 Setup & Installation

### 1. Requirements
- Python 3.10 or newer.
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather)).

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configuration
Open `bot.py` and update the following constants:

| Constant | Description |
|---|---|
| `BOT_TOKEN` | Your token from BotFather. |
| `ADMIN_USERS` | List of user IDs that receive startup / diagnostic messages. |
| `REGIONS` | Dict of supported regions and their data URLs (pre-filled). |

### 4. Running the Bot
```bash
python bot.py
```

## 📋 Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message and command list. |
| `/setgroup` | Choose your region and queue group via inline buttons. |
| `/today` | Today's outage schedule for your group. |
| `/tomorrow` | Tomorrow's schedule (if published). |
| `/now` | Current status and upcoming changes. |
| `/status` | Diagnostic info (last update time, active users, your current group). |

## 🔑 How to Find Your Group

Open the DTEK official website, enter your address, and look up your queue group number (e.g. `6.2`).
Then use `/setgroup` in the bot to save it.

## 🛠 Tech Stack
- [aiogram 3.x](https://github.com/aiogram/aiogram) — Asynchronous Telegram Bot API library.
- [APScheduler](https://github.com/agronholm/apscheduler) — Background task scheduler.
- [aiohttp](https://github.com/aio-libs/aiohttp) — Asynchronous HTTP client.

## 📊 Data Source
Outage data is fetched from the [Baskerville42/outage-data-ua](https://github.com/Baskerville42/outage-data-ua) public repository, which aggregates official DTEK schedules.
