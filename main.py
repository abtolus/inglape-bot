from dotenv import find_dotenv, load_dotenv
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyinflect import getAllInflections

import os
import telebot
import datetime
import requests

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

BOT_TOKEN = os.getenv('BOT_TOKEN')
bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "Howdy, how are you doing?")
@bot.message_handler(func=lambda message: True)
def echo_all(message):
    bot.reply_to(message, message.text)

bot.infinity_polling()