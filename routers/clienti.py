"""
routers/clienti.py — Iscrizioni clienti senza autenticazione
Nessuna dipendenza da auth.users — tabella clienti separata
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import date
import httpx

from config import get_supabase, get_settings

router = APIRouter()

# ── MODELS ──────────────────────────────────────────────────

class IscrizioneRequest(BaseModel):
    nome: str
    email: EmailStr
    birth_date: date
    birth_time: Optional[str] = None      # "HH:MM"
    birth_city: str
    birth_country: Optional[str] = "IT"
    delivery_channel: Optional[str] = "telegram"

class ClienteUpdate(BaseModel):
    nome: Optional[str] = None
    birth_time: Optional[str] = None
    birth_city: Optional[str] = None
    delivery_channel: Optional[str] = None
    delivery_time: Optional[str] = None
    voice_enabled: Optional[bool] = None

# ── HELPERS ─────────────────────────────────────────────────

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

async def get_geo_from_ip(ip: str) -> dict:
    """Geolocalizza IP per CRM."""
    try:
        settings = get_settings()
        url = f"https://ipapi.co/{ip}/json/"
        if settings.ipapi_key:
            url += f"?key={settings.ipapi_key}"
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url)
            d = r.json()
            return {
                "reg_ip": ip,
                "reg_city": d.get("city", ""),
                "reg_country": d.get("country_code", ""),
            }
    except Exception:
        return {"reg_ip": ip}

# ── ENDPOINTS ───────────────────────────────────────────────

@router.post("/iscrivi")
async def iscrivi_cliente(request: Request, body: IscrizioneRequest):
    """
    Iscrive un nuovo cliente.
    Nessuna autenticazione richiesta — salva direttamente in tabella clienti.
    """
    sb = get_supabase()

    # Controlla se email già presente
    existing = sb.table("clienti").select("id,status").eq("email", str(body.email)).execute()
    if existing.data:
        c = existing.data[0]
        return {
            "ok": True,
            "nuovo": False,
            "cliente_id": c["id"],
            "messaggio": "Sei già iscritto! Controlla il tuo Telegram."
        }

    # Calcola segno solare
    sun_sign = get_sun_sign(body.birth_date)

    # Geolocalizzazione IP
    ip = (
        request.headers.get("CF-Connecting-IP") or
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or
        request.client.host
    )
    geo = await get_geo_from_ip(ip)

    # Salva cliente
    data = {
        "nome": body.nome.strip(),
        "email": str(body.email).lower().strip(),
        "birth_date": str(body.birth_date),
        "birth_time": body.birth_time,
        "birth_city": body.birth_city.strip(),
        "birth_country": body.birth_country or "IT",
        "sun_sign": sun_sign,
        "delivery_channel": body.delivery_channel or "telegram",
        "status": "trial",
        "plan": "free",
        "luna_minutes_balance": 15,
        "source": "web",
        **geo
    }

    result = sb.table("clienti").insert(data).execute()
    if not result.data:
        raise HTTPException(500, "Errore durante l'iscrizione, riprova.")

    cliente = result.data[0]

    return {
        "ok": True,
        "nuovo": True,
        "cliente_id": cliente["id"],
        "sun_sign": sun_sign,
        "messaggio": f"Benvenuto {body.nome.split()[0]}! Ora collega il bot Telegram per ricevere il tuo oroscopo ogni mattina."
    }


@router.get("/verifica/{email}")
async def verifica_cliente(email: str):
    """Verifica se un'email è già iscritta."""
    sb = get_supabase()
    result = sb.table("clienti").select("id,nome,sun_sign,status").eq("email", email.lower()).execute()
    if not result.data:
        return {"iscritto": False}
    c = result.data[0]
    return {
        "iscritto": True,
        "nome": c["nome"],
        "sun_sign": c["sun_sign"],
        "status": c["status"]
    }


@router.get("/me/{cliente_id}")
async def get_cliente(cliente_id: str):
    """Ritorna dati del cliente per personalizzazione frontend."""
    sb = get_supabase()
    result = sb.table("clienti").select(
        "id,nome,sun_sign,birth_date,birth_city,telegram_chat_id,delivery_channel,status,plan,luna_minutes_balance"
    ).eq("id", cliente_id).execute()
    if not result.data:
        raise HTTPException(404, "Cliente non trovato")
    return result.data[0]


@router.patch("/me/{cliente_id}")
async def aggiorna_cliente(cliente_id: str, body: ClienteUpdate):
    """Aggiorna preferenze cliente."""
    sb = get_supabase()
    data = body.model_dump(exclude_none=True)
    if not data:
        return {"ok": True}
    sb.table("clienti").update(data).eq("id", cliente_id).execute()
    return {"ok": True}


# ── ADMIN endpoints (protetti da APP_SECRET_KEY) ─────────────

@router.get("/admin/lista")
async def admin_lista_clienti(request: Request):
    """CRM: lista tutti i clienti. Richiede header x-admin-key."""
    settings = get_settings()
    if request.headers.get("x-admin-key") != settings.app_secret_key:
        raise HTTPException(403, "Non autorizzato")
    sb = get_supabase()
    result = sb.table("crm_clienti").select("*").execute()
    return result.data


@router.post("/admin/attiva/{cliente_id}")
async def admin_attiva(request: Request, cliente_id: str, giorni: int = 30, plan: str = "base", note: str = ""):
    """CRM: attiva abbonamento cliente."""
    settings = get_settings()
    if request.headers.get("x-admin-key") != settings.app_secret_key:
        raise HTTPException(403, "Non autorizzato")
    sb = get_supabase()
    sb.rpc("attiva_abbonamento", {
        "p_cliente_id": cliente_id,
        "p_plan": plan,
        "p_giorni": giorni,
        "p_note": note
    }).execute()
    return {"ok": True}


@router.post("/admin/sospendi/{cliente_id}")
async def admin_sospendi(request: Request, cliente_id: str, note: str = ""):
    """CRM: sospendi cliente."""
    settings = get_settings()
    if request.headers.get("x-admin-key") != settings.app_secret_key:
        raise HTTPException(403, "Non autorizzato")
    sb = get_supabase()
    sb.rpc("sospendi_cliente", {"p_cliente_id": cliente_id, "p_note": note}).execute()
    return {"ok": True}


@router.post("/admin/luna-minuti/{cliente_id}")
async def admin_aggiungi_minuti(request: Request, cliente_id: str, minuti: int = 15):
    """CRM: aggiungi minuti Luna a un cliente."""
    settings = get_settings()
    if request.headers.get("x-admin-key") != settings.app_secret_key:
        raise HTTPException(403, "Non autorizzato")
    sb = get_supabase()
    sb.rpc("aggiungi_minuti_luna", {"p_cliente_id": cliente_id, "p_minuti": minuti}).execute()
    return {"ok": True}
