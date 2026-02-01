# Telegram UserBot - Guruh Monitoring

Bu UserBot sizning shaxsiy Telegram akkauntingiz orqali guruhlardagi xabarlarni kuzatadi va kalit so'zlarni topganda haydovchilar guruhiga yuboradi.

## 🚀 Railway ga Deploy qilish

### 1-qadam: Railway ga kirish
1. [railway.app](https://railway.app) ga o'ting
2. GitHub bilan kiring

### 2-qadam: Yangi proyekt yaratish
1. "New Project" bosing
2. "Deploy from GitHub repo" tanlang
3. Bu repository ni tanlang

### 3-qadam: Environment Variables sozlash
Railway dashboard da "Variables" bo'limiga o'ting va quyidagilarni qo'shing:

| Variable | Qiymat |
|----------|--------|
| `TELEGRAM_API_ID` | my.telegram.org dan |
| `TELEGRAM_API_HASH` | my.telegram.org dan |
| `PHONE_NUMBER` | +998901234567 |
| `SUPABASE_URL` | https://ilmtwpvjkluieuvsmpth.supabase.co |
| `SUPABASE_SERVICE_KEY` | Supabase service role key |
| `TELEGRAM_BOT_TOKEN` | Bot token |
| `DRIVERS_GROUP_ID` | -1003784903860 |

### 4-qadam: Deploy
Railway avtomatik deploy qiladi. Logs da "UserBot tayyor!" ko'rsangiz, hammasi ishlayapti!

## 🔐 Telegram API Credentials

1. [my.telegram.org](https://my.telegram.org) ga kiring
2. "API development tools" ga o'ting
3. App yarating va `api_id` va `api_hash` oling

## ⚠️ Birinchi ishga tushirishda

Birinchi marta ishga tushganda Telegram'dan tasdiqlash kodi so'raladi. Railway console da kodni kiritishingiz kerak bo'ladi.

## 📝 Qanday ishlaydi

1. UserBot sizning akkauntingiz bilan Telegram ga ulanadi
2. Admin panelda qo'shilgan guruhlarni kuzatadi
3. Kalit so'z topilganda haydovchilar guruhiga xabar yuboradi
4. Xabarga to'g'ridan-to'g'ri havola bilan

## 🔧 Lokal ishga tushirish

```bash
# Virtual environment yaratish
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Dependencies o'rnatish
pip install -r requirements.txt

# .env faylini yaratish
cp env.example .env
# .env ni to'ldiring

# Ishga tushirish
python main.py
```

## 📁 Fayl strukturasi

```
userbot/
├── main.py           # Asosiy kod
├── requirements.txt  # Python dependencies
├── Procfile          # Railway uchun
├── env.example       # Environment variables namunasi
└── README.md         # Hujjat
```
