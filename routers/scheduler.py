"""
routers/scheduler.py — Job schedulati giornalieri
- Generazione oroscopi personalizzati per tutti gli utenti
- Push Telegram
- Controllo trial scaduti

Da Render: configurare come Cron Job separato
  - generate_daily: ogni giorno alle 05:00
  - send_telegram: ogni giorno alle 07:00
  - check_trials: ogni giorno alle 09:00
"""
from fastapi import APIRouter, HTTPException, Request, Header
from datetime import date, datetime
import httpx
import anthropic
import json
import asyncio

from config import get_supabase, get_settings
from routers.horoscope import generate_horoscope_text, get_transits_today

router = APIRouter()

# ── SICUREZZA CRON ────────────────────────────────────────────

def verify_cron_secret(x_cron_secret: str = Header(None)):
    settings = get_settings()
    if x_cron_secret != settings.app_secret_key:
        raise HTTPException(401, "Non autorizzato")

# ── JOB 1: GENERA OROSCOPI GIORNALIERI ───────────────────────

@router.post("/generate-daily")
async def generate_daily_horoscopes(request: Request, x_cron_secret: str = Header(None)):
    """
    Genera oroscopi personalizzati per TUTTI gli utenti attivi.
    Eseguito ogni giorno alle 05:00.
    Salta utenti che non hanno completato i dati natali.
    """
    verify_cron_secret(x_cron_secret)

    sb = get_supabase()
    settings = get_settings()
    today = date.today()

    # Log inizio job
    job = sb.table("scheduler_jobs").insert({
        "job_type": "daily_generation",
        "job_date": today.isoformat(),
        "status": "running"
    }).execute().data[0]
    job_id = job["id"]

    # Recupera utenti attivi con dati natali completi
    users = sb.table("profiles")\
        .select("id, first_name, sun_sign, moon_sign, ascendant, birth_date, birth_lat, birth_lng, birth_timezone")\
        .eq("is_active", True)\
        .eq("onboarding_completed", True)\
        .not_.is_("birth_date", "null")\
        .execute().data

    processed = 0
    success = 0
    failed = 0
    errors = []

    # Recupera transiti del giorno UNA volta per tutti (stesso cielo per tutti)
    planetary_data = await get_transits_today(41.9, 12.5, settings)

    for user in users:
        try:
            # Controlla se già generato oggi
            existing = sb.table("daily_horoscopes")\
                .select("id")\
                .eq("user_id", user["id"])\
                .eq("horoscope_date", today.isoformat())\
                .execute().data

            if existing:
                processed += 1
                success += 1
                continue

            # Genera oroscopo
            horoscope_data = await generate_horoscope_text(user, planetary_data, settings)

            # Salva
            sb.table("daily_horoscopes").insert({
                "user_id": user["id"],
                "horoscope_date": today.isoformat(),
                "text_content": horoscope_data.get("full_text"),
                "section_general": horoscope_data.get("section_general"),
                "section_love": horoscope_data.get("section_love"),
                "section_work": horoscope_data.get("section_work"),
                "section_health": horoscope_data.get("section_health"),
                "overall_score": horoscope_data.get("overall_score", 3),
                "planetary_data": planetary_data,
                "status": "completed",
                "generated_at": datetime.utcnow().isoformat(),
            }).execute()

            success += 1
            # Pausa breve per non sovraccaricare Claude API
            await asyncio.sleep(0.3)

        except Exception as e:
            failed += 1
            errors.append({"user_id": user["id"], "error": str(e)})

        processed += 1

    # Aggiorna log job
    sb.table("scheduler_jobs").update({
        "status": "completed",
        "completed_at": datetime.utcnow().isoformat(),
        "users_processed": processed,
        "users_success": success,
        "users_failed": failed,
        "error_log": {"errors": errors[:50]}  # max 50 errori nel log
    }).eq("id", job_id).execute()

    return {
        "ok": True,
        "date": today.isoformat(),
        "processed": processed,
        "success": success,
        "failed": failed
    }

# ── JOB 2: PUSH TELEGRAM ─────────────────────────────────────

@router.post("/send-telegram")
async def send_telegram_push(request: Request, x_cron_secret: str = Header(None)):
    """
    Invia oroscopo del giorno via Telegram a tutti gli utenti connessi.
    Eseguito ogni giorno alle 07:00.
    """
    verify_cron_secret(x_cron_secret)

    sb = get_supabase()
    settings = get_settings()
    today = date.today()

    # Recupera connessioni Telegram attive
    connections = sb.table("telegram_connections")\
        .select("user_id, chat_id, send_voice")\
        .eq("status", "active")\
        .execute().data

    sent = 0
    failed = 0

    for conn in connections:
        try:
            # Recupera oroscopo del giorno
            horoscope = sb.table("daily_horoscopes")\
                .select("text_content, section_love, section_work, overall_score, audio_url")\
                .eq("user_id", conn["user_id"])\
                .eq("horoscope_date", today.isoformat())\
                .eq("status", "completed")\
                .single().execute().data

            if not horoscope:
                continue

            profile = sb.table("profiles")\
                .select("first_name, sun_sign")\
                .eq("id", conn["user_id"])\
                .single().execute().data

            # Formatta messaggio Telegram
            stars = "⭐" * (horoscope.get("overall_score") or 3)
            name = profile.get("first_name", "") if profile else ""
            sign = profile.get("sun_sign", "") if profile else ""

            message = f"""🌙 *Oroscopo di oggi — {today.strftime('%d %B %Y')}*
{'per ' + name if name else ''} {'• ' + sign if sign else ''}

{horoscope.get('text_content', '')}

❤️ *Amore:* {horoscope.get('section_love', '')}
💼 *Lavoro:* {horoscope.get('section_work', '')}

{stars} — Voto del giorno

_Vuoi approfondire? Parla con Luna_ ✨"""

            # Invia via Bot API Telegram
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                    json={
                        "chat_id": conn["chat_id"],
                        "text": message,
                        "parse_mode": "Markdown"
                    }
                )

            # Segna come inviato
            sb.table("daily_horoscopes").update({
                "telegram_sent": True,
                "telegram_sent_at": datetime.utcnow().isoformat()
            }).eq("user_id", conn["user_id"]).eq("horoscope_date", today.isoformat()).execute()

            # Aggiorna last_message_at
            sb.table("telegram_connections").update({
                "last_message_at": datetime.utcnow().isoformat()
            }).eq("user_id", conn["user_id"]).execute()

            sent += 1
            await asyncio.sleep(0.1)  # Rate limit Telegram: 30 msg/sec

        except Exception as e:
            failed += 1

    return {"ok": True, "sent": sent, "failed": failed}

# ── JOB 3: CONTROLLO TRIAL SCADUTI ───────────────────────────

@router.post("/check-trials")
async def check_expired_trials(request: Request, x_cron_secret: str = Header(None)):
    """
    Controlla trial scaduti e li porta a 'expired'.
    Eseguito ogni giorno alle 09:00.
    """
    verify_cron_secret(x_cron_secret)

    sb = get_supabase()

    # Trova trial scaduti
    expired = sb.table("subscriptions")\
        .select("id, user_id")\
        .eq("status", "trial")\
        .lt("trial_end", datetime.utcnow().isoformat())\
        .execute().data

    count = 0
    for sub in expired:
        sb.table("subscriptions").update({
            "status": "expired",
            "plan": "free"
        }).eq("id", sub["id"]).execute()
        count += 1

    return {"ok": True, "expired_count": count}
