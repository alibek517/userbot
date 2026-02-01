import os
import asyncio
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message
from supabase import create_client, Client as SupabaseClient

load_dotenv()

# Environment variables
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
DRIVERS_GROUP_ID = int(os.getenv("DRIVERS_GROUP_ID", "-1003784903860"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Supabase client
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# Pyrogram client - UserBot
app = Client(
    "userbot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    phone_number=PHONE_NUMBER
)

# Cache for keywords and groups (refresh every 5 minutes)
keywords_cache = []
groups_cache = []
last_cache_update = 0
CACHE_TTL = 300  # 5 minutes


async def refresh_cache():
    """Kalit so'zlar va guruhlarni yangilash"""
    global keywords_cache, groups_cache, last_cache_update
    
    try:
        # Get keywords
        result = supabase.table("keywords").select("keyword").execute()
        keywords_cache = [k["keyword"].lower() for k in result.data]
        
        # Get watched groups
        result = supabase.table("watched_groups").select("group_id, group_name").execute()
        groups_cache = {g["group_id"]: g["group_name"] for g in result.data}
        
        last_cache_update = asyncio.get_event_loop().time()
        print(f"✅ Cache yangilandi: {len(keywords_cache)} kalit so'z, {len(groups_cache)} guruh")
    except Exception as e:
        print(f"❌ Cache yangilashda xato: {e}")


async def send_to_drivers_group(text: str, message_link: str):
    """Haydovchilar guruhiga xabar yuborish (Bot orqali)"""
    import aiohttp
    
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": DRIVERS_GROUP_ID,
        "text": text,
        "reply_markup": {
            "inline_keyboard": [[{"text": "🔗 Xabarga o'tish", "url": message_link}]]
        }
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status == 200:
                print(f"✅ Xabar yuborildi: {message_link}")
            else:
                print(f"❌ Xabar yuborishda xato: {await resp.text()}")


def get_message_link(message: Message) -> str:
    """Xabarga havola yaratish"""
    chat = message.chat
    msg_id = message.id
    
    if chat.username:
        return f"https://t.me/{chat.username}/{msg_id}"
    else:
        # Private group/channel
        clean_id = str(chat.id).replace("-100", "")
        return f"https://t.me/c/{clean_id}/{msg_id}"


@app.on_message(filters.group | filters.channel)
async def handle_message(client: Client, message: Message):
    """Guruh xabarlarini qayta ishlash"""
    global last_cache_update
    
    # Cache ni yangilash kerakmi?
    current_time = asyncio.get_event_loop().time()
    if current_time - last_cache_update > CACHE_TTL:
        await refresh_cache()
    
    # Bu guruh kuzatilayaptimi?
    chat_id = message.chat.id
    if chat_id not in groups_cache:
        return
    
    # Xabar textini olish
    text = message.text or message.caption or ""
    if not text:
        return
    
    # Kalit so'z bormi?
    lower_text = text.lower()
    matched_keyword = None
    for keyword in keywords_cache:
        if keyword in lower_text:
            matched_keyword = keyword
            break
    
    if not matched_keyword:
        return
    
    # Xabarni haydovchilar guruhiga yuborish
    group_name = groups_cache.get(chat_id, "Guruh")
    user_mention = f"@{message.from_user.username}" if message.from_user and message.from_user.username else (message.from_user.first_name if message.from_user else "Foydalanuvchi")
    message_link = get_message_link(message)
    
    forward_text = f"🔔 Guruhdan topildi!\n📍 {group_name}\n🔑 Kalit so'z: {matched_keyword}\n\n{text}\n\n👤 {user_mention}"
    
    await send_to_drivers_group(forward_text, message_link)
    print(f"📨 Topildi: '{matched_keyword}' - {group_name}")


async def periodic_cache_refresh():
    """Har 5 daqiqada cache ni yangilash"""
    while True:
        await refresh_cache()
        await asyncio.sleep(CACHE_TTL)


async def main():
    """Asosiy funksiya"""
    print("🚀 UserBot ishga tushmoqda...")
    
    # Initial cache load
    await refresh_cache()
    
    # Start periodic refresh task
    asyncio.create_task(periodic_cache_refresh())
    
    # Start the client
    await app.start()
    print(f"✅ UserBot tayyor! {PHONE_NUMBER} bilan ulangan")
    print(f"📡 {len(groups_cache)} ta guruhni kuzatish boshlandi")
    
    # Keep running
    await asyncio.Event().wait()


if __name__ == "__main__":
    app.run(main())
