"""
routers/scheduler.py — Job schedulati giornalieri
- generate-daily : 05:00 — genera oroscopi per tutti gli utenti
- send-telegram  : 07:00 — push Telegram
- check-trials   : 09:00 — scadenza trial
"""
from fastapi import APIRouter, HTTPException, Request, Header
from datetime import date, datetime
import httpx
import asyncio

from config import get_supabase, get_settings
from routers.horoscope import generate_horoscope_text, _extract_houses
from routers.nocturna_service import get_transits, get_natal_chart

router = APIRouter()

SIGNS = [
    "Ariete","Toro","Gemelli","Cancro","Leone","Vergine",
    "Bilancia","Scorpione","Sagittario","Capricorno","Acquario","Pesci"
]

def lon_to_sign(lon: float) -> str:
    return SIGNS[int(float(lon) / 30) % 12]

def verify_cron_secret(x_cron_secret: str = Header(None)):
    settings = get_settings()
    if x_cron_secret != settings.app_secret_key:
        raise HTTPException(401, "Non autorizzato")

# ── JOB 1: GENERA OROSCOPI ───────────────────────────────────

@router.post("/generate-daily")
async def generate_daily_horoscopes(request: Request, x_cron_secret: str = Header(None)):
    verify_cron_secret(x_cron_secret)

    sb = get_supabase()
    settings = get_settings()
    today = date.today()

    job = sb.table("scheduler_jobs").insert({
        "job_type": "daily_generation",
        "job_date": today.isoformat(),
        "status": "running"
    }).execute().data[0]
    job_id = job["id"]

    users = sb.table("profiles")\
        .select("id, first_name, sun_sign, ascendant, moon_sign, birth_date, birth_time, birth_lat, birth_lng, birth_timezone")\
        .eq("is_active", True)\
        .eq("onboarding_completed", True)\
        .not_.is_("birth_date", "null")\
        .execute().data or []

    processed = success = failed = 0
    errors = []

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

            birth_date = str(user.get("birth_date", ""))
            birth_time = str(user.get("birth_time") or "12:00")
            lat  = user.get("birth_lat")  or 41.9
            lng  = user.get("birth_lng")  or 12.5
            tz   = user.get("birth_timezone") or "Europe/Rome"

            transit_data = get_transits(
                birth_date=birth_date, birth_time=birth_time,
                latitude=lat, longitude=lng, timezone=tz,
                transit_date=today.isoformat()
            )

            natal = get_natal_chart(birth_date, birth_time, lat, lng, tz)
            if natal:
                profile_update = {}
                planets = natal.get("planets", {})
                if not user.get("ascendant") and natal.get("ascendant") is not None:
                    profile_update["ascendant"] = lon_to_sign(natal["ascendant"])
                moon = planets.get("MOON") or {}
                if not user.get("moon_sign") and moon.get("longitude") is not None:
                    profile_update["moon_sign"] = lon_to_sign(moon["longitude"])
                if planets and not user.get("natal_planets_json"):
                    profile_update["natal_planets_json"] = planets
                houses_raw = natal.get("houses", {})
                cusps = houses_raw.get("cusps") or []
                if cusps and len(cusps) >= 12:
                    profile_update["natal_houses_json"] = {
                        f"house_{i+1}": lon_to_sign(float(cusps[i]))
                        for i in range(12)
                    }
                if natal.get("houses"):
                    transit_data["houses"] = _extract_houses(natal)
                if profile_update:
                    sb.table("profiles").update(profile_update).eq("id", user["id"]).execute()

            horoscope_data = await generate_horoscope_text(user, transit_data, settings)

            sb.table("daily_horoscopes").insert({
                "user_id":         user["id"],
                "horoscope_date":  today.isoformat(),
                "text_content":    horoscope_data.get("full_text"),
                "section_general": horoscope_data.get("section_general"),
                "section_love":    horoscope_data.get("section_love"),
                "section_work":    horoscope_data.get("section_work"),
                "section_health":  horoscope_data.get("section_health"),
                "overall_score":   horoscope_data.get("overall_score", 3),
                "planetary_data":  transit_data,
                "status":          "completed",
                "generated_at":    datetime.utcnow().isoformat(),
            }).execute()

            success += 1
            await asyncio.sleep(0.5)

        except Exception as e:
            failed += 1
            errors.append({"user_id": user["id"], "error": str(e)})

        processed += 1

    sb.table("scheduler_jobs").update({
        "status": "completed",
        "completed_at": datetime.utcnow().isoformat(),
        "users_processed": processed,
        "users_success": success,
        "users_failed": failed,
        "error_log": {"errors": errors[:50]}
    }).eq("id", job_id).execute()

    return {"ok": True, "date": today.isoformat(),
            "processed": processed, "success": success, "failed": failed}

# ── JOB 2: PUSH TELEGRAM ─────────────────────────────────────

@router.post("/send-telegram")
async def send_telegram_push(request: Request, x_cron_secret: str = Header(None)):
    verify_cron_secret(x_cron_secret)

    sb = get_supabase()
    settings = get_settings()
    today = date.today()

    connections = sb.table("telegram_connections")\
        .select("user_id, chat_id")\
        .eq("status", "active")\
        .execute().data or []

    sent = failed = 0

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

            name  = profile[0].get("first_name", "") if profile else ""
            sign  = profile[0].get("sun_sign", "")   if profile else ""
            stars = "⭐" * (h.get("overall_score") or 3)
            luna_url = "https://astra-personal.pages.dev/?luna=1"

            message = f"""🌙 *Oroscopo di oggi — {today.strftime('%-d %B %Y')}*
{'per ' + name if name else ''}{' · ' + sign if sign else ''}

{h.get('text_content', '')}

❤️ *Amore:* {h.get('section_love', '')}
💼 *Lavoro:* {h.get('section_work', '')}

{stars}

✨ [Approfondisci con Luna]({luna_url})"""

            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                    json={
                        "chat_id": conn["chat_id"],
                        "text": message,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": False
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

    return {"ok": True, "sent": sent, "failed": failed}

# ── JOB 3: TRIAL SCADUTI ─────────────────────────────────────

@router.post("/check-trials")
async def check_expired_trials(request: Request, x_cron_secret: str = Header(None)):
    verify_cron_secret(x_cron_secret)

    sb = get_supabase()

    expired = sb.table("subscriptions")\
        .select("id, user_id")\
        .eq("status", "trial")\
        .lt("trial_end", datetime.utcnow().isoformat())\
        .execute().data or []

    count = 0
    for sub in expired:
        sb.table("subscriptions").update({
            "status": "expired",
            "plan": "free"
        }).eq("id", sub["id"]).execute()
        count += 1

    return {"ok": True, "expired_count": count}
