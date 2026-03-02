"""
routers/nocturna_service.py — Integrazione diretta con nocturna-calculations
Sostituisce le chiamate HTTP a nocturna API con import diretto della libreria.
"""
from datetime import date, datetime
from typing import Optional
import logging

logger = logging.getLogger(__name__)

def get_natal_chart(
    birth_date: str,           # "YYYY-MM-DD"
    birth_time: str,           # "HH:MM" o "HH:MM:SS"
    latitude: float,
    longitude: float,
    timezone: str = "Europe/Rome"
) -> dict:
    """
    Calcola il tema natale completo: pianeti, case, ascendente, MC.
    Ritorna un dizionario con tutti i dati o {} in caso di errore.
    """
    try:
        from nocturna_calculations.core.chart import Chart

        # Normalizza il formato dell'orario
        if birth_time and len(birth_time) == 5:
            birth_time = birth_time + ":00"
        elif not birth_time:
            birth_time = "12:00:00"

        chart = Chart(
            date=birth_date,
            time=birth_time,
            latitude=latitude,
            longitude=longitude,
            timezone=timezone
        )

        planets = chart.calculate_planetary_positions()
        houses  = chart.calculate_houses()
        aspects = chart.calculate_aspects()

        # Estrai ascendente e MC
        asc = houses.get("angles", {}).get("ASC")
        mc  = houses.get("angles", {}).get("MC")

        return {
            "planets": planets,
            "houses": houses,
            "aspects": aspects.get("aspects", []),
            "ascendant": asc,
            "mc": mc,
            "ascendant_sign": _longitude_to_sign(asc) if asc is not None else None,
            "mc_sign": _longitude_to_sign(mc) if mc is not None else None,
        }

    except ImportError:
        logger.error("nocturna_calculations non installato — aggiungi al requirements.txt")
        return {}
    except Exception as e:
        logger.error(f"Errore calcolo tema natale: {e}")
        return {}


def get_transits(
    birth_date: str,
    birth_time: str,
    latitude: float,
    longitude: float,
    timezone: str = "Europe/Rome",
    transit_date: Optional[str] = None
) -> dict:
    """
    Calcola i transiti del giorno rispetto al tema natale.
    transit_date: "YYYY-MM-DD" (default: oggi)
    """
    try:
        from nocturna_calculations.core.chart import Chart

        if not transit_date:
            transit_date = date.today().isoformat()

        if birth_time and len(birth_time) == 5:
            birth_time = birth_time + ":00"
        elif not birth_time:
            birth_time = "12:00:00"

        # Carta natale
        natal = Chart(
            date=birth_date,
            time=birth_time,
            latitude=latitude,
            longitude=longitude,
            timezone=timezone
        )

        # Carta dei transiti (mezzogiorno del giorno richiesto)
        transit = Chart(
            date=transit_date,
            time="12:00:00",
            latitude=latitude,
            longitude=longitude,
            timezone=timezone
        )

        natal_planets   = natal.calculate_planetary_positions()
        transit_planets = transit.calculate_planetary_positions()

        # Aspetti tra transiti e natale
        transit_aspects = _calculate_transit_aspects(natal_planets, transit_planets)

        return {
            "date": transit_date,
            "natal_planets": natal_planets,
            "transit_planets": transit_planets,
            "transit_aspects": transit_aspects,
        }

    except ImportError:
        logger.error("nocturna_calculations non installato")
        return {}
    except Exception as e:
        logger.error(f"Errore calcolo transiti: {e}")
        return {}


def get_synastry(
    person1_date: str, person1_time: str, person1_lat: float, person1_lng: float, person1_tz: str,
    person2_date: str, person2_time: str, person2_lat: float, person2_lng: float, person2_tz: str
) -> dict:
    """
    Calcola la sinastria (compatibilità) tra due persone.
    Usato per il servizio compatibilità di coppia.
    """
    try:
        from nocturna_calculations.core.chart import Chart

        def normalize_time(t):
            if t and len(t) == 5:
                return t + ":00"
            return t or "12:00:00"

        chart1 = Chart(
            date=person1_date, time=normalize_time(person1_time),
            latitude=person1_lat, longitude=person1_lng, timezone=person1_tz
        )
        chart2 = Chart(
            date=person2_date, time=normalize_time(person2_time),
            latitude=person2_lat, longitude=person2_lng, timezone=person2_tz
        )

        synastry = chart1.calculate_synastry_chart(chart2)
        return synastry

    except Exception as e:
        logger.error(f"Errore calcolo sinastria: {e}")
        return {}


# ── HELPERS INTERNI ──────────────────────────────────────────

SIGNS = [
    "Ariete", "Toro", "Gemelli", "Cancro", "Leone", "Vergine",
    "Bilancia", "Scorpione", "Sagittario", "Capricorno", "Acquario", "Pesci"
]

def _longitude_to_sign(longitude: float) -> str:
    """Converte longitudine eclittica in nome del segno zodiacale."""
    index = int(longitude / 30) % 12
    return SIGNS[index]

def _longitude_to_sign_degree(longitude: float) -> str:
    """Converte longitudine in formato 'Segno GG°'."""
    sign = _longitude_to_sign(longitude)
    degree = longitude % 30
    return f"{sign} {degree:.1f}°"

def _calculate_transit_aspects(natal: dict, transits: dict, orb: float = 3.0) -> list:
    """
    Calcola aspetti principali tra pianeti in transito e natali.
    Ritorna lista di aspetti significativi (congiunzione, opposizione, trigono, quadratura, sestile).
    """
    ASPECT_ANGLES = {
        "congiunzione": 0,
        "opposizione": 180,
        "trigono": 120,
        "quadratura": 90,
        "sestile": 60,
    }

    aspects = []

    for t_planet, t_data in transits.items():
        t_lon = t_data.get("longitude", 0)

        for n_planet, n_data in natal.items():
            n_lon = n_data.get("longitude", 0)

            diff = abs(t_lon - n_lon) % 360
            if diff > 180:
                diff = 360 - diff

            for aspect_name, angle in ASPECT_ANGLES.items():
                if abs(diff - angle) <= orb:
                    aspects.append({
                        "transit_planet": t_planet,
                        "natal_planet": n_planet,
                        "aspect": aspect_name,
                        "orb": round(abs(diff - angle), 2),
                        "transit_sign": _longitude_to_sign(t_lon),
                        "natal_sign": _longitude_to_sign(n_lon),
                        "is_retrograde": t_data.get("is_retrograde", False)
                    })

    # Ordina per orb (aspetti più precisi prima)
    aspects.sort(key=lambda x: x["orb"])
    return aspects
