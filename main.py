import os
import requests
import logging
from collections import deque
from dotenv import load_dotenv
from telethon import TelegramClient, events
import time

load_dotenv()
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = "https://api.h-s.site" # Leave it as it is or get your own from https://github.com/Harshit-shrivastav/DuckAI

bot = TelegramClient("bot", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

private_conversation_history = {}  # {user_id: deque([...])}
group_conversation_history = {}  # {chat_id: deque([...])}
group_last_interaction = {}  # {chat_id: last_timestamp}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def clear_old_group_history():
    current_time = time.time()
    to_delete = [chat_id for chat_id, last_time in group_last_interaction.items() if current_time - last_time > 8 * 3600]

    for chat_id in to_delete:
        del group_conversation_history[chat_id]
        del group_last_interaction[chat_id]
        logger.info(f"Cleared history for group {chat_id} due to inactivity.")

def get_assistant_response(user_id, chat_id, user_prompt, is_private):
    try:
        if is_private:
            history = private_conversation_history.setdefault(user_id, deque(maxlen=10))
        else:
            history = group_conversation_history.setdefault(chat_id, deque(maxlen=10))
            group_last_interaction[chat_id] = time.time()  # Update last interaction time

        history.append({"role": "user", "content": user_prompt})

        # Get token
        token_response = requests.get(f"{BASE_URL}/v1/get-token")
        token_response.raise_for_status()
        token = token_response.json().get("token")

        if not token:
            return "Error: Could not retrieve authentication token."

        payload = {
            "token": token,
            "model": "gpt-4o-mini",
            "message": [{"role": "user", "content": "You are a helpful assistant!"}]
                      + list(history),
            "stream": False
        }

        response = requests.post(f"{BASE_URL}/v1/chat/completions", json=payload)
        response.raise_for_status()
        response_data = response.json()

        if "choice" not in response_data or not response_data["choice"]:
            return "Error: Unexpected response format from API."

        content = response_data["choice"][0]["message"]["content"]
        history.append({"role": "user", "content": content})  # Keep history aligned with your API
        return content

    except requests.exceptions.RequestException as e:
        logger.error(f"Request error: {e}")
        return "API request failed."
    except ValueError as e:
        logger.error(f"Invalid JSON response: {e}")
        return "Invalid response from API."
    except KeyError as e:
        logger.error(f"Missing key in response: {e}")
        return "Unexpected API response format."

@bot.on(events.NewMessage(pattern="/start"))
async def start(event):
    await event.reply("Hello! I'm your AI assistant. Just send me a message to chat.")

@bot.on(events.NewMessage)
async def message_handler(event):
    if event.text.startswith("/"):
        return

    user_id = event.sender_id
    chat_id = event.chat_id
    is_private = event.is_private

    if is_private:
        response = get_assistant_response(user_id, chat_id, event.text, is_private=True)
        await event.reply(response)

    # Group chat (only if bot is mentioned or replied to)
    elif event.is_group:
        if event.message.mentioned or (event.reply_to and event.reply_to.from_id == bot.me.id):
            response = get_assistant_response(user_id, chat_id, event.text, is_private=False)
            await event.reply(response)

    # Periodically clear old group histories
    clear_old_group_history()

@bot.on(events.InlineQuery)
async def inline_query_handler(event):
    query = event.text.strip()
    if not query:
        return
    response = get_assistant_response(event.sender_id, event.chat_id, query, is_private=True)
    await event.answer([event.builder.article(title="AI Response", description=response, text=response)])

print("🎊 Congratulations! Bot is now active.")
bot.run_until_disconnected()
