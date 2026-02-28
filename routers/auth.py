"""
routers/auth.py — Middleware autenticazione JWT Supabase
Verifica il token Bearer su ogni richiesta protetta.
Inietta user_id e user_email nel request.state.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import jwt
import httpx
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

# ── MIDDLEWARE ────────────────────────────────────────────────

class SupabaseAuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware globale: verifica JWT Supabase su ogni richiesta.
    Le route pubbliche sono escluse.
    Inietta request.state.user_id e request.state.user_email.
    """

    async def dispatch(self, request: Request, call_next):
        # Lascia passare route pubbliche e OPTIONS (CORS preflight)
        if request.method == "OPTIONS" or request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        # Estrai token dall'header Authorization
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error": "Token di autenticazione mancante"}
            )

        token = auth_header.split(" ", 1)[1]

        try:
            settings = get_settings()

            # Decodifica e verifica il JWT con il secret di Supabase
            payload = jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience="authenticated"
            )

            # Verifica scadenza
            exp = payload.get("exp", 0)
            if datetime.utcnow().timestamp() > exp:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Token scaduto"}
                )

            # Inietta nel request.state
            request.state.user_id = payload.get("sub")
            request.state.user_email = payload.get("email", "")
            request.state.user_role = payload.get("role", "authenticated")

        except jwt.ExpiredSignatureError:
            return JSONResponse(
                status_code=401,
                content={"error": "Token scaduto — effettua di nuovo il login"}
            )
        except jwt.InvalidTokenError as e:
            return JSONResponse(
                status_code=401,
                content={"error": f"Token non valido: {str(e)}"}
            )
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": f"Errore autenticazione: {str(e)}"}
            )

        return await call_next(request)


# ── ENDPOINTS AUTH ────────────────────────────────────────────

@router.post("/register")
async def register(request: Request):
    """
    Registrazione nuovo utente.
    Il frontend usa direttamente Supabase Auth JS SDK.
    Questo endpoint è un helper opzionale per la registrazione server-side
    con geo-detection immediata.
    """
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
        "message": "Registrazione completata. Controlla la tua email per confermare l'account."
    }


@router.post("/login")
async def login(request: Request):
    """
    Login con email e password.
    Ritorna access_token da usare come Bearer nelle richieste successive.
    """
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

    # Aggiorna last_login
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
    """Rinnova il token usando il refresh_token."""
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
    """Invalida il token corrente su Supabase."""
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
    """Ritorna info utente autenticato dal token."""
    return {
        "user_id": request.state.user_id,
        "email": request.state.user_email,
        "role": request.state.user_role
    }
