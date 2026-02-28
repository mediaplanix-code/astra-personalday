"""
config.py — Configurazione centralizzata e client Supabase
"""
from pydantic_settings import BaseSettings
from supabase import create_client, Client
from functools import lru_cache
import os

class Settings(BaseSettings):
    supabase_url: str
    supabase_service_key: str
    supabase_jwt_secret: str
    anthropic_api_key: str
    elevenlabs_api_key: str
    elevenlabs_voice_id_luna: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_price_15min: str
    stripe_price_30min: str
    stripe_price_60min: str
    stripe_price_monthly: str
    telegram_bot_token: str
    nocturna_api_url: str
    nocturna_service_token: str
    ipapi_key: str = ""
    app_env: str = "production"
    app_secret_key: str
    admin_emails: str = ""
    frontend_url: str

    class Config:
        env_file = ".env"

@lru_cache()
def get_settings() -> Settings:
    return Settings()

def get_supabase() -> Client:
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_key)
