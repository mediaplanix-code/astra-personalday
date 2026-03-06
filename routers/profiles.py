"""
routers/profiles.py — Gestione profilo utente e dati natali
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from datetime import date
import httpx

from config import get_supabase, get_settings
from routers.nocturna_service import get_natal_chart

router = APIRouter()

# ── MODELS ──────────────────────────────────────────────────

class BirthDataUpdate(BaseModel):
    birth_date: Optional[date] = None
    birth_time: Optional[str] = None
    birth_time_unknown: Optional[bool] = False
    birth_city: str
    birth_country: str
    birth_lat: Optional[float] = None
    birth_lng: Optional[float] = None
    birth_timezone: Optional[str] = None

class ProfileUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    gender: Optional[str] = None
    phone: Optional[str] = None
    preferred_language: Optional[str] = "it"
    report_format: Optional[str] = "both"
    notifications_email: Optional[bool] = True
    notifications_push: Optional[bool] = True

class PartnerCreate(BaseModel):
    name: str
    relationship_type: str = "romantic"
    relationship_start: Optional[date] = None
    birth_date: Optional[date] = None
    birth_time: Optional[str] = None
    birth_time_unknown: Optional[bool] = False
    birth_city: Optional[str] = None
    birth_country: Optional[str] = None
    birth_lat: Optional[float] = None
    birth_lng: Optional[float] = None
    birth_timezone: Optional[str] = None
    notes: Optional[str] = None

class LifeSituationUpdate(BaseModel):
    relationship_status: Optional[str] = None
    work_situation: Optional[str] = None
    goals: Optional[list] = []
    sensitive_topics: Optional[list] = []
    notes: Optional[str] = ""

# ── HELPERS ─────────────────────────────────────────────────

SIGNS = [
    "Ariete","Toro","Gemelli","Cancro","Leone","Vergine",
    "Bilancia","Scorpione","Sagittario","Capricorno","Acquario","Pesci"
]

def lon_to_sign(lon: float) -> str:
    return SIGNS[int(float(lon) / 30) % 12]

def get_sun_sign(birth_date: date) -> str:
    m, d = birth_date.month, birth_date.day
    signs = [
        (1,20,"Capricorno"),(2,19,"Acquario"),(3,20,"Pesci"),
        (4,20,"Ariete"),(5,21,"Toro"),(6,21,"Gemelli"),
        (7,23,"Cancro"),(8,23,"Leone"),(9,23,"Vergine"),
        (10,23,"Bilancia"),(11,22,"Scorpione"),(12,22,"Sagittario"),
        (12,31,"Capricorno")
    ]
    for end_month, end_day, sign in signs:
        if m < end_month or (m == end_month and d <= end_day):
            return sign
    return "Capricorno"

async def get_geo_from_ip(ip: str) -> dict:
    try:
        settings = get_settings()
        key = settings.ipapi_key
        url = f"https://ipapi.co/{ip}/json/"
        if key:
            url += f"?key={key}"
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url)
            d = r.json()
            return {
                "reg_city": d.get("city", ""),
                "reg_region": d.get("region", ""),
                "reg_country": d.get("country_code", ""),
                "reg_postal_code": d.get("postal", ""),
                "reg_lat": d.get("latitude"),
                "reg_lng": d.get("longitude"),
                "reg_isp": d.get("org", ""),
            }
    except Exception:
        return {}

# ── ENDPOINTS ───────────────────────────────────────────────

@router.get("/me")
async def get_my_profile(request: Request):
    user_id = request.state.user_id
    sb = get_supabase()
    result = sb.table("profiles").select("*").eq("id", user_id).single().execute()
    if not result.data:
        raise HTTPException(404, "Profilo non trovato")
    return result.data

@router.patch("/me")
async def update_profile(request: Request, body: ProfileUpdate):
    user_id = request.state.user_id
    sb = get_supabase()
    sb.table("profiles").update(body.model_dump(exclude_none=True)).eq("id", user_id).execute()
    return {"ok": True}

@router.post("/me/birth-data")
async def save_birth_data(request: Request, body: BirthDataUpdate):
    """
    Salva dati natali. Calcola subito ascendente, luna e pianeti natali
    tramite Swiss Ephemeris e li salva su profiles.
    """
    user_id = request.state.user_id
    sb = get_supabase()

    update = body.model_dump(exclude_none=True)

    if body.birth_date:
        update["sun_sign"] = get_sun_sign(body.birth_date)

    if body.birth_date and body.birth_city:
        update["onboarding_completed"] = True

    # Calcola tema natale completo subito
    if body.birth_date and body.birth_lat and body.birth_lng:
        try:
            natal = get_natal_chart(
                birth_date=str(body.birth_date),
                birth_time=str(body.birth_time or "12:00"),
                latitude=float(body.birth_lat),
                longitude=float(body.birth_lng),
                timezone=str(body.birth_timezone or "Europe/Rome")
            )
            if natal:
                # Ascendente
                if natal.get("ascendant") is not None:
                    update["ascendant"] = lon_to_sign(natal["ascendant"])

                # Luna
                planets = natal.get("planets", {})
                moon = planets.get("MOON") or {}
                if moon.get("longitude") is not None:
                    update["moon_sign"] = lon_to_sign(moon["longitude"])

                # Tutti i pianeti natali — letti direttamente dal frontend
                if planets:
                    update["natal_planets_json"] = planets

                # Case natali — già convertite in segni
                houses_raw = natal.get("houses", {})
                cusps = houses_raw.get("cusps") or []
                if cusps and len(cusps) >= 12:
                    update["natal_houses_json"] = {
                        f"house_{i+1}": lon_to_sign(float(cusps[i]))
                        for i in range(12)
                    }
        except Exception as e:
            # Non bloccare la registrazione se il calcolo fallisce
            pass

    sb.table("profiles").update(update).eq("id", user_id).execute()
    return {"ok": True, "sun_sign": update.get("sun_sign")}

@router.post("/me/geo")
async def save_registration_geo(request: Request):
    user_id = request.state.user_id
    ip = (
        request.headers.get("CF-Connecting-IP") or
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or
        request.client.host
    )
    geo = await get_geo_from_ip(ip)
    if geo:
        sb = get_supabase()
        sb.table("profiles").update({"reg_ip": ip, **geo}).eq("id", user_id).execute()
    return {"ok": True, "geo": geo}

@router.patch("/me/life-situation")
async def update_life_situation(request: Request, body: LifeSituationUpdate):
    user_id = request.state.user_id
    sb = get_supabase()
    sb.table("profiles").update({"life_situation": body.model_dump()}).eq("id", user_id).execute()
    return {"ok": True}

# ── PARTNER ─────────────────────────────────────────────────

@router.get("/me/partners")
async def get_partners(request: Request):
    user_id = request.state.user_id
    sb = get_supabase()
    result = sb.table("partner_profiles").select("*").eq("user_id", user_id).eq("is_active", True).execute()
    return result.data

@router.post("/me/partners")
async def create_partner(request: Request, body: PartnerCreate):
    user_id = request.state.user_id
    sb = get_supabase()
    data = body.model_dump(exclude_none=True)
    data["user_id"] = user_id
    if body.birth_date:
        data["sun_sign"] = get_sun_sign(body.birth_date)
    result = sb.table("partner_profiles").insert(data).execute()
    return result.data[0]

@router.patch("/me/partners/{partner_id}")
async def update_partner(request: Request, partner_id: str, body: PartnerCreate):
    user_id = request.state.user_id
    sb = get_supabase()
    data = body.model_dump(exclude_none=True)
    if body.birth_date:
        data["sun_sign"] = get_sun_sign(body.birth_date)
    sb.table("partner_profiles").update(data).eq("id", partner_id).eq("user_id", user_id).execute()
    return {"ok": True}

@router.delete("/me/partners/{partner_id}")
async def delete_partner(request: Request, partner_id: str):
    user_id = request.state.user_id
    sb = get_supabase()
    sb.table("partner_profiles").update({"is_active": False}).eq("id", partner_id).eq("user_id", user_id).execute()
    return {"ok": True}
