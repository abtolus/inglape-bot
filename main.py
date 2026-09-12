from dotenv import find_dotenv, load_dotenv
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyinflect import getAllInflections
from threading import Lock
from re import compile, MULTILINE, sub, search, DOTALL
from itertools import chain, islice
from wonderwords import RandomWord
from collections import deque
from openrouter import OpenRouter
from ast import literal_eval

import os
import telebot
import requests
import random
import mysql.connector

pool = mysql.connector.pooling.MySQLConnectionPool(
    pool_name="inglapebot", pool_size=5,
    host=os.getenv('HOST'),
    user=os.getenv('USER'),
    password=os.getenv('PASSWORD'),
    port=os.getenv('PORT'),
    database=os.getenv('DATABASE')
)

USER_DATA = {}
def get_user_data(user_id: int) -> dict:
    if user_id not in USER_DATA:
        USER_DATA[user_id] = {
            "data": {}, "pending": None, "answers": [], "random_words": [], "recent_words": []
        }
    return USER_DATA[user_id]
def clear_user_data(user_id: int) -> None:
    USER_DATA.pop(user_id, None)

random_word = RandomWord()

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

BOT_TOKEN = os.getenv('BOT_TOKEN')
bot = telebot.TeleBot(BOT_TOKEN)

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
DICTIONARY_API_KEY = os.getenv('DICTIONARY_API_KEY')
THESAURUS_API_KEY = os.getenv('THESAURUS_API_KEY')

lock = Lock()

def upsert_user(user) -> None:
    connection = pool.get_connection()
    cursor = connection.cursor()
    query = "INSERT INTO users (id, username, first_name, last_name) VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE username = VALUES(username), first_name = VALUES(first_name), last_name = VALUES(last_name)"
    values = (user.id, user.username, user.first_name, user.last_name)
    cursor.execute(query, values)
    connection.commit()
    cursor.close()
    connection.close()
def upsert_word(word: str, part_of_speech: str) -> int:
    connection = pool.get_connection()
    cursor = connection.cursor()
    query = "INSERT INTO words (word, part_of_speech) VALUES (%s, %s) ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)"
    values = (word, part_of_speech)
    cursor.execute(query, values)
    connection.commit()
    word_id = cursor.lastrowid
    cursor.close()
    connection.close()
    return word_id
def insert_log(user_id: int, word_id: int) -> None:
    connection = pool.get_connection()
    cursor = connection.cursor()
    query = "INSERT INTO logs (user_id, word_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE created_at = CURRENT_TIMESTAMP"
    values = (user_id, word_id)
    cursor.execute(query, values)
    connection.commit()
    cursor.close()
    connection.close()

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user = message.from_user
    user_data = get_user_data(user.id)
    if user_data.get("data", {}): return
    clear_user_data(user.id)

    bot.reply_to(message, "Well met, and may grace sustain thy spirit. Wilt thou learn anew, or weigh what thou hast learned?", reply_markup=main_menu())
    upsert_user(user)

regex_words = compile(r"^[a-zA-Z]+,\s[a-zA-Z]+,\s[a-zA-Z]+$")

def handle_words(message) -> None:
    user_id = message.from_user.id
    user_data = get_user_data(user_id)
    recent_words = user_data.get("recent_words", [])

    words = [word for word, *_ in recent_words]

    if not bool(regex_words.fullmatch(message.text.strip())):
        text = f"Please select your recent words in the exact same format as the following:\n<pre>{', '.join(random.sample(words, k=3))}</pre>"
        send_words_format(message, text)
        return

    input_words = [word.strip().lower() for word in message.text.split(',')]
    if len(input_words) != len(set(input_words)):
        text = f"The selected words from your recent history must be unique:\n<pre>{', '.join(words)}</pre>"
        send_words_format(message, text)
        return

    matched = [item for item in recent_words if item[0].lower() in input_words]
    found = {item[0].lower() for item in matched}
    missing = [word for word in input_words if word not in found]

    if not missing:
        if not isinstance(matched, list):
            return

        user_data['random_words'] = matched
        text_ = "Review thy words, lest thou let them be forgotten:"
        text = get_formatted_words(user_id, text_)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Refresh", callback_data="selection-manual"),
             InlineKeyboardButton("Start", callback_data="startButton")]
        ])

        bot.send_message(
            chat_id=message.chat.id,
            text=text,
            parse_mode="HTML",
            reply_markup=markup
        )
    else:
        text = f"Not all your selected words were found in your recent history:\n<pre>{', '.join(words)}</pre>"
        send_words_format(message, text)
        return
def send_words_format(message, text: str) -> None:
    call_message = bot.send_message(
        chat_id=message.chat.id,
        text=text,
        parse_mode="HTML"
    )
    bot.register_next_step_handler(call_message, handle_words)

regex_answers = compile(r"^(?:\d+\.\s+[\w\s.,!?()\'\"-]+(?:\n|$))+(?:\n(?:\d+\.\s+[\w\s.,!?()\'\"-]+(?:\n|$))+)*$", MULTILINE)

def handle_answers(message) -> None:
    user_id = message.from_user.id
    user_data = get_user_data(user_id)
    ans = user_data.get("answers", [])
    random_words = user_data.get("random_words", [])

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

        user_id = message.from_user.id
        for word, part_of_speech in random_words:
            insert_log(user_id, upsert_word(word, part_of_speech))

        clear_user_data(user_id)

        bot.send_message(
            chat_id=message.chat.id,
            text="The journey of learning hath borne fruit. Wilt thou learn anew, or weigh what thou hast learned?",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
def send_answers_format(message) -> None:
    user_id = message.from_user.id

    text = "Please provide your answers in the exact same format as the following:\n\n"
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
            if e == "Provider returned error":
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

def get_random_words() -> list:
    return [
        (random_word.word(include_parts_of_speech=[pos]), pos[:-1],) for pos in ['adjectives', 'nouns', 'verbs']
    ]

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

def fetch_dictionary(user_id: int, index: int = 0) -> dict:
    user_data = get_user_data(user_id)
    random_words = user_data.get("random_words", [])

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

    def get_formatted(entry: dict, dt: list, fl: str, vd: str | None = None) -> dict:
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

    def get_dt(entry: dict, vd: str | None = None) -> list | None:
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
    word = random_words[index][0]
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

def get_dictionary(user_id: int, word_index: int = 0, page_index: int = 0):
    user_data = get_user_data(user_id)
    data_ = user_data.get("data", {})

    word = list(data_.keys())[word_index]
    data = data_[word][0]
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

def fetch_thesaurus(user_id: int, index: int = 0) -> dict:
    user_data = get_user_data(user_id)
    random_words = user_data.get("random_words", [])

    def get_dt(entry: dict) -> list | None:
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
    def get_formatted(entry: dict, dt: list) -> dict:
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
    word = random_words[index][0]
    try:
        response = requests.get(url + word + "?key=" + THESAURUS_API_KEY)
        response.raise_for_status()
        data = response.json()

        entries = data if isinstance(data, list) else [data]
        result = {}

        if not isinstance(entries[0], dict):
            result[word] = {"text": "This word does not have a thesaurus at the moment."}
            return result

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

def get_thesaurus(user_id: int, word_index: int = 0, page_index: int = 0):
    user_data = get_user_data(user_id)
    data_ = user_data.get("data", {})

    word = list(data_.keys())[word_index]
    data = data_[word][1]
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
            text += f"{value['text'].strip()}:"
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

    data = fetch_skill_check(user_id)
    sections = deque(random.sample(data, k=5) if len(data) >= 5 else random.sample(data, k=len(data)) for _ in range(4))

    text = f"Provide a synonym for each of the following:"
    temporary = []
    for i, entry in enumerate(sections.popleft()):
        text += f"\n<b>{i+1}</b> <i>{entry.get('hw')}</i>"
        temporary.append(entry.get('syns'))
    user_data['answers'].append(temporary)
    text += "\n\n"

    text += f"Identify the term defined by each of the following:"
    temporary = []
    for i, entry in enumerate(sections.popleft()):
        text += f"\n<b>{i+1}</b> <i>{entry.get('text').strip()}</i>"
        temporary.append(entry.get('hw'))
    user_data['answers'].append(temporary)
    text += "\n\n"

    text += f"Give a term matching each pair of synonyms in the following:"
    temporary = []
    for i, entry in enumerate(sections.popleft()):
        text += f"\n<b>{i+1}</b> <i>{', '.join(entry.get('syns', []))}</i>"
        temporary.append(entry.get('hw'))
    user_data['answers'].append(temporary)
    text += "\n\n"

    text += f"Write sentences using each of the following:"
    temporary = []
    for i, entry in enumerate(sections.popleft()):
        text += f"\n<b>{i+1}</b> <i>{entry.get('hw')}</i>"
        temporary.append(entry.get('hw'))
    user_data['answers'].append(temporary)

    return text

def load_data(user_id: int) -> None:
    user_data = get_user_data(user_id)
    data_ = {}
    random_words = user_data.get("random_words", [])

    for i, (word, *_) in enumerate(random_words):
        dictionary = fetch_dictionary(user_id, i)
        thesaurus = fetch_thesaurus(user_id, i)
        if isinstance(dictionary, dict) and isinstance(thesaurus, dict):
            data_[word] = [dictionary, thesaurus]
    if data_: user_data['data'] = data_

def get_formatted_words(user_id: int, text) -> str:
    user_data = get_user_data(user_id)
    random_words = user_data.get("random_words", [])

    formatted = "\n".join([f"<i>{pos:<16}</i>{word}" for word, pos in random_words])
    return f"{text}\n\n<pre>{formatted}</pre>"

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    user_id = call.from_user.id
    user_data = get_user_data(user_id)

    if call.data in ["learnButton", "refreshButton-0", "refreshButton-1"]:
        if not lock.acquire(blocking=False): return
        try:
            bot.answer_callback_query(call.id, text="Fetching the words")
            recent_words = user_data.get("recent_words", [])
            words = random.sample(recent_words, k=3) if call.data.endswith('-1') else get_random_words()

            if not isinstance(words, list):
                bot.answer_callback_query(call.id, text="Error fetching the words")
                return

            user_data['random_words'] = words
            text = "Review thy words, lest thou let them be forgotten:" if call.data.endswith('-1') else "Learn these words, should they be granted unto thee:"
            message = get_formatted_words(user_id, text)

            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=message,
                parse_mode="HTML",
                reply_markup=learn_menu() if not call.data.endswith('-1') else InlineKeyboardMarkup([[InlineKeyboardButton("Refresh", callback_data="refreshButton-1"), InlineKeyboardButton("Start", callback_data="startButton")]])
            )
        finally:
            lock.release()
    if call.data.startswith('selection-'):
        if not lock.acquire(blocking=False): return
        try:
            mode = call.data.split('-')[1]
            message, markup = "", None

            if mode == "menu":
                bot.answer_callback_query(call.id, text="Loading (Review)")
                message = "Which method would you prefer for selecting your recent 15 words?"
                message += "\n\n— NOTE <i>With premium, have access to Manual selection and up to 45 words</i>"
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Manual", callback_data="selection-manual"),
                        InlineKeyboardButton("Automatic", callback_data="selection-automatic"),
                        InlineKeyboardButton("Learn", callback_data="learnButton")]
                ])
            elif mode == "manual":
                bot.answer_callback_query(call.id, text="Loading (Manual selection)")

                connection = pool.get_connection()
                cursor = connection.cursor()
                query = "SELECT w.word, w.part_of_speech FROM logs l JOIN words w ON l.word_id = w.id WHERE l.user_id = %s ORDER BY l.created_at DESC LIMIT 15"
                cursor.execute(query, (user_id,))
                results = cursor.fetchall()
                cursor.close()
                connection.close()

                if not results:
                    message = "No words were found in your recent history, perhaps you haven't learned any yet."
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
                message = f"Select three words from the following:\n<pre>{', '.join(words)}</pre>"
                message += f"using the exact same format like this:\n<pre>{', '.join(random.sample(words, k=3))}</pre>"

                bot.register_next_step_handler(call.message, handle_words)
            elif mode == "automatic":
                connection = pool.get_connection()
                cursor = connection.cursor()
                query = "SELECT w.word, w.part_of_speech FROM logs l JOIN words w ON l.word_id = w.id WHERE l.user_id = %s ORDER BY l.created_at DESC LIMIT 15"
                cursor.execute(query, (user_id,))
                results = cursor.fetchall()
                cursor.close()
                connection.close()

                if not results:
                    message = "No words were found in your recent history, perhaps you haven't learned any yet."
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
                text = "Review thy words, lest thou let them be forgotten:"
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
            lock.release()
    elif call.data == "startButton":
        if not lock.acquire(blocking=False): return
        try:
            bot.answer_callback_query(call.id, text="Loading the words")
            load_data(user_id)

            data_ = user_data.get("data", {})

            if not data_:
                bot.answer_callback_query(call.id, text="Error loading the words")
                return

            message, total = get_dictionary(user_id)
            markup = dictionary_menu(total, user_id)

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
            message, total = get_dictionary(user_id, word_index, page_index)
            markup = dictionary_menu(total, user_id, word_index, page_index)

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
            message, total = get_thesaurus(user_id, word_index, page_index)
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
    elif call.data.startswith('skillCheckButton'):
        if not lock.acquire(blocking=False): return
        try:
            bot.answer_callback_query(call.id, text="Loading (Skill check)")
            mode = int(call.data.split('-')[-1])

            message = "Ready to solidify your knowledge with the Skill check?\n\n— NOTE <i>You won't be able to go back to the Dictionary and Thesaurus as soon as you continue</i>" if  mode == 0 else get_skill_check(user_id)
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
            lock.release()
    elif call.data.startswith('tryAgainButton'):
        if not lock.acquire(blocking=False): return
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
            lock.release()
bot.infinity_polling(skip_pending=True)