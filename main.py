from enum import Enum
from html import escape
from aiohttp import web
from ast import literal_eval
from collections import deque
from openrouter import OpenRouter
from tempfile import NamedTemporaryFile
from contextlib import asynccontextmanager
from telebot.util import content_type_media
from telebot.async_telebot import AsyncTeleBot
from fastapi import FastAPI, Request, Response
from re import compile, sub, search, DOTALL, split
from telebot.asyncio_filters import AdvancedCustomFilter
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, LabeledPrice, Update

from src.config import *
from src.lexicon import *
from src.database import *

import os
import json
import string
import random
import telebot
import logging
import asyncio
import aiohttp
import secrets
import pymysql

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(filename)s:%(lineno)d %(message)s"
)

with open('assets/translations.json', "r", encoding="utf-8") as f:
    global_translations = json.load(f)

class HandleState(str, Enum):
    HANDLE_WORDS = "handle_words"
    HANDLE_ANSWERS = "handle_answers"
    HANDLE_PAYMENTS = "handle_payments"
    HANDLE_TRANSACTION_NUMBER = "handle_transaction_number"
class HandleFilter(AdvancedCustomFilter):
    key = 'handle'
    async def check(self, message: Message, text: bool) -> bool:
        user_id = message.from_user.id
        user_data = await get_user_data(user_id)
        boolean = user_data.get("handle_state") is not None
        return boolean == text

class PaymentState(str, Enum):
    PAYMENT_PENDING = "payment_pending"
class PaymentFilter(AdvancedCustomFilter):
    key = 'payment'
    async def check(self, message: Message, text: bool) -> bool:
        if message.chat.type not in ['private']: return text == False
        user_id = message.from_user.id
        user_media = await get_user_data(user_id)
        boolean = user_media.get("payment_state") is not None
        return boolean == text

class ExceptionHandler(telebot.ExceptionHandler):
    async def handle(self, exception):
        if isinstance(exception, (ConnectionResetError, TimeoutError, aiohttp.ClientError, asyncio.TimeoutError, pymysql.err.OperationalError)): return True

        logging.exception('Unhandled exception in Exception Handler')
        return True

session: aiohttp.ClientSession | None = None
bot = AsyncTeleBot(BOT_TOKEN, exception_handler=ExceptionHandler())
bot.add_custom_filter(PaymentFilter())
bot.add_custom_filter(HandleFilter())
openrouter_semaphore = asyncio.Semaphore(10)
acquired_lock = asyncio.Lock()
transaction_lock = asyncio.Lock()
acquired_users: set[int] = set()
target_users: dict[int, tuple[str, int]] = {}
administrators: dict[int, tuple[int, int]] = {}
regex_words = compile(r"^[a-zA-Z]+,\s[a-zA-Z]+,\s[a-zA-Z]+$")
regex_answers = compile(r"^\d+\.\s+\S.{0,300}$")
regex_transaction_number = compile(r"\d{20}")
requires_active = ['dictionary-', 'thesaurus-', 'skillCheckButton', 'tryAgainButton']

async def get_main_session() -> None:
    global session
    if session is None or session.closed:
        timeout = aiohttp.ClientTimeout(total=5)
        session = aiohttp.ClientSession(timeout=timeout)
async def close_main_session() -> None:
    global session
    if session and not session.closed:
        await session.close()

def get_global_translated(text: str, language: str) -> str:
    if language == "en-US": return text
    return global_translations.get(text, text)

async def fetch_skill_check(user_data: dict) -> list:
    random_words = user_data.get("random_words", [])
    result = user_data.get("data", {})

    results = []
    for word, *_ in random_words:
        for value in (result.get(word) or [{}, {}])[1].values():
            if not value:  continue
            first_key = next(iter(value.keys()), None)
            if first_key and first_key != 'text':
                entry = value[first_key]
                results.append({
                    "hw": entry.get('hw'),
                    "text": entry.get('text'),
                    "syns": entry.get('syns')
                })
    return results

async def get_skill_check(user_id: int) -> str:
    user_data = await get_user_data(user_id)
    user_data['answers'] = []
    settings = user_data.get("settings", {})

    data: list = await fetch_skill_check(user_data)
    sections = deque(random.sample(data, k=5) if len(data) >= 5 else random.sample(data, k=len(data)) for _ in range(4))

    raw = f"Provide a synonym for each of the following: (The answers must be from the Dictionary and Thesaurus you recently learned)"
    text = get_global_translated(raw, settings.get("language", "en-US"))
    temporary = []
    for i, entry in enumerate(sections.popleft()):
        text += f"\n<b>{i+1}</b> <i>{entry.get('hw')}</i>"
        temporary.append(entry.get('syns'))
    user_data['answers'].append(temporary)
    text += "\n\n"

    raw = f"Identify the term defined by each of the following: (The answers must be from the Dictionary and Thesaurus you recently learned)"
    text += get_global_translated(raw, settings.get("language", "en-US"))
    temporary = []
    for i, entry in enumerate(sections.popleft()):
        text += f"\n<b>{i+1}</b> <i>{entry.get('text').strip()}</i>"
        temporary.append(entry.get('hw'))
    user_data['answers'].append(temporary)
    text += "\n\n"

    raw = f"Give a term matching each pair of synonyms in the following: (The answers must be from the Dictionary and Thesaurus you recently learned)"
    text += get_global_translated(raw, settings.get("language", "en-US"))
    temporary = []
    for i, entry in enumerate(sections.popleft()):
        text += f"\n<b>{i+1}</b> <i>{', '.join(entry.get('syns', []))}</i>"
        temporary.append(entry.get('hw'))
    user_data['answers'].append(temporary)
    text += "\n\n"

    raw = f"Write sentences using each of the following:"
    text += get_global_translated(raw, settings.get("language", "en-US"))
    temporary = []
    for i, entry in enumerate(sections.popleft()):
        text += f"\n<b>{i+1}</b> <i>{entry.get('hw')}</i>"
        temporary.append(entry.get('hw'))
    user_data['answers'].append(temporary)

    text += "\n\nTime limit: 2 hours"

    await save_user_data(user_id, user_data)

    return text

async def send_subscription(message: telebot.types.Message, user_id: int):
    user_data = await get_user_data(user_id)
    settings = user_data.get("settings", {})

    raw = "Your 14-day free trial or monthly subscription has expired. To keep your momentum going, please select a monthly subscription plan, using one of the payment methods below."
    text = get_global_translated(raw, settings.get("language", "en-US"))
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Telegram stars", callback_data="telegram_stars"),
         InlineKeyboardButton("KBZPay", callback_data="kbzpay")]
    ])
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=text,
        parse_mode="HTML",
        reply_markup=markup
    )

@bot.pre_checkout_query_handler(func=lambda query: True)
async def process_pre_checkout(pre_checkout_query):
    payload = pre_checkout_query.invoice_payload or ""
    if payload.startswith("subscription-"):
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    else:
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="404")
@bot.message_handler(content_types=['successful_payment'])
async def process_successful_payment(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    first_name = message.from_user.first_name
    user_data = await get_user_data(user_id)
    settings = user_data.get("settings", {})

    await delete_message(chat_id, user_id)

    raw = "Payment successful, and your subscription is active for 30 days. Let's kick things off with /start, shall we?"
    text = get_global_translated(raw, settings.get("language", "en-US"))
    call_message = await bot.send_message(
        chat_id=chat_id,
        text=text
    )

    user_media = await get_user_media(user_id)
    user_media['last_message'] = {
        "chat_id": call_message.chat.id,
        "message_id": call_message.message_id
    }
    await save_user_media(user_id, user_media)

    text_ = f"User {first_name} ({user_id}) purchased the Valedictorian Edition plan, using 50 Telegram Stars."
    await bot.send_message(
        chat_id=GROUP_ID,
        text=text_
    )

    digits_: str = ''.join(secrets.choice(string.digits) for _ in range(18))
    transaction_number: str = '0x' + digits_
    await insert_payment(user_id, transaction_number, 50)
    await upsert_subscription(user_id)

async def delete_voice(chat_id: int, last_voice: dict | None) -> None:
    if not last_voice: return

    last_voices = last_voice.get("message_ids") or [last_voice.get("message_id")]
    for message_id in last_voices:
        if not message_id: continue
        try: await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception: pass

async def delete_message(chat_id: int, user_id: int):
    user_media = await get_user_media(user_id)
    last_message, last_voice = user_media.get("last_message"), user_media.get("last_voice")
    if not last_message and not last_voice: return
    if last_message:
        try: await bot.delete_message(chat_id=chat_id, message_id=last_message.get("message_id"))
        except Exception: pass
    await delete_voice(chat_id, last_voice)

    user_media['last_message'] = None
    user_media['last_voice'] = None
    await save_user_media(user_id, user_media)

async def get_message(chat_id: int, user):
    user_id = user.id
    await clear_user_data(user_id)
    user_data = await get_user_data(user_id)
    user_media = await get_user_media(user_id)
    settings = user_data.get("settings", {})

    raw = "Wilt thou learn anew, or weigh what thou hast learned?"
    translated = get_global_translated(raw, settings.get("language", "en-US"))
    text = f"{translated}\n\n/burmese — switch to Burmese\n/english — switch to English\n/profile — view your profile"

    call_message = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=main_menu()
    )
    user_media['last_message'] = {
        "chat_id": call_message.chat.id,
        "message_id": call_message.message_id
    }
    await save_user_media(user_id, user_media)
    await upsert_user(user)

def pick_three(items: list) -> list:
    return random.sample(items, k=min(3, len(items)))

async def handle_words(message: Message, maximum: int = 3) -> None:
    user_id = message.from_user.id
    user_data = await get_user_data(user_id)
    recent_words = user_data.get("recent_words", [])
    settings = user_data.get("settings", {})

    words = [word for word, *_ in recent_words]

    for attempt in range(1, maximum+1):
        try:
            if not bool(regex_words.fullmatch(message.text.strip())):
                raw = "Please select your recent words in the exact same format as the following:"
                translated = get_global_translated(raw, settings.get("language", "en-US"))
                text = f"{translated}\n<pre>{', '.join(pick_three(words))}</pre>"
                await send_words_format(message, text)
                return

            input_words = [word.strip().lower() for word in message.text.split(',')]
            if len(input_words) != len(set(input_words)):
                raw = "The selected words from your recent history must be unique:"
                translated = get_global_translated(raw, settings.get("language", "en-US"))
                text = f"{translated}\n<pre>{', '.join(words)}</pre>"
                await send_words_format(message, text)
                return

            matched = [item for item in recent_words if item[0].lower() in input_words]
            found = {item[0].lower() for item in matched}
            missing = [word for word in input_words if word not in found]

            if missing:
                raw = "Not all your selected words were found in your recent history:"
                translated = get_global_translated(raw, settings.get("language", "en-US"))
                text = f"{translated}\n<pre>{', '.join(words)}</pre>"
                await send_words_format(message, text)
                return

            user_data['handle_state'] = None

            user_data['random_words'] = matched
            raw = "Review thy words, lest thou let them be forgotten:"
            text_ = get_global_translated(raw, settings.get("language", "en-US"))
            text = await get_formatted_random_words(user_data, text_)
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("Refresh", callback_data="selection-manual"),
                    InlineKeyboardButton("Start", callback_data="startButton")]
            ])

            call_message = await bot.send_message(
                chat_id=message.chat.id,
                text=text,
                parse_mode="HTML",
                reply_markup=markup
            )
            user_media = await get_user_media(user_id)
            user_media['last_message'] = {
                "chat_id": call_message.chat.id,
                "message_id": call_message.message_id
            }
            await save_user_media(user_id, user_media)
            break
        except (ConnectionResetError, TimeoutError, aiohttp.ClientError, asyncio.TimeoutError):
            if attempt < maximum: await asyncio.sleep(5)
            else:
                raw = "Connection kept dropping during the process of retrieving the words. Should your network be stable, select your recent words again:"
                translated = get_global_translated(raw, settings.get("language", "en-US"))
                text = f"{translated}\n<pre>{', '.join(words)}</pre>"

                user_data['handle_state'] = HandleState.HANDLE_WORDS
                await save_user_data(user_id, user_data)

                await bot.send_message(
                    chat_id=message.chat.id,
                    text=text,
                    parse_mode="HTML"
                )

async def send_words_format(message, text: str) -> None:
    await bot.send_message(
        chat_id=message.chat.id,
        text=text,
        parse_mode="HTML"
    )

def parse_answers(text: str) -> list[list[str]]:
    text = (text or "").replace("\r\n", "\n").strip()
    if not text or len(text) > 4000: return None
    sections = []
    for block in split(r"\n{2,}", text):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines or not all(regex_answers.match(line) for line in lines):
            return None
        sections.append([sub(r"^\d+\.\s+", "", line) for line in lines])
    return sections

async def clear_session(chat_id: int, user_id: int, result: str) -> None:
    user_data = await get_user_data(user_id)
    settings = user_data.get("settings", {})
    random_words = user_data.get("random_words", [])
    data = user_data.get("data", {})

    await bot.send_message(
        chat_id=chat_id,
        text=result,
        parse_mode="HTML"
    )

    for word, part_of_speech in random_words:
        if word in data:
            await insert_log(user_id, await upsert_word(word, part_of_speech, data[word]))

    user_media = await get_user_media(user_id)
    await delete_voice(chat_id, user_media.get("last_voice"))
    user_media['last_voice'] = None

    await clear_user_data(user_id)

    raw = "The journey of learning hath borne fruit. Wilt thou learn anew, or weigh what thou hast learned?"
    text = get_global_translated(raw, settings.get("language", "en-US"))
    call_message = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=main_menu()
    )

    user_media['last_message'] = {
        "chat_id": call_message.chat.id,
        "message_id": call_message.message_id
    }
    await save_user_media(user_id, user_media)

async def handle_answers(message, maximum: int = 3) -> None:
    user_id = message.from_user.id
    user_data = await get_user_data(user_id)
    ans = user_data.get("answers", [])
    settings = user_data.get("settings", {})

    for attempt in range(1, maximum+1):
        try:
            lines = parse_answers(message.text)
            if lines is None or len(lines) != 4 or len(ans) != 4 or not all(len(i) == len(j) for i, j in zip(lines, ans)):
                await send_answers_format(message)
                return

            result = await evaluate_answers(lines, message.chat.id, user_id)

            user_data['handle_state'] = None
            await save_user_data(user_id, user_data)

            if result:
                await clear_session(message.chat.id, user_id, result)
            break
        except (ConnectionResetError, TimeoutError, aiohttp.ClientError, asyncio.TimeoutError):
            if attempt < maximum: await asyncio.sleep(5)
            else:
                raw = "Connection kept dropping during the process of evaluating your answers. Should your network be stable, resend your answers."

                user_data['handle_state'] = HandleState.HANDLE_ANSWERS
                await save_user_data(user_id, user_data)

                await bot.send_message(
                    chat_id=message.chat.id,
                    text=get_global_translated(raw, settings.get('language', 'en-US')),
                    parse_mode="HTML"
                )

async def send_answers_format(message) -> None:
    user_id = message.from_user.id
    user_data = await get_user_data(user_id)
    settings = user_data.get("settings", {})

    raw = "Please provide your answers in the exact same format as the following:"
    translated = get_global_translated(raw, settings.get("language", "en-US"))
    text = f"{translated}\n\n"
    text += f"<pre>{'\n\n'.join(await get_format(user_id))}</pre>"

    await bot.send_message(
        chat_id=message.from_user.id,
        text=text,
        parse_mode="HTML"
    )

async def get_format(user_id: int) -> list:
    user_data = await get_user_data(user_id)
    ans = user_data.get("answers", [])

    paragraph = "Curabitur lobortis quam sit amet augue porta hendrerit.\nMorbi efficitur ante sed lorem porttitor, a mollis purus sagittis.\nPellentesque nec erat sit amet nunc volutpat mollis sit amet nec nibh.\nMorbi dignissim risus vitae dui sollicitudin, at fermentum risus rhoncus.\nNam fermentum elit eu neque malesuada feugiat.\nAliquam porta est ac eros ultricies varius."

    sentences = [sentence for sentence in paragraph.strip().split('\n')]
    result = []

    for _ in range(3):
        result.append('\n'.join([f"{i+1}. {(words := sentences[i % len(sentences)].split())[0].lower()} {words[1].lower()}" for i in range(len(ans[0]))]))
    result.append('\n'.join([f"{i+1}. {sentences[i % len(sentences)]}" for i in range(len(ans[0]))]))
    return result

async def evaluate_sentences(sentences: str) -> list:
    async with openrouter_semaphore:
        try:
            async with OpenRouter(api_key=OPENROUTER_API_KEY) as client:
                content = f"Inspect the following sentences for grammatical errors. Only return a Python list of True if it is correct, or False if it is incorrect, in the exact same order as the sentences.\nSentences:\n{sentences}"
                response = await client.chat.send(model="liquid/lfm-2.5-2.6b:free", messages=[{"role": "user", "content": content}])

                if not response.choices[0]: return []

                match = search(r"\[.*?\]", response.choices[0].message.content, DOTALL)
                if match: return literal_eval(match.group(0))
        except Exception: pass
        return []
async def evaluate_answers(answers: list, chat_id: int, user_id: int) -> str:
    user_data = await get_user_data(user_id)
    ans = user_data.get("answers", [])

    score = 0
    total = sum(len(sublist) for sublist in answers)

    text = "— RESULTS\n"

    for i, j in zip(answers[0], ans[0]):
        text += f"\n{'✅' if i in j else '❌'} <i>{escape(i)}</i>"
        score += int(i in j)

    text += '\n'
    for i, j in zip(answers[1], ans[1]):
        text += f"\n{'✅' if i == j else '❌'} <i>{escape(i)}</i>"
        score += int(i == j)

    text += '\n'
    for i, j in zip(answers[2], ans[2]):
        text += f"\n{'✅' if i == j else '❌'} <i>{escape(i)}</i>"
        score += int(i == j)

    text += '\n'
    stage1, stage2 = [str(j).lower() in str(i).lower() for i, j in zip(answers[3], ans[3])], []
    sentences = "\n".join([f"{i+1}. {s}" for i, s in enumerate(answers[3])])

    try:
        stage2 = await asyncio.wait_for(evaluate_sentences(sentences), timeout=10)
    except Exception:
        user_data['pending'] = answers
        await save_user_data(user_id, user_data)

        await try_again(chat_id)
        return None

    if not stage2 or len(stage2) != len(stage1):
        stage2 = [False] * len(stage1)
    evaluated = [i and j for i, j in zip(stage1, stage2)]
    for i, j in zip(answers[3], evaluated):
        text += f"\n{'✅' if j else '❌'} <i>{escape(i)}</i>"
        score += int(j)

    final = round((score / total) * 100, 2)
    text += f"\n\nYou <b>{'passed' if final >= 40 else 'failed'}</b> the Skill check with a score of {final}%."
    return text

async def try_again(chat_id: int) -> None:
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Abort checking", callback_data="tryAgainButton-0"),
         InlineKeyboardButton("Try again", callback_data="tryAgainButton-1")]
    ])
    text = "Too many requests are being sent at the moment. Please <b>Try again</b> later, or <b>Abort checking</b> if you do not need your answers to be evaluated."

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=markup
    )

def main_menu() -> InlineKeyboardMarkup:
    main_menu_buttons = [
        [InlineKeyboardButton("Review", callback_data="selection-menu"),
         InlineKeyboardButton("Learn", callback_data="learnButton")]
    ]
    return InlineKeyboardMarkup(main_menu_buttons)

def learn_menu() -> InlineKeyboardMarkup:
    learn_menu_buttons = [
        [InlineKeyboardButton("Refresh", callback_data="refreshButton-0"),
         InlineKeyboardButton("Start", callback_data="startButton")]
    ]
    return InlineKeyboardMarkup(learn_menu_buttons)

async def dictionary_menu(total: int, user_id: int, word_index: int = 0, page_index: int = 0) -> InlineKeyboardMarkup:
    user_data = await get_user_data(user_id)
    random_words = user_data.get("random_words", [])
    number_of_words = len(random_words) or 1

    previous_page, next_page = (page_index - 1) % total, (page_index + 1) % total
    previous_word, next_word = (word_index - 1) % number_of_words, (word_index + 1) % number_of_words
    dictionary_menu_buttons = [
        [InlineKeyboardButton('⬅', callback_data=f"dictionary-{word_index}-{previous_page}"),
         InlineKeyboardButton(f"{page_index+1}/{total}", callback_data="none"),
         InlineKeyboardButton('➡', callback_data=f"dictionary-{word_index}-{next_page}")],
        [InlineKeyboardButton('Previous word', callback_data=f"dictionary-{previous_word}-0"),
         InlineKeyboardButton('Next word', callback_data=f"dictionary-{next_word}-0")],
        [InlineKeyboardButton('Skill check', callback_data="skillCheckButton-0"),
         InlineKeyboardButton("Thesaurus", callback_data=f"thesaurus-{word_index}-0")]
    ]
    return InlineKeyboardMarkup(dictionary_menu_buttons)

def thesaurus_menu(total: int, word_index: int = 0, page_index: int = 0) -> InlineKeyboardMarkup:
    previous_page, next_page = (page_index - 1) % total, (page_index + 1) % total
    thesaurus_menu_buttons = [
        [InlineKeyboardButton('⬅', callback_data=f"thesaurus-{word_index}-{previous_page}"),
         InlineKeyboardButton(f"{page_index+1}/{total}", callback_data="none"),
         InlineKeyboardButton('➡', callback_data=f"thesaurus-{word_index}-{next_page}")],
        [InlineKeyboardButton('Go back to the Dictionary', callback_data=f"dictionary-{word_index}-0")]
    ]
    return InlineKeyboardMarkup(thesaurus_menu_buttons)

async def recent_words_menu(user_data: dict, page_index: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    recent_words = user_data.get('recent_words', [])
    settings = user_data.get('settings', {})

    words = [word for word, *_ in recent_words]
    total, per = len(words), 15
    total_pages = min(6, (total + per - 1) // per) or 1

    page_index = max(0, min(page_index, total_pages - 1))

    start = page_index * per
    end = min(start + per, total)
    current = words[start:end]

    raws = ["Select three words from the following:", "using the exact same format like this:"]
    translated = [get_global_translated(raw, settings.get('language', 'en-US')) for raw in raws]

    message = f"{translated[0]}\n<pre>{', '.join(current)}</pre>\n"
    sample_words = pick_three(current)
    message += f"{translated[1]}\n<pre>{', '.join(sample_words)}</pre>"

    previous_page = (page_index - 1) % total_pages
    next_page = (page_index + 1) % total_pages

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton('⬅', callback_data=f"recent_words_page-{previous_page}"),
         InlineKeyboardButton(f"{page_index+1}/{total_pages}", callback_data="none"),
         InlineKeyboardButton('➡', callback_data=f"recent_words_page-{next_page}")],
        [InlineKeyboardButton("Back to the Selection Menu", callback_data="selection-menu")]
    ])

    return message, markup

async def send_transaction_number_format(chat_id: int) -> None:
    text = "The transaction number must be 20 digits long."
    await bot.send_message(
        chat_id=chat_id,
        text=text
    )

async def handle_transaction_number(message, maximum: int = 3) -> None:
    admin_id = GROUP_ID
    admin_data = await get_user_data(admin_id)

    if message.text is None or not regex_transaction_number.fullmatch(message.text):
        await send_transaction_number_format(admin_id)
        return

    async with transaction_lock:
        transaction_data = administrators.pop(admin_id, (None, None))

    if not transaction_data:
        text = "The request may have timed out or already been processed."
        await bot.send_message(
            chat_id=admin_id,
            text=text
        )

        admin_data['handle_state'] = None
        await save_user_data(admin_id, admin_data)
        return

    target_user_id, original_message_id = transaction_data
    target_user_data = await get_user_data(target_user_id)
    target_user_settings = target_user_data.get("settings", {})

    for attempt in range(1, maximum+1):
        try:
            raw = "Payment successful, and your subscription is active for 30 days. Let's kick things off with /start, shall we?"
            text = get_global_translated(raw, target_user_settings.get("language", "en-US"))
            async with transaction_lock:
                target_user_first_name, target_user_message_id = target_users.pop(target_user_id, (None, None))

            await delete_message(target_user_id, target_user_id)

            try:
                await bot.delete_message(
                    chat_id=target_user_id,
                    message_id=target_user_message_id
                )
            except Exception: pass
            call_message = await bot.send_message(
                chat_id=target_user_id,
                text=text
            )

            target_user_media = await get_user_media(target_user_id)
            target_user_media['last_message'] = {
                "chat_id": call_message.chat.id,
                "message_id": call_message.message_id
            }
            target_user_media['payment_state'] = None
            await save_user_media(target_user_id, target_user_media)

            await insert_payment(target_user_id, message.text)
            await upsert_subscription(target_user_id)

            caption = f"User {target_user_first_name} ({target_user_id}) was verified."
            await bot.edit_message_caption(
                chat_id=GROUP_ID,
                message_id=original_message_id,
                caption=caption,
                reply_markup=None
            )
            text_ = f"User {target_user_first_name} ({target_user_id}) purchased the Valedictorian Edition plan, paying 3,000 Ks via KBZPay."
            await bot.send_message(
                chat_id=GROUP_ID,
                text=text_
            )
            admin_data['handle_state'] = None
            await save_user_data(admin_id, admin_data)
            break
        except (ConnectionResetError, TimeoutError, aiohttp.ClientError, asyncio.TimeoutError):
            if attempt < maximum: await asyncio.sleep(5)
            else:
                text = "Connection kept dropping while processing the transaction number. Should your network be stable, send the transaction number again."

                async with transaction_lock:
                    administrators[admin_id] = (target_user_id, original_message_id,)

                admin_data['handle_state'] = HandleState.HANDLE_TRANSACTION_NUMBER
                await save_user_data(admin_id, admin_data)

                await bot.send_message(
                    chat_id=GROUP_ID,
                    text=text
                )

async def send_payments_format(chat_id: int, text: str) -> None:
    await bot.send_message(
        chat_id=chat_id,
        text=text
    )

async def handle_payments(message, maximum: int = 3):
    user_id = message.from_user.id
    user_data = await get_user_data(user_id)
    settings = user_data.get("settings", {})

    for attempt in range(1, maximum+1):
        try:
            if message.content_type != 'photo' or message.media_group_id is not None:
                raw = "Please make sure the screenshot is a single photo."
                text = get_global_translated(raw, settings.get("language", "en-US"))
                await send_payments_format(message.chat.id, text)
                return

            photo = message.photo[-1].file_id
            first_name = message.from_user.first_name
            caption = f"User {first_name} ({str(user_id)}) requested approval for the Valedictorian Edition plan with a screenshot of the E-receipt attached."
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("Reject", callback_data=f"reject-{user_id}"),
                 InlineKeyboardButton("Approve", callback_data=f"approve-{user_id}")]
            ])
            await bot.send_photo(
                chat_id=GROUP_ID,
                photo=photo,
                caption=caption,
                reply_markup=markup
            )
            raw = "We received your screenshot, and we'll let you know when the administrator finished reviewing it."
            text = get_global_translated(raw, settings.get("language", "en-US"))
            call_message = await bot.send_message(
                chat_id=message.chat.id,
                text=text
            )
            if call_message:
                async with transaction_lock:
                    target_users[user_id] = (message.from_user.first_name, call_message.message_id,)

            user_media = await get_user_media(user_id)
            user_media['payment_state'] = PaymentState.PAYMENT_PENDING
            await save_user_media(user_id, user_media)

            user_data['handle_state'] = None
            await save_user_data(user_id, user_data)
            break
        except (ConnectionResetError, TimeoutError, aiohttp.ClientError, asyncio.TimeoutError):
            if attempt < maximum: await asyncio.sleep(5)
            else:
                raw = "Connection kept dropping while processing the screenshot. Should your network be stable, send the screenshot again."
                text = get_global_translated(raw, settings.get("language", "en-US"))

                user_data['handle_state'] = HandleState.HANDLE_PAYMENTS
                await save_user_data(user_id, user_data)

                await bot.send_message(
                    chat_id=message.chat.id,
                    text=text
                )

async def download_audio(audio: str) -> str | None:
    try:
        async with session.get(audio) as response:
            if response.status == 200:
                content = await response.read()
                with NamedTemporaryFile(delete=False, suffix=".mp3") as temporary:
                    temporary.write(content)
                    return temporary.name
    except Exception: pass
    return None

async def acquire_lock(user_id: int) -> bool:
    async with acquired_lock:
        if user_id in acquired_users: return False
        acquired_users.add(user_id)
        return True
async def release_lock(user_id: int) -> None:
    async with acquired_lock: acquired_users.discard(user_id)
@asynccontextmanager
async def user_lock(user_id: int):
    acquired = await acquire_lock(user_id)
    try:
        yield acquired
    finally:
        if acquired:
            await release_lock(user_id)

@bot.message_handler(payment=True, chat_types=['private'], content_types=content_type_media)
async def ignore(message): return

@bot.message_handler(payment=False, handle=False, chat_types=['private'], commands=['start'])
async def send_welcome(message):
    user = message.from_user
    chat_id = message.chat.id
    user_id = user.id
    user_data = await get_user_data(user_id)

    if user_data.get("is_active"): return
    await delete_message(chat_id, user_id)
    await get_message(chat_id, user)

@bot.message_handler(payment=False, handle=False, chat_types=['private'], commands=['burmese', 'english'])
async def handle_languages(message):
    user = message.from_user
    chat_id = message.chat.id
    user_id = message.from_user.id
    user_data = await get_user_data(user_id)

    if user_data.get("is_active"): return
    await delete_message(chat_id, user_id)

    language = "my-MM" if message.text[1:] == "burmese" else "en-US"
    await upsert_settings(user_id, language)
    await get_message(chat_id, user)

@bot.message_handler(payment=False, handle=False, chat_types=['private'], commands=['profile'])
async def view_profile(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    user_data = await get_user_data(user_id)

    if user_data.get("is_active"): return
    await delete_message(chat_id, user_id)

    text = await get_formatted_profile(user_id)

    call_message = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML"
    )

    user_media = await get_user_media(user_id)
    user_media['last_message'] = {
        "chat_id": call_message.chat.id,
        "message_id": call_message.message_id
    }
    await save_user_media(user_id, user_media)

@bot.message_handler(payment=False, content_types=content_type_media)
async def state_dispatcher(message):
    if message.chat.type not in ['private']:
        if message.chat.id != GROUP_ID or message.from_user is None or message.from_user.id not in ADMINISTRATORS: return
    user_id = message.from_user.id if message.chat.type in ['private'] else GROUP_ID
    chat_id = message.chat.id
    user_data = await get_user_data(user_id)
    settings = user_data.get("settings")
    state = user_data.get("handle_state")

    if message.chat.type in ['private'] and user_data.get("is_active") == False and state is None:
        await delete_message(chat_id, user_id)
        raw = "Your session has expired due to inactivity. Please send /start to begin anew."
        text = get_global_translated(raw, settings.get("language", "en-US"))
        call_message = await bot.send_message(
            chat_id=chat_id,
            text=text
        )

        user_media = await get_user_media(user_id)
        user_media['last_message'] = {
            "chat_id": call_message.chat.id,
            "message_id": call_message.message_id
        }
        await save_user_media(user_id, user_media)

    if state is None: return

    if state in (HandleState.HANDLE_WORDS, HandleState.HANDLE_ANSWERS) and not message.text:
        return

    async with user_lock(user_id) as acquired:
        if not acquired:
            return
        if state == HandleState.HANDLE_WORDS:
            await handle_words(message)
        elif state == HandleState.HANDLE_ANSWERS:
            await handle_answers(message)
        elif state == HandleState.HANDLE_PAYMENTS:
            await handle_payments(message)
        elif state == HandleState.HANDLE_TRANSACTION_NUMBER:
            await handle_transaction_number(message)

@bot.callback_query_handler(func=lambda call: True)
async def handle_query(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    user_data = await get_user_data(user_id)
    if user_data.get("is_active") == False and any(call.data.startswith(button) for button in requires_active):
        settings = user_data.get("settings")
        await bot.answer_callback_query(call.id, text="Your session has expired", show_alert=True)
        await delete_message(chat_id, user_id)

        raw = "Your session has expired due to inactivity. Please send /start to begin anew."
        text = get_global_translated(raw, settings.get("language", "en-US"))
        call_message = await bot.send_message(
            chat_id=chat_id,
            text=text
        )

        user_media = await get_user_media(user_id)
        user_media['last_message'] = {
            "chat_id": call_message.chat.id,
            "message_id": call_message.message_id
        }
        await save_user_media(user_id, user_media)
        return

    if call.data in ['telegram_stars', 'kbzpay']:
        async with user_lock(user_id) as acquired:
            if not acquired:
                await bot.answer_callback_query(call.id, text="Processing your previous request")
                return

            user_data = await get_user_data(user_id)
            user_media = await get_user_media(user_id)

            if call.data == "telegram_stars":
                await bot.answer_callback_query(call.id, text="Loading (Telegram stars)")

                description = "Become a valedictorian for 30 days with these features: Unlimited learning, Word history, Dictionary/Thesaurus, Skill check, and Burmese support"
                prices = [LabeledPrice(label="Subscription", amount=50)]

                try: await bot.delete_message(chat_id=chat_id, message_id=call.message.message_id)
                except Exception: pass

                call_message = await bot.send_invoice(
                    chat_id=chat_id,
                    title="Valedictorian Edition",
                    description=description,
                    invoice_payload=f"subscription-{user_id}",
                    provider_token="",
                    currency="XTR",
                    prices=prices,
                    start_parameter="valedictorian-edition"
                )

                user_media['last_message'] = {
                    "chat_id": call_message.chat.id,
                    "message_id": call_message.message_id
                }
            elif call.data == "kbzpay":
                await bot.answer_callback_query(call.id, text="Loading (KBZPay)")

                text = "► Valedictorian Edition\nBecome a valedictorian for 30 days with these features:\n— Unlimited learning\n— Word history\n— Dictionary/Thesaurus\n— Skill check\n— Burmese support\n\n"
                text += f"► Payment Details\nPhone number: {KBZPAY_PHONE_NUMBER}\nFull name: {KBZPAY_FULL_NAME}\nAmount (Ks): {KBZPAY_AMOUNT}\nNote: {user_id}\n\n"
                raw = "After making the payment, send me the E-receipt showing it was successful as a screenshot."
                settings = user_data.get("settings")
                text += get_global_translated(raw, settings.get("language", "en-US"))

                call_message = await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=call.message.message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=None
                )
                user_media['last_message'] = {
                    "chat_id": call_message.chat.id,
                    "message_id": call_message.message_id
                }

                user_data['handle_state'] = HandleState.HANDLE_PAYMENTS
                await save_user_data(user_id, user_data)

            await save_user_media(user_id, user_media)
        return
    elif call.data.startswith('reject-') or call.data.startswith('approve-'):
        if user_id not in ADMINISTRATORS:
            await bot.answer_callback_query(call.id, text="Unauthorized action", show_alert=True)
            return

        admin_id = GROUP_ID

        async with user_lock(admin_id) as acquired:
            if not acquired:
                await bot.answer_callback_query(call.id, text="Processing your previous request")
                return

            mode, target_user_id = call.data.split('-')[0], int(call.data.split('-')[1])

            admin_data = await get_user_data(admin_id)

            if mode == "reject":
                target_user_data = await get_user_data(target_user_id)
                target_user_settings = target_user_data.get("settings", {})

                raw = "Your request was unsuccessful because the screenshot may be irrelevant, or the transaction has not been made yet."
                text = get_global_translated(raw, target_user_settings.get("language", "en-US"))
                async with transaction_lock:
                    target_user_first_name, original_message_id = target_users.pop(target_user_id, (None, None))

                await delete_message(target_user_id, target_user_id)

                try:
                    await bot.delete_message(
                        chat_id=target_user_id,
                        message_id=original_message_id
                    )
                except Exception: pass
                call_message = await bot.send_message(
                    chat_id=target_user_id,
                    text=text
                )

                target_user_media = await get_user_media(target_user_id)
                target_user_media['last_message'] = {
                    "chat_id": call_message.chat.id,
                    "message_id": call_message.message_id
                }
                target_user_media['payment_state'] = None
                await save_user_media(target_user_id, target_user_media)

                caption = f"User {target_user_first_name} ({target_user_id}) was rejected."
                await bot.edit_message_caption(
                    chat_id=admin_id,
                    message_id=call.message.message_id,
                    caption=caption,
                    reply_markup=None
                )
            elif mode == "approve":
                async with transaction_lock:
                    administrators[admin_id] = (target_user_id, call.message.message_id,)

                caption = "Please enter the Transaction No. of the payment."

                admin_data['handle_state'] = HandleState.HANDLE_TRANSACTION_NUMBER
                await save_user_data(admin_id, admin_data)

                await bot.edit_message_caption(
                    chat_id=admin_id,
                    message_id=call.message.message_id,
                    caption=caption,
                    reply_markup=None
                )
        return

    _, subscribed, user_data = await asyncio.gather(
        upsert_last_active(user_id), has_subscription(user_id), get_user_data(user_id)
    )
    if subscribed == False:
        await send_subscription(call.message, user_id)
        return

    settings = user_data.get("settings", {})

    if call.data in ["learnButton", "refreshButton-0", "refreshButton-1"]:
        async with user_lock(user_id) as acquired:
            if not acquired:
                await bot.answer_callback_query(call.id, text="Processing your previous request")
                return

            if call.data.endswith('-1'):
                recent_words = user_data.get("recent_words") or await get_recent_words(user_id)
                if not recent_words:
                    await bot.answer_callback_query(call.id, text="Error fetching the words", show_alert=True)
                    return
                words = pick_three(recent_words)
            else: words = await get_random_words()
            await bot.answer_callback_query(call.id, text="Fetching the words")

            user_data['random_words'] = words
            await save_user_data(user_id, user_data)

            text: str | None = None
            if call.data.endswith('-1'):
                raw = "Review thy words, lest thou let them be forgotten:"
                text = get_global_translated(raw, settings.get("language", "en-US"))
            else:
                raw = "Learn these words, should they be granted unto thee:"
                text = get_global_translated(raw, settings.get("language", "en-US"))
            message = await get_formatted_random_words(user_data, text)

            await bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="HTML",
                reply_markup=learn_menu() if not call.data.endswith('-1') else InlineKeyboardMarkup([[InlineKeyboardButton("Refresh", callback_data="refreshButton-1"), InlineKeyboardButton("Start", callback_data="startButton")]])
            )

    elif call.data.startswith('selection-'):
        async with user_lock(user_id) as acquired:
            if not acquired:
                await bot.answer_callback_query(call.id, text="Processing your previous request")
                return

            mode = call.data.split('-')[1]
            message, markup = "", None

            if mode == "menu":
                await bot.answer_callback_query(call.id, text="Loading (Review)")
                raw = "Which method would you prefer for selecting your recent 90 words?"
                message = get_global_translated(raw, settings.get("language", "en-US"))
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Manual", callback_data="selection-manual"),
                        InlineKeyboardButton("Automatic", callback_data="selection-automatic"),
                        InlineKeyboardButton("Learn", callback_data="learnButton")]
                ])

                user_data['handle_state'] = None
                await save_user_data(user_id, user_data)
            elif mode == "manual":
                await bot.answer_callback_query(call.id, text="Loading (Manual selection)")

                results = await get_recent_words(user_id)

                if not results:
                    raw = "No words were found in your recent history, perhaps you haven't learned any yet."
                    message = get_global_translated(raw, settings.get("language", "en-US"))
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("Back", callback_data="selection-menu"),
                         InlineKeyboardButton("Learn", callback_data="learnButton")]
                    ])
                    await bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text=message,
                        parse_mode="HTML",
                        reply_markup=markup
                    )
                    return

                user_data['recent_words'] = results
                message, markup = await recent_words_menu(user_data)

                user_data['handle_state'] = HandleState.HANDLE_WORDS
                await save_user_data(user_id, user_data)
            elif mode == "automatic":
                results = await get_recent_words(user_id)

                if not results:
                    raw = "No words were found in your recent history, perhaps you haven't learned any yet."
                    message = get_global_translated(raw, settings.get("language", "en-US"))
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("Back", callback_data="selection-menu"),
                         InlineKeyboardButton("Learn", callback_data="learnButton")]
                    ])
                    await bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text=message,
                        parse_mode="HTML",
                        reply_markup=markup
                    )
                    return

                user_data['recent_words'] = results
                user_data['random_words'] = pick_three(results)

                await save_user_data(user_id, user_data)

                raw = "Review thy words, lest thou let them be forgotten:"
                text = get_global_translated(raw, settings.get("language", "en-US"))
                message = await get_formatted_random_words(user_data, text)
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Refresh", callback_data="refreshButton-1"),
                     InlineKeyboardButton("Start", callback_data="startButton")]
                ])

            await bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="HTML",
                reply_markup=markup
            )

    elif call.data.startswith("startButton"):
        async with user_lock(user_id) as acquired:
            if not acquired:
                await bot.answer_callback_query(call.id, text="Processing your previous request")
                return

            await bot.answer_callback_query(call.id, text="Loading the words")
            result = await load(user_id)

            user_data = await get_user_data(user_id)

            if not result:
                await bot.answer_callback_query(call.id, text="Error loading the words")
                return

            user_data['is_active'] = True
            await save_user_data(user_id, user_data)

            message, total = await get_dictionary(user_id)
            markup = await dictionary_menu(total, user_id)

            await bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="HTML",
                reply_markup=markup
            )

            media, paths, opened = [], [], []
            try:
                audios = [(data[0].get("prs", {}).get("audio"), data[0].get("hw")) for data in result.values() if data[0].get("prs", {}).get("audio")]
                if audios:
                    tasks = [download_audio(url) for url, _ in audios]
                    downloaded = await asyncio.gather(*tasks)
                    for path, (_, hw) in zip(downloaded, audios):
                        if path:
                            paths.append(path)
                            f = open(path, "rb")
                            opened.append(f)
                            media.append(telebot.types.InputMediaAudio(media=f, title=hw))
                voice_message = None
                if 1 < len(media) < 11:
                    voice_message = await bot.send_media_group(
                        chat_id=call.message.chat.id,
                        media=media
                    )
                elif len(media) == 1:
                    voice_message = await bot.send_audio(
                        chat_id=call.message.chat.id,
                        audio=media[0].media,
                        title=media[0].title
                    )

                user_media = await get_user_media(user_id)
                await delete_voice(call.message.chat.id, user_media.get("last_voice"))
                user_media['last_voice'] = None

                if voice_message:
                    sent = voice_message if isinstance(voice_message, list) else [voice_message]
                    user_media['last_voice'] = {
                        "chat_id": call.message.chat.id,
                        "message_id": sent[0].message_id,
                        "message_ids": [m.message_id for m in sent]
                    }
                await save_user_media(user_id, user_media)
            finally:
                for f in opened:
                    try: f.close()
                    except Exception: pass
                for path in paths:
                    try:
                        if path and os.path.exists(path):
                            os.remove(path)
                    except OSError: pass

    elif call.data.startswith('recent_words_page-'):
        async with user_lock(user_id) as acquired:
            if not acquired:
                await bot.answer_callback_query(call.id, text="Processing your previous request")
                return

            try:
                await bot.answer_callback_query(call.id, text="Loading (Manual selection)")

                page_index = int(call.data.split('-')[-1])
                message, markup = await recent_words_menu(user_data, page_index)

                await bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=message,
                    parse_mode="HTML",
                    reply_markup=markup
                )
            except Exception: pass

    elif call.data.startswith('dictionary-'):
        async with user_lock(user_id) as acquired:
            if not acquired:
                await bot.answer_callback_query(call.id, text="Processing your previous request")
                return

            try:
                await bot.answer_callback_query(call.id, text="Loading (Dictionary)")

                word_index, page_index = call.data.split('-')[1:]
                word_index, page_index = int(word_index), int(page_index)
                message, total = await get_dictionary(user_id, word_index, page_index)
                markup = await dictionary_menu(total, user_id, word_index, page_index)

                await bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=message,
                    parse_mode="HTML",
                    reply_markup=markup
                )
            except Exception: pass

    elif call.data.startswith('thesaurus-'):
        async with user_lock(user_id) as acquired:
            if not acquired:
                await bot.answer_callback_query(call.id, text="Processing your previous request")
                return

            try:
                await bot.answer_callback_query(call.id, text="Loading (Thesaurus)")

                word_index, page_index = call.data.split('-')[1:]
                word_index, page_index = int(word_index), int(page_index)
                message, total = await get_thesaurus(user_id, word_index, page_index)
                markup = thesaurus_menu(total, word_index, page_index)

                await bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=message,
                    parse_mode="HTML",
                    reply_markup=markup
                )
            except Exception: pass

    elif call.data.startswith('skillCheckButton'):
        async with user_lock(user_id) as acquired:
            if not acquired:
                await bot.answer_callback_query(call.id, text="Processing your previous request")
                return

            await bot.answer_callback_query(call.id, text="Loading (Skill check)")
            mode = int(call.data.split('-')[-1])

            raw = "Ready to solidify your knowledge with the Skill check?"
            translated = get_global_translated(raw, settings.get("language", "en-US"))
            message = f"{translated}\n\n— NOTE <i>You won't be able to go back to the Dictionary and Thesaurus as soon as you continue</i>" if  mode == 0 else await get_skill_check(user_id)
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton('Not really', callback_data="dictionary-0-0"),
                 InlineKeyboardButton('Yes, I am', callback_data="skillCheckButton-1")]
            ])

            if mode == 1:
                user_data = await get_user_data(user_id)

                user_data['handle_state'] = HandleState.HANDLE_ANSWERS
                await save_user_data(user_id, user_data)

            await bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="HTML",
                reply_markup=markup if mode == 0 else None
            )

    elif call.data.startswith('tryAgainButton'):
        async with user_lock(user_id) as acquired:
            if not acquired:
                await bot.answer_callback_query(call.id, text="Processing your previous request")
                return

            chat_id = call.message.chat.id
            await bot.answer_callback_query(call.id, text="Loading (Try again)")
            mode = int(call.data.split('-')[-1])

            try:
                await bot.delete_message(chat_id=chat_id, message_id=call.message.message_id)
            except Exception: pass

            if mode == 1:
                answers = user_data.get("pending")
                user_data['pending'] = None
                user_data['handle_state'] = None
                await save_user_data(user_id, user_data)
                if answers:
                    message = await evaluate_answers(answers, chat_id, user_id)
                    if message: await clear_session(chat_id, user_id, message)
                else:
                    text = "Your answers are no longer available. Please send /start to begin anew."
                    await bot.send_message(chat_id=chat_id, text=text)
            else:
                await clear_user_data(user_id)
                text = "The Skill check was successfully aborted."
                call_message = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=main_menu() if mode == 0 else None
                )

                user_media = await get_user_media(user_id)
                user_media['last_message'] = {
                    "chat_id": call_message.chat.id,
                    "message_id": call_message.message_id
                }
                await save_user_media(user_id, user_media)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_main_session()
    await get_lexicon_session()
    await initialize_pool()

    clear_inactive_task = asyncio.create_task(clear_inactive())
    webhook_url = f"{FASTAPI_WEBHOOK_URL}/webhook"
    await bot.set_webhook(url=webhook_url)

    yield

    clear_inactive_task.cancel()
    await asyncio.gather(clear_inactive_task, return_exceptions=True)

    if pool:
        pool.close()
        await pool.wait_closed()

    await close_main_session()
    await close_lexicon_session()
    await redis.aclose()
    await bot.remove_webhook()

app = FastAPI(lifespan=lifespan)

@app.post('/webhook')
async def handle_webhook(request: Request):
    if request.headers.get("content-type") == "application/json":
        json_data = await request.json()
        update = Update.de_json(json_data)
        await bot.process_new_updates([update])
        return Response(status_code=200)
    return Response(status_code=403)

@app.get('/')
async def handle_get():
    return {"status": "ok"}

@app.head('/')
async def handle_head():
    return Response(status_code=200)