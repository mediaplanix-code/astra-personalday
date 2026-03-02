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
import asyncio

from config import get_supabase, get_settings
from routers.horoscope import generate_horoscope_text
from routers.nocturna_service import get_transits

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
    Usa Swiss Ephemeris via nocturna_service per calcoli reali.
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
        .select("id, first_name, nome, sun_sign, moon_sign, ascendant, birth_date, birth_time, birth_lat, birth_lng, birth_timezone")\
        .eq("is_active", True)\
        .eq("onboarding_completed", True)\
        .not_.is_("birth_date", "null")\
        .execute().data

    # Prova anche dalla tabella clienti
    clienti = sb.table("clienti")\
        .select("id, nome, sun_sign, birth_date, birth_time, birth_city, status")\
        .in_("status", ["trial", "active"])\
        .not_.is_("birth_date", "null")\
        .execute().data or []

    processed = 0
    success = 0
    failed = 0
    errors = []

    # Processa utenti da profiles
    for user in users:
        try:
            existing = sb.table("daily_horoscopes")\
                .select("id")\
                .eq("user_id", user["id"])\
                .eq("horoscope_date", today.isoformat())\
                .execute().data

            if existing:
                processed += 1
                success += 1
                continue

            # Calcola transiti con Swiss Ephemeris
            transit_data = get_transits(
                birth_date=str(user.get("birth_date", "")),
                birth_time=str(user.get("birth_time") or "12:00"),
                latitude=user.get("birth_lat") or 41.9,
                longitude=user.get("birth_lng") or 12.5,
                timezone=user.get("birth_timezone") or "Europe/Rome",
                transit_date=today.isoformat()
            )

            horoscope_data = await generate_horoscope_text(user, transit_data, settings)

            sb.table("daily_horoscopes").insert({
                "user_id": user["id"],
                "horoscope_date": today.isoformat(),
                "text_content": horoscope_data.get("full_text"),
                "section_general": horoscope_data.get("section_general"),
                "section_love": horoscope_data.get("section_love"),
                "section_work": horoscope_data.get("section_work"),
                "section_health": horoscope_data.get("section_health"),
                "overall_score": horoscope_data.get("overall_score", 3),
                "planetary_data": transit_data,
                "status": "completed",
                "generated_at": datetime.utcnow().isoformat(),
            }).execute()

            success += 1
            await asyncio.sleep(0.5)  # pausa per non sovraccaricare Claude API

        except Exception as e:
            failed += 1
            errors.append({"user_id": user["id"], "error": str(e)})

        processed += 1

    # Processa anche clienti (tabella separata)
    for cliente in clienti:
        try:
            existing = sb.table("oroscopi_clienti")\
                .select("id")\
                .eq("cliente_id", cliente["id"])\
                .eq("data", today.isoformat())\
                .execute().data

            if existing:
                continue

            transit_data = get_transits(
                birth_date=str(cliente.get("birth_date", "")),
                birth_time=str(cliente.get("birth_time") or "12:00"),
                latitude=41.9,   # fallback Italia — migliorabile con geocoding
                longitude=12.5,
                timezone="Europe/Rome",
                transit_date=today.isoformat()
            )

            horoscope_data = await generate_horoscope_text(cliente, transit_data, settings)

            sb.table("oroscopi_clienti").insert({
                "cliente_id": cliente["id"],
                "data": today.isoformat(),
                "testo": horoscope_data.get("full_text"),
                "sezione_amore": horoscope_data.get("section_love"),
                "sezione_lavoro": horoscope_data.get("section_work"),
                "sezione_salute": horoscope_data.get("section_health"),
                "punteggio": horoscope_data.get("overall_score", 3),
                "dati_planetari": transit_data,
                "status": "completato",
            }).execute()

            success += 1
            await asyncio.sleep(0.5)

        except Exception as e:
            failed += 1
            errors.append({"cliente_id": cliente["id"], "error": str(e)})

        processed += 1

    # Aggiorna log job
    sb.table("scheduler_jobs").update({
        "status": "completed",
        "completed_at": datetime.utcnow().isoformat(),
        "users_processed": processed,
        "users_success": success,
        "users_failed": failed,
        "error_log": {"errors": errors[:50]}
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

    connections = sb.table("telegram_connections")\
        .select("user_id, chat_id")\
        .eq("status", "active")\
        .execute().data

    sent = 0
    failed = 0

    for conn in connections:
        try:
            horoscope = sb.table("daily_horoscopes")\
                .select("text_content, section_love, section_work, overall_score")\
                .eq("user_id", conn["user_id"])\
                .eq("horoscope_date", today.isoformat())\
                .eq("status", "completed")\
                .execute().data

            if not horoscope:
                continue

            h = horoscope[0]
            profile = sb.table("profiles")\
                .select("first_name, sun_sign")\
                .eq("id", conn["user_id"])\
                .execute().data

            name = profile[0].get("first_name", "") if profile else ""
            sign = profile[0].get("sun_sign", "") if profile else ""
            stars = "⭐" * (h.get("overall_score") or 3)

            message = f"""🌙 *Oroscopo di oggi — {today.strftime('%d %B %Y')}*
{'per ' + name if name else ''} {'• ' + sign if sign else ''}

{h.get('text_content', '')}

❤️ *Amore:* {h.get('section_love', '')}
💼 *Lavoro:* {h.get('section_work', '')}

{stars} — Voto del giorno

_Vuoi approfondire? Parla con Luna_ ✨"""

            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                    json={
                        "chat_id": conn["chat_id"],
                        "text": message,
                        "parse_mode": "Markdown"
                    }
                )

            sb.table("daily_horoscopes").update({
                "telegram_sent": True,
                "telegram_sent_at": datetime.utcnow().isoformat()
            }).eq("user_id", conn["user_id"]).eq("horoscope_date", today.isoformat()).execute()

            sent += 1
            await asyncio.sleep(0.1)

        except Exception as e:
            failed += 1

    # Invia anche ai clienti (tabella separata)
    clienti_conn = sb.table("clienti")\
        .select("id, nome, sun_sign, telegram_chat_id")\
        .not_.is_("telegram_chat_id", "null")\
        .in_("status", ["trial", "active"])\
        .execute().data or []

    for cliente in clienti_conn:
        try:
            oroscopo = sb.table("oroscopi_clienti")\
                .select("testo, sezione_amore, sezione_lavoro, punteggio")\
                .eq("cliente_id", cliente["id"])\
                .eq("data", today.isoformat())\
                .execute().data

            if not oroscopo:
                continue

            o = oroscopo[0]
            stars = "⭐" * (o.get("punteggio") or 3)
            nome = cliente.get("nome", "").split()[0] if cliente.get("nome") else ""
            sign = cliente.get("sun_sign", "")

            message = f"""🌙 *Oroscopo di oggi — {today.strftime('%d %B %Y')}*
{'per ' + nome if nome else ''} {'• ' + sign if sign else ''}

{o.get('testo', '')}

❤️ *Amore:* {o.get('sezione_amore', '')}
💼 *Lavoro:* {o.get('sezione_lavoro', '')}

{stars} — Voto del giorno

_Vuoi approfondire? Parla con Luna_ ✨"""

            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                    json={
                        "chat_id": cliente["telegram_chat_id"],
                        "text": message,
                        "parse_mode": "Markdown"
                    }
                )

            sent += 1
            await asyncio.sleep(0.1)

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

    # Profili (vecchio sistema)
    expired_subs = sb.table("subscriptions")\
        .select("id, user_id")\
        .eq("status", "trial")\
        .lt("trial_end", datetime.utcnow().isoformat())\
        .execute().data or []

    count = 0
    for sub in expired_subs:
        sb.table("subscriptions").update({
            "status": "expired",
            "plan": "free"
        }).eq("id", sub["id"]).execute()
        count += 1

    # Clienti (nuovo sistema)
    expired_clienti = sb.table("clienti")\
        .select("id")\
        .eq("status", "trial")\
        .lt("trial_end", datetime.utcnow().isoformat())\
        .execute().data or []

    for c in expired_clienti:
        sb.table("clienti").update({
            "status": "expired",
            "plan": "free"
        }).eq("id", c["id"]).execute()
        count += 1

    return {"ok": True, "expired_count": count}
