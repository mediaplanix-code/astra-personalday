"""
routers/auth.py — Middleware autenticazione JWT Supabase
Verifica il token Bearer chiamando Supabase /auth/v1/user
Compatibile con HS256 (legacy) e ECC P-256 (nuovo sistema)
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import httpx
import jwt
from datetime import datetime

from config import get_settings, get_supabase

router = APIRouter()

# ── ROUTE PUBBLICHE (no auth richiesta) ──────────────────────

PUBLIC_PATHS = {
    "/health",
    "/docs",
    "/openapi.json",
    "/api/webhooks/stripe",
    "/api/telegram/webhook",
}

PUBLIC_PREFIXES = (
    "/api/clienti/",
)

# ── MIDDLEWARE ────────────────────────────────────────────────

class SupabaseAuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware globale: verifica JWT Supabase su ogni richiesta.
    Usa l'API Supabase /auth/v1/user per validare il token —
    compatibile sia con HS256 legacy che con ECC P-256.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error": "Token di autenticazione mancante"}
            )

        token = auth_header.split(" ", 1)[1]
        settings = get_settings()

        # Strategia 1: verifica locale HS256 (legacy, più veloce)
        user_id = None
        user_email = ""
        user_role = "authenticated"

        try:
            payload = jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience="authenticated"
            )
            exp = payload.get("exp", 0)
            if datetime.utcnow().timestamp() > exp:
                return JSONResponse(status_code=401, content={"error": "Token scaduto"})
            user_id = payload.get("sub")
            user_email = payload.get("email", "")
            user_role = payload.get("role", "authenticated")

        except (jwt.InvalidTokenError, Exception):
            # Strategia 2: verifica remota via Supabase API (ECC P-256 e altri)
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.get(
                        f"{settings.supabase_url}/auth/v1/user",
                        headers={
                            "apikey": settings.supabase_service_key,
                            "Authorization": f"Bearer {token}"
                        }
                    )
                if r.status_code != 200:
                    return JSONResponse(
                        status_code=401,
                        content={"error": "Token non valido o scaduto"}
                    )
                data = r.json()
                user_id = data.get("id")
                user_email = data.get("email", "")
                user_role = data.get("role", "authenticated")

            except Exception as e:
                return JSONResponse(
                    status_code=500,
                    content={"error": f"Errore autenticazione: {str(e)}"}
                )

        if not user_id:
            return JSONResponse(status_code=401, content={"error": "Token non valido"})

        request.state.user_id = user_id
        request.state.user_email = user_email
        request.state.user_role = user_role

        return await call_next(request)


# ── ENDPOINTS AUTH ────────────────────────────────────────────

@router.post("/register")
async def register(request: Request):
    body = await request.json()
    email = body.get("email")
    password = body.get("password")

    if not email or not password:
        raise HTTPException(400, "Email e password obbligatorie")

    settings = get_settings()

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{settings.supabase_url}/auth/v1/signup",
            headers={
                "apikey": settings.supabase_service_key,
                "Content-Type": "application/json"
            },
            json={"email": email, "password": password}
        )

    if r.status_code not in (200, 201):
        error = r.json().get("error_description") or r.json().get("msg", "Errore registrazione")
        raise HTTPException(400, error)

    data = r.json()
    return {
        "user_id": data.get("user", {}).get("id"),
        "email": email,
        "message": "Registrazione completata."
    }


@router.post("/login")
async def login(request: Request):
    body = await request.json()
    email = body.get("email")
    password = body.get("password")

    if not email or not password:
        raise HTTPException(400, "Email e password obbligatorie")

    settings = get_settings()

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{settings.supabase_url}/auth/v1/token?grant_type=password",
            headers={
                "apikey": settings.supabase_service_key,
                "Content-Type": "application/json"
            },
            json={"email": email, "password": password}
        )

    if r.status_code != 200:
        raise HTTPException(401, "Email o password non corretti")

    data = r.json()

    sb = get_supabase()
    user_id = data.get("user", {}).get("id")
    if user_id:
        ip = (
            request.headers.get("CF-Connecting-IP") or
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or
            request.client.host
        )
        sb.table("profiles").update({
            "last_login_at": datetime.utcnow().isoformat(),
            "last_login_ip": ip
        }).eq("id", user_id).execute()

    return {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "expires_in": data.get("expires_in"),
        "user_id": user_id
    }


@router.post("/refresh")
async def refresh_token(request: Request):
    body = await request.json()
    refresh_token = body.get("refresh_token")

    if not refresh_token:
        raise HTTPException(400, "refresh_token mancante")

    settings = get_settings()

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{settings.supabase_url}/auth/v1/token?grant_type=refresh_token",
            headers={
                "apikey": settings.supabase_service_key,
                "Content-Type": "application/json"
            },
            json={"refresh_token": refresh_token}
        )

    if r.status_code != 200:
        raise HTTPException(401, "Refresh token non valido o scaduto")

    data = r.json()
    return {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "expires_in": data.get("expires_in")
    }


@router.post("/logout")
async def logout(request: Request):
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.split(" ", 1)[1] if " " in auth_header else ""

    settings = get_settings()

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{settings.supabase_url}/auth/v1/logout",
            headers={
                "apikey": settings.supabase_service_key,
                "Authorization": f"Bearer {token}"
            }
        )

    return {"ok": True, "message": "Logout effettuato"}


@router.get("/me")
async def get_current_user(request: Request):
    return {
        "user_id": request.state.user_id,
        "email": request.state.user_email,
        "role": request.state.user_role
    }
