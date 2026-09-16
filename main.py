from dotenv import find_dotenv, load_dotenv
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyinflect import getAllInflections
from re import compile, MULTILINE, sub, search, DOTALL
from itertools import chain, islice
from wonderwords import RandomWord
from collections import deque
from openrouter import OpenRouter
from ast import literal_eval
from deep_translator import MyMemoryTranslator
from concurrent.futures import ThreadPoolExecutor
from time import sleep
from tempfile import NamedTemporaryFile

import os
import telebot
import requests
import random
import mysql.connector
import json
import urllib3
import gzip

with open('trans.json', "r", encoding="utf-8") as f:
    global_translations = json.load(f)
with open('google-10000-english.txt', "r", encoding="utf-8") as f:
    common_words = set(line.strip() for line in f)
pool = mysql.connector.pooling.MySQLConnectionPool(
    pool_name="inglapebot", pool_size=24,
    host=os.getenv('HOST'),
    user=os.getenv('USER'),
    password=os.getenv('PASSWORD'),
    port=os.getenv('PORT'),
    database=os.getenv('DATABASE')
)
def get_pool_connection(): return pool.get_connection()

def get_settings(user_id: int) -> dict:
    with get_pool_connection() as connection:
        with connection.cursor() as cursor:
            query = "SELECT settings FROM users WHERE id = %s"
            cursor.execute(query, (user_id,))
            result = cursor.fetchone()
            if result and result[0]: return json.loads(result[0]) if isinstance(result[0], str) else result[0]
            return {
                "language": "en-US"
            }

def get_global_translated(text: str, language: str) -> str:
    if language == "en-US": return text
    return global_translations[text]

USER_DATA = {}
def get_user_data(user_id: int) -> dict:
    if user_id not in USER_DATA:
        USER_DATA[user_id] = {
            "data": {}, "pending": None, "answers": [], "random_words": [], "recent_words": [], "translations": {}, "is_active": False, "last_message": None, "last_voice": None, "settings": get_settings(user_id)
        }
    return USER_DATA[user_id]
def clear_user_data(user_id: int) -> None:
    USER_DATA.pop(user_id, None)

random_word = RandomWord()

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

class ExceptionHandler(telebot.ExceptionHandler):
    def handle(self, exception):
        if isinstance(exception, (ConnectionResetError, urllib3.exceptions.ProtocolError, requests.exceptions.ConnectionError, TimeoutError, urllib3.exceptions.ReadTimeoutError, requests.exceptions.ReadTimeout,)): return True
BOT_TOKEN = os.getenv('BOT_TOKEN')
bot = telebot.TeleBot(BOT_TOKEN, exception_handler=ExceptionHandler())

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
DICTIONARY_API_URL = os.getenv('DICTIONARY_API_URL')
DICTIONARY_API_KEY = os.getenv('DICTIONARY_API_KEY')
THESAURUS_API_URL = os.getenv('THESAURUS_API_URL')
THESAURUS_API_KEY = os.getenv('THESAURUS_API_KEY')

session = requests.Session()

lock = set()

translator = MyMemoryTranslator(source="en-US", target="my-MM", session=session)

def get_text(text: str, user_id: int) -> str:
    user_data = get_user_data(user_id)
    language = user_data.get("settings", {}).get("language", "en-US")
    translations = user_data.get("translations", {})
    if language == "my-MM": return translations.get(text, text)
    return text
def get_definitions(data: list) -> list:
    dictionary, thesaurus = data[0], data[1]
    result = []

    for entries in dictionary.get("def", {}).values():
        for entry in entries:
            if entry.get("text"):
                result.append(entry['text'].strip())

    for hws in thesaurus.values():
        for posi in hws.values():
            if isinstance(posi, dict) and posi.get("text"):
                result.append(posi['text'].strip())

    return result
def translate_single(text: str) -> tuple[str, str]:
    try:
        translated = translator.translate(text)
        return text, translated
    except Exception:
        return text, text
def handle_translations(data: list, user_id: int) -> None:
    user_data = get_user_data(user_id)
    translations = user_data['translations']

    definitions = list(set(get_definitions(data)))
    untranslated = [definition for definition in definitions if definition not in translations]

    if not untranslated: return

    with ThreadPoolExecutor(max_workers=min(10, len(untranslated))) as executor:
        results = executor.map(translate_single, untranslated)

    for origin, translated in results:
        translations[origin] = translated

def upsert_settings(user_id: int, new_value: str, key: str = 'language') -> None:
    with get_pool_connection() as connection:
        with connection.cursor() as cursor:
            query = f"UPDATE users SET settings = JSON_SET(settings, '$.{key}', %s) WHERE id = %s"
            values = (new_value, user_id,)
            cursor.execute(query, values)
            connection.commit()
def upsert_user(user) -> None:
    query = "INSERT INTO users (id, username, first_name, last_name, settings) VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE username = VALUES(username), first_name = VALUES(first_name), last_name = VALUES(last_name)"
    settings = {"language": "en-US"}
    values = (user.id, user.username, user.first_name, user.last_name, json.dumps(settings),)
    with get_pool_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, values)
            connection.commit()
def upsert_word(word: str, part_of_speech: str, data: list) -> int:
    string = json.dumps(data, ensure_ascii=False)
    compressed = gzip.compress(string.encode("utf-8"), compresslevel=9)
    query = "INSERT INTO words (word, part_of_speech, data) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)"
    with get_pool_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (word, part_of_speech, compressed,))
            connection.commit()
            return cursor.lastrowid
def insert_log(user_id: int, word_id: int) -> None:
    query = "INSERT INTO logs (user_id, word_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE created_at = CURRENT_TIMESTAMP"
    cleanup = "DELETE FROM logs WHERE user_id = %s AND id NOT IN (SELECT id FROM (SELECT id FROM logs WHERE user_id = %s ORDER BY created_at DESC LIMIT 50) AS subquery)"
    with get_pool_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (user_id, word_id,))
            cursor.execute(cleanup, (user_id, user_id,))
            connection.commit()
def get_recent_words(user_id: int, limit: int = 45) -> list:
    query = "SELECT w.word, w.part_of_speech FROM logs l JOIN words w ON l.word_id = w.id WHERE l.user_id = %s ORDER BY l.created_at DESC LIMIT %s"
    with get_pool_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (user_id, limit,))
            return cursor.fetchall()

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

def get_parsed(text: str) -> str:
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

def get_audio(audio: str) -> str:
    if not audio: return ""

    subdirectory = "bix" if audio.startswith("bix") else "gg" if audio.startswith("gg") else "number" if audio[0].isdigit() or not audio[0].isalpha() else audio[0]

    return f"https://media.merriam-webster.com/audio/prons/en/us/mp3/{subdirectory}/{audio}.mp3"

def get_inflections(hw: str, fl: str) -> list:
    if not hw or fl not in pos: return []

    cleaned = regex_cleaned.sub("", hw).strip()
    if not cleaned: return []

    inflections = getAllInflections(cleaned) or {}
    tags = pos[fl]

    result = {form for tag in tags if tag in inflections for form in inflections[tag] if form != cleaned}
    return sorted(result)

def get_formatted_dictionary(entry: dict, dt: list, fl: str, vd: str | None = None) -> dict:
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

def get_formatted_thesaurus(entry: dict, dt: list) -> dict:
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

def get_dt_dictionary(entry: dict, vd: str | None = None) -> list | None:
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

def get_dt_thesaurus(entry: dict) -> list | None:
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

def parse_dictionary(word: str, data: list) -> dict:
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
                    dt = get_dt_dictionary(entry, vd)
                    if dt is not None:
                        fls[fl].append(get_formatted_dictionary(entry, dt, 'verb', vd))
        else:
            dt = get_dt_dictionary(entry)
            if dt is not None:
                fls[fl].append(get_formatted_dictionary(entry, dt, fl))

    result = {"hw": word, "prs": prs, "vrs": vrs, "def": dict(sorted(fls.items()))}
    return result

def parse_thesaurus(word: str, data: list) -> dict:
    entries = data if isinstance(data, list) else [data]
    result = {}

    if not isinstance(entries[0], dict):
        result[word] = {"text": "This word does not have a thesaurus at the moment."}
        return result

    for entry in entries:
        hw = entry.get('hwi', {}).get('hw')
        fl = entry.get('fl')

        if not fl or not hw: continue

        dt = get_dt_thesaurus(entry)
        if dt is not None:
            if hw not in result: result[hw] = {}
            result[hw][fl] = get_formatted_thesaurus(entry, dt)
    for key in result:
        result[key] = dict(sorted(result[key].items()))
    return result

def fetch_dictionary(word: str) -> dict:
    try:
        response = session.get(f"{DICTIONARY_API_URL}{word}?key={DICTIONARY_API_KEY}", timeout=5)
        response.raise_for_status()
        return {"word": word, "data": response.json()}
    except Exception as e:
        return {"word": word, "error": str(e)}

def fetch_thesaurus(word: str) -> dict:
    try:
        response = session.get(f"{THESAURUS_API_URL}{word}?key={THESAURUS_API_KEY}", timeout=5)
        response.raise_for_status()
        return {"word": word, "data": response.json()}
    except Exception as e:
        return {"word": word, "error": str(e)}

def get_parsed_data(word: str) -> tuple[dict | None, dict | None]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        dictionary_futures = executor.submit(fetch_dictionary, word)
        thesaurus_futures = executor.submit(fetch_thesaurus, word)

        dictionary_results = dictionary_futures.result().get('data')
        thesaurus_results = thesaurus_futures.result().get('data')

    if dictionary_results and thesaurus_results: 
        parsed_dictionary = parse_dictionary(word, dictionary_results)
        parsed_thesaurus = parse_thesaurus(word, thesaurus_results)
        return parsed_dictionary, parsed_thesaurus
    return None, None

def reload_data(random_words: list[tuple[str, str]]) -> dict:
    if not random_words: return {}

    condition = " OR ".join(["(word = %s AND part_of_speech = %s)"] * len(random_words))
    values = [subitem for item in random_words for subitem in item]

    query = f"SELECT word, part_of_speech, data FROM words WHERE {condition}"
    result = {}

    with get_pool_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, values)
            results = cursor.fetchall()
            for word, part_of_speech, compressed in results:
                data = gzip.decompress(compressed).decode("utf-8")
                result[(word, part_of_speech)] = json.loads(data)

    return result

def load_data(user_id: int) -> None:
    user_data = get_user_data(user_id)
    random_words = user_data.get("random_words", [])
    if not random_words: return

    cache = reload_data(random_words)

    result = {}
    missing = []

    for word, pos in random_words:
        if (word, pos) in cache: result[word] = cache[(word, pos)]
        else: missing.append((word, pos))

    if missing:
        words = list({word[0] for word in missing})
        apis = {}

        with ThreadPoolExecutor(max_workers=min(6, len(words))) as executor:
            futures = {executor.submit(get_parsed_data, word): word for word in words}

            for future in futures:
                parsed_word = futures[future]
                parsed_dictionary, parsed_thesaurus = future.result()
                if parsed_dictionary and parsed_thesaurus:
                    data = [parsed_dictionary, parsed_thesaurus]
                    handle_translations(data, user_id)
                    apis[parsed_word] = data

        for word, pos in missing:
            if word in apis:
                data = apis[word]
                result[word] = data

    if result: user_data['data'] = result

def delete_message(chat_id: int, user_id: int):
    user_data = get_user_data(user_id)
    last_message = user_data.get("last_message")
    if not last_message: return
    try:
        bot.delete_message(chat_id=chat_id, message_id=last_message.message_id)
    except Exception: pass
    user_data['last_message'] = None

def get_message(chat_id: int, user):
    user_id = user.id
    clear_user_data(user_id)
    user_data = get_user_data(user_id)
    settings = user_data.get("settings", {})

    raw = "Wilt thou learn anew, or weigh what thou hast learned?"
    translated = get_global_translated(raw, settings['language'])
    text = f"{translated}\n\n/my — မြန်မာဘာသာစကားသို့\n/en — to the English language"

    sent = bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=main_menu()
    )
    user_data['last_message'] = sent
    upsert_user(user)

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user = message.from_user
    chat_id = message.chat.id
    user_id = user.id
    user_data = get_user_data(user_id)

    if user_data.get("last_message"):
        try:
            bot.delete_message(chat_id=chat_id, message_id=message.message_id)
        except Exception: pass

    if user_data.get("is_active"): return
    delete_message(chat_id, user_id)
    get_message(chat_id, user)

@bot.message_handler(commands=['my', 'en'])
def handle_languages(message):
    user = message.from_user
    chat_id = message.chat.id
    user_id = message.from_user.id
    user_data = get_user_data(user_id)

    try:
        bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception: pass

    if user_data.get("is_active"): return
    delete_message(chat_id, user_id)

    language = "my-MM" if message.text[1:] == "my" else "en-US"
    upsert_settings(user_id, language)
    get_message(chat_id, user)

regex_words = compile(r"^[a-zA-Z]+,\s[a-zA-Z]+,\s[a-zA-Z]+$")

def handle_words(message, maximum: int = 3) -> None:
    user_id = message.from_user.id
    user_data = get_user_data(user_id)
    recent_words = user_data.get("recent_words", [])
    settings = user_data.get("settings", {})

    words = [word for word, *_ in recent_words]

    for attempt in range(1, maximum+1):
        try:
            if not bool(regex_words.fullmatch(message.text.strip())):
                raw = "Please select your recent words in the exact same format as the following:"
                translated = get_global_translated(raw, settings['language'])
                text = f"{translated}\n<pre>{', '.join(random.sample(words, k=3))}</pre>"
                send_words_format(message, text)
                return

            input_words = [word.strip().lower() for word in message.text.split(',')]
            if len(input_words) != len(set(input_words)):
                raw = "The selected words from your recent history must be unique:"
                translated = get_global_translated(raw, settings['language'])
                text = f"{translated}\n<pre>{', '.join(words)}</pre>"
                send_words_format(message, text)
                return

            matched = [item for item in recent_words if item[0].lower() in input_words]
            found = {item[0].lower() for item in matched}
            missing = [word for word in input_words if word not in found]

            if not missing:
                if not isinstance(matched, list):
                    return

                user_data['random_words'] = matched
                raw = "Review thy words, lest thou let them be forgotten:"
                text_ = get_global_translated(raw, settings['language'])
                text = get_formatted_words(user_id, text_)
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Refresh", callback_data="selection-manual"),
                        InlineKeyboardButton("Start", callback_data="startButton")]
                ])

                sent = bot.send_message(
                    chat_id=message.chat.id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup
                )
                user_data['last_message'] = sent
            else:
                raw = "Not all your selected words were found in your recent history:"
                translated = get_global_translated(raw, settings['language'])
                text = f"{translated}\n<pre>{', '.join(words)}</pre>"
                send_words_format(message, text)
                return
            break
        except (ConnectionResetError, urllib3.exceptions.ProtocolError, requests.exceptions.ConnectionError, TimeoutError, urllib3.exceptions.ReadTimeoutError, requests.exceptions.ReadTimeout) as e:
            if attempt < maximum: sleep(5)
            else:
                raw = "Connection kept dropping during the process of retrieving the words. Should your network be stable, select your recent words again:"
                translated = get_global_translated(raw, settings['language'])
                text = f"{translated}\n<pre>{', '.join(words)}</pre>"
                bot.send_message(
                    chat_id=message.chat.id,
                    text=text,
                    parse_mode="HTML"
                )
                bot.register_next_step_handler(message, handle_answers)

def send_words_format(message, text: str) -> None:
    call_message = bot.send_message(
        chat_id=message.chat.id,
        text=text,
        parse_mode="HTML"
    )
    bot.register_next_step_handler(call_message, handle_words)

regex_answers = compile(r"^(?:\d+\.\s+[\w\s.,!?()\'\"-]+(?:\n|$))+(?:\n(?:\d+\.\s+[\w\s.,!?()\'\"-]+(?:\n|$))+)*$", MULTILINE)

def handle_answers(message, maximum: int = 3) -> None:
    user_id = message.from_user.id
    user_data = get_user_data(user_id)
    ans = user_data.get("answers", [])
    random_words = user_data.get("random_words", [])
    settings = user_data.get("settings", {})
    data = user_data.get("data", {})

    for attempt in range(1, maximum+1):
        try:
            if not bool(regex_answers.fullmatch(message.text.strip())):
                send_answers_format(message)
                return

            sections = message.text.strip().split('\n\n')
            lines = [[sub(r"^\d+[\.\s]*", "", line).strip() for line in section.splitlines() if line.strip()] for section in sections]

            if len(lines) != 4 or len(ans) != 4 or not all(len(i) == len(j) for i, j in zip(lines, ans)):
                send_answers_format(message)
                return

            result = check_answers(lines, message.chat.id, user_id)

            if result:
                bot.send_message(
                    chat_id = message.chat.id,
                    text=result,
                    parse_mode="HTML"
                )

                for word, part_of_speech in random_words:
                    insert_log(user_id, upsert_word(word, part_of_speech, data[word]))

                last_voice = user_data.get("last_voice")
                if last_voice:
                    try:
                        bot.delete_message(chat_id=message.chat.id, message_id=last_voice.message_id)
                    except Exception: pass

                clear_user_data(user_id)
                user_data = get_user_data(user_id)

                raw = "The journey of learning hath borne fruit. Wilt thou learn anew, or weigh what thou hast learned?"
                sent = bot.send_message(
                    chat_id=message.chat.id,
                    text=get_global_translated(raw, settings['language']),
                    parse_mode="HTML",
                    reply_markup=main_menu()
                )

                user_data['last_message'] = sent
            break
        except (ConnectionResetError, urllib3.exceptions.ProtocolError, requests.exceptions.ConnectionError, TimeoutError, urllib3.exceptions.ReadTimeoutError, requests.exceptions.ReadTimeout) as e:
            if attempt < maximum: sleep(5)
            else:
                raw = "Connection kept dropping during the process of evaluating your answers. Should your network be stable, resend your answers."
                bot.send_message(
                    chat_id=message.chat.id,
                    text=get_global_translated(raw, settings.get('language', 'en-US')),
                    parse_mode="HTML"
                )
                bot.register_next_step_handler(message, handle_answers)

def send_answers_format(message) -> None:
    user_id = message.from_user.id
    user_data = get_user_data(user_id)
    settings = user_data.get("settings", {})

    raw = "Please provide your answers in the exact same format as the following:"
    translated = get_global_translated(raw, settings['language'])
    text = f"{translated}\n\n"
    text += f"<pre>{'\n\n'.join(get_format(user_id))}</pre>"

    call_message = bot.send_message(
        chat_id=message.chat.id,
        text=text,
        parse_mode="HTML"
    )
    bot.register_next_step_handler(call_message, handle_answers)

def get_format(user_id: int) -> list:
    user_data = get_user_data(user_id)
    ans = user_data.get("answers", [])

    paragraph = "Curabitur lobortis quam sit amet augue porta hendrerit.\nMorbi efficitur ante sed lorem porttitor, a mollis purus sagittis.\nPellentesque nec erat sit amet nunc volutpat mollis sit amet nec nibh.\nMorbi dignissim risus vitae dui sollicitudin, at fermentum risus rhoncus.\nNam fermentum elit eu neque malesuada feugiat.\nAliquam porta est ac eros ultricies varius."

    sentences = [sentence for sentence in paragraph.strip().split('\n')]
    result = []

    for _ in range(3):
        result.append('\n'.join([f"{i+1}. {(words := sentences[i].split())[0].lower()} {words[1].lower()}" for i in range(len(ans[0]))]))
    result.append('\n'.join([f"{i+1}. {sentences[i]}" for i in range(len(ans[0]))]))
    return result

def check_answers(answers: list, chat_id: int, user_id: int) -> str:
    user_data = get_user_data(user_id)
    ans = user_data.get("answers", [])

    score = 0
    total = sum(len(sublist) for sublist in answers)

    text = "— RESULTS\n"

    for i, j in zip(answers[0], ans[0]):
        text += f"\n{'✅' if i in j else '❌'} <i>{i}</i>"
        score += int(i in j)

    text += '\n'
    for i, j in zip(answers[1], ans[1]):
        text += f"\n{'✅' if i == j else '❌'} <i>{i}</i>"
        score += int(i == j)

    text += '\n'
    for i, j in zip(answers[2], ans[2]):
        text += f"\n{'✅' if i == j else '❌'} <i>{i}</i>"
        score += int(i == j)

    text += '\n'
    stage1, stage2 = [j in i.lower() for i, j in zip(answers[3], ans[3])], []
    with OpenRouter(api_key=OPENROUTER_API_KEY) as client:
        try:
            sentences = "\n".join([f"{i+1}. {s}" for i, s in enumerate(answers[3])])
            prompt = f"Inspect the following sentences for grammatical errors. Only return a Python list of True if it is correct, or False if it is incorrect, in the exact same order as the sentences.\nSentences:\n{sentences}"

            response = client.chat.send(model="liquid/lfm-2.5-2.6b:free", messages=[{"role": "user", "content": prompt}])

            match = search(r"\[.*?\]", response.choices[0].message.content, DOTALL)
            if match: stage2 = literal_eval(match.group(0))
        except Exception as e:
            user_data['pending'] = answers
            try_again(chat_id)

    if not stage2: stage2 = [False] * len(stage1)
    ans[3] = [i and j for i, j in zip(stage1, stage2)]
    for i, j in zip(answers[3], ans[3]):
        text += f"\n{'✅' if j else '❌'} <i>{i}</i>"
        score += int(j)

    final = round((score / total) * 100, 2)
    text += f"\n\nYou <b>{'passed' if final >= 40 else 'failed'}</b> the Skill check with a score of {final}%."
    return text

def try_again(chat_id: int) -> None:
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Abort checking", callback_data="tryAgainButton-0"),
         InlineKeyboardButton("Try again", callback_data="tryAgainButton-1")]
    ])
    message = "Too many requests are being sent at the moment. Please <b>Try again</b> later, or <b>Abort checking</b> if you do not need your answers to be evaluated."

    bot.send_message(
        chat_id=chat_id,
        text=message,
        parse_mode="HTML",
        reply_markup=markup
    )

def can_learn(user_id: int) -> bool: return get_count(user_id) < 15
def get_count(user_id: int) -> int:
    query = "SELECT COUNT(*) FROM logs WHERE user_id = %s AND DATE(created_at) = CURDATE()"
    with get_pool_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (user_id,))
            result = cursor.fetchone()
            return result[0] if result else 0
def send_daily_limit(call, settings: dict) -> None:
    bot.answer_callback_query(call.id, text="Daily limit reached", show_alert=True)
    raw = "Since you have reached your daily limit of 15 words for today, come back tomorrow."
    translated = get_global_translated(raw, settings.get('language', 'en-US'))
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=translated,
        parse_mode="HTML"
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

def dictionary_menu(total: int, user_id: int, word_index: int = 0, page_index: int = 0) -> InlineKeyboardMarkup:
    user_data = get_user_data(user_id)
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

def get_random_word(pos: str) -> tuple[str, str]:
    while True:
        word = random_word.word(include_parts_of_speech=[pos])
        if word in common_words: return (word, pos[:-1],)
def get_random_words() -> list: return [get_random_word(pos) for pos in ['adjectives', 'nouns', 'verbs']]

def get_dictionary(user_id: int, word_index: int = 0, page_index: int = 0):
    user_data = get_user_data(user_id)
    result = user_data.get("data", {})

    word = list(result.keys())[word_index]
    data = result[word][0]
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
            definition = get_text(entry['text'].strip(), user_id)
            text += f"{definition}:"
            for i, vis in enumerate(entry.get('vis', [])):
                text += f"\n   ▸ <i>{vis}</i>"

    et, date = entries[0].get('et'), entries[0].get('date')
    if et: text += f"\n\n— ETYMOLOGY {et}\n{date}"

    return text, total

def get_thesaurus(user_id: int, word_index: int = 0, page_index: int = 0):
    user_data = get_user_data(user_id)
    result = user_data.get("data", {})

    word = list(result.keys())[word_index]
    data = result[word][1]
    hws = list(data.keys())

    total = len(hws)
    page_index %= total
    hw = hws[page_index]
    entries = data[hw]

    text = f"<b>{hw}</b>"

    for i, (key, value) in enumerate(entries.items()):
        if key == 'text':
            text += f"\n\n► {value}"
            return text, total
        text += f"\n\n► {key.upper()}"

        if value.get('text'):
            text += f"\n<b>{i+1}</b> "

            definition = get_text(value['text'].strip(), user_id)
            text += f"{definition}:"
            for i, vis in enumerate(value.get('vis', [])):
                text += f"\n   ▸ <i>{vis}</i>"

        syns = value.get('syns', [])
        if syns: text += f"\n\nSynonyms: <i>{', '.join(syns)}</i>"
        ants = value.get('ants', [])
        if ants: text += f"\nAntonyms: <i>{', '.join(ants)}</i>"

    return text, total

def fetch_skill_check(user_id: int) -> list:
    user_data = get_user_data(user_id)
    random_words = user_data.get("random_words", [])
    data_ = user_data.get("data", {})

    return [{"hw": entry.get('hw'), "text": entry.get('text'), "syns": entry.get('syns')} for word, *_ in random_words for value in data_[word][1].values() for pos, entry in (next(iter(value.items())),) if pos != 'text']

def get_skill_check(user_id: int) -> str:
    user_data = get_user_data(user_id)
    user_data['answers'] = []
    settings = user_data.get("settings", {})

    data = fetch_skill_check(user_id)
    sections = deque(random.sample(data, k=5) if len(data) >= 5 else random.sample(data, k=len(data)) for _ in range(4))

    raw = f"Provide a synonym for each of the following: (The answers must be from the Dictionary and Thesaurus you recently learned)"
    text = get_global_translated(raw, settings['language'])
    temporary = []
    for i, entry in enumerate(sections.popleft()):
        text += f"\n<b>{i+1}</b> <i>{entry.get('hw')}</i>"
        temporary.append(entry.get('syns'))
    user_data['answers'].append(temporary)
    text += "\n\n"

    raw = f"Identify the term defined by each of the following: (The answers must be from the Dictionary and Thesaurus you recently learned)"
    text += get_global_translated(raw, settings['language'])
    temporary = []
    for i, entry in enumerate(sections.popleft()):
        text += f"\n<b>{i+1}</b> <i>{get_text(entry.get('text').strip(), user_id)}</i>"
        temporary.append(entry.get('hw'))
    user_data['answers'].append(temporary)
    text += "\n\n"

    raw = f"Give a term matching each pair of synonyms in the following: (The answers must be from the Dictionary and Thesaurus you recently learned)"
    text += get_global_translated(raw, settings['language'])
    temporary = []
    for i, entry in enumerate(sections.popleft()):
        text += f"\n<b>{i+1}</b> <i>{', '.join(entry.get('syns', []))}</i>"
        temporary.append(entry.get('hw'))
    user_data['answers'].append(temporary)
    text += "\n\n"

    raw = f"Write sentences using each of the following:"
    text += get_global_translated(raw, settings['language'])
    temporary = []
    for i, entry in enumerate(sections.popleft()):
        text += f"\n<b>{i+1}</b> <i>{entry.get('hw')}</i>"
        temporary.append(entry.get('hw'))
    user_data['answers'].append(temporary)

    return text

def get_formatted_words(user_id: int, text) -> str:
    user_data = get_user_data(user_id)
    random_words = user_data.get("random_words", [])

    formatted = "\n".join([f"<i>{pos:<16}</i>{word}" for word, pos in random_words])
    return f"{text}\n\n<pre>{formatted}</pre>"

def acquire_lock(user_id: int) -> bool:
    if user_id in lock:
        return False
    lock.add(user_id)
    return True
def release_lock(user_id: int) -> None:
    lock.discard(user_id)

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    user_id = call.from_user.id
    user_data = get_user_data(user_id)
    settings = user_data.get("settings", {})

    if call.data in ["learnButton", "refreshButton-0", "refreshButton-1"]:
        if not acquire_lock(user_id):
            bot.answer_callback_query(call.id, text="Processing your previous request")
            return
        try:
            if not can_learn(user_id):
                send_daily_limit(call, settings)
                return
            bot.answer_callback_query(call.id, text="Fetching the words")
            recent_words = user_data.get("recent_words", [])
            words = random.sample(recent_words, k=3) if call.data.endswith('-1') else get_random_words()

            if not isinstance(words, list):
                bot.answer_callback_query(call.id, text="Error fetching the words")
                return

            user_data['random_words'] = words
            text = ""
            if call.data.endswith('-1'):
                raw = "Review thy words, lest thou let them be forgotten:"
                text = get_global_translated(raw, settings['language'])
            else:
                raw = "Learn these words, should they be granted unto thee:"
                text = get_global_translated(raw, settings['language'])
            message = get_formatted_words(user_id, text)

            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="HTML",
                reply_markup=learn_menu() if not call.data.endswith('-1') else InlineKeyboardMarkup([[InlineKeyboardButton("Refresh", callback_data="refreshButton-1"), InlineKeyboardButton("Start", callback_data="startButton")]])
            )
        finally:
            release_lock(user_id)
    if call.data.startswith('selection-'):
        if not acquire_lock(user_id):
            bot.answer_callback_query(call.id, text="Processing your previous request")
            return
        try:
            mode = call.data.split('-')[1]
            message, markup = "", None

            if mode == "menu":
                bot.answer_callback_query(call.id, text="Loading (Review)")
                raw = "Which method would you prefer for selecting your recent 45 words?"
                message = get_global_translated(raw, settings['language'])
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Manual", callback_data="selection-manual"),
                        InlineKeyboardButton("Automatic", callback_data="selection-automatic"),
                        InlineKeyboardButton("Learn", callback_data="learnButton")]
                ])
            elif mode == "manual":
                bot.answer_callback_query(call.id, text="Loading (Manual selection)")

                results = get_recent_words(user_id)

                if not results:
                    raw = "No words were found in your recent history, perhaps you haven't learned any yet."
                    message = get_global_translated(raw, settings['language'])
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("Back", callback_data="selection-menu"),
                         InlineKeyboardButton("Learn", callback_data="learnButton")]
                    ])
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text=message,
                        parse_mode="HTML",
                        reply_markup=markup
                    )
                    return

                user_data['recent_words'] = results
                words = [word for word, *_ in results]
                raws = ["Select three words from the following:", "using the exact same format like this:"]
                translated = [get_global_translated(raw, settings['language']) for raw in raws]
                message = f"{translated[0]}\n<pre>{', '.join(words)}</pre>"
                message += f"{translated[1]}\n<pre>{', '.join(random.sample(words, k=3))}</pre>"

                bot.register_next_step_handler(call.message, handle_words)
            elif mode == "automatic":
                results = get_recent_words(user_id)

                if not results:
                    raw = "No words were found in your recent history, perhaps you haven't learned any yet."
                    message = get_global_translated(raw, settings['language'])
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("Back", callback_data="selection-menu"),
                         InlineKeyboardButton("Learn", callback_data="learnButton")]
                    ])
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text=message,
                        parse_mode="HTML",
                        reply_markup=markup
                    )
                    return

                user_data['recent_words'] = results
                user_data['random_words'] = random.sample(results, k=3)
                raw = "Review thy words, lest thou let them be forgotten:"
                text = get_global_translated(raw, settings['language'])
                message = get_formatted_words(user_id, text)
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Refresh", callback_data="refreshButton-1"),
                     InlineKeyboardButton("Start", callback_data="startButton")]
                ])

            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="HTML",
                reply_markup=markup
            )
        finally:
            release_lock(user_id)

    elif call.data.startswith("startButton"):
        if not acquire_lock(user_id):
            bot.answer_callback_query(call.id, text="Processing your previous request")
            return
        try:
            bot.answer_callback_query(call.id, text="Loading the words")
            load_data(user_id)

            result = user_data.get("data", {})

            if not result:
                bot.answer_callback_query(call.id, text="Error loading the words")
                return

            user_data['is_active'] = True

            message, total = get_dictionary(user_id)
            markup = dictionary_menu(total, user_id)

            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="HTML",
                reply_markup=markup
            )

            media = []
            for data in result.values():
                dictionary = data[0]
                audio = dictionary.get("prs", {}).get("audio")
                if audio:
                    response = requests.get(audio)
                    if response.status_code == 200:
                        temporary = NamedTemporaryFile(delete=False, suffix=".mp3")
                        temporary.write(response.content)
                        temporary.close()
                        media.append(telebot.types.InputMediaAudio(media=open(temporary.name, "rb"), title=dictionary.get("hw")))
            sent = None
            if 1 < len(media) < 11: sent = bot.send_media_group(
                chat_id=call.message.chat.id,
                media=media
            )
            elif len(media) == 1:
                sent = bot.send_audio(
                    chat_id=call.message.chat.id,
                    audio=media[0].media,
                    title=media[0].title
                )
            if sent: user_data['last_voice'] = sent
        finally:
            release_lock(user_id)

    elif call.data.startswith('dictionary-'):
        if not acquire_lock(user_id):
            bot.answer_callback_query(call.id, text="Processing your previous request")
            return
        try:
            bot.answer_callback_query(call.id, text="Loading (Dictionary)")

            word_index, page_index = call.data.split('-')[1:]
            word_index, page_index = int(word_index), int(page_index)
            message, total = get_dictionary(user_id, word_index, page_index)
            markup = dictionary_menu(total, user_id, word_index, page_index)

            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="HTML",
                reply_markup=markup
            )
        except Exception: pass
        finally:
            release_lock(user_id)

    elif call.data.startswith('thesaurus-'):
        if not acquire_lock(user_id):
            bot.answer_callback_query(call.id, text="Processing your previous request")
            return
        try:
            bot.answer_callback_query(call.id, text="Loading (Thesaurus)")

            word_index, page_index = call.data.split('-')[1:]
            word_index, page_index = int(word_index), int(page_index)
            message, total = get_thesaurus(user_id, word_index, page_index)
            markup = thesaurus_menu(total, word_index, page_index)

            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="HTML",
                reply_markup=markup
            )
        except Exception: pass
        finally:
            release_lock(user_id)

    elif call.data.startswith('skillCheckButton'):
        if not acquire_lock(user_id):
            bot.answer_callback_query(call.id, text="Processing your previous request")
            return
        try:
            bot.answer_callback_query(call.id, text="Loading (Skill check)")
            mode = int(call.data.split('-')[-1])

            raw = "Ready to solidify your knowledge with the Skill check?"
            translated = get_global_translated(raw, settings['language'])
            message = f"{translated}\n\n— NOTE <i>You won't be able to go back to the Dictionary and Thesaurus as soon as you continue</i>" if  mode == 0 else get_skill_check(user_id)
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton('Not really', callback_data="dictionary-0-0"),
                 InlineKeyboardButton('Yes, I am', callback_data="skillCheckButton-1")]
            ])

            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="HTML",
                reply_markup=markup if mode == 0 else None
            )

            if mode == 1: bot.register_next_step_handler(call.message, handle_answers)
        finally:
            release_lock(user_id)

    elif call.data.startswith('tryAgainButton'):
        if not acquire_lock(user_id):
            bot.answer_callback_query(call.id, text="Processing your previous request")
            return
        try:
            chat_id = call.message.chat.id
            bot.answer_callback_query(call.id, text="Loading (Try again)")
            mode = int(call.data.split('-')[-1])
            message = ""

            bot.delete_message(
                chat_id=chat_id,
                message_id=call.message.message_id
            )
            if mode == 1:
                answers = user_data.get("pending")
                user_data['pending'] = None
                result = check_answers(answers, chat_id, user_id)
                if result: message = result
            else: message = "The Skill check was successfully aborted."

            bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="HTML",
                reply_markup=main_menu() if mode == 0 else None
            )
        finally:
            release_lock(user_id)
bot.infinity_polling(timeout=20, long_polling_timeout=20, skip_pending=True)