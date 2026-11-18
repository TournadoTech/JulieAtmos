from flask import Flask, jsonify, request, render_template_string
import requests, datetime, xml.etree.ElementTree as ET, re, time, threading
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime

app = Flask(__name__)
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "ATMOS-Weather/1.0 (personal weather dashboard)"})
GEOCODE = "https://geocoding-api.open-meteo.com/v1"
FORECAST = "https://api.open-meteo.com/v1/forecast"

# Cache mémoire : limite les appels externes et absorbe les pointes de trafic.
WEATHER_CACHE_TTL = 10 * 60       # 10 minutes
WEATHER_STALE_TTL = 60 * 60       # jusqu’à 1 h en secours si le fournisseur est indisponible
GEOCODE_CACHE_TTL = 24 * 60 * 60  # 24 heures
_weather_cache = {}
_geocode_cache = {}
_cache_lock = threading.RLock()

def get_json(url, params=None, timeout=12):
    r = HTTP.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

@app.get("/")
def home():
    return render_template_string(PAGE)

@app.get("/api/search")
def search():
    q = request.args.get("q", "").strip()
    if len(q) < 2: return jsonify([])
    cache_key = q.casefold()
    now = time.time()
    with _cache_lock:
        cached = _geocode_cache.get(cache_key)
        if cached and now - cached[0] < GEOCODE_CACHE_TTL:
            return jsonify(cached[1])
    try:
        data = get_json(f"{GEOCODE}/search", {"name": q, "count": 8, "language": "fr", "format": "json"})
        results = [{"name": x["name"], "admin": x.get("admin1",""), "country": x.get("country",""),
                    "lat": x["latitude"], "lon": x["longitude"], "timezone": x.get("timezone","auto")}
                   for x in data.get("results", [])]
        with _cache_lock:
            _geocode_cache[cache_key] = (time.time(), results)
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.get("/api/reverse")
def reverse_geocode():
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return jsonify({"error": "Coordonnées invalides"}), 400
        data = get_json("https://nominatim.openstreetmap.org/reverse", {
            "format": "jsonv2", "lat": lat, "lon": lon,
            "zoom": 10, "addressdetails": 1, "accept-language": "fr"
        })
        address = data.get("address", {})
        city = (address.get("city") or address.get("town") or
                address.get("village") or address.get("municipality") or
                address.get("hamlet") or address.get("suburb") or
                address.get("county") or address.get("state"))
        country = address.get("country", "")
        name = ", ".join(x for x in (city, country) if x) or data.get("name") or data.get("display_name", "Zone sélectionnée").split(",")[0]
        return jsonify({"name": name, "city": city or name, "country": country})
    except Exception as e:
        return jsonify({"error": str(e)}), 502



# Source météo secondaire : wttr.in (JSON j1), utilisée si Open-Meteo est indisponible.
WTTR_URLS = ("https://wttr.in", "https://wttr.is")

_WWO_TO_WMO = {
    113: 0, 116: 2, 119: 3, 122: 3, 143: 45, 176: 61, 179: 71,
    182: 71, 185: 56, 200: 95, 227: 75, 230: 75, 248: 45, 260: 48,
    263: 51, 266: 51, 281: 56, 284: 57, 293: 61, 296: 61, 299: 63,
    302: 63, 305: 65, 308: 65, 311: 66, 314: 67, 317: 66, 320: 67,
    323: 71, 326: 71, 329: 73, 332: 75, 335: 75, 338: 75, 350: 75,
    353: 80, 356: 81, 359: 82, 362: 81, 365: 82, 368: 71, 371: 73,
    374: 80, 377: 81, 386: 95, 389: 96, 392: 95, 395: 96,
}

def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)

def _wmo_code(value):
    try:
        return _WWO_TO_WMO.get(int(value), 3)
    except (TypeError, ValueError):
