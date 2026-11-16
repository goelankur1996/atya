"""Today's weather for a user's city, for the start of a call. Open-Meteo: free, no key.

    place = find_city("Noida")          # {"name", "lat", "lon", "label"} or None
    line = today_line(place["lat"], place["lon"])   # "24°C and clear now; high 28, low 14."

Both return None on any failure: weather is a nice-to-have, never a reason a call fails.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 6

# WMO weather codes, in plain words.
CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers", 81: "rain showers",
    82: "heavy rain showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


def find_city(name: str, http=requests) -> dict | None:
    name = (name or "").strip()
    if not name:
        return None
    try:
        r = http.get(GEOCODE, params={"name": name, "count": 1, "language": "en", "format": "json"},
                     timeout=TIMEOUT)
        r.raise_for_status()
        hits = r.json().get("results") or []
    except Exception as e:
        log.warning("city lookup failed: %s", e)
        return None
    if not hits:
        return None
    h = hits[0]
    label = ", ".join(x for x in (h.get("name"), h.get("admin1"), h.get("country")) if x)
    return {"name": h.get("name") or name, "lat": float(h["latitude"]), "lon": float(h["longitude"]), "label": label,
            "timezone": h.get("timezone")}


def today_line(lat: float, lon: float, http=requests) -> str | None:
    try:
        r = http.get(FORECAST, params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"},
            timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        cur, day = data.get("current") or {}, data.get("daily") or {}
        now_t = round(cur["temperature_2m"])
        sky = CODES.get(int(cur.get("weather_code", -1)), "")
        hi, lo = round(day["temperature_2m_max"][0]), round(day["temperature_2m_min"][0])
        rain = (day.get("precipitation_probability_max") or [None])[0]
    except Exception as e:
        log.warning("weather lookup failed: %s", e)
        return None
    line = f"{now_t}°C{' and ' + sky if sky else ''} now; high {hi}, low {lo}"
    if rain is not None and rain >= 40:
        line += f"; {round(rain)}% chance of rain"
    return line + "."


def set_city(store, tenant: dict, text: str, **kw) -> dict | None:
    """Where the user lives (for the weather on calls). The one function behind /city, the call's save_city and
    the setup interview. Returns the place, or None if it wasn't found."""
    place = find_city(text, **kw)
    if place is None:
        return None
    with store.tx(tenant["id"]) as tx:
        tx.set_city(tenant["id"], place["label"], place["lat"], place["lon"])
    tenant.update(city=place["label"], city_lat=place["lat"], city_lon=place["lon"])
    return place
