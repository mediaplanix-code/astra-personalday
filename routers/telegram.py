"""
routers/telegram.py — Bot Telegram
- Webhook per ricevere messaggi dal bot
- Comandi: /start, /stop, /oroscopo, /saldo, /aiuto
- Connessione account utente via token univoco
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import httpx

from config import get_supabase, get_settings

router = APIRouter()

# ── HELPERS ──────────────────────────────────────────────────

async def send_telegram_message(chat_id: int, text: str, parse_mode: str = "Markdown"):
    """Invia messaggio Telegram."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )

async def send_telegram_voice(chat_id: int, audio_url: str, caption: str = ""):
    """Invia messaggio vocale Telegram."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendAudio",
            json={"chat_id": chat_id, "audio": audio_url, "caption": caption, "parse_mode": "Markdown"}
        )

# ── MESSAGGI BOT ─────────────────────────────────────────────

MSG_WELCOME = """🌙 *Benvenuto su Astra Personal!*

Sono il tuo assistente astrologico personale.

Per collegare il tuo account e ricevere il tuo oroscopo personale ogni mattina, vai su:
👉 {frontend_url}/collegamento-telegram

Ti verrà fornito un codice da inviarmi qui.

*Comandi disponibili:*
/oroscopo — Oroscopo di oggi
/saldo — Minuti Luna disponibili
/stop — Interrompi le notifiche
/aiuto — Mostra questo messaggio"""

MSG_ALREADY_CONNECTED = """✅ Il tuo account è già collegato!

*Comandi disponibili:*
/oroscopo — Oroscopo di oggi
/saldo — I tuoi minuti Luna
/stop — Interrompi le notifiche
/aiuto — Mostra l'elenco comandi"""

MSG_STOP = """🔕 Notifiche sospese.

Non riceverai più l'oroscopo giornaliero su Telegram.

Per riattivarle in qualsiasi momento, scrivi /start oppure vai nelle impostazioni del sito."""

MSG_AIUTO = """🌟 *Comandi Astra Personal*

/oroscopo — Ricevi l'oroscopo di oggi
/saldo — Controlla i tuoi minuti Luna
/stop — Sospendi le notifiche giornaliere
/start — Riattiva le notifiche
/aiuto — Mostra questo messaggio

Per parlare con Luna o accedere ai servizi:
👉 {frontend_url}"""

# ── WEBHOOK TELEGRAM ──────────────────────────────────────────

@router.post("/webhook")
async def telegram_webhook(request: Request):
    """
    Riceve aggiornamenti da Telegram.
    Questa route è pubblica — Telegram non invia Bearer token.
    """
    try:
        data = await request.json()
    except Exception:
        return {"ok": True}

    settings = get_settings()
    sb = get_supabase()

    message = data.get("message") or data.get("edited_message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    from_user = message.get("from", {})
    username = from_user.get("username", "")
    first_name = from_user.get("first_name", "")

    # ── /start ──────────────────────────────────────────────
    if text.startswith("/start"):
        parts = text.split(" ", 1)
        token = parts[1].strip() if len(parts) > 1 else None

        # Controlla se già connesso
        existing = sb.table("telegram_connections").select("id, status, user_id").eq("chat_id", chat_id).execute().data

        if token:
            # Collega account tramite token
            profile = sb.table("profiles").select("id, first_name").eq("telegram_link_token", token).execute().data
            if not profile:
                await send_telegram_message(chat_id, "❌ Codice non valido o scaduto. Genera un nuovo codice dal sito.")
                return {"ok": True}

            user_id = profile[0]["id"]
            user_name = profile[0].get("first_name", "")

            if existing:
                # Aggiorna connessione esistente
                sb.table("telegram_connections").update({
                    "user_id": user_id,
                    "username": username,
                    "first_name": first_name,
                    "status": "active",
                    "connected_at": datetime.utcnow().isoformat()
                }).eq("chat_id", chat_id).execute()
            else:
                # Crea nuova connessione
                sb.table("telegram_connections").insert({
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "username": username,
                    "first_name": first_name,
                    "status": "active"
                }).execute()

            # Invalida token usato
            sb.table("profiles").update({"telegram_link_token": None}).eq("id", user_id).execute()

            await send_telegram_message(chat_id,
                f"✅ *Account collegato con successo{', ' + user_name if user_name else ''}!*\n\n"
                f"Riceverai il tuo oroscopo personale ogni mattina alle 07:00 🌙\n\n"
                f"Scrivi /oroscopo per ricevere subito quello di oggi."
            )

        elif existing and existing[0]["status"] == "active":
            await send_telegram_message(chat_id, MSG_ALREADY_CONNECTED)
        else:
            await send_telegram_message(chat_id,
                MSG_WELCOME.format(frontend_url=settings.frontend_url)
            )

    # ── /stop ────────────────────────────────────────────────
    elif text == "/stop":
        sb.table("telegram_connections").update({"status": "paused"}).eq("chat_id", chat_id).execute()
        await send_telegram_message(chat_id, MSG_STOP)

    # ── /oroscopo ────────────────────────────────────────────
    elif text == "/oroscopo":
        conn = sb.table("telegram_connections").select("user_id, status").eq("chat_id", chat_id).execute().data

        if not conn or conn[0]["status"] != "active":
            await send_telegram_message(chat_id,
                f"Per ricevere il tuo oroscopo personale devi prima collegare il tuo account.\n\n"
                f"👉 {settings.frontend_url}/collegamento-telegram"
            )
            return {"ok": True}

        user_id = conn[0]["user_id"]
        today = datetime.utcnow().date().isoformat()

        horoscope = sb.table("daily_horoscopes")\
            .select("text_content, section_love, section_work, overall_score")\
            .eq("user_id", user_id)\
            .eq("horoscope_date", today)\
            .eq("status", "completed")\
            .single().execute().data

        if not horoscope:
            await send_telegram_message(chat_id,
                "⏳ Il tuo oroscopo di oggi non è ancora pronto. Riprova tra qualche minuto."
            )
            return {"ok": True}

        stars = "⭐" * (horoscope.get("overall_score") or 3)
        msg = f"""🌙 *Il tuo oroscopo di oggi*

{horoscope.get('text_content', '')}

❤️ *Amore:* {horoscope.get('section_love', '')}
💼 *Lavoro:* {horoscope.get('section_work', '')}

{stars}

_Vuoi approfondire? Parla con Luna su {settings.frontend_url}_ ✨"""

        await send_telegram_message(chat_id, msg)

    # ── /saldo ───────────────────────────────────────────────
    elif text == "/saldo":
        conn = sb.table("telegram_connections").select("user_id").eq("chat_id", chat_id).execute().data

        if not conn:
            await send_telegram_message(chat_id, f"Account non collegato.\n👉 {settings.frontend_url}/collegamento-telegram")
            return {"ok": True}

        sub = sb.table("subscriptions")\
            .select("luna_minutes_balance, plan, status")\
            .eq("user_id", conn[0]["user_id"])\
            .single().execute().data

        if not sub:
            await send_telegram_message(chat_id, "Nessun abbonamento trovato.")
            return {"ok": True}

        balance = sub.get("luna_minutes_balance", 0)
        plan = sub.get("plan", "free").upper()
        status = sub.get("status", "")

        await send_telegram_message(chat_id,
            f"💫 *Il tuo saldo Luna*\n\n"
            f"⏱ Minuti disponibili: *{balance} min*\n"
            f"📋 Piano: *{plan}*\n"
            f"📊 Stato: *{status}*\n\n"
            f"Per acquistare minuti: {settings.frontend_url}/luna"
        )

    # ── /aiuto ───────────────────────────────────────────────
    elif text in ("/aiuto", "/help"):
        await send_telegram_message(chat_id, MSG_AIUTO.format(frontend_url=settings.frontend_url))

    # ── Messaggio generico ───────────────────────────────────
    else:
        await send_telegram_message(chat_id,
            f"Ciao! 🌙 Sono il bot di Astra Personal.\n\n"
            f"Scrivi /aiuto per vedere i comandi disponibili.\n"
            f"Per parlare con Luna: {settings.frontend_url}"
        )

    return {"ok": True}


# ── ENDPOINT: GENERA TOKEN COLLEGAMENTO ──────────────────────

@router.post("/generate-link-token")
async def generate_link_token(request: Request):
    """
    Genera un token univoco per collegare l'account Telegram.
    L'utente lo invia al bot via /start <token>.
    """
    import secrets
    user_id = request.state.user_id
    sb = get_supabase()

    token = secrets.token_urlsafe(16)

    # Aggiungi colonna telegram_link_token al profilo
    # (da aggiungere allo schema Supabase se non presente)
    sb.table("profiles").update({"telegram_link_token": token}).eq("id", user_id).execute()

    settings = get_settings()
    bot_username = await get_bot_username(settings)

    return {
        "token": token,
        "link": f"https://t.me/{bot_username}?start={token}",
        "instructions": f"Clicca il link o invia /start {token} al bot @{bot_username}"
    }


@router.delete("/disconnect")
async def disconnect_telegram(request: Request):
    """Disconnette l'account Telegram dell'utente."""
    user_id = request.state.user_id
    sb = get_supabase()
    sb.table("telegram_connections").update({"status": "blocked"}).eq("user_id", user_id).execute()
    return {"ok": True}


@router.get("/status")
async def telegram_status(request: Request):
    """Ritorna stato connessione Telegram dell'utente."""
    user_id = request.state.user_id
    sb = get_supabase()
    conn = sb.table("telegram_connections").select("chat_id, username, status, connected_at, last_message_at").eq("user_id", user_id).execute().data
    return conn[0] if conn else {"status": "not_connected"}


async def get_bot_username(settings) -> str:
    """Recupera username del bot da Telegram API."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe")
            return r.json().get("result", {}).get("username", "AstraPersonalBot")
    except Exception:
        return "AstraPersonalBot"


# ── SETUP WEBHOOK (chiamare una volta sola) ───────────────────

@router.post("/setup-webhook")
async def setup_webhook(request: Request):
    """
    Registra il webhook su Telegram.
    Chiamare UNA VOLTA dopo il deploy su Render.
    URL webhook: https://astra-personalday.onrender.com/api/telegram/webhook
    """
    # Solo admin può chiamare questo endpoint
    user_email = getattr(request.state, "user_email", "")
    settings = get_settings()
    admin_emails = [e.strip() for e in settings.admin_emails.split(",")]
    if user_email not in admin_emails:
        raise HTTPException(403, "Solo gli admin possono configurare il webhook")

    webhook_url = f"https://astra-personalday.onrender.com/api/telegram/webhook"

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["message"]}
        )
        result = r.json()

    return {"ok": result.get("ok"), "description": result.get("description")}
