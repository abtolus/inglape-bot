from telebot.types import User
from redis.asyncio import Redis
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

from src.config import *

import json
import gzip
import asyncio
import pymysql
import aiomysql
import functools

__all__ = [
    'pool', 'initialize_pool', 'get_profile', 'get_word', 'get_user_media', 'save_user_media', 'get_user_data', 'save_user_data', 'clear_user_data', 'get_recent_words', 'upsert_settings', 'upsert_user', 'upsert_word', 'upsert_subscription', 'upsert_last_active', 'insert_log', 'insert_payment', 'has_subscription', 'clear_inactive', 'redis'
]

redis = Redis(
    host=VALKEY_HOST,
    password=VALKEY_PASSWORD,
    port=VALKEY_PORT,
    db=0,
    ssl=True,
    ssl_cert_reqs=None,
    decode_responses=True
)

pool = None
expiration: int = 7200
user_media_keys = ('last_message', 'last_voice',)

def get_new_user_data(settings: dict = {}) -> dict:
    return {
        "data": {},
        "answers": [],
        "random_words": [],
        "recent_words": [],
        "is_active": False,
        "settings": settings,
        "pending": None,
        "handle_state": None,
        "payment_state": None
    }

def autoconnect(maximum: int = 3):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(maximum):
                try: return await func(*args, **kwargs)
                except pymysql.err.OperationalError as e:
                    if e.args[0] in (2013, 2006) and attempt < maximum - 1: continue
                    raise
        return wrapper
    return decorator

async def initialize_pool():
    global pool
    pool = await aiomysql.create_pool(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        port=MYSQL_PORT,
        db=MYSQL_DATABASE,
        minsize=6,
        maxsize=18,
        autocommit=True
    )

@asynccontextmanager
async def get_pool_connection():
    async with pool.acquire() as connection:
        yield connection

@autoconnect()
async def get_profile(user_id: int) -> dict:
    query = "SELECT first_name, created_at, expired_at FROM users WHERE id = %s"
    async with get_pool_connection() as connection:
        async with connection.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute(query, (user_id,))
            result = await cursor.fetchone()

    if not result:
        return {"first_name": "Unknown", "created_at": "N/A", "start_date": None, "end_date": None}
    utc630 = timezone(timedelta(hours=6, minutes=30))
    expired_at = result.pop("expired_at", None)

    created_at = result.get("created_at")
    created_at = created_at.replace(tzinfo=timezone.utc) if created_at.tzinfo is None else created_at
    created_at = created_at.astimezone(utc630)
    result['created_at'] = created_at.strftime("%Y-%m-%d %H:%M:%S")

    if expired_at:
        end_date = expired_at.replace(tzinfo=timezone.utc) if expired_at.tzinfo is None else expired_at
        end_date = end_date.astimezone(utc630)
        start_date = end_date - timedelta(days=30)
        result['start_date'], result['end_date'] = start_date, end_date
    else: result['start_date'], result['end_date'] = None, None

    return result

@autoconnect()
async def get_word(random_words: list[tuple[str, str]]) -> dict:
    if not random_words: return {}
    condition = " OR ".join(["(word = %s AND part_of_speech = %s)"] * len(random_words))
    values = [subitem for item in random_words for subitem in item]
    query = f"SELECT word, part_of_speech, data FROM words WHERE {condition}"
    result = {}

    async with get_pool_connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(query, values)
            results = await cursor.fetchall()
            for word, part_of_speech, compressed in results:
                data = gzip.decompress(compressed).decode("utf-8")
                result[(word, part_of_speech)] = json.loads(data)
    return result

@autoconnect()
async def get_settings(user_id: int) -> dict:
    async with get_pool_connection() as connection:
        async with connection.cursor() as cursor:
            query = "SELECT settings FROM users WHERE id = %s"
            await cursor.execute(query, (user_id,))
            result = await cursor.fetchone()
            if result and result[0]: return json.loads(result[0]) if isinstance(result[0], str) else result[0]
            return {
                "language": "en-US"
            }

async def get_user_media(user_id: int) -> dict:
    key = f"user:{user_id}:media"
    raw_data = await redis.get(key)
    if raw_data:
        try: user_media = json.loads(raw_data)
        except ValueError: user_media = None
        if isinstance(user_media, dict):
            for name in user_media_keys: user_media.setdefault(name, None)
            return user_media
    return {
        name: None for name in user_media_keys
    }

async def save_user_media(user_id: int, user_media: dict) -> None:
    key = f"user:{user_id}:media"
    await redis.set(key, json.dumps(user_media))

async def get_user_data(user_id: int) -> dict:
    key = f"user:{user_id}"
    raw_data = await redis.getex(key, ex=expiration)
    now = datetime.now(timezone.utc).isoformat()

    if raw_data:
        try: user_data = json.loads(raw_data)
        except ValueError: user_data = None
        if isinstance(user_data, dict):
            changed = False
            for name, value in get_new_user_data().items():
                if name not in user_data:
                    user_data[name] = value
                    changed = True
            if not user_data.get("settings"):
                user_data['settings'] = await get_settings(user_id)
                changed = True
            if changed: await save_user_data(user_id, user_data)
            return user_data

    user_data = get_new_user_data(await get_settings(user_id))
    created = await redis.set(key, json.dumps(user_data), ex=expiration, nx=True)
    if created: return user_data

    raw_data = await redis.getex(key, ex=expiration)
    return json.loads(raw_data) if raw_data else user_data

async def save_user_data(user_id: int, user_data: dict) -> None:
    key = f"user:{user_id}"
    await redis.set(key, json.dumps(user_data), ex=expiration)

async def clear_user_data(user_id: int) -> None:
    await redis.delete(f"user:{user_id}")

@autoconnect()
async def get_recent_words(user_id: int, limit: int = 90) -> list:
    query = "SELECT w.word, w.part_of_speech FROM logs l JOIN words w ON l.word_id = w.id WHERE l.user_id = %s ORDER BY l.created_at DESC LIMIT %s"
    async with get_pool_connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(query, (user_id, limit,))
            return await cursor.fetchall()

@autoconnect()
async def upsert_settings(user_id: int, new_value: str, key: str = 'language') -> None:
    async with get_pool_connection() as connection:
        async with connection.cursor() as cursor:
            query = f"UPDATE users SET settings = JSON_SET(settings, '$.{key}', %s) WHERE id = %s"
            values = (new_value, user_id,)
            await cursor.execute(query, values)

@autoconnect()
async def upsert_user(user: User) -> None:
    query = "INSERT INTO users (id, username, first_name, last_name, settings) VALUES (%s, %s, %s, %s, %s) AS new_user ON DUPLICATE KEY UPDATE username = new_user.username, first_name = new_user.first_name, last_name = new_user.last_name"
    settings = {"language": "en-US"}
    values = (user.id, user.username, user.first_name, user.last_name, json.dumps(settings),)
    async with get_pool_connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(query, values)

@autoconnect()
async def upsert_word(word: str, part_of_speech: str, data: list) -> int:
    string = json.dumps(data, ensure_ascii=False)
    compressed: bytes = gzip.compress(string.encode("utf-8"), compresslevel=9)
    compressed_hex: str = compressed.hex()
    query = "INSERT INTO words (word, part_of_speech, data) VALUES (%s, %s, UNHEX(%s)) ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)"
    async with get_pool_connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(query, (word, part_of_speech, compressed_hex,))
            return cursor.lastrowid

@autoconnect()
async def upsert_subscription(user_id: int, days: int = 30) -> None:
    query = f"""UPDATE users
    SET expired_at = CASE
        WHEN expired_at IS NOT NULL AND expired_at > NOW()
        THEN DATE_ADD(expired_at, INTERVAL {int(days)} DAY)
        ELSE DATE_ADD(NOW(), INTERVAL {int(days)} DAY)
    END
    WHERE id = %s
    """
    async with get_pool_connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(query, (user_id,))

@autoconnect()
async def upsert_last_active(user_id: int) -> None:
    query = "UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE id = %s"
    async with get_pool_connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(query, (user_id,))

@autoconnect()
async def insert_log(user_id: int, word_id: int) -> None:
    query = "INSERT INTO logs (user_id, word_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE created_at = CURRENT_TIMESTAMP"
    cleanup = "DELETE FROM logs WHERE user_id = %s AND id NOT IN (SELECT id FROM (SELECT id FROM logs WHERE user_id = %s ORDER BY created_at DESC LIMIT 100) AS subquery)"
    async with get_pool_connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(query, (user_id, word_id,))
            await cursor.execute(cleanup, (user_id, user_id,))

@autoconnect()
async def insert_payment(user_id: int, transaction_number: str, amount: int = KBZPAY_AMOUNT) -> None:
    query = "INSERT INTO payments (user_id, transaction_number, amount) VALUES (%s, %s, %s)"
    async with get_pool_connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(query, (user_id, transaction_number, amount,))

@autoconnect()
async def has_subscription(user_id: int) -> bool:
    query = "SELECT created_at, expired_at FROM users WHERE id = %s"
    async with get_pool_connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(query, (user_id,))
            user = await cursor.fetchone()
    if not user: return False
    def utc(dt):
        return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt
    created_at, expired_at = utc(user[0]), utc(user[1])
    now = datetime.now(timezone.utc)
    return bool(expired_at and expired_at > now) or bool(created_at and now - created_at <= timedelta(days=14))

async def clear_inactive() -> None:
    while True:
        try:
            condition = "last_active < NOW() - INTERVAL 30 DAY AND (expired_at IS NULL OR expired_at < NOW())"
            async with get_pool_connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(f"SELECT id FROM users WHERE {condition}")
                    stale = [row[0] for row in await cursor.fetchall()]
                    await cursor.execute("DELETE l FROM logs l INNER JOIN users u ON l.user_id = u.id WHERE u.last_active < NOW() - INTERVAL 30 DAY AND (u.expired_at IS NULL OR u.expired_at < NOW())")
                    await cursor.execute(f"DELETE FROM users WHERE {condition}")
            for i in range(0, len(stale), 500):
                keys = [f"user:{user_id}{suffix}" for user_id in stale[i:i + 500] for suffix in ("", ":media",)]
                await redis.delete(*keys)
            await asyncio.sleep(86400)
        except Exception as e:
            print(e)
            await asyncio.sleep(8)