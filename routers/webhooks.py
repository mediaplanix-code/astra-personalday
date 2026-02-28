"""
routers/webhooks.py — Stripe webhooks per pagamenti automatici
routers/services.py — Acquisto pacchetti minuti Luna e servizi
"""
from fastapi import APIRouter, HTTPException, Request, Header
from pydantic import BaseModel
from typing import Optional
import stripe
import json

from config import get_supabase, get_settings

router = APIRouter()

# ── PACCHETTI MINUTI ─────────────────────────────────────────

LUNA_PACKS = {
    "15min":   {"minutes": 15,  "price": 2.99, "name": "15 minuti con Luna"},
    "30min":   {"minutes": 30,  "price": 4.99, "name": "30 minuti con Luna"},
    "60min":   {"minutes": 60,  "price": 8.99, "name": "60 minuti con Luna"},
    "monthly": {"minutes": 300, "price": 19.99, "name": "Abbonamento mensile Luna (5h)"},
}

# ── MODELS ──────────────────────────────────────────────────

class CreateCheckout(BaseModel):
    pack_id: str  # '15min', '30min', '60min', 'monthly'

class CreateServiceCheckout(BaseModel):
    service_type: str
    partner_id: Optional[str] = None

# ── ACQUISTO PACCHETTI LUNA ──────────────────────────────────

router_services = APIRouter()

@router_services.post("/luna/checkout")
async def create_luna_checkout(request: Request, body: CreateCheckout):
    """Crea sessione Stripe per acquisto pacchetto minuti Luna."""
    user_id = request.state.user_id
    settings = get_settings()

    pack = LUNA_PACKS.get(body.pack_id)
    if not pack:
        raise HTTPException(400, "Pacchetto non valido")

    sb = get_supabase()
    profile = sb.table("profiles").select("email").eq("id", user_id).single().execute().data

    stripe.api_key = settings.stripe_secret_key

    # Recupera o crea customer Stripe
    sub = sb.table("subscriptions").select("stripe_customer_id").eq("user_id", user_id).single().execute().data
    customer_id = sub.get("stripe_customer_id") if sub else None

    if not customer_id:
        customer = stripe.Customer.create(email=profile["email"], metadata={"user_id": user_id})
        customer_id = customer.id
        sb.table("subscriptions").update({"stripe_customer_id": customer_id}).eq("user_id", user_id).execute()

    # Crea ordine nel DB
    order = sb.table("luna_minute_packs").insert({
        "user_id": user_id,
        "pack_name": body.pack_id,
        "minutes_purchased": pack["minutes"],
        "price_eur": pack["price"],
        "order_status": "pending"
    }).execute().data[0]

    # Crea sessione Stripe
    session = stripe.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "eur",
                "product_data": {"name": pack["name"]},
                "unit_amount": int(pack["price"] * 100),
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=f"{settings.frontend_url}/luna?success=true",
        cancel_url=f"{settings.frontend_url}/luna?cancelled=true",
        metadata={
            "user_id": user_id,
            "order_id": order["id"],
            "pack_id": body.pack_id,
            "minutes": pack["minutes"]
        }
    )

    # Aggiorna ordine con session id
    sb.table("luna_minute_packs").update({"stripe_session_id": session.id}).eq("id", order["id"]).execute()

    return {"checkout_url": session.url}


# ── STRIPE WEBHOOK ────────────────────────────────────────────

@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="stripe-signature")
):
    """
    Riceve eventi Stripe. Gestisce:
    - checkout.session.completed → accredita minuti Luna o attiva servizio
    - customer.subscription.deleted → cancella abbonamento
    - invoice.payment_failed → notifica utente
    """
    settings = get_settings()
    stripe.api_key = settings.stripe_secret_key

    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, settings.stripe_webhook_secret
        )
    except Exception as e:
        raise HTTPException(400, f"Webhook signature invalida: {str(e)}")

    sb = get_supabase()

    # ── Pagamento completato ─────────────────────────────────
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata", {})
        user_id = meta.get("user_id")

        if not user_id:
            return {"ok": True}

        # Pacchetto minuti Luna
        if meta.get("pack_id"):
            minutes = int(meta.get("minutes", 0))
            order_id = meta.get("order_id")

            # Aggiorna ordine come pagato
            sb.table("luna_minute_packs").update({
                "order_status": "paid",
                "stripe_payment_intent": session.get("payment_intent"),
                "paid_at": "NOW()",
                "is_consumed": False
            }).eq("id", order_id).execute()

            # Aggiungi minuti al saldo
            sb.rpc("add_luna_minutes", {"p_user_id": user_id, "p_minutes": minutes}).execute()

        # Servizio singolo
        elif meta.get("service_type"):
            service_order_id = meta.get("order_id")
            sb.table("services_orders").update({
                "order_status": "paid",
                "paid_at": "NOW()",
                "status": "pending"  # trigger generazione report
            }).eq("id", service_order_id).execute()

    # ── Abbonamento cancellato ────────────────────────────────
    elif event["type"] == "customer.subscription.deleted":
        stripe_sub_id = event["data"]["object"]["id"]
        sb.table("subscriptions").update({
            "status": "cancelled",
            "cancelled_at": "NOW()"
        }).eq("stripe_subscription_id", stripe_sub_id).execute()

    # ── Pagamento fallito ─────────────────────────────────────
    elif event["type"] == "invoice.payment_failed":
        stripe_customer_id = event["data"]["object"]["customer"]
        sb.table("subscriptions").update({
            "status": "paused"
        }).eq("stripe_customer_id", stripe_customer_id).execute()

    return {"ok": True}
