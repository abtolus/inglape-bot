from dotenv import find_dotenv, load_dotenv
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyinflect import getAllInflections
from threading import Lock
from re import compile
from itertools import chain, islice
from wonderwords import RandomWord

import os
import telebot
import datetime
import requests
import json

DATA = {}
random_word = RandomWord()
random_words = []

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

BOT_TOKEN = os.getenv('BOT_TOKEN')
bot = telebot.TeleBot(BOT_TOKEN)
DICTIONARY_API_KEY = os.getenv('DICTIONARY_API_KEY')
THESAURUS_API_KEY = os.getenv('THESAURUS_API_KEY')

lock = Lock()

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "Howdy, how are you doing?", reply_markup=main_menu())
@bot.message_handler(func=lambda message: True)
def echo_all(message):
    bot.reply_to(message, message.text, reply_markup=main_menu())

def main_menu():
    main_menu_buttons = [
        [InlineKeyboardButton("Review", callback_data="reviewButton"),
         InlineKeyboardButton("Learn", callback_data="learnButton")]
    ]
    return InlineKeyboardMarkup(main_menu_buttons)
def learn_menu():
    learn_menu_buttons = [
        [InlineKeyboardButton("Refresh", callback_data="refreshButton"),
         InlineKeyboardButton("Start", callback_data="startButton")]
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
def thesaurus_menu(total: int, word_index: int = 0, page_index: int = 0):
    previous_page, next_page = (page_index - 1) % total, (page_index + 1) % total
    thesaurus_menu_buttons = [
        [InlineKeyboardButton('⬅', callback_data=f"thesaurus-{word_index}-{previous_page}"),
         InlineKeyboardButton(f"{page_index+1}/{total}", callback_data="none"),
         InlineKeyboardButton('➡', callback_data=f"thesaurus-{word_index}-{next_page}")],
        [InlineKeyboardButton('Go back to the Dictionary', callback_data=f"dictionary-{word_index}-0")]
    ]
    return InlineKeyboardMarkup(thesaurus_menu_buttons)

def get_random_words():
    return [random_word.word(include_parts_of_speech=[pos]) for pos in ['adjectives', 'nouns', 'verbs']]

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

def fetch_dictionary(index: int = 0):
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
        response = requests.get(url + word + "?key=" + DICTIONARY_API_KEY)
        response.raise_for_status()
        data = response.json()
        entries = data if isinstance(data, list) else [data]

        prs, vrs, fls, hws = {}, [], {}, set()

        for entry in entries:
            if not isinstance(entry, dict): continue
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
    word = list(DATA.keys())[word_index]
    data = DATA[word][0]
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

def fetch_thesaurus(index: int = 0):
    def get_dt(entry):
        for definition in entry.get('def', []):
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
    def get_formatted(entry, dt):
        hw = entry.get("hwi", {}).get("hw")
        meta = entry.get("meta", {})

        texti = []
        visi = []
        if dt:
            for item in dt:
                if isinstance(item, list) and item:
                    item_type = item[0]
                    if item_type == 'text':
                        texti.append(item[1])
                    elif item_type == 'vis':
                        for subitem in item[1]:
                            if 't' in subitem:
                                visi.append(subitem['t'])

        result = {
            "hw": hw,
            "text": get_parsed(" ".join(texti)),
            "vis": [get_parsed(vis) for vis in visi],
            "syns": list(islice(chain.from_iterable(meta.get("syns", [])), 5)),
            "ants": list(islice(chain.from_iterable(meta.get("ants", [])), 5))
        }

        return result
    url = os.getenv('THESAURUS_API_URL')
    word = random_words[index]
    try:
        response = requests.get(url + word + "?key=" + THESAURUS_API_KEY)
        response.raise_for_status()
        data = response.json()
        entries = data if isinstance(data, list) else [data]
        result = {}

        for entry in entries:
            hw = entry.get('hwi', {}).get('hw')
            fl = entry.get('fl')

            if not fl or not hw: continue

            dt = get_dt(entry)
            if dt is not None:
                if hw not in result: result[hw] = {}
                result[hw][fl] = get_formatted(entry, dt)
        for key in result:
            result[key] = dict(sorted(result[key].items()))
        return result
    except Exception as e:
        return f"Error connecting to the server: {e}"

def get_thesaurus(word_index: int = 0, page_index: int = 0):
    word = list(DATA.keys())[word_index]
    data = DATA[word][1]
    hws = list(data.keys())

    total = len(hws)
    page_index %= total
    hw = hws[page_index]
    entries = data[hw]

    text = f"<b>{hw}</b>"

    for i, (key, value) in enumerate(entries.items()):
        text += f"\n\n► {key.upper()}"

        if value.get('text'):
            text += f"\n<b>{i+1}</b> "
            text += f"{value['text'].strip()}:"
            for i, vis in enumerate(value.get('vis', [])):
                text += f"\n   ▸ <i>{vis}</i>"

        text += f"\n\nSynonyms: <i>{', '.join(value.get('syns', []))}</i>"
        ants = value.get('ants', [])
        if ants: text += f"\nAntonyms: <i>{', '.join(ants)}</i>"

    return text, total

def load_data():
    global DATA
    DATA = {}
    for i, word in enumerate(random_words):
        dictionary = fetch_dictionary(i)
        thesaurus = fetch_thesaurus(i)
        if isinstance(dictionary, dict) and isinstance(thesaurus, dict):
            DATA[word] = [dictionary, thesaurus]

def get_formatted_words():
    text = "Ponder these words, should they be granted unto thee:\n\n<pre>" + '\n'.join([f"<i>{pos:<16}</i>{word}" for word, pos in zip(random_words, ['(adjective)', '(noun)', '(verb)'])]) + "</pre>"
    return text

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    if call.data in ["learnButton", "refreshButton"]:
        if not lock.acquire(blocking=False): return
        try:
            bot.answer_callback_query(call.id, text="Fetching the words")
            words = get_random_words()

            if not isinstance(words, list):
                bot.answer_callback_query(call.id, text="Error fetching the words")
                return

            global random_words
            random_words = words
            message = get_formatted_words()

            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="HTML",
                reply_markup=learn_menu()
            )
        finally:
            lock.release()
    elif call.data == "startButton":
        if not lock.acquire(blocking=False): return
        try:
            bot.answer_callback_query(call.id, text="Loading the words")
            load_data()

            if not DATA:
                bot.answer_callback_query(call.id, text="Error loading the words")
                return

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
            bot.answer_callback_query(call.id, text="Loading (Dictionary)")

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
    elif call.data.startswith('thesaurus-'):
        if not lock.acquire(blocking=False): return
        try:
            bot.answer_callback_query(call.id, text="Loading (Thesaurus)")

            word_index, page_index = call.data.split('-')[1:]
            word_index, page_index = int(word_index), int(page_index)
            message, total = get_thesaurus(word_index, page_index)
            markup = thesaurus_menu(total, word_index, page_index)

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