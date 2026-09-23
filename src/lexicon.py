from re import compile
from wonderwords import RandomWord
from itertools import chain, islice
from pyinflect import getAllInflections
from concurrent.futures import ThreadPoolExecutor

from src.config import *
from src.database import *

import asyncio
import aiohttp

__all__ = [
    'get_lexicon_session', 'close_lexicon_session', 'get_formatted_profile', 'get_random_words', 'load', 'get_dictionary', 'get_thesaurus', 'get_formatted_random_words'
]

with open('assets/words.txt', "r", encoding="utf-8") as f:
    common_words = set(line.strip() for line in f)

session: aiohttp.ClientSession | None = None
random_word = RandomWord()
primary_executor = ThreadPoolExecutor(max_workers=3)
secondary_executor = ThreadPoolExecutor(max_workers=2)

replacements = {"bc": "", "ldquo": '"', "rdquo": '"', "p_br": "\n", "dx_def": " (-", "/dx_def": ")", "dx": "-", "/dx": "", "dx_ety": "-", "/dx_ety": "", "ma": "-more at ", "/ma": ""}
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
pos = {"noun": ["NNS"], "verb": ["VBD", "VBG", "VBN", "VBZ"], "adjective": ["JJR", "JJS"], "adverb": ["RBR", "RBS"]}

async def get_lexicon_session() -> None:
    global session
    if session is None or session.closed:
        timeout = aiohttp.ClientTimeout(total=5)
        session = aiohttp.ClientSession(timeout=timeout)
async def close_lexicon_session() -> None:
    global session
    if session and not session.closed:
        await session.close()

async def get_formatted_profile(user_id: int) -> str:
    data = await get_profile(user_id)
    text = f"► About\nFirst name: {data.get('first_name')}\nCreated at: {data.get('created_at')}\n\n► Subscription\n"
    end_date = data.get('end_date')
    if end_date:
        text += f"Start date: {data.get('start_date').strftime('%Y-%m-%d %H:%M:%S')}\nEnd date: {end_date.strftime('%Y-%m-%d %H:%M:%S')}"
    else:
        text += "<i>No data available</i>"
    return text

async def get_random_word(pos: str) -> tuple[str, str]:
    loop = asyncio.get_running_loop()
    def fetch_random_word():
        while True:
            word = random_word.word(include_parts_of_speech=[pos])
            if word in common_words: return (word, pos[:-1],)
    return await loop.run_in_executor(primary_executor, fetch_random_word)

async def get_random_words() -> list:
    tasks = [get_random_word(pos) for pos in ['adjectives', 'nouns', 'verbs']]
    return await asyncio.gather(*tasks)

def get_formatted_text(text: str) -> str:
    if not text: return ""
    if isinstance(text, list):
        text = " ".join([item for item in text if isinstance(item, str)])
    if not isinstance(text, str):
        return str(text)
    for key, value in replacements.items():
        text = text.replace(f"{{{key}}}", value)
    for regex_pattern, replacement in regex_patterns:
        text = regex_pattern.sub(replacement, text)
    text = regex_link1.sub(lambda match: f"<i>{match.group(1).split('|')[1] if '|' in match.group(1) else match.group(1)}</i>", text)
    text = regex_link2.sub(lambda match: f"<i>{match.group(1).split('|')[0]}</i>", text)
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

async def fetch_dictionary(word: str) -> dict:
    url = f"{DICTIONARY_API_URL}{word}?key={DICTIONARY_API_KEY}"
    try:
        async with session.get(url) as response:
            response.raise_for_status()
            result = await response.json()
            return {"word": word, "data": result}
    except Exception as e: return {"word": word, "error": str(e)}

async def fetch_thesaurus(word: str) -> dict:
    url = f"{THESAURUS_API_URL}{word}?key={THESAURUS_API_KEY}"
    try:
        async with session.get(url) as response:
            response.raise_for_status()
            result = await response.json()
            return {"word": word, "data": result}
    except Exception as e: return {"word": word, "error": str(e)}

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
        "et": get_formatted_text(et) if et else None,
        "date": get_formatted_text(date) if date else None,
        "ins": insi if insi else [],
        "text": get_formatted_text(" ".join(texti)),
        "vis": [get_formatted_text(vis) for vis in visi]
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
        "text": get_formatted_text(" ".join(texti)),
        "vis": [get_formatted_text(vis) for vis in visi],
        "syns": list(islice(chain.from_iterable(meta.get("syns", [])), 5)),
        "ants": list(islice(chain.from_iterable(meta.get("ants", [])), 5))
    }

    return result

def get_dictionary_dt(entry: dict, vd: str | None = None) -> list | None:
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

def get_thesaurus_dt(entry: dict) -> list | None:
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
                    dt = get_dictionary_dt(entry, vd)
                    if dt is not None:
                        fls[fl].append(get_formatted_dictionary(entry, dt, 'verb', vd))
        else:
            dt = get_dictionary_dt(entry)
            if dt is not None:
                fls[fl].append(get_formatted_dictionary(entry, dt, fl))

    result = {"hw": word, "prs": prs, "vrs": vrs, "def": dict(sorted(fls.items()))}
    return result

def parse_thesaurus(word: str, data: list) -> dict:
    entries = data if isinstance(data, list) else [data]
    result = {}

    if not entries or not isinstance(entries[0], dict):
        result[word] = {"text": "This word does not have a thesaurus at the moment."}
        return result

    for entry in entries:
        hw = entry.get('hwi', {}).get('hw')
        fl = entry.get('fl')

        if not fl or not hw: continue

        dt = get_thesaurus_dt(entry)
        if dt is not None:
            if hw not in result: result[hw] = {}
            result[hw][fl] = get_formatted_thesaurus(entry, dt)
    for key in result:
        result[key] = dict(sorted(result[key].items()))
    return result

async def get_data(word: str) -> tuple[dict | None, dict | None]:
    dictionary_response, thesaurus_response = await asyncio.gather(
        fetch_dictionary(word), fetch_thesaurus(word)
    )

    dictionary_results = dictionary_response.get('data')
    thesaurus_results = thesaurus_response.get('data')

    if dictionary_results and thesaurus_results: 
        parsed_dictionary = parse_dictionary(word, dictionary_results)
        parsed_thesaurus = parse_thesaurus(word, thesaurus_results)
        return parsed_dictionary, parsed_thesaurus
    return None, None

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

async def load(user_id: int) -> None:
    user_data = await get_user_data(user_id)
    random_words = user_data.get("random_words", [])
    if not random_words: return

    cache: dict = await get_word(random_words)

    result = {}
    missing = []

    for word, pos in random_words:
        if (word, pos) in cache: result[word] = cache[(word, pos)]
        else: missing.append((word, pos))

    if missing:
        words = list({word[0] for word in missing})

        tasks = [get_data(word) for word in words]
        results = await asyncio.gather(*tasks)

        apis = {}
        for word, (parsed_dictionary, parsed_thesaurus) in zip(words, results):
            if parsed_dictionary and parsed_thesaurus:
                apis[word] = [parsed_dictionary, parsed_thesaurus]

        for word, pos in missing:
            if word in apis:
                result[word] = apis[word]

    if result:
        user_data['data'] = result
        await save_user_data(user_id, user_data)
        return result

async def get_dictionary(user_id: int, word_index: int = 0, page_index: int = 0) -> tuple[str, int]:
    user_data = await get_user_data(user_id)
    result = user_data.get("data", {})

    words = list(result.keys())
    if not words: return "There are no words you have saved.", 1

    word = words[word_index % len(words)]
    word_data = result.get(word)
    if not word_data: return "This word does not have any data available.", 1

    data = word_data[0]
    definitions = data.get('def', {})
    posi = list(definitions.keys())
    if not posi: return "This word does not have any parts of speech available.", 1

    total = len(posi)
    page_index %= total
    pos = posi[page_index]
    entries = definitions[pos]

    prs = data.get("prs")
    phonetics = prs.get("phonetics")
    text = f"<b>{data.get('hw')}</b> /{phonetics}/"
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
            definition = entry['text'].strip()
            text += f"{definition}:"
            for i, vis in enumerate(entry.get('vis', [])):
                text += f"\n   ▸ <i>{vis}</i>"

    et, date = entries[0].get('et'), entries[0].get('date')
    if et: text += f"\n\n— ETYMOLOGY {et}\n{date}"

    return text, total

async def get_thesaurus(user_id: int, word_index: int = 0, page_index: int = 0) -> tuple[str, int]:
    user_data = await get_user_data(user_id)
    result = user_data.get("data", {})

    words = list(result.keys())
    if not words: return "There are no words you have saved.", 1

    word = words[word_index % len(words)]
    word_data = result.get(word)
    if not word_data: return "This word does not have any data available.", 1

    data = word_data[1]
    hws = list(data.keys())
    if not hws: return "This word does not have any headwords available.", 1

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

            definition = value['text'].strip()
            text += f"{definition}:"
            for i, vis in enumerate(value.get('vis', [])):
                text += f"\n   ▸ <i>{vis}</i>"

        syns = value.get('syns', [])
        if syns: text += f"\n\nSynonyms: <i>{', '.join(syns)}</i>"
        ants = value.get('ants', [])
        if ants: text += f"\nAntonyms: <i>{', '.join(ants)}</i>"

    return text, total

async def get_formatted_random_words(user_data: dict, text: str) -> str:
    random_words = user_data.get("random_words", [])
    formatted = "\n".join([f"<i>{pos:<16}</i>{word}" for word, pos in random_words])
    return f"{text}\n\n<pre>{formatted}</pre>"