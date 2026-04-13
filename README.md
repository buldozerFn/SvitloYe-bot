# SvitloYeBot 💡

[![Telegram](https://img.shields.io/badge/Telegram-@SvitloYe__IrpinBot-blue?logo=telegram)](https://t.me/SvitloYe_IrpinBot)

Telegram bot for tracking power outage schedules in Kyiv Oblast (DTEK). Pre-configured for **Group 6.2**.

## ✨ Key Features

- 🔄 **Automatic Updates:** The bot checks for new data every 20 minutes.
- 📅 **Today & Tomorrow Schedules:** Get the actual schedule with a single command.
- 🔔 **Smart Notifications:**
    - Warning 10 minutes before a planned outage or restoration.
    - Instant notification upon status change.
    - Automatic delivery of tomorrow's schedule as soon as it becomes available.
- 🕒 **Real-time Status:** The `/now` command shows what's happening currently and when to expect the next change.
- 🔐 **Whitelist:** Access is restricted to authorized users only.

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
- `BOT_TOKEN`: Your token from BotFather.
- `ALLOWED_USERS`: A list of user IDs allowed to use the bot.
- `TARGET_GROUP`: Your group number (default is `6.2`).

### 4. Running the Bot
```bash
python bot.py
```

## 📋 Bot Commands

- `/start` — Welcome message and command list.
- `/today` — Today's outage schedule.
- `/tomorrow` — Tomorrow's outage schedule (if published).
- `/now` — Current status and upcoming changes.
- `/status` — Diagnostic information (cache status, last update time).

## 🛠 Tech Stack
- [aiogram 3.x](https://github.com/aiogram/aiogram) — Asynchronous Telegram API library.
- [APScheduler](https://github.com/agronholm/apscheduler) — Background task scheduler.
- [aiohttp](https://github.com/aio-libs/aiohttp) — Asynchronous HTTP requests.

## 📊 Data Source
Data is fetched from the [Baskerville42/outage-data-ua](https://github.com/Baskerville42/outage-data-ua) repository, which aggregates official DTEK schedules.
