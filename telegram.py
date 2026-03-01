"""
routers/telegram.py — Bot Telegram
"""
from fastapi import APIRouter, HTTPException, Request
from typing import Optional
from datetime import datetime
import httpx

from config import get_supabase, get_settings

router = APIRouter()

async def send_telegram_message(chat_id: int, text: str, parse_mode: str = "Markdown"):
    settings = get_settings()
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )

MSG_WELCOME_NEW = """🌙 *Benvenuto su Astra Personal!*

Sono il tuo assistente astrologico personale.

Per ricevere il tuo oroscopo personalizzato ogni mattina, iscriviti qui:
👉 {frontend_url}/registrazione

Inserisci nome, email, data di nascita, orario e luogo.

Già iscritto? Collega il tuo account:
👉 {frontend_url}/collegamento-telegram"""

MSG_ALREADY_CONNECTED = """✅ *Account già collegato!*

/oroscopo — Il tuo oroscopo di oggi
/saldo — I tuoi minuti Luna
/stop — Sospendi le notifiche
/aiuto — Tutti i comandi"""

MSG_STOP = "🔕 *Notifiche sospese.* Per riattivarle scrivi /start"

MSG_AIUTO = """🌟 *Comandi Astra Personal*

/oroscopo — Oroscopo personalizzato di oggi
/saldo — I tuoi minuti Luna
/stop — Sospendi le notifiche
/start — Riattiva le notifiche
/aiuto — Questo messaggio

👉 {frontend_url}"""

MSG_NON_ISCRITTO = "Per usare questo comando iscriviti prima:\n👉 {frontend_url}/registrazione"

@router.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"ok": True}

    settings = get_settings()
    message = data.get("message") or data.get("edited_message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    from_user = message.get("from", {})
    username = from_user.get("username", "")
    first_name = from_user.get("first_name", "")

    if text.startswith("/start"):
        parts = text.split(" ", 1)
        token = parts[1].strip() if len(parts) > 1 else None

        if token:
            # Ha token → collega account
            try:
                sb = get_supabase()
                profile = sb.table("profiles").select("id, first_name").eq("telegram_link_token", token).execute().data
                if not profile:
                    await send_telegram_message(chat_id, "❌ Codice non valido o scaduto. Genera un nuovo codice dal sito.")
                    return {"ok": True}

                user_id = profile[0]["id"]
                user_name = profile[0].get("first_name", "")
                existing = sb.table("telegram_connections").select("id").eq("chat_id", chat_id).execute().data

                if existing:
                    sb.table("telegram_connections").update({
                        "user_id": user_id, "username": username,
                        "first_name": first_name, "status": "active",
                        "connected_at": datetime.utcnow().isoformat()
                    }).eq("chat_id", chat_id).execute()
                else:
                    sb.table("telegram_connections").insert({
                        "user_id": user_id, "chat_id": chat_id,
                        "username": username, "first_name": first_name, "status": "active"
                    }).execute()

                sb.table("profiles").update({"telegram_link_token": None}).eq("id", user_id).execute()
                await send_telegram_message(chat_id,
                    f"✅ *Account collegato{', ' + user_name if user_name else ''}!*\n\n"
                    f"Riceverai il tuo oroscopo ogni mattina 🌙\n\nScrivi /oroscopo per quello di oggi."
                )
            except Exception as e:
                await send_telegram_message(chat_id, f"❌ Errore nel collegamento. Riprova dal sito.")
        else:
            # Nessun token → benvenuto senza toccare Supabase
            try:
                sb = get_supabase()
                existing = sb.table("telegram_connections").select("status").eq("chat_id", chat_id).execute().data
                if existing and existing[0]["status"] == "active":
                    await send_telegram_message(chat_id, MSG_ALREADY_CONNECTED)
                    return {"ok": True}
            except Exception:
                pass
            await send_telegram_message(chat_id, MSG_WELCOME_NEW.format(frontend_url=settings.frontend_url))

    elif text == "/stop":
        try:
            sb = get_supabase()
            sb.table("telegram_connections").update({"status": "paused"}).eq("chat_id", chat_id).execute()
        except Exception:
            pass
        await send_telegram_message(chat_id, MSG_STOP)

    elif text == "/oroscopo":
        try:
            sb = get_supabase()
            conn = sb.table("telegram_connections").select("user_id, status").eq("chat_id", chat_id).execute().data
            if not conn or conn[0]["status"] != "active":
                await send_telegram_message(chat_id, MSG_NON_ISCRITTO.format(frontend_url=settings.frontend_url))
                return {"ok": True}

            user_id = conn[0]["user_id"]
            today = datetime.utcnow().date().isoformat()
            horoscope = sb.table("daily_horoscopes")\
                .select("text_content, section_love, section_work, overall_score")\
                .eq("user_id", user_id).eq("horoscope_date", today).eq("status", "completed")\
                .single().execute().data

            if not horoscope:
                await send_telegram_message(chat_id, "⏳ Oroscopo non ancora pronto. Riprova tra qualche minuto.")
                return {"ok": True}

            stars = "⭐" * (horoscope.get("overall_score") or 3)
            await send_telegram_message(chat_id,
                f"🌙 *Il tuo oroscopo di oggi*\n\n{horoscope.get('text_content', '')}\n\n"
                f"❤️ *Amore:* {horoscope.get('section_love', '')}\n"
                f"💼 *Lavoro:* {horoscope.get('section_work', '')}\n\n{stars}"
            )
        except Exception:
            await send_telegram_message(chat_id, MSG_NON_ISCRITTO.format(frontend_url=settings.frontend_url))

    elif text == "/saldo":
        try:
            sb = get_supabase()
            conn = sb.table("telegram_connections").select("user_id").eq("chat_id", chat_id).execute().data
            if not conn:
                await send_telegram_message(chat_id, MSG_NON_ISCRITTO.format(frontend_url=settings.frontend_url))
                return {"ok": True}
            sub = sb.table("subscriptions").select("luna_minutes_balance, plan, status")\
                .eq("user_id", conn[0]["user_id"]).single().execute().data
            balance = sub.get("luna_minutes_balance", 0) if sub else 0
            plan = sub.get("plan", "free").upper() if sub else "FREE"
            await send_telegram_message(chat_id,
                f"💫 *Saldo Luna*\n\n⏱ Minuti: *{balance}*\n📋 Piano: *{plan}*\n\n"
                f"Acquista minuti: {settings.frontend_url}/luna"
            )
        except Exception:
            await send_telegram_message(chat_id, MSG_NON_ISCRITTO.format(frontend_url=settings.frontend_url))

    elif text in ("/aiuto", "/help"):
        await send_telegram_message(chat_id, MSG_AIUTO.format(frontend_url=settings.frontend_url))

    else:
        await send_telegram_message(chat_id,
            f"Ciao {first_name}! 🌙\n/aiuto per i comandi\n{settings.frontend_url}/registrazione per iscriverti"
        )

    return {"ok": True}


@router.post("/generate-link-token")
async def generate_link_token(request: Request):
    import secrets
    user_id = request.state.user_id
    sb = get_supabase()
    settings = get_settings()
    token = secrets.token_urlsafe(16)
    sb.table("profiles").update({"telegram_link_token": token}).eq("id", user_id).execute()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe")
            bot_username = r.json().get("result", {}).get("username", "AstraPersonalBot")
    except Exception:
        bot_username = "AstraPersonalBot"
    return {
        "token": token,
        "link": f"https://t.me/{bot_username}?start={token}",
        "instructions": f"Clicca il link o invia /start {token} al bot @{bot_username}"
    }

@router.delete("/disconnect")
async def disconnect_telegram(request: Request):
    user_id = request.state.user_id
    sb = get_supabase()
    sb.table("telegram_connections").update({"status": "blocked"}).eq("user_id", user_id).execute()
    return {"ok": True}

@router.get("/status")
async def telegram_status(request: Request):
    user_id = request.state.user_id
    sb = get_supabase()
    conn = sb.table("telegram_connections").select("chat_id, username, status, connected_at").eq("user_id", user_id).execute().data
    return conn[0] if conn else {"status": "not_connected"}
