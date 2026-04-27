#!/data/data/com.termux/files/usr/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# If BOT_TOKEN is not set in environment, try local file.
if [ -z "$BOT_TOKEN" ] && [ -f "bot_token.txt" ]; then
  BOT_TOKEN="$(cat bot_token.txt)"
fi

if [ -z "$BOT_TOKEN" ]; then
  echo "ERROR: BOT_TOKEN is empty."
  echo "Set it before launch: export BOT_TOKEN='your_token'"
  exit 1
fi

# Optional: export ADMIN_CHAT_ID="-5273311194"
python random_circles_bot.py
