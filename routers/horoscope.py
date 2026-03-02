"""
routers/horoscope.py — Generazione e recupero oroscopi giornalieri personalizzati
Usa nocturna_service per calcoli planetari reali (Swiss Ephemeris).
"""
from fastapi import APIRouter, HTTPException, Request
from datetime import date, datetime
import anthropic
import json

from config import get_supabase, get_settings
from routers.nocturna_service import get_transits, get_natal_chart

router = APIRouter()

# ── PROMPT GENERAZIONE ───────────────────────────────────────

HOROSCOPE_PROMPT = """Sei un astrologo esperto. Genera un oroscopo giornaliero PERSONALIZZATO in italiano.

DATI NATALI:
- Nome: {first_name}
- Segno Solare: {sun_sign}
- Ascendente: {ascendant}
- Data nascita: {birth_date}

POSIZIONI PLANETARIE NATALI:
{natal_planets}

TRANSITI DI OGGI ({today}):
{transit_aspects}

GENERA un oroscopo strutturato in JSON con questo formato esatto:
{{
  "section_general": "testo 2-3 frasi sulla giornata generale basato sui transiti reali",
  "section_love": "testo 2 frasi su amore e relazioni",
  "section_work": "testo 2 frasi su lavoro e carriera",
  "section_health": "testo 1-2 frasi su salute ed energia",
  "overall_score": <numero da 1 a 5>,
  "full_text": "testo completo narrativo di 150-200 parole che integra tutto"
}}

L'oroscopo deve essere SPECIFICO per questa persona basandoti sui transiti reali, non generico per il segno.
Cita pianeti e aspetti concreti quando rilevanti.
Rispondi SOLO con il JSON, niente altro."""

# ── HELPERS ─────────────────────────────────────────────────

def _format_planets_for_prompt(planets: dict) -> str:
    """Formatta le posizioni planetarie in testo leggibile per il prompt."""
    if not planets:
        return "Dati non disponibili"
    lines = []
    for planet, data in planets.items():
        lon = data.get("longitude", 0)
        retro = " (R)" if data.get("is_retrograde") else ""
        sign = _longitude_to_sign(lon)
        lines.append(f"- {planet}: {sign} {lon:.1f}°{retro}")
    return "\n".join(lines)

def _format_aspects_for_prompt(aspects: list) -> str:
    """Formatta gli aspetti di transito in testo leggibile per il prompt."""
    if not aspects:
        return "Nessun aspetto significativo oggi"
    lines = []
    for a in aspects[:10]:  # max 10 aspetti più importanti
        retro = " (retrogrado)" if a.get("is_retrograde") else ""
        lines.append(
            f"- {a['transit_planet']}{retro} in {a['transit_sign']} "
            f"{a['aspect']} {a['natal_planet']} natale (orb {a['orb']}°)"
        )
    return "\n".join(lines)

SIGNS = [
    "Ariete","Toro","Gemelli","Cancro","Leone","Vergine",
    "Bilancia","Scorpione","Sagittario","Capricorno","Acquario","Pesci"
]

def _longitude_to_sign(lon: float) -> str:
    return SIGNS[int(lon / 30) % 12]

async def generate_horoscope_text(profile: dict, transit_data: dict, settings) -> dict:
    """Genera testo oroscopo con Claude usando i dati planetari reali."""

    natal_planets_str  = _format_planets_for_prompt(transit_data.get("natal_planets", {}))
    transit_aspects_str = _format_aspects_for_prompt(transit_data.get("transit_aspects", []))

    ascendant = profile.get("ascendant") or "N/D"
    # Se abbiamo l'ascendente calcolato, convertiamolo in segno
    if isinstance(ascendant, float):
        ascendant = _longitude_to_sign(ascendant)

    prompt = HOROSCOPE_PROMPT.format(
        first_name=profile.get("first_name") or profile.get("nome", ""),
        sun_sign=profile.get("sun_sign", "N/D"),
        ascendant=ascendant,
        birth_date=str(profile.get("birth_date", "")),
        today=date.today().strftime("%d/%m/%Y"),
        natal_planets=natal_planets_str,
        transit_aspects=transit_aspects_str
    )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]

    return json.loads(text)

# ── ENDPOINTS ───────────────────────────────────────────────

@router.get("/today")
async def get_today_horoscope(request: Request):
    """
    Ritorna l'oroscopo personalizzato di oggi.
    Se non esiste ancora, lo genera al volo con calcoli planetari reali.
    """
    user_id = request.state.user_id
    sb = get_supabase()
    today = date.today()

    # Cerca oroscopo già generato oggi
    existing = sb.table("daily_horoscopes")\
        .select("*")\
        .eq("user_id", user_id)\
        .eq("horoscope_date", today.isoformat())\
        .execute().data

    if existing and existing[0].get("status") == "completed":
        return existing[0]

    # Recupera profilo
    profile = sb.table("profiles").select("*").eq("id", user_id).single().execute().data
    if not profile or not profile.get("birth_date"):
        raise HTTPException(400, "Completa prima i tuoi dati natali per ricevere l'oroscopo personalizzato.")

    settings = get_settings()

    # Calcola transiti con Swiss Ephemeris
    birth_date  = str(profile.get("birth_date", ""))
    birth_time  = str(profile.get("birth_time", "12:00")) or "12:00"
    latitude    = profile.get("birth_lat") or 41.9
    longitude   = profile.get("birth_lng") or 12.5
    timezone    = profile.get("birth_timezone") or "Europe/Rome"

    transit_data = get_transits(
        birth_date=birth_date,
        birth_time=birth_time,
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
        transit_date=today.isoformat()
    )

    # Aggiorna ascendente nel profilo se non presente
    if not profile.get("ascendant") and birth_date:
        natal = get_natal_chart(birth_date, birth_time, latitude, longitude, timezone)
        if natal.get("ascendant"):
            sb.table("profiles").update({
                "ascendant": _longitude_to_sign(natal["ascendant"]),
                "moon_sign": _longitude_to_sign(
                    natal.get("planets", {}).get("MOON", {}).get("longitude", 0)
                ) if natal.get("planets", {}).get("MOON") else None
            }).eq("id", user_id).execute()
            profile["ascendant"] = natal["ascendant"]

    # Genera testo con Claude
    try:
        horoscope_data = await generate_horoscope_text(profile, transit_data, settings)
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
        "planetary_data": transit_data,
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
