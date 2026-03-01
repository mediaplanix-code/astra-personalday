"""
routers/horoscope.py — Generazione e recupero oroscopi giornalieri personalizzati
"""
from fastapi import APIRouter, HTTPException, Request
from datetime import date, datetime
import anthropic
import httpx

from config import get_supabase, get_settings

router = APIRouter()

# ── PROMPT GENERAZIONE ───────────────────────────────────────

HOROSCOPE_PROMPT = """Sei un astrologo esperto. Genera un oroscopo giornaliero PERSONALIZZATO in italiano.

DATI NATALI UTENTE:
- Nome: {first_name}
- Segno Solare: {sun_sign}
- Luna: {moon_sign}
- Ascendente: {ascendant}
- Data nascita: {birth_date}

POSIZIONI PLANETARIE OGGI ({today}):
{planetary_positions}

GENERA un oroscopo strutturato in JSON con questo formato esatto:
{{
  "section_general": "testo 2-3 frasi sulla giornata generale",
  "section_love": "testo 2 frasi su amore e relazioni",
  "section_work": "testo 2 frasi su lavoro e carriera",
  "section_health": "testo 1-2 frasi su salute ed energia",
  "overall_score": <numero da 1 a 5>,
  "full_text": "testo completo narrativo di 150-200 parole che integra tutto"
}}

L'oroscopo deve essere SPECIFICO per questa persona, non generico per il segno.
Rispondi SOLO con il JSON, niente altro."""

# ── HELPERS ─────────────────────────────────────────────────

async def get_planetary_positions(birth_lat: float, birth_lng: float, birth_date: str, birth_time: str, timezone: str, settings) -> dict:
    """Chiama nocturna-calculations (API stateless) per ottenere il tema natale."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{settings.nocturna_api_url}/api/stateless/natal-chart",
                headers={"Authorization": f"Bearer {settings.nocturna_service_token}"},
                json={
                    "date": birth_date,
                    "time": birth_time or "12:00:00",
                    "latitude": birth_lat or 41.9,
                    "longitude": birth_lng or 12.5,
                    "timezone": timezone or "Europe/Rome"
                }
            )
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return {}

async def get_transits_today(birth_lat: float, birth_lng: float, settings, profile: dict = None) -> dict:
    """Recupera transiti del giorno da nocturna-calculations (API stateless)."""
    try:
        today = date.today().isoformat()
        natal = {
            "date": str(profile.get("birth_date", "1990-01-01")) if profile else "1990-01-01",
            "time": (str(profile.get("birth_time", "12:00:00")) + ":00")[:8] if profile else "12:00:00",
            "latitude": birth_lat or 41.9,
            "longitude": birth_lng or 12.5,
            "timezone": (profile.get("birth_timezone") or "Europe/Rome") if profile else "Europe/Rome"
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{settings.nocturna_api_url}/api/stateless/transits",
                headers={"Authorization": f"Bearer {settings.nocturna_service_token}"},
                json={
                    "natal_chart": natal,
                    "transit_date": today,
                    "transit_time": "12:00:00"
                }
            )
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return {}

async def generate_horoscope_text(profile: dict, planetary_data: dict, settings) -> dict:
    """Genera testo oroscopo con Claude."""
    import json

    # Formatta dati planetari per il prompt
    planets_str = json.dumps(planetary_data, ensure_ascii=False, indent=2) if planetary_data else "Posizioni non disponibili"

    prompt = HOROSCOPE_PROMPT.format(
        first_name=profile.get("first_name", ""),
        sun_sign=profile.get("sun_sign", "N/D"),
        moon_sign=profile.get("moon_sign", "N/D"),
        ascendant=profile.get("ascendant", "N/D"),
        birth_date=str(profile.get("birth_date", "")),
        today=date.today().strftime("%d/%m/%Y"),
        planetary_positions=planets_str[:2000]  # limite caratteri
    )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    import json as j
    text = response.content[0].text.strip()
    # Rimuovi eventuali backtick markdown
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]

    return j.loads(text)

# ── ENDPOINTS ───────────────────────────────────────────────

@router.get("/today")
async def get_today_horoscope(request: Request):
    """
    Ritorna l'oroscopo personalizzato di oggi.
    Se non esiste ancora, lo genera al volo (fallback).
    """
    user_id = request.state.user_id
    sb = get_supabase()
    today = date.today()

    # Cerca oroscopo già generato
    existing = sb.table("daily_horoscopes")\
        .select("*")\
        .eq("user_id", user_id)\
        .eq("horoscope_date", today.isoformat())\
        .single().execute().data

    if existing and existing.get("status") == "completed":
        return existing

    # Non esiste ancora → genera al volo
    profile = sb.table("profiles").select("*").eq("id", user_id).single().execute().data
    if not profile or not profile.get("birth_date"):
        raise HTTPException(400, "Completa prima i tuoi dati natali per ricevere l'oroscopo personalizzato.")

    settings = get_settings()

    # Recupera dati planetari
    planetary_data = await get_transits_today(
        profile.get("birth_lat"),
        profile.get("birth_lng"),
        settings,
        profile=profile
    )

    # Genera testo
    try:
        horoscope_data = await generate_horoscope_text(profile, planetary_data, settings)
    except Exception as e:
        raise HTTPException(500, f"Errore generazione oroscopo: {str(e)}")

    # Salva nel DB
    record = {
        "user_id": user_id,
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
    }

    result = sb.table("daily_horoscopes").upsert(record).execute()
    return result.data[0] if result.data else record

@router.get("/history")
async def get_horoscope_history(request: Request, limit: int = 7):
    """Ultimi N oroscopi dell'utente."""
    user_id = request.state.user_id
    sb = get_supabase()
    result = sb.table("daily_horoscopes")\
        .select("horoscope_date, section_general, section_love, section_work, overall_score, audio_url")\
        .eq("user_id", user_id)\
        .eq("status", "completed")\
        .order("horoscope_date", desc=True)\
        .limit(limit)\
        .execute()
    return result.data
