import os
import re
import requests
import logging
import time
import asyncio
from collections import defaultdict, deque
from dotenv import load_dotenv
from telethon import TelegramClient, events, types
from youtube_transcript_api import YouTubeTranscriptApi

load_dotenv()
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = "https://api.h-s.site"

bot = TelegramClient("bot", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

private_conversation_history = {}
group_conversation_history = {}
group_last_interaction = {}
group_settings = defaultdict(dict)

user_message_count = defaultdict(int)
last_message_time = defaultdict(float)
muted_users = defaultdict(dict)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

YOUTUBE_REGEX = r"(https?://(?:www.)?(?:youtube.com/watch\?v=|youtu.be/)([\w-]+))"
MUTE_DURATION = 6 * 30 * 24 * 60 * 60  # 6 months in seconds

async def get_chat_context(event):
    context = ""
    if event.is_private:
        user = await event.get_sender()
        context = f"Private chat with {user.first_name}"
        if user.username:
            context += f" (@{user.username})"
    else:
        chat = await event.get_chat()
        context = f"Group: {chat.title}"
        if chat.username:
            context += f" (@{chat.username})"
    return context

SYSTEM_PROMPT_TEMPLATE = """You are @askllmbot, an AI assistant in Telegram. Current context:
{context}

Guidelines:
1. Respond appropriately to the chat environment
2. Detect spam patterns
3. For spam, respond with exactly "[SPAM_DETECTED]"
4. In groups, only respond when mentioned or replied to"""

def get_system_prompt(context):
    return SYSTEM_PROMPT_TEMPLATE.format(context=context)

def clear_old_conversation_history():
    current_time = time.time()
    to_delete = [chat_id for chat_id, last_time in group_last_interaction.items() if current_time - last_time > 86400]
    for chat_id in to_delete:
        del group_conversation_history[chat_id]
        del group_last_interaction[chat_id]

def get_youtube_transcript(url):
    match = re.search(YOUTUBE_REGEX, url)
    if not match:
        return None
    video_id = match.group(2)
    try:
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join([entry["text"] for entry in transcript])
    except:
        return None

def is_spam_behavior(user_id, chat_id, message_text):
    if not group_settings[chat_id].get('spam_detection', True):
        return False
        
    current_time = time.time()
    if current_time - last_message_time[(chat_id, user_id)] < 2:
        user_message_count[(chat_id, user_id)] += 1
    else:
        user_message_count[(chat_id, user_id)] = 1
    last_message_time[(chat_id, user_id)] = current_time
    
    if user_message_count[(chat_id, user_id)] > 5:
        return True
    
    special_char_ratio = sum(1 for c in message_text if not c.isalnum()) / len(message_text)
    repeated_content = any(message_text.count(word) > 3 for word in message_text.split() if len(word) > 3)
    all_caps = message_text.isupper() and len(message_text) > 15
    multiple_mentions = len(re.findall(r"@\w+", message_text)) > 2
    
    if (special_char_ratio > 0.3) or repeated_content or all_caps or multiple_mentions:
        return True
    
    return False

async def mute_user(chat_id, user_id):
    try:
        await bot.edit_permissions(
            chat_id,
            user_id,
            send_messages=False,
            until_date=int(time.time()) + MUTE_DURATION
        )
        muted_users[chat_id][user_id] = time.time() + MUTE_DURATION
        return True
    except Exception as e:
        logger.error(f"Mute error: {e}")
        return False

async def get_assistant_response(event, user_prompt, is_private):
    try:
        context = await get_chat_context(event)
        system_prompt = get_system_prompt(context)
        
        if is_private:
            history = private_conversation_history.setdefault(event.sender_id, deque(maxlen=50))
        else:
            history = group_conversation_history.setdefault(event.chat_id, deque(maxlen=50))
            group_last_interaction[event.chat_id] = time.time()

        history.append({"role": "user", "content": user_prompt})

        token_response = requests.get(f"{BASE_URL}/v1/get-token")
        token = token_response.json().get("token")
        if not token:
            return "Error: API token failed"

        payload = {
            "token": token,
            "model": "gpt-4o-mini",
            "message": [{"role": "system", "content": system_prompt}] + list(history),
            "stream": False
        }

        response = requests.post(f"{BASE_URL}/v1/chat/completions", json=payload)
        response_data = response.json()
        content = response_data["choice"][0]["message"]["content"]
        history.append({"role": "assistant", "content": content})
        return content
    except Exception as e:
        logger.error(f"API error: {e}")
        return "Sorry, I encountered an error"

@bot.on(events.NewMessage(pattern="/start"))
async def start(event):
    await event.reply("Hello! I'm your AI assistant. Send me a message to chat.")

@bot.on(events.NewMessage(pattern="/mute"))
async def mute_command(event):
    if not event.is_group:
        return
    
    sender = await event.get_sender()
    chat = await event.get_chat()
    if not isinstance(sender, types.User) or not isinstance(chat, types.Chat):
        return
    
    participant = await bot.get_permissions(chat.id, sender.id)
    if not (participant.is_admin or participant.is_creator):
        return
    
    args = event.raw_text.split()
    if len(args) < 2:
        return
    
    try:
        user_to_mute = await event.get_reply_message()
        if user_to_mute:
            user_id = user_to_mute.sender_id
        else:
            username = args[1].lstrip('@')
            user_entity = await bot.get_entity(username)
            user_id = user_entity.id
        
        success = await mute_user(event.chat_id, user_id)
        if success:
            user = await bot.get_entity(user_id)
            await event.reply(f"🚫 {user.first_name} muted for 6 months")
            await event.delete()
    except Exception as e:
        logger.error(f"Mute command error: {e}")

@bot.on(events.NewMessage(pattern="/detectspam"))
async def toggle_spam_detection(event):
    if not event.is_group:
        return
    
    sender = await event.get_sender()
    chat = await event.get_chat()
    if not isinstance(sender, types.User) or not isinstance(chat, types.Chat):
        return
    
    participant = await bot.get_permissions(chat.id, sender.id)
    if not (participant.is_admin or participant.is_creator):
        return
    
    current_setting = group_settings[event.chat_id].get('spam_detection', True)
    group_settings[event.chat_id]['spam_detection'] = not current_setting
    
    status = "ENABLED" if not current_setting else "DISABLED"
    await event.reply(f"🛡️ Spam detection has been {status}")
    await event.delete()

@bot.on(events.NewMessage)
async def message_handler(event):
    if event.text.startswith("/"):
        return
    
    user_id = event.sender_id
    chat_id = event.chat_id
    is_private = event.is_private
    
    if not is_private and user_id in muted_users.get(chat_id, {}):
        if time.time() < muted_users[chat_id][user_id]:
            await event.delete()
            return
        else:
            del muted_users[chat_id][user_id]
    
    message_text = event.raw_text.strip()
    bot_username = (await bot.get_me()).username
    
    if not is_private and is_spam_behavior(user_id, chat_id, message_text):
        spam_check = await get_assistant_response(event, f"Check spam: {message_text}", False)
        if "[SPAM_DETECTED]" in spam_check:
            await mute_user(chat_id, user_id)
            await event.delete()
            user = await bot.get_entity(user_id)
            await event.respond(f"🚫 {user.first_name} auto-muted for 6 months")
            return
    
    youtube_links = re.findall(YOUTUBE_REGEX, message_text)
    if youtube_links:
        for full_url, video_id in youtube_links:
            transcript = get_youtube_transcript(full_url)
            if transcript:
                message_text = message_text.replace(full_url, transcript)

    if is_private:
        async with bot.action(chat_id, "typing"):
            response = await get_assistant_response(event, message_text, True)
            await event.reply(response)
    elif event.is_group:
        should_respond = False
        if message_text.startswith(f"@{bot_username}"):
            message_text = message_text[len(bot_username)+2:].strip()
            should_respond = True
        if event.message.mentioned:
            should_respond = True
        if event.is_reply:
            reply_msg = await event.get_reply_message()
            if reply_msg and reply_msg.sender_id == (await bot.get_me()).id:
                should_respond = True
        if should_respond and message_text:
            async with bot.action(chat_id, "typing"):
                response = await get_assistant_response(event, message_text, False)
                await event.reply(response)

    clear_old_conversation_history()

print("Bot running")
bot.run_until_disconnected()
