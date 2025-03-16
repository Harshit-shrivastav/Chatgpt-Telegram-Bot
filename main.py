import os
import re
import requests
import logging
import time
from collections import deque
from dotenv import load_dotenv
from telethon import TelegramClient, events
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

# Load environment variables
load_dotenv()
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = "https://api.h-s.site"  # Keep as is or use your own API URL

# Initialize bot
bot = TelegramClient("bot", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# Conversation history
private_conversation_history = {}  # {user_id: deque([...])}
group_conversation_history = {}  # {chat_id: deque([...])}
group_last_interaction = {}  # {chat_id: last_timestamp}

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# YouTube link regex
YOUTUBE_REGEX = r"(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]+))"

def clear_old_conversation_history():
    """Clears old group history after 24 hours of inactivity."""
    current_time = time.time()
    to_delete = [chat_id for chat_id, last_time in group_last_interaction.items() if current_time - last_time > 24 * 3600]

    for chat_id in to_delete:
        del group_conversation_history[chat_id]
        del group_last_interaction[chat_id]
        logger.info(f"Cleared history for group {chat_id} due to inactivity.")

def get_youtube_transcript(url):
    """Fetches YouTube transcript if available, otherwise returns an error message."""
    match = re.search(YOUTUBE_REGEX, url)
    if not match:
        return None

    video_id = match.group(2)
    
    try:
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
        transcript_text = " ".join([entry["text"] for entry in transcript])
        return transcript_text if transcript_text else "No transcript found in the YouTube video"
    
    except (TranscriptsDisabled, NoTranscriptFound):
        return "No transcript found in the YouTube video"
    except Exception as e:
        logger.error(f"Transcript error: {e}")
        return "No transcript found in the YouTube video"

def get_assistant_response(user_id, chat_id, user_prompt, is_private):
    """Sends user input to the LLM and retrieves the response."""
    try:
        if is_private:
            history = private_conversation_history.setdefault(user_id, deque(maxlen=50))  # Store for 3 days
        else:
            history = group_conversation_history.setdefault(chat_id, deque(maxlen=50))
            group_last_interaction[chat_id] = time.time()  # Update last interaction

        history.append({"role": "user", "content": user_prompt})

        # Get API token
        token_response = requests.get(f"{BASE_URL}/v1/get-token")
        token_response.raise_for_status()
        token = token_response.json().get("token")

        if not token:
            return "Error: Could not retrieve authentication token."

        payload = {
            "token": token,
            "model": "gpt-4o-mini",
            "message": [{"role": "user", "content": "You are an AI Telegram bot, your name is @askllmbot (Ask LLM). You are replying to users in both private messages and group chats, keeping this note in mind, reply to the users accordingly."}] + list(history),
            "stream": False
        }

        response = requests.post(f"{BASE_URL}/v1/chat/completions", json=payload)
        response.raise_for_status()
        response_data = response.json()

        if "choice" not in response_data or not response_data["choice"]:
            return "Error: Unexpected response format from API."

        content = response_data["choice"][0]["message"]["content"]
        history.append({"role": "user", "content": content})  # Keep history aligned
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
    """Handles the /start command."""
    await event.reply("Hello! I'm your AI assistant. Just send me a message to chat.")

@bot.on(events.NewMessage)
async def message_handler(event):
    """Handles messages in private and group chats."""
    if event.text.startswith("/"):
        return

    user_id = event.sender_id
    chat_id = event.chat_id
    is_private = event.is_private
    sender = await event.get_sender()

    message_text = event.raw_text.strip()  # Get raw text
    bot_username = (await bot.get_me()).username

    youtube_links = re.findall(YOUTUBE_REGEX, message_text)
    if youtube_links:
        await bot.send_chat_action(chat_id, "record_video")  # Show recording video action
        for full_url, video_id in youtube_links:
            transcript = get_youtube_transcript(full_url)
            message_text = message_text.replace(full_url, transcript)

    if is_private:
        await bot.send_chat_action(chat_id, "typing")  # Show typing action
        response = get_assistant_response(user_id, chat_id, message_text, is_private=True)
        await event.reply(response)

    elif event.is_group:
        should_respond = False

        if message_text.startswith(f"@{bot_username}"):
            message_text = message_text[len(bot_username) + 2:].strip()
            should_respond = True

        if event.message.mentioned or (event.reply_to and event.reply_to.from_id == bot.me.id):
            should_respond = True

        if should_respond and message_text:
            await bot.send_chat_action(chat_id, "typing")  # Show typing action
            response = get_assistant_response(user_id, chat_id, message_text, is_private=False)
            await event.reply(response)

    clear_old_conversation_history()

"""
@bot.on(events.InlineQuery)
async def inline_query_handler(event):
    query = event.text.strip()
    if not query:
        return
    response = get_assistant_response(event.sender_id, event.chat_id, query, is_private=True)
    await event.answer([event.builder.article(title="AI Response", description=response, text=response)])
"""
print("🎊 Bot is now active!")
bot.run_until_disconnected()
