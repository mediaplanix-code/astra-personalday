"""
routers/profiles.py — Gestione profilo utente e dati natali
"""
from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel
from typing import Optional
from datetime import date, time
import httpx

from config import get_supabase, get_settings

router = APIRouter()

# ── MODELS ──────────────────────────────────────────────────

class BirthDataUpdate(BaseModel):
    birth_date: Optional[date] = None
    birth_time: Optional[str] = None       # "HH:MM"
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

async def get_geo_from_ip(ip: str) -> dict:
    """Geolocalizza IP per CRM. Ritorna city, region, country, postal_code, lat, lng, isp."""
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

def get_sun_sign(birth_date: date) -> str:
    """Calcola segno solare da data di nascita."""
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

# ── ENDPOINTS ───────────────────────────────────────────────

@router.get("/me")
async def get_my_profile(request: Request):
    """Ritorna profilo completo dell'utente autenticato."""
    user_id = request.state.user_id
    sb = get_supabase()
    result = sb.table("profiles").select("*").eq("id", user_id).single().execute()
    if not result.data:
        raise HTTPException(404, "Profilo non trovato")
    return result.data

@router.patch("/me")
async def update_profile(request: Request, body: ProfileUpdate):
    """Aggiorna dati anagrafici e preferenze."""
    user_id = request.state.user_id
    sb = get_supabase()
    sb.table("profiles").update(body.model_dump(exclude_none=True)).eq("id", user_id).execute()
    return {"ok": True}

@router.post("/me/birth-data")
async def save_birth_data(request: Request, body: BirthDataUpdate):
    """
    Salva dati natali. Calcola segno solare.
    Chiamato durante onboarding e aggiornabile in seguito.
    """
    user_id = request.state.user_id
    sb = get_supabase()

    update = body.model_dump(exclude_none=True)

    # Calcola segno solare se data presente
    if body.birth_date:
        update["sun_sign"] = get_sun_sign(body.birth_date)

    # Segna onboarding completato se tutti i campi essenziali presenti
    if body.birth_date and body.birth_city:
        update["onboarding_completed"] = True

    sb.table("profiles").update(update).eq("id", user_id).execute()
    return {"ok": True, "sun_sign": update.get("sun_sign")}

@router.post("/me/geo")
async def save_registration_geo(request: Request):
    """
    Salva geolocalizzazione IP alla registrazione.
    Chiamato automaticamente dal frontend al primo accesso.
    """
    user_id = request.state.user_id
    # Prende IP reale (Cloudflare o proxy)
    ip = (
        request.headers.get("CF-Connecting-IP") or
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or
        request.client.host
    )

    geo = await get_geo_from_ip(ip)
    if geo:
        sb = get_supabase()
        sb.table("profiles").update({
            "reg_ip": ip,
            **geo
        }).eq("id", user_id).execute()

    return {"ok": True, "geo": geo}

@router.patch("/me/life-situation")
async def update_life_situation(request: Request, body: LifeSituationUpdate):
    """Aggiorna contesto di vita — usato da Luna per personalizzare le risposte."""
    user_id = request.state.user_id
    sb = get_supabase()
    sb.table("profiles").update({
        "life_situation": body.model_dump()
    }).eq("id", user_id).execute()
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
