# Private Video Downloader (Telegram bot)

Aiogram 3 + yt-dlp + FFmpeg. Faqat administratorlar uchun.

## Fayllar
| Fayl | Vazifasi |
|---|---|
| main.py | Ishga tushirish, sessiya, middleware |
| config.py | .env sozlamalari |
| handlers.py | Buyruqlar, sifat tanlash, progress, yuborish |
| downloader.py | yt-dlp: ma'lumot olish, yuklash, xato xabarlari |
| store.py | URL'larni vaqtincha xotirada saqlash (TTL 15 daqiqa) |
| utils.py | URL tekshiruvi, formatlash, URL/domenni yashirish |

## Windows
1. Python 3.11+ va FFmpeg o'rnating (`ffmpeg -version` ishlashi kerak, yoki `FFMPEG_LOCATION`).
2. ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   copy .env.example .env     # to'ldiring
   python main.py
   ```
3. O'z ID'ingizni bilish uchun botga `/id` yuboring va `ADMIN_IDS` ga yozing.

## Railway
1. Repoga yuklang (Dockerfile FFmpeg'ni o'zi o'rnatadi).
2. Variables: `BOT_TOKEN`, `ADMIN_IDS`, `ALLOWED_HOSTS` (+ ixtiyoriylar).
3. Bot polling rejimida ishlaydi, port kerak emas.

## Telegram hajm limiti
Oddiy Bot API 50 MB gacha fayl yuboradi. 1080p videolar ko'pincha undan katta.
2 GB gacha yuborish uchun lokal Bot API server (`telegram-bot-api --local`) ishga tushirib,
`LOCAL_API_URL` va `MAX_UPLOAD_MB=2000` ni sozlang.

## 50 MB dan katta videolar (qayta siqmasdan)
`.env` ga `TG_API_ID` va `TG_API_HASH` yozing (https://my.telegram.org -> API development tools).
Shunda video MTProto orqali 2 GB gacha, asl sifatda yuboriladi (`MAX_UPLOAD_MB` e'tiborga olinmaydi).

## Kodlar va xarajat nazorati
Har bir video 50 bilan boshlanadigan kod oladi (5001, 5002, ... 5010 ...). Bir xil video har doim bir xil kod oladi.
Kodlar va oylik trafik `DATA_DIR` ichida saqlanadi. Railway'da bu papka o'chib ketmasligi uchun
servisga Volume ulang (mount path: `/data`). `/stat` buyrug'i oylik trafikni ko'rsatadi,
`MONTHLY_LIMIT_GB` limitga yetganda bot yangi videolarni to'xtatadi.

## Eslatma
- Bot cookie/login ishlatmaydi, yopiq yoki DRM bilan himoyalangan videolarni yuklamaydi.
- Faqat o'zingizda yuklab olish huquqi bor kontentdan va sayt shartlariga mos holda foydalaning.
- yt-dlp saytlar o'zgarganda eskiradi: `pip install -U yt-dlp`.
