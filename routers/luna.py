"""
routers/luna.py — Sessioni Luna AI con timer e gestione minuti
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
import anthropic
from datetime import datetime

from config import get_supabase, get_settings

router = APIRouter()

# ── SYSTEM PROMPT LUNA ───────────────────────────────────────

LUNA_SYSTEM_PROMPT = """Sei Luna, una consulente astrologica AI empatica, perspicace e autentica.

CARATTERE:
- Parli in italiano, con calore e profondità
- Non sei generica: ogni risposta è personalizzata sui dati reali dell'utente
- Sei diretta quando serve, gentile sempre
- Non inventi — se non hai dati sufficienti, lo dici

DATI A TUA DISPOSIZIONE:
{user_context}

COME USI I DATI:
- Conosci il tema natale dell'utente → puoi parlare di pianeti, case, aspetti
- Conosci la situazione di vita → puoi contestualizzare ogni risposta
- Se c'è un partner → puoi analizzare la dinamica di coppia
- Sai i transiti del momento → puoi dire cosa sta succedendo ORA nel cielo

REGOLE:
- Non inventare mai posizioni planetarie che non ti sono state fornite
- Non fare diagnosi mediche o consulenze legali
- Se la domanda è fuori dall'astrologia, porta gentilmente il discorso sui temi astrologici
- Risposte concise ma ricche (max 200 parole per messaggio, salvo richiesta diversa)
- Timer: sei consapevole che la sessione ha un tempo limitato — aiuta l'utente a usarlo bene"""

# ── MODELS ──────────────────────────────────────────────────

class StartSession(BaseModel):
    partner_id: Optional[str] = None
    use_voice: bool = False

class SendMessage(BaseModel):
    session_id: str
    message: str

class EndSession(BaseModel):
    session_id: str

# ── HELPERS ─────────────────────────────────────────────────

async def build_user_context(user_id: str, partner_id: Optional[str] = None) -> str:
    """Costruisce il contesto completo dell'utente per Luna."""
    sb = get_supabase()

    # Profilo utente
    profile = sb.table("profiles").select("*").eq("id", user_id).single().execute().data
    if not profile:
        return "Dati utente non disponibili."

    ctx = f"""
UTENTE:
- Nome: {profile.get('first_name', 'N/D')} {profile.get('last_name', '')}
- Data nascita: {profile.get('birth_date', 'N/D')}
- Ora nascita: {profile.get('birth_time', 'sconosciuta')}
- Luogo nascita: {profile.get('birth_city', 'N/D')}, {profile.get('birth_country', '')}
- Segno Solare: {profile.get('sun_sign', 'N/D')}
- Luna: {profile.get('moon_sign', 'N/D')}
- Ascendente: {profile.get('ascendant', 'N/D')}

SITUAZIONE DI VITA:
{profile.get('life_situation', {})}
"""

    # Dati partner se richiesti
    if partner_id:
        partner = sb.table("partner_profiles").select("*").eq("id", partner_id).single().execute().data
        if partner:
            ctx += f"""
PARTNER:
- Nome: {partner.get('name', 'N/D')}
- Tipo relazione: {partner.get('relationship_type', 'N/D')}
- Data nascita: {partner.get('birth_date', 'N/D')}
- Segno Solare: {partner.get('sun_sign', 'N/D')}
- Luna: {partner.get('moon_sign', 'N/D')}
- Ascendente: {partner.get('ascendant', 'N/D')}
"""

    # Storico sessioni precedenti (ultimi 3 sommari)
    prev_sessions = sb.table("luna_sessions").select("created_at, duration_minutes, messages_count").eq("user_id", user_id).eq("status", "ended").order("created_at", desc=True).limit(3).execute().data
    if prev_sessions:
        ctx += f"\nSESSIONI PRECEDENTI: {len(prev_sessions)} sessioni totali. Ultima: {prev_sessions[0].get('created_at', '')[:10]}"

    return ctx

async def generate_voice(text: str, settings) -> Optional[str]:
    """Genera audio con ElevenLabs. Ritorna URL o None se fallisce."""
    try:
        import httpx
        headers = {
            "xi-api-key": settings.elevenlabs_api_key,
            "Content-Type": "application/json"
        }
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{settings.elevenlabs_voice_id_luna}",
                headers=headers,
                json=payload
            )
            if r.status_code == 200:
                # In produzione: salva su Supabase Storage e ritorna URL pubblico
                # Per ora ritorna placeholder
                return "audio_generated"
        return None
    except Exception:
        return None

# ── ENDPOINTS ───────────────────────────────────────────────

@router.post("/session/start")
async def start_session(request: Request, body: StartSession):
    """
    Avvia sessione Luna. Verifica saldo minuti.
    Ritorna session_id e minuti disponibili.
    """
    user_id = request.state.user_id
    sb = get_supabase()

    # Verifica saldo minuti
    sub = sb.table("subscriptions").select("luna_minutes_balance, status").eq("user_id", user_id).single().execute().data
    if not sub:
        raise HTTPException(403, "Nessun abbonamento trovato")

    balance = sub.get("luna_minutes_balance", 0)
    if balance < 1:
        raise HTTPException(402, "Minuti Luna esauriti. Acquista un pacchetto per continuare.")

    # Costruisci contesto
    context = await build_user_context(user_id, body.partner_id)

    # Crea sessione
    session = sb.table("luna_sessions").insert({
        "user_id": user_id,
        "partner_id": body.partner_id,
        "status": "active",
        "voice_used": body.use_voice,
        "context_snapshot": {"context": context},
    }).execute().data[0]

    return {
        "session_id": session["id"],
        "minutes_available": balance,
        "started_at": session["created_at"]
    }

@router.post("/message")
async def send_message(request: Request, body: SendMessage):
    """
    Invia messaggio a Luna nella sessione attiva.
    Verifica che la sessione sia attiva e i minuti non siano esauriti.
    """
    user_id = request.state.user_id
    sb = get_supabase()
    settings = get_settings()

    # Verifica sessione
    session = sb.table("luna_sessions").select("*").eq("id", body.session_id).eq("user_id", user_id).eq("status", "active").single().execute().data
    if not session:
        raise HTTPException(404, "Sessione non trovata o già terminata")

    # Verifica saldo minuti (ricontrolla ad ogni messaggio)
    sub = sb.table("subscriptions").select("luna_minutes_balance").eq("user_id", user_id).single().execute().data
    if not sub or sub["luna_minutes_balance"] < 1:
        # Pausa sessione per minuti esauriti
        sb.table("luna_sessions").update({
            "status": "paused",
            "ended_reason": "time_expired"
        }).eq("id", body.session_id).execute()
        raise HTTPException(402, "Minuti Luna esauriti. Acquista un pacchetto per continuare.")

    # Recupera storico messaggi sessione
    history = sb.table("luna_messages").select("role, content").eq("session_id", body.session_id).order("created_at").execute().data

    # Costruisci messaggi per Claude
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": body.message})

    # Contesto utente dalla sessione
    context = session.get("context_snapshot", {}).get("context", "")
    system = LUNA_SYSTEM_PROMPT.format(user_context=context)

    # Chiama Claude
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=system,
        messages=messages
    )
    reply_text = response.content[0].text
    tokens_used = response.usage.input_tokens + response.usage.output_tokens

    # Salva messaggio utente
    sb.table("luna_messages").insert({
        "session_id": body.session_id,
        "user_id": user_id,
        "role": "user",
        "content": body.message,
    }).execute()

    # Genera voce se richiesta
    audio_url = None
    if session.get("voice_used"):
        audio_url = await generate_voice(reply_text, settings)

    # Salva risposta Luna
    sb.table("luna_messages").insert({
        "session_id": body.session_id,
        "user_id": user_id,
        "role": "assistant",
        "content": reply_text,
        "audio_url": audio_url,
        "tokens_used": tokens_used
    }).execute()

    # Aggiorna contatore messaggi sessione
    sb.table("luna_sessions").update({
        "messages_count": (session.get("messages_count") or 0) + 1
    }).eq("id", body.session_id).execute()

    # Saldo minuti aggiornato
    updated_sub = sb.table("subscriptions").select("luna_minutes_balance").eq("user_id", user_id).single().execute().data

    return {
        "reply": reply_text,
        "audio_url": audio_url,
        "minutes_remaining": updated_sub.get("luna_minutes_balance", 0)
    }

@router.post("/session/end")
async def end_session(request: Request, body: EndSession):
    """
    Termina sessione e addebita i minuti usati.
    Calcola durata reale e scala dal saldo.
    """
    user_id = request.state.user_id
    sb = get_supabase()

    session = sb.table("luna_sessions").select("*").eq("id", body.session_id).eq("user_id", user_id).single().execute().data
    if not session:
        raise HTTPException(404, "Sessione non trovata")

    # Calcola durata in minuti (arrotondamento per eccesso)
    started = datetime.fromisoformat(session["created_at"].replace("Z", "+00:00"))
    duration_sec = (datetime.now(started.tzinfo) - started).total_seconds()
    duration_min = max(1, int(duration_sec / 60) + (1 if duration_sec % 60 > 0 else 0))

    # Addebita minuti
    sb.rpc("deduct_luna_minutes", {"p_user_id": user_id, "p_minutes": duration_min}).execute()

    # Chiudi sessione
    sb.table("luna_sessions").update({
        "status": "ended",
        "ended_at": datetime.utcnow().isoformat(),
        "ended_reason": "user_ended",
        "duration_minutes": duration_min,
        "minutes_charged": duration_min
    }).eq("id", body.session_id).execute()

    # Saldo aggiornato
    sub = sb.table("subscriptions").select("luna_minutes_balance").eq("user_id", user_id).single().execute().data

    return {
        "ok": True,
        "duration_minutes": duration_min,
        "minutes_charged": duration_min,
        "minutes_remaining": sub.get("luna_minutes_balance", 0)
    }

@router.get("/balance")
async def get_luna_balance(request: Request):
    """Ritorna saldo minuti Luna dell'utente."""
    user_id = request.state.user_id
    sb = get_supabase()
    sub = sb.table("subscriptions").select("luna_minutes_balance, luna_minutes_used").eq("user_id", user_id).single().execute().data
    return {
        "balance": sub.get("luna_minutes_balance", 0) if sub else 0,
        "used_total": sub.get("luna_minutes_used", 0) if sub else 0
    }
