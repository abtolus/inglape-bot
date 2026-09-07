from dotenv import find_dotenv, load_dotenv
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyinflect import getAllInflections
from threading import Lock
from re import compile

import os
import telebot
import datetime
import requests
import asyncio
import httpx
import json

DATA = {}
random_words = []

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

BOT_TOKEN = os.getenv('BOT_TOKEN')
bot = telebot.TeleBot(BOT_TOKEN)
API_KEY = os.getenv('API_KEY')

lock = Lock()

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "Howdy, how are you doing?", reply_markup=main_menu())
@bot.message_handler(func=lambda message: True)
def echo_all(message):
    bot.reply_to(message, message.text, reply_markup=main_menu())

def main_menu():
    main_menu_buttons = [
        [InlineKeyboardButton("Learn", callback_data="learnButton"),
         InlineKeyboardButton("Review", callback_data="reviewButton")]
    ]
    return InlineKeyboardMarkup(main_menu_buttons)
def learn_menu():
    learn_menu_buttons = [
        [InlineKeyboardButton("Start", callback_data="startButton"),
         InlineKeyboardButton("Refresh", callback_data="refreshButton")]
    ]
    return InlineKeyboardMarkup(learn_menu_buttons)
def dictionary_menu(total: int, word_index: int = 0, page_index: int = 0):
    previous_page, next_page = (page_index - 1) % total, (page_index + 1) % total
    previous_word, next_word = (word_index - 1) % len(random_words), (word_index + 1) % len(random_words)
    dictionary_menu_buttons = [
        [InlineKeyboardButton('⬅', callback_data=f"dictionary-{word_index}-{previous_page}"),
         InlineKeyboardButton(f"{page_index+1}/{total}", callback_data="none"),
         InlineKeyboardButton('➡', callback_data=f"dictionary-{word_index}-{next_page}")],
        [InlineKeyboardButton('Previous', callback_data=f"dictionary-{previous_word}-0"),
         InlineKeyboardButton("Thesaurus", callback_data=f"thesaurus-{word_index}-0"),
         InlineKeyboardButton('Next', callback_data=f"dictionary-{next_word}-0")]
    ]
    return InlineKeyboardMarkup(dictionary_menu_buttons)

async def fetch_word(client: httpx.AsyncClient, url: str):
    response = await client.get(url)
    data = response.json()
    return data.get('data', [])[0]['word']
async def get_random_words():
    url = os.getenv('RANDOM_API_URL')
    try:
        async with httpx.AsyncClient() as client:
            tasks = [fetch_word(client, f"{url}?type={pos}&count=1") for pos in ['noun', 'verb', 'adjective', 'adverb']]
            results = await asyncio.gather(*tasks)
            print(results)
            return results
    except Exception as e:
        return f"Error connecting to the server: {e}"

replacements = {
    "bc": "", "ldquo": '"', "rdquo": '"', "p_br": "\n",
    "dx_def": " (-", "/dx_def": ")", "dx": "-", "/dx": "",
    "dx_ety": "-", "/dx_ety": "", "ma": "-more at ", "/ma": "",
}
regex_patterns = [
    (compile(r"\{b\}(.*?)\{\/b\}"), r"<b>\1</b>"),
    (compile(r"\{(?:it|wi|qword)\}(.*?)\{\/(?:it|wi|qword)\}"), r"<i>\1</i>"),
    (compile(r"\{phrase\}(.*?)\{\/phrase\}"), r"<b><i>\1</i></b>"),
    (compile(r"\{parahw\}(.*?)\{\/parahw\}"), r"<b>\1</b>"),
    (compile(r"\{sc\}(.*?)\{\/sc\}"), r"\1"),
    (compile(r"\{inf\}(.*?)\{\/inf\}"), r"<sub>\1</sub>"),
    (compile(r"\{sup\}(.*?)\{\/sup\}"), r"<sup>\1</sup>"),
    (compile(r"\{gloss\}(.*?)\{\/gloss\}"), r"[\1]"),
    (compile(r"\{ds[^}]*\}"), "")
]
regex_link1 = compile(r"\{(?:a_link|d_link|i_link|et_link|mat|sx|dxt):(.*?)\}")
regex_link2 = compile(r"\{(?:a_link|d_link|i_link|et_link|mat|sx|dxt)\|([^}]*)\}")
regex_cleaned = compile(r"\*\d+")
pos = {
    "noun": ["NNS"],
    "verb": ["VBD", "VBG", "VBN", "VBZ"],
    "adjective": ["JJR", "JJS"],
    "adverb": ["RBR", "RBS"],
}
def fetch_dictionary(index: int = 0):
    def get_parsed(text):
        if not text: return ""

        for key, value in replacements.items():
            text = text.replace(f"{{{key}}}", value)

        for regex_pattern, replacement in regex_patterns:
            text = regex_pattern.sub(replacement, text)

        text = regex_link1.sub(
            lambda match: f"<i>{match.group(1).split('|')[1] if '|' in match.group(1) else match.group(1)}</i>", text
            )
        text = regex_link2.sub(
            lambda match: f"<i>{match.group(1).split('|')[0]}</i>", text
        )

        return text

    def get_audio(audio):
        if not audio: return ""

        subdirectory = "bix" if audio.startswith("bix") else "gg" if audio.startswith("gg") else "number" if audio[0].isdigit() or not audio[0].isalpha() else audio[0]

        return f"https://media.merriam-webster.com/audio/prons/en/us/mp3/{subdirectory}/{audio}.mp3"

    def get_inflections(hw, fl):
        if not hw or fl not in pos: return []

        cleaned = regex_cleaned.sub("", hw).strip()
        if not cleaned: return []

        inflections = getAllInflections(cleaned) or {}
        tags = pos[fl]

        result = {form for tag in tags if tag in inflections for form in inflections[tag] if form != cleaned}
        return sorted(result)

    def get_formatted(entry, dt, fl, vd=None):
        hw = entry.get("hwi", {}).get("hw")
        et = entry.get("et", [[None, None]])[0][1] if entry.get("et") else None
        date = entry.get("date")

        dti = dt or []
        texti = [item[1] for item in dti if isinstance(item, list) and item and item[0] == 'text']
        visi = [subitem['t'] for item in dti if isinstance(item, list) and item and item[0] == 'vis' for subitem in item[1]]
        insi = get_inflections(hw, fl)

        result = {
            "hw": hw,
            "et": get_parsed(et) if et else None,
            "date": get_parsed(date) if date else None,
            "ins": insi if insi else [],
            "text": get_parsed(" ".join(texti)),
            "vis": [get_parsed(vis) for vis in visi]
        }

        if fl == "verb": result['vd'] = vd

        return result

    def get_dt(entry, vd=None):
        for definition in entry.get('def', []):
            if vd is not None and definition.get('vd') != vd: continue

            for sseq in definition.get('sseq', []):
                if not sseq or not sseq[0]: continue

                sense = sseq[0]
                sense_type, sense_data = sense[0], sense[1]

                dt = None
                if sense_type == 'sense': dt = sense_data.get('dt')
                elif sense_type == 'pseq' and sense_data: dt = sense_data[0][1].get('dt')
                elif sense_type == 'bs': dt = sense_data.get('sense').get('dt')

                if dt: return dt[0][1][0] if dt[0][0] == 'uns' else dt

        return None

    url = os.getenv('DICTIONARY_API_URL')
    word = random_words[index]
    try:
        response = requests.get(url + word + "?key=" + API_KEY)
        response.raise_for_status()
        data = response.json()
        entries = data if isinstance(data, list) else [data]

        prs, vrs, fls, hws = {}, [], {}, set()

        for entry in entries:
            fl = entry.get('fl')

            if not prs:
                for pr in entry.get('hwi', {}).get('prs', []):
                    phonetics, audio = pr.get('mw'), pr.get('sound', {}).get('audio')

                    if phonetics and audio:
                        prs = {"phonetics": phonetics, "audio": get_audio(audio)}
                        break

            for vr in entry.get('vrs', []):
                if vr: vrs.append(vr.get('va').replace('*', ''))

            if not fl: continue

            if fl not in fls: fls[fl] = []

            if fl == 'verb':
                hw = entry.get('hwi', {}).get('hw')

                if hw and hw not in hws:
                    hws.add(hw)

                    for vd in ['transitive verb', 'intransitive verb']:
                        dt = get_dt(entry, vd)
                        if dt is not None:
                            fls[fl].append(get_formatted(entry, dt, 'verb', vd))
            else:
                dt = get_dt(entry)
                if dt is not None:
                    fls[fl].append(get_formatted(entry, dt, fl))

        result = {"hw": word, "prs": prs, "vrs": vrs, "def": dict(sorted(fls.items()))}
        return result
    except Exception as e:
        return f"Error connecting to the server: {e}"

def get_dictionary(word_index: int = 0, page_index: int = 0):
    global DATA
    if not DATA: DATA = {word: fetch_dictionary(i) for i, word in enumerate(random_words)}
    word = list(DATA.keys())[word_index]
    data = DATA[word]
    definitions = data.get('def', {})
    posi = list(definitions.keys())

    total = len(posi)
    page_index %= total
    pos = posi[page_index]
    entries = definitions[pos]

    text = f"<b>{data.get('hw')}</b> /{data.get('prs')['phonetics']}/"
    vrs = data.get('vrs')
    if vrs: text += f" (also <i>{vrs[0]}</i>)" if len(vrs) == 1 else f" (also <i>{vrs[0]}</i> or <i>{vrs[1]}</i>)" if len(vrs) == 2 else f" (also <i>{', '.join(vrs[:-1])},</i> or <i>{vrs[-1]}</i>)"
    text += "\n\n"

    text += f"► {pos.upper()}"
    ins = entries[0].get('ins')
    if ins: text += f" ({', '.join(ins)})"

    for i, entry in enumerate(entries):
        if entry.get('text'):
            text += f"\n<b>{i+1}</b> "
            hw = entry.get('hw').replace('*', '')
            if hw != word: text += f"({hw}) "
            text += f"{entry['text'].strip()}:"
            for i, vis in enumerate(entry.get('vis', [])):
                text += f"\n   ▸ <i>{vis}</i>"

    et, date = entries[0].get('et'), entries[0].get('date')
    if et: text += f"\n\n— ETYMOLOGY {et}\n{date}"

    return text, total

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    if call.data == "learnButton":
        if not lock.acquire(blocking=False): return
        try:
            global random_words
            bot.answer_callback_query(call.id, text="Loading the first word.")
            random_words = asyncio.run(get_random_words())
            message, total = get_dictionary()
            markup = dictionary_menu(total)
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="HTML",
                reply_markup=markup
            )
        finally:
            lock.release()
    elif call.data.startswith('dictionary-'):
        if not lock.acquire(blocking=False): return
        try:
            bot.answer_callback_query(call.id, text="Loading the next word.")
            word_index, page_index = call.data.split('-')[1:]
            word_index, page_index = int(word_index), int(page_index)
            message, total = get_dictionary(word_index, page_index)
            markup = dictionary_menu(total, word_index, page_index)
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="HTML",
                reply_markup=markup
            )
        finally:
            lock.release()
bot.infinity_polling()