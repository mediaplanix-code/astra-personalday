"""
routers/admin.py — Pannello CRM Admin
Accesso solo per email admin configurate nelle env vars
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from config import get_supabase, get_settings

router = APIRouter()

# ── MIDDLEWARE ADMIN ──────────────────────────────────────────

def require_admin(request: Request):
    settings = get_settings()
    admin_emails = [e.strip() for e in settings.admin_emails.split(",")]
    user_email = getattr(request.state, "user_email", None)
    if not user_email or user_email not in admin_emails:
        raise HTTPException(403, "Accesso riservato agli amministratori")

# ── MODELS ──────────────────────────────────────────────────

class SubscriptionOverride(BaseModel):
    plan: str
    status: str
    luna_minutes_add: Optional[int] = 0
    admin_notes: Optional[str] = None

class UserNote(BaseModel):
    note: str

class SegmentCreate(BaseModel):
    name: str
    description: Optional[str] = None
    target_countries: Optional[List[str]] = []
    target_regions: Optional[List[str]] = []
    target_postal_codes: Optional[List[str]] = []
    target_plans: Optional[List[str]] = []
    offer_type: Optional[str] = None
    offer_payload: Optional[dict] = {}
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None

# ── DASHBOARD ────────────────────────────────────────────────

@router.get("/dashboard")
async def admin_dashboard(request: Request):
    """KPI principali per la dashboard CRM."""
    require_admin(request)
    sb = get_supabase()

    # Statistiche aggregate
    total_users = len(sb.table("profiles").select("id").execute().data)
    active_subs = len(sb.table("subscriptions").select("id").eq("status", "active").execute().data)
    trial_subs = len(sb.table("subscriptions").select("id").eq("status", "trial").execute().data)
    expired_subs = len(sb.table("subscriptions").select("id").eq("status", "expired").execute().data)
    telegram_connected = len(sb.table("telegram_connections").select("id").eq("status", "active").execute().data)

    # Sessioni Luna oggi
    today = datetime.utcnow().date().isoformat()
    luna_today = len(sb.table("luna_sessions").select("id").gte("created_at", today).execute().data)

    # Oroscopi generati oggi
    horoscopes_today = len(sb.table("daily_horoscopes").select("id").eq("horoscope_date", today).execute().data)

    # Ultimi job scheduler
    last_jobs = sb.table("scheduler_jobs").select("*").order("created_at", desc=True).limit(5).execute().data

    return {
        "users": {
            "total": total_users,
            "trial": trial_subs,
            "active": active_subs,
            "expired": expired_subs,
            "telegram_connected": telegram_connected
        },
        "today": {
            "luna_sessions": luna_today,
            "horoscopes_generated": horoscopes_today
        },
        "last_jobs": last_jobs
    }

# ── UTENTI ───────────────────────────────────────────────────

@router.get("/users")
async def list_users(
    request: Request,
    page: int = 1,
    limit: int = 50,
    country: Optional[str] = None,
    plan: Optional[str] = None,
    search: Optional[str] = None
):
    """Lista utenti con filtri. Usa vista CRM con dati geografici."""
    require_admin(request)
    sb = get_supabase()

    query = sb.table("crm_users").select("*")

    if country:
        query = query.eq("reg_country", country)
    if plan:
        query = query.eq("plan", plan)
    if search:
        query = query.or_(f"email.ilike.%{search}%,first_name.ilike.%{search}%,last_name.ilike.%{search}%")

    offset = (page - 1) * limit
    result = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()

    return {"users": result.data, "page": page, "limit": limit}

@router.get("/users/{user_id}")
async def get_user_detail(request: Request, user_id: str):
    """Dettaglio completo utente per il CRM."""
    require_admin(request)
    sb = get_supabase()

    profile = sb.table("profiles").select("*").eq("id", user_id).single().execute().data
    subscription = sb.table("subscriptions").select("*").eq("user_id", user_id).single().execute().data
    partners = sb.table("partner_profiles").select("*").eq("user_id", user_id).execute().data
    telegram = sb.table("telegram_connections").select("*").eq("user_id", user_id).single().execute().data
    luna_sessions = sb.table("luna_sessions").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(10).execute().data
    orders = sb.table("services_orders").select("*").eq("user_id", user_id).order("created_at", desc=True).execute().data
    reports = sb.table("generated_reports").select("id, service_type, created_at, status").eq("user_id", user_id).execute().data

    return {
        "profile": profile,
        "subscription": subscription,
        "partners": partners,
        "telegram": telegram,
        "luna_sessions": luna_sessions,
        "orders": orders,
        "reports": reports
    }

@router.patch("/users/{user_id}/subscription")
async def override_subscription(request: Request, user_id: str, body: SubscriptionOverride):
    """Override manuale abbonamento utente (admin)."""
    require_admin(request)
    sb = get_supabase()

    update = {
        "plan": body.plan,
        "status": body.status,
        "manually_managed": True,
        "admin_notes": body.admin_notes
    }
    sb.table("subscriptions").update(update).eq("user_id", user_id).execute()

    if body.luna_minutes_add and body.luna_minutes_add > 0:
        sb.rpc("add_luna_minutes", {"p_user_id": user_id, "p_minutes": body.luna_minutes_add}).execute()

    # Log azione admin
    sb.table("admin_actions").insert({
        "admin_user_id": request.state.user_id,
        "target_user_id": user_id,
        "action_type": "subscription_override",
        "description": f"Piano: {body.plan}, Stato: {body.status}",
        "payload": body.model_dump()
    }).execute()

    return {"ok": True}

@router.post("/users/{user_id}/ban")
async def ban_user(request: Request, user_id: str):
    """Disattiva account utente."""
    require_admin(request)
    sb = get_supabase()
    sb.table("profiles").update({"is_active": False}).eq("id", user_id).execute()
    sb.table("admin_actions").insert({
        "admin_user_id": request.state.user_id,
        "target_user_id": user_id,
        "action_type": "ban",
        "description": "Account disattivato"
    }).execute()
    return {"ok": True}

# ── GEOGRAFIA / CRM ──────────────────────────────────────────

@router.get("/geo/summary")
async def geo_summary(request: Request):
    """Riepilogo geografico utenti per targeting offerte."""
    require_admin(request)
    sb = get_supabase()
    result = sb.table("crm_geo_summary").select("*").execute()
    return result.data

@router.get("/geo/users-by-area")
async def users_by_area(request: Request, country: str, region: Optional[str] = None, postal_code: Optional[str] = None):
    """Lista utenti per area geografica specifica."""
    require_admin(request)
    sb = get_supabase()

    query = sb.table("crm_users").select("id, first_name, last_name, email, plan, sub_status, reg_city, reg_postal_code").eq("reg_country", country)
    if region:
        query = query.eq("reg_region", region)
    if postal_code:
        query = query.eq("reg_postal_code", postal_code)

    result = query.execute()
    return result.data

# ── SEGMENTI MARKETING ───────────────────────────────────────

@router.get("/segments")
async def list_segments(request: Request):
    require_admin(request)
    sb = get_supabase()
    return sb.table("marketing_segments").select("*").execute().data

@router.post("/segments")
async def create_segment(request: Request, body: SegmentCreate):
    """Crea segmento per offerte territoriali."""
    require_admin(request)
    sb = get_supabase()
    result = sb.table("marketing_segments").insert(body.model_dump(exclude_none=True)).execute()
    return result.data[0]

@router.post("/segments/{segment_id}/assign")
async def assign_users_to_segment(request: Request, segment_id: str):
    """
    Assegna automaticamente gli utenti al segmento
    in base ai filtri geografici e di piano.
    """
    require_admin(request)
    sb = get_supabase()

    segment = sb.table("marketing_segments").select("*").eq("id", segment_id).single().execute().data
    if not segment:
        raise HTTPException(404, "Segmento non trovato")

    # Costruisci query utenti
    query = sb.table("crm_users").select("id, reg_country, reg_region, reg_postal_code, plan")

    if segment.get("target_countries"):
        query = query.in_("reg_country", segment["target_countries"])
    if segment.get("target_plans"):
        query = query.in_("plan", segment["target_plans"])

    users = query.execute().data

    assigned = 0
    for user in users:
        try:
            sb.table("user_segment_assignments").upsert({
                "user_id": user["id"],
                "segment_id": segment_id
            }).execute()
            assigned += 1
        except Exception:
            pass

    return {"ok": True, "assigned": assigned}

# ── SCHEDULER LOGS ───────────────────────────────────────────

@router.get("/scheduler/logs")
async def scheduler_logs(request: Request, limit: int = 20):
    """Log dei job schedulati."""
    require_admin(request)
    sb = get_supabase()
    result = sb.table("scheduler_jobs").select("*").order("created_at", desc=True).limit(limit).execute()
    return result.data
