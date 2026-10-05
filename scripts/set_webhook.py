import asyncio, os
from dotenv import load_dotenv, find_dotenv
from telebot.async_telebot import AsyncTeleBot
load_dotenv(find_dotenv())

async def main():
    bot = AsyncTeleBot(os.getenv("BOT_TOKEN"))
    await bot.set_webhook(
        url=f'{os.getenv("WEBHOOK_URL")}/webhook',
        secret_token=os.getenv("WEBHOOK_SECRET"),
        drop_pending_updates=True
    )
    print((await bot.get_webhook_info()).url)
    await bot.close_session()
asyncio.run(main())