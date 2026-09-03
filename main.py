from dotenv import find_dotenv, load_dotenv
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyinflect import getAllInflections
from threading import Lock

import os
import telebot
import datetime
import requests
import asyncio
import httpx

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

BOT_TOKEN = os.getenv('BOT_TOKEN')
bot = telebot.TeleBot(BOT_TOKEN)

lock = Lock()

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "Howdy, how are you doing?", reply_markup=main_menu())
@bot.message_handler(func=lambda message: True)
def echo_all(message):
    bot.reply_to(message, message.text, reply_markup=main_menu())

def main_menu():
    main_menu_buttons = [
        [InlineKeyboardButton("learn", callback_data="learnButton"),
         InlineKeyboardButton("review", callback_data="reviewButton")]
    ]
    return InlineKeyboardMarkup(main_menu_buttons)
def learn_menu():
    learn_menu_buttons = [
        [InlineKeyboardButton("Start", callback_data="startButton"),
         InlineKeyboardButton("Refresh", callback_data="refreshButton")]
    ]
    return InlineKeyboardMarkup(learn_menu_buttons)

async def fetch_word(client: httpx.AsyncClient, url: str):
    response = await client.get(url)
    data = response.json()
    return data.get('data', [])[0]['word']
async def get_random_words():
    url = os.getenv('RANDOM_API_URL')
    try:
        async with httpx.AsyncClient() as client:
            tasks = [fetch_word(client, f"{url}?type={pos}&count=1") for pos in ['noun', 'verb', 'adjective', 'adverb']]
            print(tasks)
            results = await asyncio.gather(*tasks)
            print(results)
            return results
    except Exception as e:
        return f"Error connecting to the server: {e}"
def get_entry():
    url = os.getenv('DICTIONARY_API_URL')
    try:
        pass
    except Exception as e:
        return f"Error connecting to the server: {e}"
def get_inflections():
    pass

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    if call.data == "learnButton":
        if not lock.acquire(blocking=False): return
        try:
            bot.answer_callback_query(call.id, text="learn...")

            message = asyncio.run(get_random_words())
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="Markdown",
                reply_markup=learn_menu()
            )
        finally:
            lock.release()
bot.infinity_polling()