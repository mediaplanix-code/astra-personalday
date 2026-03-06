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
    if not aspects:
        return "Nessun aspetto significativo oggi"
    lines = []
    for a in aspects[:10]:
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

def _extract_houses(natal_data: dict) -> dict:
    """Estrae le case dal tema natale e le converte in segni zodiacali."""
    houses = {}
    raw_houses = natal_data.get("houses", {})

    # Prova il formato cusps (lista di 12 longitudini)
    cusps = raw_houses.get("cusps") or raw_houses.get("house_cusps") or []
    if cusps and len(cusps) >= 12:
        for i, lon in enumerate(cusps[:12], 1):
            houses[f"house_{i}"] = _longitude_to_sign(float(lon))
        return houses

    # Prova formato dizionario {1: lon, 2: lon, ...} o {"I": lon, ...}
    roman = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,
             "VII":7,"VIII":8,"IX":9,"X":10,"XI":11,"XII":12}
    for k, v in raw_houses.items():
        if k in roman:
            houses[f"house_{roman[k]}"] = _longitude_to_sign(float(v))
        elif str(k).isdigit():
            houses[f"house_{k}"] = _longitude_to_sign(float(v))

    return houses

async def generate_horoscope_text(profile: dict, transit_data: dict, settings) -> dict:
    natal_planets_str = _format_planets_for_prompt(transit_data.get("natal_planets", {}))
    transit_aspects_str = _format_aspects_for_prompt(transit_data.get("transit_aspects", []))

    ascendant = profile.get("ascendant") or "N/D"
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
        # Se mancano le case, ricalcola e aggiorna
        pd = existing[0].get("planetary_data") or {}
        if not pd.get("houses"):
            profile = sb.table("profiles").select("*").eq("id", user_id).single().execute().data
            if profile and profile.get("birth_date"):
                birth_date = str(profile.get("birth_date", ""))
                birth_time = str(profile.get("birth_time") or "12:00")
                latitude   = profile.get("birth_lat") or 41.9
                longitude  = profile.get("birth_lng") or 12.5
                timezone   = profile.get("birth_timezone") or "Europe/Rome"
                natal = get_natal_chart(birth_date, birth_time, latitude, longitude, timezone)
                if natal.get("houses"):
                    pd["houses"] = _extract_houses(natal)
                    sb.table("daily_horoscopes").update({
                        "planetary_data": pd
                    }).eq("id", existing[0]["id"]).execute()
                    existing[0]["planetary_data"] = pd
        return existing[0]

    # Recupera profilo
    profile = sb.table("profiles").select("*").eq("id", user_id).single().execute().data
    if not profile or not profile.get("birth_date"):
        raise HTTPException(400, "Completa prima i tuoi dati natali per ricevere l'oroscopo personalizzato.")

    settings = get_settings()

    birth_date = str(profile.get("birth_date", ""))
    birth_time = str(profile.get("birth_time") or "12:00")
    latitude   = profile.get("birth_lat") or 41.9
    longitude  = profile.get("birth_lng") or 12.5
    timezone   = profile.get("birth_timezone") or "Europe/Rome"

    transit_data = get_transits(
        birth_date=birth_date,
        birth_time=birth_time,
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
        transit_date=today.isoformat()
    )

    # Calcola tema natale completo (pianeti + case + ascendente)
    natal = get_natal_chart(birth_date, birth_time, latitude, longitude, timezone)

    # Aggiorna ascendente e luna nel profilo se mancano
    if natal:
        update_fields = {}
        if not profile.get("ascendant") and natal.get("ascendant"):
            update_fields["ascendant"] = _longitude_to_sign(natal["ascendant"])
        if not profile.get("moon_sign") and natal.get("planets", {}).get("MOON"):
            update_fields["moon_sign"] = _longitude_to_sign(
                natal["planets"]["MOON"].get("longitude", 0)
            )
        if update_fields:
            sb.table("profiles").update(update_fields).eq("id", user_id).execute()
            profile.update(update_fields)

    # Aggiungi case a planetary_data
    if natal.get("houses"):
        transit_data["houses"] = _extract_houses(natal)

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
