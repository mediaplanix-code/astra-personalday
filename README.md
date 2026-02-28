# Astra Personal — Backend API
## Repository: astra-personalday

---

## Struttura cartelle

```
astra-personalday/
├── main.py                    # Entry point FastAPI
├── config.py                  # Settings e client Supabase
├── requirements.txt           # Dipendenze Python
├── render.yaml                # Deploy Render (web + 3 cron jobs)
├── .env.example               # Template variabili d'ambiente
├── .gitignore                 # NON committare .env
│
└── routers/
    ├── __init__.py
    ├── auth.py                # Middleware autenticazione JWT Supabase
    ├── profiles.py            # Profilo utente, dati natali, partner, geo
    ├── horoscope.py           # Oroscopo giornaliero personalizzato
    ├── luna.py                # Sessioni Luna AI con timer minuti
    ├── services.py            # Acquisto servizi (tema natale, ecc.)
    ├── webhooks.py            # Stripe webhooks
    ├── telegram.py            # Connessione bot Telegram
    ├── admin.py               # CRM admin panel
    └── scheduler.py           # Job giornalieri (genera + invia)
```

---

## Setup locale

```bash
# 1. Clona il repository
git clone https://github.com/mediaplanix-code/astra-personalday.git
cd astra-personalday

# 2. Crea ambiente virtuale
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Installa dipendenze
pip install -r requirements.txt

# 4. Copia e compila le variabili d'ambiente
cp .env.example .env
# Edita .env con i tuoi valori reali

# 5. Avvia il server
uvicorn main:app --reload --port 8000
```

Documentazione API disponibile su: http://localhost:8000/docs

---

## Deploy su Render

1. Connetti il repository GitHub a Render
2. Render legge automaticamente `render.yaml`
3. Aggiunge le variabili d'ambiente nel pannello Render (Environment)
4. Deploy automatico ad ogni push su `main`

Il `render.yaml` crea automaticamente:
- **1 Web Service** — API FastAPI (sempre attivo)
- **3 Cron Jobs** — generazione oroscopi, push Telegram, controllo trial

---

## Flusso principale

```
1. Utente si registra → Supabase Auth crea user
   → Trigger SQL crea profilo + abbonamento trial 30gg + 15 min Luna omaggio

2. Utente completa onboarding → inserisce data/ora/luogo nascita
   → Backend salva dati natali + geolocalizzazione IP (per CRM)
   → Calcola segno solare

3. Ogni notte alle 05:00 (Cron Job 1):
   → Nocturna-calculations calcola posizioni planetarie
   → Claude genera oroscopo personalizzato per ogni utente
   → Salva in daily_horoscopes

4. Ogni mattina alle 07:00 (Cron Job 2):
   → Invia oroscopo via Telegram a chi è connesso

5. Utente apre il sito → vede oroscopo già pronto

6. Utente vuole parlare con Luna:
   → Verifica saldo minuti → avvia sessione
   → Timer visibile → messaggi → Luna risponde con contesto completo
   → Fine sessione → addebita minuti usati

7. Minuti esauriti → checkout Stripe → webhook → accredita minuti

8. Admin accede a /api/admin → vede CRM completo
   → Utenti per area geografica → crea segmenti → invia offerte
```

---

## Variabili d'ambiente necessarie

| Variabile | Dove si trova |
|---|---|
| SUPABASE_URL | Supabase → Project Settings → API |
| SUPABASE_SERVICE_KEY | Supabase → Project Settings → API → service_role |
| SUPABASE_JWT_SECRET | Supabase → Project Settings → API → JWT Secret |
| ANTHROPIC_API_KEY | console.anthropic.com |
| ELEVENLABS_API_KEY | elevenlabs.io → Profile |
| ELEVENLABS_VOICE_ID_LUNA | elevenlabs.io → Voices |
| STRIPE_SECRET_KEY | dashboard.stripe.com → Developers → API Keys |
| STRIPE_WEBHOOK_SECRET | dashboard.stripe.com → Webhooks |
| TELEGRAM_BOT_TOKEN | @BotFather su Telegram |
| NOCTURNA_API_URL | URL del deploy nocturna-calculations su Render |
| NOCTURNA_SERVICE_TOKEN | Token generato durante setup nocturna |
| APP_SECRET_KEY | Stringa casuale sicura (usata per proteggere i cron jobs) |
| ADMIN_EMAILS | Email separate da virgola degli amministratori |

---

## Prossimi step da sviluppare

- [ ] `routers/auth.py` — Middleware JWT per verificare token Supabase
- [ ] `routers/telegram.py` — Connessione bot + webhook comandi (/start, /stop)
- [ ] `routers/services.py` — Checkout e generazione report completi
- [ ] Audio ElevenLabs → storage su Supabase Storage
- [ ] CRM Admin Panel frontend (HTML separato o sezione protetta)
