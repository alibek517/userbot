import os
import sys
import asyncio
import aiohttp
import time
import html
import re
from typing import Optional, List, Tuple, Dict
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ChatType, MessageEntityType
from pyrogram.errors import FloodWait
from supabase import create_client, Client as SupabaseClient

load_dotenv()

# ===================== ENV =====================
API_ID = int(os.getenv("TELEGRAM_API_ID", "0") or "0")
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
DRIVERS_GROUP_ID = int(os.getenv("DRIVERS_GROUP_ID", "-1003784903860"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

PHONE_NUMBERS_RAW = os.getenv("PHONE_NUMBER", "")
PHONE_NUMBERS_ENV_FALLBACK = [
    p.strip().strip('"').strip("'") for p in PHONE_NUMBERS_RAW.split(",") if p.strip()
]

# ===================== GLOBALS =====================
supabase: SupabaseClient = None

keywords_cache: List[str] = []
keywords_map: Dict[str, int] = {}
last_cache_update = 0
CACHE_TTL = 300  # 5 min

watched_groups_cache = set()
account_groups_cache: Dict[str, set] = {}
groups_cache_loaded = False

account_stats = {}          # phone -> {"groups_count": N, "active_count": N}
running_clients = {}        # phone -> asyncio.Task
ALL_PHONES = []             # full phones list for statistics

# ===== DEDUPE (MUHIM!) =====
# Bir xil guruhda 2-3 akkaunt turganda bitta xabar 2-3 marta yuborilib Bot API 429 bo'lib qoladi.
# Shuni oldini olish uchun: (chat_id, msg_id) bo'yicha 1 marta forward qilamiz.
forwarded_cache: Dict[Tuple[int, int], float] = {}
FORWARD_TTL = 120  # 2 minut
forward_lock = asyncio.Lock()


# ===================== HELPERS =====================
def normalize_chat_id(chat_id: int) -> int:
    try:
        return int(chat_id)
    except Exception:
        return chat_id


def _normalize_phone(p: str) -> str:
    return (p or "").strip().replace(" ", "")


def uniq_keep_order(items):
    seen = set()
    out = []
    for x in items:
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def init_supabase() -> bool:
    global supabase
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase ulandi")
        return True
    except Exception as e:
        print(f"❌ Supabase ulanishda xato: {e}")
        return False


# ===================== LINK / TEXT CLEAN =====================
URL_RE = re.compile(r"(https?://\S+|t\.me/\S+|telegram\.me/\S+)", re.IGNORECASE)


def strip_links(text: str) -> str:
    if not text:
        return ""
    text = URL_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def extract_text_and_urls(message: Message):
    raw = message.text or message.caption or ""
    if not raw:
        return "", []

    urls = []
    ents = None
    if message.text and message.entities:
        ents = message.entities
    elif message.caption and message.caption_entities:
        ents = message.caption_entities

    if ents:
        for ent in ents:
            try:
                if ent.type == MessageEntityType.URL:
                    urls.append(raw[ent.offset: ent.offset + ent.length])
                elif ent.type == MessageEntityType.TEXT_LINK and getattr(ent, "url", None):
                    urls.append(ent.url)
            except Exception:
                pass

    urls.extend(URL_RE.findall(raw))
    urls = uniq_keep_order([u.strip() for u in urls if u and u.strip()])

    cleaned = strip_links(raw)
    return cleaned, urls


# ===================== TELEGRAM MESSAGE/GROUP LINK =====================
def get_message_link(message: Message) -> str:
    chat = message.chat
    msg_id = message.id
    if chat.username:
        return f"https://t.me/{chat.username}/{msg_id}"
    clean_id = str(chat.id).replace("-100", "")
    return f"https://t.me/c/{clean_id}/{msg_id}"


def get_chat_link(message: Message) -> str:
    """
    Guruhni ochib beradigan link.
    Agar public username bo'lsa: https://t.me/username
    Aks holda private supergroup: https://t.me/c/<id> (message bo'lmasdan ochilmaydi)
    Shuning uchun private holatda message linkdan "chat"ni ochish ham shu link bo'ladi.
    """
    chat = message.chat
    if chat.username:
        return f"https://t.me/{chat.username}"
    # private supergroup/channel uchun "c/<id>" faqat message bilan ishlaydi,
    # shuning uchun fallback message linkdan foydalanamiz
    return get_message_link(message)


# ===================== SEND TO DRIVERS GROUP =====================
async def send_to_drivers_group(
    text: str,
    group_link: str,
    message_link: str,
    extra_urls: Optional[List[str]] = None
):
    """
    Tugmalar:
      - Guruhga o'tish
      - Xabarga o'tish
      - (bo'lsa) Link 1..3

    Bot API 429 (Too Many Requests) bo'lsa retry qiladi.
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    keyboard = [[
        {"text": "👥 Guruhga o'tish", "url": group_link},
        {"text": "🔗 Xabarga o'tish", "url": message_link},
    ]]

    extra_urls = uniq_keep_order(extra_urls or [])[:3]
    for i, u in enumerate(extra_urls, 1):
        keyboard.append([{"text": f"🔗 Link {i}", "url": u}])

    payload = {
        "chat_id": DRIVERS_GROUP_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": keyboard},
    }

    try:
        async with aiohttp.ClientSession() as session:
            for attempt in range(4):  # 0..3
                async with session.post(url, json=payload, timeout=30) as resp:
                    if resp.status == 200:
                        print(f"✅ Xabar yuborildi: {message_link}")
                        return

                    # 429 -> retry_after kutish
                    if resp.status == 429:
                        retry_after = 3
                        try:
                            j = await resp.json()
                            retry_after = int(j.get("parameters", {}).get("retry_after", retry_after))
                        except Exception:
                            pass
                        print(f"⏳ Bot API Flood (429). {retry_after}s kutyapman... attempt={attempt + 1}")
                        await asyncio.sleep(retry_after + 1)
                        continue

                    body = await resp.text()
                    print(f"❌ Xabar yuborishda xato ({resp.status}): {body}")
                    return
    except Exception as e:
        print(f"❌ Xabar yuborishda xato: {e}")


# ===================== STATISTICS =====================
def print_statistics():
    global account_stats, watched_groups_cache, ALL_PHONES

    print("\n" + "=" * 60)
    print("📊 USERBOT STATISTIKASI")
    print("=" * 60)

    total_groups_all = 0
    total_active_all = 0

    phones_list = ALL_PHONES or list(running_clients.keys()) or PHONE_NUMBERS_ENV_FALLBACK

    print(f"\n📱 AKKAUNTLAR ({len(phones_list)} ta):")
    print("-" * 40)

    for phone in phones_list:
        stats = account_stats.get(phone, {})
        total = int(stats.get("groups_count", 0) or 0)
        active = int(stats.get("active_count", 0) or 0)
        total_groups_all += total
        total_active_all += active
        print(f"  {phone}: {total} guruh, {active} ta faol kuzatilmoqda")

    print("-" * 40)
    print(f"  JAMI: {total_groups_all} guruh, {total_active_all} ta faol kuzatilmoqda")
    print(f"\n🚫 Bloklangan: Faqat DRIVERS_GROUP_ID ({DRIVERS_GROUP_ID})")
    print(f"💾 Keshda: {len(watched_groups_cache)} ta guruh")
    print("=" * 60 + "\n")


# ===================== SUPABASE CACHE LOAD =====================
async def load_groups_cache():
    global watched_groups_cache, account_groups_cache, groups_cache_loaded, supabase

    if groups_cache_loaded or not supabase:
        return

    try:
        result = supabase.table("watched_groups").select("group_id").execute()
        watched_groups_cache = {row["group_id"] for row in (result.data or [])}
        print(f"✅ Kesh yuklandi: {len(watched_groups_cache)} ta guruh bazada mavjud")

        acc_result = supabase.table("account_groups").select("phone_number, group_id").execute()
        for row in (acc_result.data or []):
            phone = row.get("phone_number")
            gid = row.get("group_id")
            if phone and gid:
                account_groups_cache.setdefault(phone, set()).add(gid)

        groups_cache_loaded = True
    except Exception as e:
        print(f"⚠️ Kesh yuklashda xato: {e}")


# ===================== SUPABASE PHONES =====================
def fetch_phone_numbers_from_db() -> list:
    global supabase
    if not supabase:
        return []

    try:
        res = supabase.table("userbot_accounts").select("phone_number,status").execute()
        phones = []
        for row in (res.data or []):
            status = (row.get("status") or "").lower()
            phone = _normalize_phone(row.get("phone_number"))
            if not phone:
                continue
            if status in ["pending", "active", "error", "connecting"]:
                phones.append(phone)
        return uniq_keep_order(phones)
    except Exception as e:
        print(f"⚠️ Bazadan raqamlarni olishda xato: {e}")
        return []


async def ensure_accounts_seeded_from_env():
    global supabase
    if not supabase or not PHONE_NUMBERS_ENV_FALLBACK:
        return

    try:
        existing = supabase.table("userbot_accounts").select("phone_number").execute()
        existing_phones = {_normalize_phone(row.get("phone_number", "")) for row in (existing.data or [])}

        for phone in PHONE_NUMBERS_ENV_FALLBACK:
            phone = _normalize_phone(phone)
            if not phone or phone in existing_phones:
                continue
            try:
                supabase.table("userbot_accounts").insert({
                    "phone_number": phone,
                    "status": "pending",
                    "two_fa_required": False,
                }).execute()
                print(f"✅ Yangi raqam qo'shildi: {phone}")
            except Exception as e:
                if "duplicate" not in str(e).lower():
                    print(f"⚠️ Raqam qo'shishda xato: {phone} - {e}")
    except Exception as e:
        print(f"⚠️ .env seed'da xato: {e}")


def update_account_status(phone: str, status: str):
    global supabase
    if not supabase:
        return
    try:
        supabase.table("userbot_accounts").update(
            {"status": status, "updated_at": "now()"}
        ).eq("phone_number", phone).execute()
        print(f"📊 Status yangilandi: {phone} -> {status}")
    except Exception as e:
        print(f"⚠️ Status yangilashda xato: {e}")


# ===================== GROUP SYNC =====================
async def sync_account_groups(phone: str, groups: list):
    global supabase, account_groups_cache
    if not supabase:
        return

    try:
        existing_ids = account_groups_cache.get(phone, set())
        new_groups = [g for g in groups if g["group_id"] not in existing_ids]
        if not new_groups:
            return

        for group in new_groups:
            try:
                supabase.table("account_groups").insert({
                    "phone_number": phone,
                    "group_id": group["group_id"],
                    "group_name": group["group_name"],
                }).execute()
                account_groups_cache.setdefault(phone, set()).add(group["group_id"])
            except Exception:
                pass

        print(f"📝 [{phone}] {len(new_groups)} ta yangi guruh qo'shildi")
    except Exception as e:
        print(f"⚠️ Guruhlarni saqlashda xato: {e}")


async def sync_all_groups(client: Client, phone: str) -> list:
    global supabase, account_stats, watched_groups_cache

    if not supabase:
        return []

    try:
        groups_found = []

        try:
            async for dialog in client.get_dialogs():
                chat = dialog.chat
                if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                    groups_found.append({
                        "group_id": chat.id,
                        "group_name": chat.title or f"Guruh {chat.id}"
                    })
        except FloodWait as fw:
            wait_s = int(getattr(fw, "value", 0) or 0)
            print(f'[{phone}] Waiting for {wait_s} seconds before continuing (FloodWait GetDialogs)')
            await asyncio.sleep(wait_s + 1)
            async for dialog in client.get_dialogs():
                chat = dialog.chat
                if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                    groups_found.append({
                        "group_id": chat.id,
                        "group_name": chat.title or f"Guruh {chat.id}"
                    })

        await sync_account_groups(phone, groups_found)

        new_groups = [g for g in groups_found if g["group_id"] not in watched_groups_cache]
        for g in new_groups:
            try:
                is_blocked = normalize_chat_id(g["group_id"]) == normalize_chat_id(DRIVERS_GROUP_ID)
                supabase.table("watched_groups").insert({
                    "group_id": g["group_id"],
                    "group_name": g["group_name"],
                    "is_blocked": is_blocked
                }).execute()
                watched_groups_cache.add(g["group_id"])
            except Exception:
                pass

        active_groups = [
            g for g in groups_found
            if normalize_chat_id(g["group_id"]) != normalize_chat_id(DRIVERS_GROUP_ID)
        ]

        account_stats[phone] = {"groups_count": len(groups_found), "active_count": len(active_groups)}
        return groups_found

    except Exception as e:
        print(f"❌ [{phone}] Guruhlarni sinxronlashda xato: {e}")
        return []


# ===================== KEYWORDS =====================
async def refresh_keywords():
    global keywords_cache, keywords_map, last_cache_update, supabase
    if not supabase:
        return
    try:
        result = supabase.table("keywords").select("id, keyword").execute()
        keywords_cache = [k["keyword"].lower() for k in (result.data or []) if k.get("keyword")]
        keywords_map = {k["keyword"].lower(): k["id"] for k in (result.data or []) if k.get("keyword")}
        last_cache_update = time.time()
        print(f"✅ Kalit so'zlar yangilandi: {len(keywords_cache)} ta")
    except Exception as e:
        print(f"❌ Kalit so'zlar yangilashda xato: {e}")


async def periodic_keywords_refresh():
    while True:
        await asyncio.sleep(CACHE_TTL)
        await refresh_keywords()


# ===================== HIT LOG =====================
async def save_keyword_hit(keyword: str, group_id: int, group_name: str, phone: str, message_text: str):
    global supabase, keywords_map
    if not supabase:
        return
    try:
        keyword_id = keywords_map.get(keyword.lower())
        preview = (message_text or "")[:200]
        supabase.table("keyword_hits").insert({
            "keyword_id": keyword_id,
            "group_id": group_id,
            "group_name": group_name,
            "phone_number": phone,
            "message_preview": preview,
        }).execute()
    except Exception as e:
        print(f"⚠️ Statistika saqlashda xato: {e}")


# ===================== HANDLER =====================
def create_message_handler(phone: str):
    async def handle_message(client: Client, message: Message):
        global last_cache_update, forwarded_cache

        chat_id = message.chat.id
        group_name = getattr(message.chat, "title", None) or f"Chat {chat_id}"

        # loop oldini olish
        if normalize_chat_id(chat_id) == normalize_chat_id(DRIVERS_GROUP_ID):
            return
        if getattr(message, "outgoing", False):
            return
        if message.from_user and getattr(message.from_user, "is_bot", False):
            return

        # keywords refresh
        now = time.time()
        if now - last_cache_update > CACHE_TTL:
            await refresh_keywords()

        # text + urls
        cleaned_text, urls = extract_text_and_urls(message)
        if not cleaned_text:
            return

        lower_text = cleaned_text.lower()
        matched_keyword = None
        for kw in keywords_cache:
            if kw and kw in lower_text:
                matched_keyword = kw
                break
        if not matched_keyword:
            return

        # ===== DEDUPE: bitta xabarni faqat 1 marta forward qilish =====
        cache_key = (normalize_chat_id(chat_id), int(message.id))
        now_ts = time.time()

        async with forward_lock:
            # eski yozuvlarni tozalash
            for k, ts in list(forwarded_cache.items()):
                if now_ts - ts > FORWARD_TTL:
                    forwarded_cache.pop(k, None)

            if cache_key in forwarded_cache:
                # boshqa akkaunt allaqachon forward qilgan
                return

            forwarded_cache[cache_key] = now_ts

        # save hit
        await save_keyword_hit(matched_keyword, chat_id, group_name, phone, cleaned_text)

        # who (matn ichida ko'rsatish uchun)
        username = getattr(message.from_user, "username", None) if message.from_user else None
        label = f"@{username}" if username else "Клент личкаси"
        client_html = html.escape(label)

        # source link
        message_link = get_message_link(message)
        group_link = get_chat_link(message)

        safe_text = html.escape(cleaned_text)

        forward_text = (
            f"🔔 <b>Yangi buyurtma</b>\n"
            f"📍 Guruh12: <b>{html.escape(group_name)}</b>\n"
            f"👤 Kimdan: {client_html}\n\n"
            f"{safe_text}\n\n"
            f"🔗 {message_link}"
        )

        await send_to_drivers_group(
            forward_text,
            group_link=group_link,
            message_link=message_link,
            extra_urls=urls
        )

        print(f"📨 [{phone}] Topildi: '{matched_keyword}' - {group_name}")

    return handle_message


# ===================== RUN CLIENT =====================
async def run_client(phone: str, retry_count: int = 0):
    MAX_RETRIES = 2

    print(
        f"\n📱 [{phone}] Ishga tushmoqda..."
        + (f" (qayta urinish {retry_count})" if retry_count > 0 else "")
    )

    update_account_status(phone, "connecting")

    session_name = f"userbot_session_{phone.replace('+', '').replace(' ', '')}"
    client = Client(session_name, api_id=API_ID, api_hash=API_HASH, phone_number=phone)

    client.on_message(filters.group | filters.channel)(create_message_handler(phone))

    try:
        await client.start()
        print(f"✅ [{phone}] Ulandi!")
        update_account_status(phone, "active")

        await sync_all_groups(client, phone)
        print_statistics()

        async def periodic_sync():
            while True:
                try:
                    await asyncio.sleep(1800)
                    if client and client.is_connected:
                        await sync_all_groups(client, phone)
                        print_statistics()
                except Exception as e:
                    print(f"⚠️ [{phone}] periodic_sync xato: {e}")

        asyncio.create_task(periodic_sync())
        await asyncio.Event().wait()

    except Exception as e:
        msg = str(e)
        print(f"❌ [{phone}] Xato: {msg}")

        if "AUTH_KEY_UNREGISTERED" in msg:
            try:
                for suffix in [".session", ".session-journal"]:
                    path = f"{session_name}{suffix}"
                    if os.path.exists(path):
                        os.remove(path)
                        print(f"🧹 [{phone}] Session o'chirildi: {path}")
            except Exception as cleanup_err:
                print(f"⚠️ [{phone}] Session tozalashda xato: {cleanup_err}")

            if retry_count < MAX_RETRIES:
                print(f"🔄 [{phone}] Qayta login qilish...")
                await asyncio.sleep(2)
                return await run_client(phone, retry_count + 1)

        update_account_status(phone, "error")
        return


# ===================== MAIN =====================
async def main():
    global ALL_PHONES

    print("🚀 UserBot Multi-Account ishga tushmoqda...")

    if not init_supabase():
        print("❌ Supabase'ga ulanib bo'lmadi. Chiqish...")
        sys.exit(1)

    await load_groups_cache()
    await ensure_accounts_seeded_from_env()

    phones = fetch_phone_numbers_from_db() or PHONE_NUMBERS_ENV_FALLBACK
    phones = uniq_keep_order(phones)
    ALL_PHONES = phones

    print(f"📱 Raqamlar soni: {len(phones)}")
    if not phones:
        print("❌ Bazada ham, .env fallback'da ham raqam yo'q!")
        sys.exit(1)

    await refresh_keywords()
    asyncio.create_task(periodic_keywords_refresh())

    async def start_phone(p: str):
        if p in running_clients:
            return
        running_clients[p] = asyncio.create_task(run_client(p))

    print("\n🔄 Akkauntlar ishga tushirilmoqda...")
    for p in phones:
        await start_phone(p)

    async def watch_new_accounts():
        global ALL_PHONES
        while True:
            await asyncio.sleep(30)
            latest = fetch_phone_numbers_from_db()
            latest = uniq_keep_order(latest)
            if latest:
                ALL_PHONES = latest
            for p in latest:
                await start_phone(p)

    asyncio.create_task(watch_new_accounts())
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 UserBot to'xtatildi")
        for phone in list(running_clients.keys()) or PHONE_NUMBERS_ENV_FALLBACK:
            update_account_status(phone, "stopped")
    except Exception as e:
        print(f"❌ Kritik xato: {e}")
        for phone in list(running_clients.keys()):
            update_account_status(phone, "error")
