"""
routers/nocturna_service.py — Integrazione con nocturna-calculations

Usa la struttura REALE di nocturna:
  chart.calculate_planetary_positions() → {
      "SUN": {"longitude": float, "latitude": float, "distance": float, "speed": float, "is_retrograde": bool},
      "MOON": {...}, "MERCURY": {...}, ... "NORTH_NODE": {...}, "LILITH": {...}
  }
  chart.calculate_houses() → {
      "cusps": [lon1, lon2, ..., lon12],
      "angles": {"ASC": float, "MC": float, "DESC": float, "IC": float},
      "system": "PLACIDUS"
  }
  chart.calculate_aspects() → {"aspects": [...]}
"""
from datetime import date, datetime
from typing import Optional
import logging

logger = logging.getLogger(__name__)

SIGNS = [
    "Ariete", "Toro", "Gemelli", "Cancro", "Leone", "Vergine",
    "Bilancia", "Scorpione", "Sagittario", "Capricorno", "Acquario", "Pesci"
]

def longitude_to_sign(lon: float) -> str:
    return SIGNS[int(lon / 30) % 12]


def get_natal_chart(
    birth_date: str,        # "YYYY-MM-DD"
    birth_time: str,        # "HH:MM" o "HH:MM:SS"
    latitude: float,
    longitude: float,
    timezone: str = "Europe/Rome"
) -> dict:
    """
    Calcola il tema natale completo.

    Ritorna:
    {
        "planets": {
            "SUN":   {"longitude": float, "latitude": float, "distance": float, "speed": float, "is_retrograde": bool},
            "MOON":  {...},
            ... (MERCURY, VENUS, MARS, JUPITER, SATURN, URANUS, NEPTUNE, PLUTO, NORTH_NODE, LILITH)
        },
        "houses": {
            "cusps": [lon1..lon12],
            "angles": {"ASC": float, "MC": float, "DESC": float, "IC": float},
            "system": "PLACIDUS"
        },
        "aspects": [...],
        "ascendant": float,          # longitudine ASC — da houses.angles.ASC
        "ascendant_sign": str,       # es. "Ariete"
        "mc": float,                 # longitudine MC
        "mc_sign": str,
        "houses_signs": {            # case già convertite in segni
            "house_1": "Ariete", "house_2": "Toro", ...
        }
    }
    Ritorna {} in caso di errore.
    """
    try:
        from nocturna_calculations.core.chart import Chart

        # Normalizza orario
        if not birth_time:
            birth_time = "12:00:00"
        elif len(birth_time) == 5:
            birth_time = birth_time + ":00"

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

        # Estrai ASC e MC dagli angoli
        asc_lon = houses["angles"]["ASC"]
        mc_lon  = houses["angles"]["MC"]

        # Converti cuspidi in segni
        houses_signs = {}
        for i, lon in enumerate(houses["cusps"][:12], 1):
            houses_signs[f"house_{i}"] = longitude_to_sign(float(lon))

        return {
            "planets": planets,
            "houses": houses,
            "aspects": aspects.get("aspects", []),
            "ascendant": asc_lon,
            "ascendant_sign": longitude_to_sign(asc_lon),
            "mc": mc_lon,
            "mc_sign": longitude_to_sign(mc_lon),
            "houses_signs": houses_signs,
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

    Ritorna:
    {
        "date": "YYYY-MM-DD",
        "natal_planets": { stessa struttura di planets sopra },
        "transit_planets": { stessa struttura },
        "transit_aspects": [
            {
                "transit_planet": "JUPITER",
                "natal_planet": "SUN",
                "aspect": "trigono",
                "orb": 1.2,
                "transit_sign": "Cancro",
                "natal_sign": "Pesci",
                "is_retrograde": False
            }, ...
        ],
        "houses_signs": { "house_1": "Ariete", ... }
    }
    """
    try:
        from nocturna_calculations.core.chart import Chart

        if not transit_date:
            transit_date = date.today().isoformat()

        if not birth_time:
            birth_time = "12:00:00"
        elif len(birth_time) == 5:
            birth_time = birth_time + ":00"

        # Tema natale
        natal_chart = Chart(
            date=birth_date,
            time=birth_time,
            latitude=latitude,
            longitude=longitude,
            timezone=timezone
        )

        # Carta transiti (mezzogiorno del giorno richiesto)
        transit_chart = Chart(
            date=transit_date,
            time="12:00:00",
            latitude=latitude,
            longitude=longitude,
            timezone=timezone
        )

        natal_planets   = natal_chart.calculate_planetary_positions()
        transit_planets = transit_chart.calculate_planetary_positions()
        natal_houses    = natal_chart.calculate_houses()

        # Aspetti transiti → natale
        transit_aspects = _calculate_transit_aspects(natal_planets, transit_planets)

        # Case in segni
        houses_signs = {}
        for i, lon in enumerate(natal_houses["cusps"][:12], 1):
            houses_signs[f"house_{i}"] = longitude_to_sign(float(lon))

        return {
            "date": transit_date,
            "natal_planets": natal_planets,
            "transit_planets": transit_planets,
            "transit_aspects": transit_aspects,
            "houses_signs": houses_signs,
            "ascendant": natal_houses["angles"]["ASC"],
            "ascendant_sign": longitude_to_sign(natal_houses["angles"]["ASC"]),
        }

    except ImportError:
        logger.error("nocturna_calculations non installato")
        return {}
    except Exception as e:
        logger.error(f"Errore calcolo transiti: {e}")
        return {}


def _calculate_transit_aspects(natal: dict, transits: dict, orb: float = 3.0) -> list:
    """Aspetti principali tra pianeti in transito e natali."""
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
                        "transit_sign": longitude_to_sign(t_lon),
                        "natal_sign": longitude_to_sign(n_lon),
                        "is_retrograde": t_data.get("is_retrograde", False)
                    })

    aspects.sort(key=lambda x: x["orb"])
    return aspects
