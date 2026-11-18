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
        return 3

def _wttr_local_time(date_text, time_text):
    """Convertit les dates/heures locales wttr en ISO local (sans décalage)."""
    raw = str(time_text or "0").strip()
    try:
        minutes = int(raw)
        hour, minute = divmod(minutes, 100)
        dt = datetime.datetime.strptime(date_text, "%Y-%m-%d").replace(hour=hour, minute=minute)
        return dt.isoformat(timespec="minutes")
    except (TypeError, ValueError):
        return datetime.datetime.strptime(date_text, "%Y-%m-%d").isoformat(timespec="minutes")

def wttr_weather_payload(lat, lon, name=None):
    """Adapte le JSON wttr.in au schéma Open-Meteo attendu par l'interface ATMOS."""
    last_error = None
    data = None
    for base in WTTR_URLS:
        try:
            response = HTTP.get(
                f"{base}/{float(lat):.2f},{float(lon):.2f}",
                params={"format": "j1", "lang": "fr"}, timeout=18,
                headers={"Accept-Language": "fr"}
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("current_condition") or not data.get("weather"):
                raise ValueError("Réponse météo secondaire incomplète")
            break
        except Exception as exc:
            last_error = exc
            data = None
    if data is None:
        raise RuntimeError(f"Source météo secondaire indisponible : {last_error}")

    current_raw = data["current_condition"][0]
    days_raw = data["weather"]
    first_day = days_raw[0]
    obs = current_raw.get("localObsDateTime", "")
    try:
        current_dt = datetime.datetime.strptime(obs, "%Y-%m-%d %I:%M %p")
    except (TypeError, ValueError):
        current_dt = datetime.datetime.now().astimezone().replace(tzinfo=None, second=0, microsecond=0)

    # Lever/coucher du soleil du jour, pour déterminer le jour/nuit actuel.
    astronomy = (first_day.get("astronomy") or [{}])[0]
    def parse_clock(value):
        try:
            return datetime.datetime.strptime(value, "%I:%M %p").time()
        except (TypeError, ValueError):
            return None
    sunrise_t, sunset_t = parse_clock(astronomy.get("sunrise")), parse_clock(astronomy.get("sunset"))
    is_day = int(bool(sunrise_t and sunset_t and sunrise_t <= current_dt.time() <= sunset_t))

    current = {
        "time": current_dt.isoformat(timespec="minutes"),
        "temperature_2m": _num(current_raw.get("temp_C")),
        "relative_humidity_2m": _num(current_raw.get("humidity")),
        "apparent_temperature": _num(current_raw.get("FeelsLikeC"), current_raw.get("temp_C", 0)),
        "is_day": is_day,
        "precipitation": _num(current_raw.get("precipMM")),
        "weather_code": _wmo_code(current_raw.get("weatherCode")),
        "cloud_cover": _num(current_raw.get("cloudcover")),
        "pressure_msl": _num(current_raw.get("pressure")),
        "wind_speed_10m": _num(current_raw.get("windspeedKmph")),
        "wind_direction_10m": _num(current_raw.get("winddirDegree")),
    }

    hourly = {key: [] for key in ("time", "temperature_2m", "apparent_temperature", "precipitation_probability", "precipitation", "weather_code", "wind_speed_10m", "uv_index")}
    daily = {key: [] for key in ("time", "weather_code", "temperature_2m_max", "temperature_2m_min", "sunrise", "sunset", "uv_index_max", "precipitation_probability_max")}

    for day in days_raw:
        date_text = str(day.get("date", ""))
        if not date_text:
            continue
        day_hours = day.get("hourly") or []
        day_temps, day_codes, day_rain = [], [], []
        for hour in day_hours:
            iso_time = _wttr_local_time(date_text, hour.get("time", "0"))
            temp = _num(hour.get("tempC"))
            code = _wmo_code(hour.get("weatherCode"))
            day_temps.append(temp); day_codes.append(code)
            rain_chance = _num(hour.get("chanceofrain"))
            day_rain.append(rain_chance)
            hourly["time"].append(iso_time)
            hourly["temperature_2m"].append(temp)
            hourly["apparent_temperature"].append(_num(hour.get("FeelsLikeC"), temp))
            hourly["precipitation_probability"].append(rain_chance)
            hourly["precipitation"].append(_num(hour.get("precipMM")))
            hourly["weather_code"].append(code)
            hourly["wind_speed_10m"].append(_num(hour.get("windspeedKmph")))
            hourly["uv_index"].append(_num(hour.get("uvIndex", day.get("uvIndex", 0))))
        if not day_temps:
            continue
        astro = (day.get("astronomy") or [{}])[0]
        daily["time"].append(date_text)
        daily["weather_code"].append(day_codes[min(4, len(day_codes)-1)])
        daily["temperature_2m_max"].append(_num(day.get("maxtempC"), max(day_temps)))
        daily["temperature_2m_min"].append(_num(day.get("mintempC"), min(day_temps)))
        daily["sunrise"].append(_wttr_local_time(date_text, _clock_to_24(astro.get("sunrise"))))
        daily["sunset"].append(_wttr_local_time(date_text, _clock_to_24(astro.get("sunset"))))
        daily["uv_index_max"].append(_num(day.get("uvIndex", 0)))
        daily["precipitation_probability_max"].append(max(day_rain or [0]))

    if not hourly["time"] or not daily["time"]:
        raise ValueError("La source secondaire n'a fourni aucune prévision exploitable")
    return {
        "place": name or f"{lat:.2f}, {lon:.2f}", "latitude": lat, "longitude": lon,
        "timezone": "auto", "current": current, "hourly": hourly, "daily": daily,
        "updated": datetime.datetime.now().isoformat(timespec="minutes"),
        "source": "wttr.in (source de secours)",
        "source_notice": "Prévisions de secours : pas de 3 heures et horizon d'environ 3 jours. Les UV et probabilités peuvent être moins détaillés que sur la source principale."
    }

def _clock_to_24(value):
    try:
        return datetime.datetime.strptime(value, "%I:%M %p").strftime("%H%M")
    except (TypeError, ValueError):
        return "0000"


def weather_payload(lat, lon, name=None):
    # Arrondir à 2 décimales regroupe les demandes quasi identiques (≈ 1 km).
    cache_key = (round(float(lat), 2), round(float(lon), 2))
    now = time.time()
    with _cache_lock:
        cached = _weather_cache.get(cache_key)
        if cached and now - cached[0] < WEATHER_CACHE_TTL:
            payload = dict(cached[1])
            if name:
                payload["place"] = name
            return payload

    params = {
      "latitude": lat, "longitude": lon, "timezone": "auto",
      "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,cloud_cover,pressure_msl,wind_speed_10m,wind_direction_10m",
      "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,wind_speed_10m,uv_index",
      "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,uv_index_max,precipitation_probability_max",
      "forecast_days": 7
    }
    # Le verrou évite que plusieurs requêtes simultanées interrogent toutes les API.
    with _cache_lock:
        cached = _weather_cache.get(cache_key)
        if cached and time.time() - cached[0] < WEATHER_CACHE_TTL:
            payload = dict(cached[1])
            if name:
                payload["place"] = name
            return payload
        primary_error = None
        try:
            d = get_json(FORECAST, params, timeout=20)
            payload = {"place": name or f"{lat:.2f}, {lon:.2f}", "latitude": lat, "longitude": lon,
                       "timezone": d.get("timezone","Europe/Paris"), "current": d["current"],
                       "hourly": d["hourly"], "daily": d["daily"],
                       "updated": datetime.datetime.now().isoformat(timespec="minutes"),
                       "source": "Open-Meteo"}
            _weather_cache[cache_key] = (time.time(), payload)
            return payload
        except Exception as exc:
            primary_error = exc

        # Secours automatique : wttr.in fournit du JSON sans clé API.
        try:
            payload = wttr_weather_payload(lat, lon, name)
            payload["primary_notice"] = f"Open-Meteo indisponible ({type(primary_error).__name__}); source secondaire utilisée."
            _weather_cache[cache_key] = (time.time(), payload)
            return payload
        except Exception as fallback_error:
            # Si les deux sources échouent, on sert la dernière météo connue pendant 1 h.
            cached = _weather_cache.get(cache_key)
            if cached and time.time() - cached[0] < WEATHER_STALE_TTL:
                payload = dict(cached[1])
                if name:
                    payload["place"] = name
                payload["cache_notice"] = "Données météo en cache : les fournisseurs sont momentanément indisponibles."
                return payload
            raise RuntimeError(f"Open-Meteo : {primary_error} | Source secondaire : {fallback_error}")

@app.get("/api/weather")
def weather():
    try:
        lat, lon = float(request.args["lat"]), float(request.args["lon"])
        return jsonify(weather_payload(lat, lon, request.args.get("name")))
    except Exception as e: return jsonify({"error": str(e)}), 502


def esri_vigilance_fallback():
    """Secours : niveaux départementaux Météo-France relayés par Esri France."""
    url = "https://services.esrifrance.fr/arcgis/rest/services/MeteoFrance/FranceDOM_CarteVigilance/FeatureServer/0/query"
    params = {
        "where": "1=1",
        "outFields": "adm_departement_ref_insee_dep,adm_departement_ref_nom,max_color",
        "returnGeometry": "false",
        "f": "json"
    }
    response = HTTP.get(url, params=params, timeout=15)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError("Le service cartographique de secours a renvoyé une erreur.")
    features = payload.get("features") or []
    if not features:
        raise RuntimeError("Le service cartographique de secours n'a renvoyé aucun département.")

    levels = {
        "rouge": ("rouge", "Rouge", "danger"),
        "orange": ("orange", "Orange", "danger"),
        "jaune": ("jaune", "Jaune", "warning"),
        "vert": ("vert", "Vert", "success")
    }
    alerts = []
    for feature in features:
        attrs = feature.get("attributes") or {}
        color = str(attrs.get("max_color") or "").strip().lower()
        if color not in levels or color == "vert":
            continue
        code = str(attrs.get("adm_departement_ref_insee_dep") or "").strip()
        department = str(attrs.get("adm_departement_ref_nom") or "").strip()
        if not department:
            continue
        level, level_fr, tone = levels[color]
        alerts.append({
            "title": "Vigilance météorologique départementale",
            "event": "Niveau maximal publié pour le département",
            "area": f"{department} ({code})" if code else department,
            "severity": color,
            "level": level,
            "level_fr": level_fr,
            "tone": tone,
            "emoji": "⚠️",
            "description": (
                f"La carte départementale de vigilance indique un niveau {level_fr.lower()} "
                f"pour {department}. Cette couche donne le niveau maximal départemental ; "
                "elle ne détaille pas ici le phénomène ni ses horaires."
            ),
            "advice": (
                "Consultez le bulletin officiel de Météo-France pour connaître le phénomène, "
                "les horaires concernés et les consignes adaptées à votre commune."
            ),
            "onset": "",
            "expires": "",
            "source": "Météo-France / Esri France (secours)"
        })
    return {
        "source": "Météo-France / Esri France",
        "freshness": "Source cartographique de secours : mise à jour annoncée deux fois par jour. Les détails par phénomène et les horaires sont à vérifier sur le bulletin officiel.",
        "alerts": alerts,
        "active_count": len(alerts),
        "available": True,
        "fallback": True,
        "updated": datetime.datetime.now(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y à %H:%M")
    }

@app.get("/api/alerts")
def alerts():
    """Source principale MeteoAlarm, puis secours cartographique Météo-France/Esri."""
    feed = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-france"
    ns = {"a":"http://www.w3.org/2005/Atom", "cap":"urn:oasis:names:tc:emergency:cap:1.2"}
    event_map = [
        (("wind","vent"), "Vent violent", "🌬️", "Des rafales peuvent provoquer des chutes de branches ou d’objets. Éloignez-vous des arbres et sécurisez ce qui peut s’envoler.", "Limitez les déplacements, mettez à l’abri les objets sensibles et ne vous abritez pas sous les arbres."),
        (("flood","rain","pluie","inond"), "Pluie-inondation", "🌧️", "Des pluies soutenues peuvent entraîner du ruissellement et des inondations localisées.", "Évitez les cours d’eau et les zones inondées. Ne vous engagez jamais sur une route submergée."),
        (("thunder","storm","orage"), "Orages", "⛈️", "Des orages peuvent s’accompagner de fortes pluies, de rafales et parfois de grêle.", "Mettez-vous à l’abri, évitez les activités en extérieur et débranchez les appareils sensibles si nécessaire."),
        (("snow","ice","neige","verglas"), "Neige-verglas", "❄️", "La neige ou le verglas peuvent rendre les déplacements difficiles et les chaussées glissantes.", "Différez les déplacements non indispensables et adaptez votre conduite."),
        (("heat","canicule","high temperature"), "Canicule", "🌡️", "Une forte chaleur est susceptible de présenter un risque pour les personnes les plus fragiles.", "Buvez régulièrement, évitez les efforts aux heures chaudes et prenez des nouvelles des personnes vulnérables."),
        (("cold","froid","low temperature"), "Grand froid", "🥶", "Une période de froid marqué peut présenter un risque, notamment pour les personnes vulnérables.", "Limitez les expositions prolongées au froid et veillez aux personnes isolées."),
        (("avalanche","avalan"), "Avalanches", "🏔️", "Le manteau neigeux peut être instable en montagne.", "Respectez les consignes locales et les informations des services de pistes. Ne vous aventurez pas hors des itinéraires sécurisés."),
        (("coastal","wave","submersion","vague"), "Vagues-submersion", "🌊", "La mer peut devenir dangereuse sur le littoral et provoquer une submersion des zones exposées.", "Éloignez-vous du bord de mer, des digues et des zones basses exposées."),
        (("river","crue","flooding"), "Crues", "🏞️", "Le niveau de certains cours d’eau peut monter et provoquer des débordements.", "Tenez-vous informé de l’évolution des cours d’eau et éloignez-vous des berges."),
        (("forest fire","wildfire","fire","feu"), "Feux de forêt", "🔥", "Les conditions météorologiques peuvent favoriser les départs ou la propagation de feux.", "N’allumez pas de feu, ne jetez aucun mégot et signalez tout départ de feu aux secours."),
        (("tropical","cyclone","hurricane","tempête"), "Cyclone / tempête tropicale", "🌀", "Un phénomène cyclonique ou une tempête tropicale peut entraîner des vents violents et de fortes précipitations.", "Suivez les consignes des autorités et préparez-vous à limiter vos déplacements.")
    ]
    def cap_text(entry, path, default=""):
        node = entry.find(path, ns)
        return (node.text or "").strip() if node is not None else default
    def parse_raw_dt(value):
        if not value: return None
        try:
            dt = datetime.datetime.fromisoformat(value.replace("Z","+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except Exception: return None
    def parse_dt(value):
        dt = parse_raw_dt(value)
        return dt.astimezone(ZoneInfo("Europe/Paris")).strftime("%d/%m à %Hh") if dt else ""

    primary_error = None
    try:
        response = HTTP.get(feed, timeout=15)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        out = []
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        for entry in root.findall("a:entry", ns):
            status = cap_text(entry, ".//cap:status", "Actual").lower()
            msg_type = cap_text(entry, ".//cap:msgType", "Alert").lower()
            if status != "actual" or msg_type in ("cancel", "ack"):
                continue
            effective_raw = cap_text(entry, ".//cap:effective")
            onset_raw = cap_text(entry, ".//cap:onset")
            expires_raw = cap_text(entry, ".//cap:expires")
            starts_at = parse_raw_dt(onset_raw or effective_raw)
            ends_at = parse_raw_dt(expires_raw)
            if starts_at and starts_at > now_utc: continue
            if ends_at and ends_at < now_utc: continue
            title = cap_text(entry, "a:title", "Vigilance météo")
            event = cap_text(entry, ".//cap:event", title)
            event_l = (event + " " + title).lower()
            matched = next((item for item in event_map if any(k in event_l for k in item[0])), None)
            if matched:
                category, emoji, fallback_desc, advice = matched[1], matched[2], matched[3], matched[4]
            else:
                category, emoji = event, "⚠️"
                fallback_desc = "Un phénomène météorologique fait l’objet d’un bulletin. Consultez les informations officielles pour connaître les conditions locales."
                advice = "Suivez les consignes des autorités et limitez les déplacements si les conditions se dégradent."
            severity = cap_text(entry, ".//cap:severity", "Unknown").lower()
            level_data = {
                "extreme": ("rouge","Rouge","danger"),
                "severe": ("orange","Orange","danger"),
                "moderate": ("jaune","Jaune","warning"),
                "minor": ("vert","Vert","success")
            }.get(severity, ("inconnu","Niveau non précisé","neutral"))
            area = cap_text(entry, ".//cap:areaDesc", "Zone non précisée")
            description = cap_text(entry, ".//cap:description", "")
            instruction = cap_text(entry, ".//cap:instruction", "")
            is_french = bool(re.search(r"\b(le|la|les|des|dans|avec|risque|pluie|vent|orage|neige|vigilance)\b", description.lower()))
            if not description or not is_french:
                description = fallback_desc
            else:
                description = re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", description)).strip()[:700]
            if instruction and re.search(r"\b(les|des|vous|évitez|restez|limitez|ne |consignes)\b", instruction.lower()):
                advice = re.sub(r"\s+", " ", instruction).strip()[:450]
            out.append({
                "title": category, "event": event, "area": area,
                "severity": severity, "level": level_data[0], "level_fr": level_data[1], "tone": level_data[2],
                "emoji": emoji, "description": description, "advice": advice,
                "onset": parse_dt(onset_raw or effective_raw), "expires": parse_dt(expires_raw),
                "source": "MeteoAlarm"
            })
        unique = {}
        for item in out:
            unique[(item["title"], item["area"], item["level"])] = item
        if unique:
            return jsonify({
                "source": "MeteoAlarm", "freshness": "Flux européen de bulletins d’alerte CAP.",
                "alerts": list(unique.values()), "active_count": len(unique), "available": True,
                "fallback": False,
                "updated": datetime.datetime.now(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y à %H:%M")
            })
    except Exception as exc:
        primary_error = f"{type(exc).__name__}: {exc}"
        app.logger.warning("Flux MeteoAlarm indisponible : %s", primary_error)

    # Fallback if the primary feed is unavailable or returns no active alert.
    try:
        result = esri_vigilance_fallback()
        result["primary_status"] = "Flux MeteoAlarm indisponible ou sans bulletin actif ; source cartographique de secours utilisée."
        return jsonify(result)
    except Exception as fallback_exc:
        fallback_error = f"{type(fallback_exc).__name__}: {fallback_exc}"
        app.logger.exception("Source de secours vigilance indisponible : %s", fallback_error)
        return jsonify({
            "source": None, "alerts": [], "available": False, "fallback": False,
            "updated": None,
            "message": "Les deux sources de vigilance sont momentanément injoignables. Aucun niveau ne peut être confirmé.",
            "diagnostic": {
                "meteoalarm": primary_error or "Aucun bulletin actif retourné",
                "secours": fallback_error
            }
        }), 503

@app.get("/api/news")
def news():
    feeds = [
      ("BFMTV","https://www.bfmtv.com/rss/news-24-7/"),
      ("Le Parisien","https://feeds.leparisien.fr/leparisien/rss"),
      ("Franceinfo","https://www.francetvinfo.fr/titres.rss"),
      ("France 24","https://www.france24.com/fr/france/rss")
    ]
    items=[]
    for source,url in feeds:
        try:
            root=ET.fromstring(HTTP.get(url, timeout=8).content)
            for it in root.findall(".//item")[:8]:
                title=it.findtext("title","").strip()
                link=it.findtext("link","").strip()
                desc=it.findtext("description","").strip()
                date=it.findtext("pubDate","")
                if title and link:
                    items.append({"source":source,"title":title,"link":link,"description":re.sub("<[^>]+>"," ",desc)[:240],"date":date})
        except Exception:
            continue
    items.sort(key=lambda x: x.get("date",""), reverse=True)
    return jsonify({"items":items[:30], "sources":[x[0] for x in feeds]})

PAGE = r"""
<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ATMOS — Weather Intelligence</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{--glass:rgba(8,30,58,.70);--line:rgba(190,220,255,.28);--white:#f5f8ff;--muted:#c9d9ed}
*{box-sizing:border-box}body{margin:0;color:var(--white);font-family:Inter,Segoe UI,Arial,sans-serif;background:#061b37;min-height:100vh}
body:before{content:"";position:fixed;inset:0;z-index:-2;background:linear-gradient(180deg,rgba(4,25,56,.12),rgba(2,18,39,.55)),url('https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?auto=format&fit=crop&w=2200&q=85') center/cover}
body:after{content:"";position:fixed;inset:0;z-index:-1;background:linear-gradient(90deg,rgba(2,17,39,.30),rgba(3,23,48,.22))}
header{height:78px;display:flex;align-items:center;gap:28px;padding:0 2.7%;background:rgba(3,27,61,.82);border-bottom:1px solid #ffffff20;backdrop-filter:blur(14px);position:sticky;top:0;z-index:1000}
.brand{display:flex;align-items:center;gap:12px;min-width:335px}.cloud{font-size:46px;filter:drop-shadow(0 3px 8px #6ccaff)}.brand b{font-size:30px;letter-spacing:-1px;display:block}.brand small{font-size:16px;color:#d4e2f5}
nav{display:flex;gap:12px;align-items:center;flex:1}nav button,.search{border:1px solid transparent;background:transparent;color:white;padding:12px 17px;border-radius:28px;font-size:16px;cursor:pointer;white-space:nowrap}nav button.active,nav button:hover{background:linear-gradient(120deg,#ffffff32,#ffffff12);border-color:#a9d9ff65;box-shadow:0 0 18px #4ebcff44}
.search{border:1px solid #a9c9eb88;width:330px;text-align:left;background:#061d3a80}
main{max-width:1700px;margin:auto;padding:36px 3.2% 58px;position:relative}
.view{display:none}.view.active{display:block}
.homegrid{display:grid;grid-template-columns:29% 23% 1fr;grid-template-rows:300px 180px 300px;gap:16px;min-height:820px}
.presenter{grid-column:1;grid-row:1/4;position:relative;display:flex;align-items:flex-end;justify-content:center;overflow:visible;min-height:820px;align-self:stretch}
.presenter img{display:block;position:absolute;left:40%;bottom:-260px;height:118%;width:auto;max-width:none;max-height:none;object-fit:contain;object-position:center bottom;filter:drop-shadow(0 8px 12px #00101e55);z-index:2;transform:translateX(-50%)}
.intro{grid-column:2;grid-row:1;align-self:center;padding:20px 0}.pill{display:inline-block;border:1px solid #b7d9ff66;background:#ffffff22;border-radius:25px;padding:9px 20px;font-size:17px}.intro h1{font-size:48px;margin:22px 0 10px}.intro p{font-size:19px;line-height:1.35;max-width:340px}
.panel{background:linear-gradient(135deg,rgba(27,57,91,.72),rgba(5,26,51,.76));border:1px solid var(--line);border-radius:24px;box-shadow:0 12px 32px #00132955,inset 0 1px #ffffff1b;backdrop-filter:blur(16px);padding:17px 20px}
.current{grid-column:3;grid-row:1;align-self:center;min-height:250px}.placehead{display:flex;justify-content:space-between;align-items:center;font-size:28px;font-weight:700}.sub{color:var(--muted);font-size:16px;margin:6px 0 12px}.currentbody{display:flex;align-items:center;gap:22px}.weathericon{font-size:62px}.temp{font-size:86px;font-weight:700;line-height:1}.desc{font-size:19px;margin-top:5px}.metrics{margin-left:auto;min-width:240px}.uvhome b{color:#ffe28b}.metric{display:flex;justify-content:space-between;border-bottom:1px solid #ffffff18;padding:8px 0;color:#d9e7f8}.metric b{color:white}
.hourly{grid-column:2/4;grid-row:2}.panel h3{margin:0 0 12px;font-size:18px}.hours{display:grid;grid-template-columns:repeat(8,1fr);gap:9px}.hour{background:#ffffff12;border:1px solid #ffffff13;border-radius:13px;text-align:center;padding:10px 3px}.hour .ico{font-size:25px;margin:5px}.hour b{display:block;font-size:17px}
.bottomleft{grid-column:2;grid-row:3}.bottomright{grid-column:3;grid-row:3}.days{display:flex;flex-direction:column;gap:0}.day{display:grid;grid-template-columns:1fr 34px 70px;align-items:center;padding:6px 2px;border-bottom:1px solid #ffffff15;font-size:15px}.day:last-child{border:0}
#miniMap{height:220px;border-radius:15px;overflow:hidden}.mapnote{font-size:12px;color:#d2e2f3;margin-top:6px}
.pagehead{font-size:34px;margin:10px 0 8px}.lead{color:#d0e1f4;margin-bottom:20px}.widepanel{margin-top:15px}.forecastgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.forecastcard{background:#ffffff10;border:1px solid #ffffff18;border-radius:15px;padding:16px}.forecastcard strong{font-size:22px}.forecastcard .big{font-size:34px;margin:9px 0}.uv{color:#ffe28b}.maplayout{display:grid;grid-template-columns:1fr 340px;gap:15px}#worldMap{height:620px;border-radius:20px}.mapinfo{min-height:250px}.alertgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.alert{padding:17px;border-radius:16px;background:#ffffff12;border:1px solid #ffffff20}.alert .level{font-weight:700;margin-bottom:8px}.newsgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.newsitem{padding:18px}.newsitem a{color:white;font-size:19px;font-weight:650;text-decoration:none}.newsitem a:hover{text-decoration:underline}.newsitem p{color:#c9d9ed;line-height:1.4}.muted{color:var(--muted)}.star{cursor:pointer;font-size:25px}
#searchResults{position:absolute;right:3%;top:67px;z-index:2000;background:#0a294c;border:1px solid #8ab8e8;border-radius:12px;width:330px;display:none;max-height:320px;overflow:auto}.result{padding:10px 14px;cursor:pointer}.result:hover{background:#ffffff20}
.leaflet-container{background:#0b2945}.leaflet-control-attribution{font-size:9px!important}

.alert-heading{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:14px}
.alert-heading .pagehead{margin-bottom:5px}
.official-link{display:inline-flex;align-items:center;justify-content:center;padding:11px 15px;border:1px solid #8ec8f2a8;border-radius:12px;background:#0c3456a8;color:#eaf6ff;text-decoration:none;font-weight:700;white-space:nowrap}
.official-link:hover{background:#185078}
.alert-summary{display:flex;align-items:center;gap:16px;padding:18px 22px;margin-bottom:14px;border-left:5px solid #6bca82}
.alert-summary b{font-size:19px}.alert-summary p{margin:5px 0 0}
.summary-pulse{font-size:25px;color:#76d98a}
.level-legend{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.level-card{display:flex;align-items:flex-start;gap:12px;padding:15px;border-radius:15px;background:#0b2544d9;border:1px solid #ffffff24;min-height:105px}
.level-card b{display:block;margin-bottom:5px}.level-card p{margin:0;color:var(--muted);font-size:13px;line-height:1.35}
.level-dot{width:13px;height:13px;border-radius:50%;flex:none;margin-top:3px}
.level-card.green{border-color:#39b86c77}.green .level-dot{background:#35c46a;box-shadow:0 0 12px #35c46a88}
.level-card.yellow{border-color:#e5ca3c88}.yellow .level-dot{background:#f3d529;box-shadow:0 0 12px #f3d52988}
.level-card.orange{border-color:#ff923d88}.orange .level-dot{background:#ff8b32;box-shadow:0 0 12px #ff8b3288}
.level-card.red{border-color:#ff536388}.red .level-dot{background:#f04455;box-shadow:0 0 12px #f0445588}
.alerts-layout{display:grid;grid-template-columns:minmax(0,1.8fr) minmax(280px,.85fr);gap:15px;align-items:start}
.alerts-main,.alert-side{padding:20px}
.section-title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}
.section-title h2,.alert-side h2,.widepanel h2{margin:0;font-size:20px}
.active-alert-list{display:grid;gap:12px}
.alert-item{padding:16px 17px;border-radius:15px;background:#ffffff0d;border:1px solid #ffffff20;border-left:5px solid #8495a8}
.alert-item.level-rouge{border-left-color:#f04455;background:linear-gradient(90deg,#7d1b2b55,#ffffff0b)}
.alert-item.level-orange{border-left-color:#ff8b32;background:linear-gradient(90deg,#8c421f55,#ffffff0b)}
.alert-item.level-jaune{border-left-color:#f3d529;background:linear-gradient(90deg,#806c1d55,#ffffff0b)}
.alert-item.level-vert{border-left-color:#35c46a;background:linear-gradient(90deg,#1b633955,#ffffff0b)}
.alert-item-top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.alert-pill{display:inline-flex;align-items:center;gap:7px;border-radius:999px;padding:6px 11px;font-weight:800;font-size:12px;color:#fff}
.alert-pill.rouge{background:#c9283e}.alert-pill.orange{background:#d9691f}.alert-pill.jaune{background:#d5b900;color:#172033}.alert-pill.vert{background:#218b4a}.alert-pill.inconnu{background:#65758a}
.alert-item h3{margin:0;font-size:18px}.alert-area{font-weight:700;color:#e5efff;margin:3px 0 9px}
.alert-item p{line-height:1.45;margin:7px 0;color:#d3e0f1}
.alert-meta{display:flex;gap:14px;flex-wrap:wrap;color:#b9cde3;font-size:12px}
.alert-advice{margin-top:11px;padding:10px 12px;border-radius:10px;background:#071c35a8;color:#e6f2ff;font-size:13px;line-height:1.4}
.alert-empty{padding:20px;border-radius:14px;background:#0b3c2b55;border:1px solid #45c17a66}
.alert-empty b{font-size:18px;color:#a7f0bf}
.alert-unavailable{padding:18px;border-radius:14px;background:#6d4b1d44;border:1px solid #dba84a88;line-height:1.5}
.source-note{font-size:12px;color:#afc4db;line-height:1.45;margin-top:14px}
.alert-news-list{display:grid;gap:12px;margin-top:12px}
.alert-news-item{padding:12px;border-radius:12px;background:#ffffff0d;border:1px solid #ffffff18}
.alert-news-item a{color:#f4f8ff;text-decoration:none;font-weight:700;line-height:1.35}
.alert-news-item a:hover{text-decoration:underline}
.alert-news-item p{margin:6px 0 0;color:#bfd0e4;font-size:12px;line-height:1.4}
.side-link{width:100%;margin-top:14px}
.alertgrid .alert{min-height:92px}
.alertgrid .alert .level{font-weight:750}
@media(max-width:1000px){.level-legend{grid-template-columns:repeat(2,1fr)}.alerts-layout{grid-template-columns:1fr}}
@media(max-width:650px){.alert-heading{align-items:flex-start;flex-direction:column}.level-legend{grid-template-columns:1fr}.section-title{align-items:flex-start;flex-direction:column}}
@media(max-width:1200px){header{gap:10px}.brand{min-width:210px}.brand b{font-size:23px}nav button{padding:9px;font-size:14px}.search{width:210px}.homegrid{grid-template-columns:24% 28% 1fr}.intro h1{font-size:38px}.metrics{min-width:180px}.temp{font-size:66px}}
@media(max-width:850px){header{height:auto;flex-wrap:wrap;padding:10px}.brand{min-width:100%}nav{overflow:auto}.search{width:100%}.homegrid{display:flex;flex-direction:column}.presenter{height:640px;min-height:640px;order:0}.presenter img{height:100%;max-height:100%;}.intro{order:1}.current{order:2}.hourly{order:3}.bottomleft,.bottomright{order:4}.hours{grid-template-columns:repeat(4,1fr)}.maplayout{grid-template-columns:1fr}.forecastgrid,.alertgrid,.newsgrid{grid-template-columns:1fr}#worldMap{height:430px}}

/* ATMOS V17 — responsive PC / tablette / mobile */
html{scroll-behavior:smooth}
img,video,canvas,svg{max-width:100%}
.panel,.level-card,.alert,.newsitem,.forecastcard{min-width:0}
@media(max-width:850px){
  header{height:auto;min-height:0;display:grid;grid-template-columns:1fr;gap:8px;padding:10px 14px;position:sticky;top:0}
  .brand{min-width:0;width:100%;gap:9px}
  .cloud{font-size:34px}
  .brand b{font-size:23px}
  .brand small{font-size:12px}
  nav{width:100%;min-width:0;gap:6px;overflow-x:auto;overflow-y:hidden;padding:2px 0 5px;scrollbar-width:thin;flex-wrap:nowrap}
  nav button{flex:0 0 auto;padding:10px 13px;font-size:14px;min-height:40px}
  .search{width:100%;min-width:0;padding:11px 14px;font-size:15px}
  #searchResults{left:14px;right:14px;top:auto;width:auto;max-height:45vh}
  main{padding:18px 14px 34px;width:100%;overflow:hidden}
  .homegrid{display:grid;grid-template-columns:minmax(0,1fr);grid-template-rows:auto;gap:12px;min-height:0}
  .presenter{grid-column:1;grid-row:auto;order:0;height:350px;min-height:350px;width:100%;overflow:hidden;align-self:auto}
  .presenter img{height:100%;width:auto;max-width:100%;max-height:none;left:50%;bottom:0;object-fit:contain;object-position:center bottom;transform:translateX(-50%)}
  .intro{grid-column:1;grid-row:auto;order:1;padding:8px 4px 12px;text-align:center}
  .intro h1{font-size:clamp(30px,7vw,42px);margin:12px 0 7px}
  .intro p{font-size:16px;max-width:520px;margin:0 auto;line-height:1.45}
  .pill{font-size:14px;padding:7px 14px}
  .current{grid-column:1;grid-row:auto;order:2;min-height:0;align-self:stretch}
  .hourly{grid-column:1;grid-row:auto;order:3}
  .bottomleft{grid-column:1;grid-row:auto;order:4}
  .bottomright{grid-column:1;grid-row:auto;order:5}
  .panel{padding:15px;border-radius:18px}
  .placehead{font-size:23px}
  .sub{font-size:14px}
  .currentbody{display:grid;grid-template-columns:54px minmax(0,1fr);gap:8px 12px;align-items:center}
  .weathericon{font-size:44px;grid-column:1;grid-row:1}
  .currentbody>div:nth-child(2){grid-column:2;grid-row:1}
  .temp{font-size:clamp(48px,12vw,68px)}
  .desc{font-size:16px}
  .metrics{grid-column:1/-1;width:100%;min-width:0;margin:8px 0 0}
  .metric{padding:8px 1px;font-size:14px}
  .hours{grid-template-columns:repeat(4,minmax(0,1fr));gap:7px}
  .hour{padding:9px 3px}
  .hour .ico{font-size:22px}
  .hour b{font-size:15px}
  .day{grid-template-columns:minmax(0,1fr) 30px minmax(66px,auto);font-size:14px}
  #miniMap{height:230px}
  .forecastgrid{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
  .forecastcard{padding:12px}
  .forecastcard strong{font-size:19px}
  .forecastcard .big{font-size:28px}
  .maplayout{grid-template-columns:minmax(0,1fr);gap:12px}
  #worldMap{height:55vh;min-height:330px;max-height:560px}
  .mapinfo{min-height:0}
  .alertgrid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .level-legend{grid-template-columns:repeat(2,minmax(0,1fr))}
  .alerts-layout{grid-template-columns:minmax(0,1fr)}
  .newsgrid{grid-template-columns:minmax(0,1fr)}
  .pagehead{font-size:clamp(26px,6vw,34px)}
  .alert-heading{align-items:flex-start;flex-direction:column}
  .official-link{white-space:normal;text-align:center}
}
@media(max-width:520px){
  header{padding:9px 10px}
  nav button{font-size:13px;padding:9px 11px}
  main{padding:13px 10px 26px}
  .presenter{height:300px;min-height:300px}
  .presenter img{height:100%;bottom:0}
  .panel{padding:13px;border-radius:16px}
  .currentbody{grid-template-columns:45px minmax(0,1fr);gap:7px 9px}
  .weathericon{font-size:38px}
  .temp{font-size:52px}
  .metrics{margin-top:5px}
  .metric{font-size:13px;padding:7px 0}
  .hours{grid-template-columns:repeat(2,minmax(0,1fr))}
  .hour{display:grid;grid-template-columns:1fr 28px 1fr;align-items:center;gap:3px;padding:8px 5px}
  .hour .ico{margin:0;font-size:20px}
  .hour b{font-size:14px}
  .forecastgrid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .forecastcard{padding:10px}
  .forecastcard strong{font-size:17px}
  .forecastcard .big{font-size:25px}
  #miniMap{height:205px}
  #worldMap{height:58vh;min-height:300px}
  .alertgrid,.level-legend{grid-template-columns:minmax(0,1fr)}
  .alert-summary{padding:13px;align-items:flex-start}
  .alert-summary b{font-size:16px}
  .section-title{align-items:flex-start;flex-direction:column}
  .newsitem{padding:14px}
  .newsitem a{font-size:17px}
  .pagehead{margin-top:5px}
}
@media(max-width:360px){
  .presenter{height:270px;min-height:270px}
  .temp{font-size:46px}
  .weathericon{font-size:34px}
  .forecastgrid{grid-template-columns:minmax(0,1fr)}
}

</style></head><body>
<header><div class="brand"><span class="cloud">☁️</span><div><b>ATMOS.</b><small>Weather Intelligence</small></div></div>
<nav><button class="active" data-view="home">⌂ &nbsp;Accueil</button><button data-view="forecast">▦ &nbsp;Prévisions</button><button data-view="map">◈ &nbsp;Carte</button><button data-view="alerts">⚠ &nbsp;Vigilance</button><button data-view="news">▤ &nbsp;Actualités</button></nav>
<input id="citySearch" class="search" placeholder="⌕  Rechercher une ville..."><div id="searchResults"></div></header>
<main>
<section id="home" class="view active"><div class="homegrid">
<div class="presenter"><img src="/static/presenter.png" alt="Présentatrice météo"></div>
<div class="intro"><span class="pill">Votre météo avec Julie</span><h1>Bonjour ! <span id="helloIcon">🌤️</span></h1><p>Découvrez la météo en temps réel, les prévisions et les alertes pour votre ville.</p></div>
<div class="panel current"><div class="placehead"><span>📍 <span id="cityName">Paris</span></span><span class="star" title="Favori">☆</span></div><div class="sub" id="todayDate"></div><div class="currentbody"><div class="weathericon" id="currentIcon">🌤️</div><div><div class="temp"><span id="temp">--</span>°C</div><div class="desc" id="description">Chargement...</div></div><div class="metrics"><div class="metric">🌡 Ressenti <b id="feels">--°C</b></div><div class="metric">≋ Vent <b id="wind">-- km/h</b></div><div class="metric">♧ Humidité <b id="humidity">-- %</b></div><div class="metric">◉ Pression <b id="pressure">-- hPa</b></div><div class="metric uvhome">☀ Indice UV <b id="uvHome">—</b></div></div><div class="muted" id="homeWeatherSource" style="font-size:11px;margin-top:8px"></div></div></div>
<div class="panel hourly"><h3 id="homeHoursTitle">Prévisions heures par heures</h3><div class="hours" id="homeHours"></div></div>
<div class="panel bottomleft"><h3 id="homeDaysTitle">Prévisions 7 jours</h3><div class="days" id="homeDays"></div></div>
<div class="panel bottomright"><h3>Carte météo — <span id="mapCity">Paris</span></h3><div id="miniMap"></div><div class="mapnote">Cliquez sur la carte pour explorer une zone météo</div></div>
</div></section>
<section id="forecast" class="view"><h1 class="pagehead">Prévisions météo</h1><div class="lead" id="forecastLead">Les prochaines 24 heures, l'indice UV et les tendances sur 7 jours.</div><div class="panel"><h3 id="fullHoursTitle">Prévisions détaillées — 24 heures</h3><div class="forecastgrid" id="fullHours"></div></div><div class="panel widepanel"><h3>Indice UV aujourd'hui</h3><div class="forecastgrid" id="uvCards"></div><p class="muted">Repères : 0–2 faible · 3–5 modéré · 6–7 élevé · 8–10 très élevé · 11+ extrême.</p></div><div class="panel widepanel"><h3 id="fullDaysTitle">Tendance sur 7 jours</h3><div class="muted" id="weatherSourceNote"></div><div class="forecastgrid" id="fullDays"></div></div></section>
<section id="map" class="view"><h1 class="pagehead">Carte météo mondiale</h1><div class="lead">Cliquez n'importe où sur la carte pour obtenir la température et les prévisions locales.</div><div class="maplayout"><div id="worldMap"></div><div class="panel mapinfo"><h3 id="mapPlace">Sélectionnez une zone</h3><div class="big" id="mapTemp">— °C</div><p id="mapDesc" class="muted">Les données météo apparaîtront ici.</p><div class="metric">Ressenti <b id="mapFeels">—</b></div><div class="metric">Vent <b id="mapWind">—</b></div><div class="metric">Humidité <b id="mapHum">—</b></div><h3 style="margin-top:18px">Prochaines heures</h3><div id="mapNext" class="muted">—</div></div></div></section>
<section id="alerts" class="view"><div class="alert-heading"><div><h1 class="pagehead">Vigilance météorologique</h1><div class="lead">Les alertes officielles en France, expliquées simplement pour savoir quoi faire.</div></div><a class="official-link" href="https://vigilance.meteofrance.fr/fr" target="_blank" rel="noopener">↗ Météo-France Vigilance</a></div><div id="alertSummary" class="panel alert-summary"><div class="summary-pulse">●</div><div><b>Chargement de la situation nationale…</b><p class="muted">Récupération des derniers bulletins disponibles.</p></div></div><div class="level-legend"><div class="level-card green"><span class="level-dot"></span><div><b>Vert — Pas de vigilance</b><p>Aucun phénomène particulier signalé.</p></div></div><div class="level-card yellow"><span class="level-dot"></span><div><b>Jaune — Soyez attentif</b><p>Phénomène habituel mais potentiellement dangereux.</p></div></div><div class="level-card orange"><span class="level-dot"></span><div><b>Orange — Soyez très vigilant</b><p>Phénomène dangereux, des dégâts sont possibles.</p></div></div><div class="level-card red"><span class="level-dot"></span><div><b>Rouge — Vigilance absolue</b><p>Phénomène exceptionnel, danger majeur.</p></div></div></div><div class="alerts-layout"><div class="panel alerts-main"><div class="section-title"><h2>Vigilances en cours</h2><span id="alertUpdated" class="muted">Mise à jour en cours</span></div><div id="activeAlerts" class="active-alert-list">Chargement des bulletins…</div><div class="source-note" id="alertSourceNote">Sources : MeteoAlarm et, en secours, couche départementale Météo-France relayée par Esri France. Pour les phénomènes et horaires exacts, consultez toujours le bulletin officiel.</div></div><aside class="panel alert-side"><h2>Actualités météo</h2><p class="muted">Articles récents en lien avec les phénomènes météo.</p><div id="alertNews" class="alert-news-list">Chargement des actualités…</div><a class="official-link side-link" href="https://vigilance.meteofrance.fr/fr" target="_blank" rel="noopener">Consulter les cartes officielles ↗</a></aside></div><div class="panel widepanel"><h2>Les phénomènes surveillés</h2><div class="alertgrid" id="alertTypes"></div></div></section>
<section id="news" class="view"><h1 class="pagehead">Actualités</h1><div class="lead">Dernières actualités issues de flux RSS de médias francophones.</div><div id="newsItems" class="newsgrid"><div class="panel">Chargement des actualités…</div></div></section>
</main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const $=id=>document.getElementById(id); let current={lat:48.8566,lon:2.3522,name:"Paris",timezone:"Europe/Paris"}, weather=null, miniMap, worldMap, miniMarker, worldMarker, mapClickSeq=0;
const icons={0:"☀️",1:"🌤️",2:"⛅",3:"☁️",45:"🌫️",48:"🌫️",51:"🌦️",53:"🌦️",55:"🌧️",61:"🌧️",63:"🌧️",65:"🌧️",71:"🌨️",73:"❄️",75:"❄️",80:"🌦️",81:"🌧️",82:"⛈️",95:"⛈️",96:"⛈️",99:"⛈️"};
const labels={0:"Ensoleillé",1:"Principalement dégagé",2:"Partiellement nuageux",3:"Couvert",45:"Brouillard",48:"Brouillard givrant",51:"Bruine légère",53:"Bruine",55:"Bruine forte",61:"Pluie faible",63:"Pluie",65:"Pluie forte",71:"Neige faible",73:"Neige",75:"Neige forte",80:"Averses faibles",81:"Averses",82:"Fortes averses",95:"Orage",96:"Orage et grêle",99:"Orage violent"};
function icon(code){return icons[code]||"🌤️"} function label(code){return labels[code]||"Conditions météo"}
function niceDate(s,opt={weekday:"short",day:"numeric",month:"short"}){return new Date(s).toLocaleDateString("fr-FR",opt)}
function niceTime(s){return new Date(s).toLocaleTimeString("fr-FR",{hour:"2-digit",minute:"2-digit"})}
function setView(v){document.querySelectorAll(".view").forEach(x=>x.classList.toggle("active",x.id===v));document.querySelectorAll("nav button").forEach(x=>x.classList.toggle("active",x.dataset.view===v));if(v==="map"&&!worldMap)initWorldMap();if(v==="map"&&worldMap)requestAnimationFrame(()=>worldMap.invalidateSize());if(v==="news")loadNews();if(v==="alerts")loadAlerts();if(v==="forecast"&&weather)renderForecast()}
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>setView(b.dataset.view));
async function loadWeather(place=current){
 current=place; try{const r=await fetch(`/api/weather?lat=${place.lat}&lon=${place.lon}&name=${encodeURIComponent(place.name)}`);if(!r.ok)throw Error("API météo indisponible");weather=await r.json();renderWeather()}catch(e){$("description").textContent="Impossible de récupérer les données météo. Vérifiez votre connexion."}
}
function renderWeather(){
 const c=weather.current,h=weather.hourly,d=weather.daily,now=new Date(c.time),idx=Math.max(0,h.time.findIndex(t=>new Date(t)>=now));
 const fallbackSource=String(weather.source||"").includes("wttr.in");
 if($("homeHoursTitle"))$("homeHoursTitle").textContent=fallbackSource?"Prévisions par pas de 3 heures":"Prévisions heures par heures";
 if($("homeDaysTitle"))$("homeDaysTitle").textContent=fallbackSource?"Prévisions 3 jours (source de secours)":"Prévisions 7 jours";
 if($("homeWeatherSource"))$("homeWeatherSource").textContent=weather.source_notice||`Source météo : ${weather.source||"Open-Meteo"}.`;
 $("cityName").textContent=current.name;$("mapCity").textContent=current.name;$("todayDate").textContent="Aujourd’hui · "+niceDate(c.time,{day:"numeric",month:"long"});
 $("temp").textContent=Math.round(c.temperature_2m);$("currentIcon").textContent=icon(c.weather_code);$("description").textContent=label(c.weather_code);
 $("feels").textContent=Math.round(c.apparent_temperature)+"°C";$("wind").textContent=Math.round(c.wind_speed_10m)+" km/h";$("humidity").textContent=c.relative_humidity_2m+" %";$("pressure").textContent=Math.round(c.pressure_msl)+" hPa";const uvNow=Number(d.uv_index_max?.[0]??h.uv_index?.[idx]??0);$("uvHome").textContent=uvNow.toFixed(1)+" · "+uvLevel(uvNow);
 $("homeHours").innerHTML=Array.from({length:8},(_,i)=>{let j=Math.min(idx+i,h.time.length-1);return `<div class="hour"><div>${i===0?"Maintenant":niceTime(h.time[j])}</div><div class="ico">${icon(h.weather_code[j])}</div><b>${Math.round(h.temperature_2m[j])}°C</b></div>`}).join("");
 $("homeDays").innerHTML=d.time.map((t,i)=>`<div class="day"><span>${niceDate(t)}</span><span>${icon(d.weather_code[i])}</span><b>${Math.round(d.temperature_2m_max[i])}° / ${Math.round(d.temperature_2m_min[i])}°</b></div>`).join("");
 if($("map").classList.contains("active"))renderMapInfo(weather);
 if(miniMap){miniMap.invalidateSize();miniMap.setView([current.lat,current.lon],10,{animate:false});if(miniMarker)miniMap.removeLayer(miniMarker);miniMarker=L.marker([current.lat,current.lon]).addTo(miniMap).bindPopup(`<b>${current.name}</b><br>${Math.round(c.temperature_2m)}°C`).openPopup();requestAnimationFrame(()=>miniMap.invalidateSize())}
}
function renderForecast(){
 const h=weather.hourly,now=new Date(weather.current.time),idx=Math.max(0,h.time.findIndex(t=>new Date(t)>=now));
 const fallbackSource=String(weather.source||"").includes("wttr.in");
 $("fullHours").innerHTML=Array.from({length:fallbackSource?8:24},(_,i)=>{let j=Math.min(idx+i,h.time.length-1);return `<div class="forecastcard"><span>${niceDate(h.time[j],{weekday:"short",day:"numeric",month:"short"})} · ${niceTime(h.time[j])}</span><div class="big">${icon(h.weather_code[j])}</div><strong>${Math.round(h.temperature_2m[j])}°C</strong><p class="muted">Pluie : ${h.precipitation_probability[j]??0}%</p><p class="muted">Vent : ${Math.round(h.wind_speed_10m[j])} km/h</p><p class="uv">UV : ${Number(h.uv_index[j]||0).toFixed(1)}</p></div>`}).join("");
 const u=h.uv_index.slice(idx,idx+(fallbackSource?8:24)),max=Math.max(...u,0),peak=u.indexOf(max);
 $("uvCards").innerHTML=`<div class="forecastcard"><span>Indice UV maximum prévu</span><div class="big uv">${max.toFixed(1)}</div><strong>${uvLevel(max)}</strong></div><div class="forecastcard"><span>Heure du pic</span><div class="big">☀️</div><strong>${peak>=0?niceTime(h.time[idx+peak]):"—"}</strong></div><div class="forecastcard"><span>UV actuel</span><div class="big uv">${Number(h.uv_index[idx]||0).toFixed(1)}</div><strong>${uvLevel(h.uv_index[idx]||0)}</strong></div><div class="forecastcard"><span>Conseil</span><div class="big">🧴</div><strong>${max>=3?"Protection solaire conseillée":"Exposition généralement faible"}</strong></div>`;
 if($("fullDaysTitle")){const fallbackSource=String(weather.source||"").includes("wttr.in");$("fullDaysTitle").textContent=fallbackSource?"Tendance sur 3 jours (source de secours)":"Tendance sur 7 jours";$("weatherSourceNote").textContent=weather.source_notice||`Source météo : ${weather.source||"Open-Meteo"}.`;if($("fullHoursTitle"))$("fullHoursTitle").textContent=fallbackSource?"Prévisions détaillées — 24 heures (pas de 3 h)":"Prévisions détaillées — 24 heures";if($("forecastLead"))$("forecastLead").textContent=fallbackSource?"Prévisions de secours par pas de 3 heures, indice UV et tendance sur 3 jours.":"Les prochaines 24 heures, l'indice UV et les tendances sur 7 jours.";}
 $("fullDays").innerHTML=weather.daily.time.map((t,i)=>`<div class="forecastcard"><span>${niceDate(t,{weekday:"long",day:"numeric",month:"short"})}</span><div class="big">${icon(weather.daily.weather_code[i])}</div><strong>${Math.round(weather.daily.temperature_2m_max[i])}° / ${Math.round(weather.daily.temperature_2m_min[i])}°</strong><p class="uv">UV max : ${Number(weather.daily.uv_index_max[i]||0).toFixed(1)}</p><p class="muted">Pluie max : ${weather.daily.precipitation_probability_max[i]??0}%</p></div>`).join("");
}
function uvLevel(v){return v<3?"Faible":v<6?"Modéré":v<8?"Élevé":v<11?"Très élevé":"Extrême"}
function initMini(){miniMap=L.map("miniMap",{zoomControl:false,attributionControl:false}).setView([current.lat,current.lon],6);L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:18}).addTo(miniMap);miniMap.on("click",e=>selectCoords(e.latlng.lat,e.latlng.lng));miniMarker=L.marker([current.lat,current.lon]).addTo(miniMap).bindPopup("<b>Paris</b>").openPopup()}
function initWorldMap(){worldMap=L.map("worldMap").setView([25,5],2);L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:18,attribution:'© OpenStreetMap'}).addTo(worldMap);worldMap.on("click",e=>selectCoords(e.latlng.lat,e.latlng.lng))}
async function selectCoords(lat,lon){
 const clickId=++mapClickSeq;
 const coords=[lat,lon];
 try{
  if(worldMap){
   worldMap.setView(coords,Math.max(worldMap.getZoom(),9),{animate:true});
   if(worldMarker)worldMap.removeLayer(worldMarker);
   worldMarker=L.marker(coords).addTo(worldMap).bindPopup("Recherche du lieu…").openPopup();
  }
  if(miniMap){
   miniMap.setView(coords,10,{animate:true});
   if(miniMarker)miniMap.removeLayer(miniMarker);
   miniMarker=L.marker(coords).addTo(miniMap).bindPopup("Recherche du lieu…").openPopup();
  }
  $("mapPlace").textContent="Recherche de la ville…";
  const [weatherResponse,placeResponse]=await Promise.all([
   fetch(`/api/weather?lat=${lat}&lon=${lon}`),
   fetch(`/api/reverse?lat=${lat}&lon=${lon}`)
  ]);
  if(!weatherResponse.ok)throw Error("Météo indisponible");
  const d=await weatherResponse.json();
  let place=`${lat.toFixed(3)}, ${lon.toFixed(3)}`;
  if(placeResponse.ok){
   const p=await placeResponse.json();
   if(p.name)place=p.name;
  }
  if(clickId!==mapClickSeq)return;
  renderMapInfo(d,place);
  if(worldMarker){
   worldMarker.setLatLng(coords);
   worldMarker.setPopupContent(`<b>${place}</b><br>${Math.round(d.current.temperature_2m)} °C`).openPopup();
  }
 }catch(e){
  if(clickId!==mapClickSeq)return;
  $("mapPlace").textContent="Données indisponibles";
  if(worldMarker)worldMarker.setPopupContent("Données indisponibles").openPopup();
 }
}
function renderMapInfo(d,place=current.name){$("mapPlace").textContent=place;$("mapTemp").textContent=Math.round(d.current.temperature_2m)+" °C";$("mapDesc").textContent=label(d.current.weather_code);$("mapFeels").textContent=Math.round(d.current.apparent_temperature)+"°C";$("mapWind").textContent=Math.round(d.current.wind_speed_10m)+" km/h";$("mapHum").textContent=d.current.relative_humidity_2m+" %";$("mapNext").textContent=d.hourly.time.slice(0,6).map((t,i)=>niceTime(t)+" "+Math.round(d.hourly.temperature_2m[i])+"°").join(" · ")}
const types=[
 ["🌬️","Vent violent","Rafales pouvant provoquer des chutes de branches ou d’objets."],
 ["🌧️","Pluie-inondation","Pluies intenses, ruissellements et inondations possibles."],
 ["⛈️","Orages","Foudre, fortes pluies, grêle et rafales possibles."],
 ["❄️","Neige-verglas","Chaussées glissantes et conditions de circulation difficiles."],
 ["🌡️","Canicule","Chaleur intense, vigilance pour les personnes fragiles."],
 ["🥶","Grand froid","Températures très basses et risques liés au froid."],
 ["🏔️","Avalanches","Instabilité du manteau neigeux en montagne."],
 ["🌊","Vagues-submersion","Mer dangereuse et risque de submersion du littoral."],
 ["🏞️","Crues","Montée des cours d’eau et débordements possibles."],
 ["🌊","Inondations","Accumulation d’eau et inondations de zones basses."],
 ["🔥","Feux de forêt","Conditions favorables aux départs et à la propagation des feux."],
 ["🌀","Cyclone / tempête tropicale","Vents violents et fortes précipitations possibles."]
];
function alertTypeLevel(name,alerts){
 const a=alerts.find(x=>x.title===name);
 return a?a.level_fr:"Aucune alerte reçue";
}
async function loadAlerts(){
 const typesEl=$("alertTypes");
 typesEl.innerHTML=types.map(([emoji,name,desc])=>`<div class="alert"><div class="level">${emoji} ${name}</div><div class="muted">${desc}</div></div>`).join("");
 try{
  const [r,nr]=await Promise.all([fetch("/api/alerts"),fetch("/api/news")]);
  const d=await r.json(), newsData=await nr.json();
  const alerts=Array.isArray(d.alerts)?d.alerts:[];
  $("alertUpdated").textContent=d.updated?`Vérifié : ${d.updated}`:"Mise à jour non disponible";
  if($("alertSourceNote")) $("alertSourceNote").textContent=(d.source?`Source utilisée : ${d.source}. `:"")+(d.freshness||"Pour les phénomènes, horaires et consignes exacts, consultez le bulletin officiel de Météo-France.");
  if(!d.available){
   $("alertSummary").style.borderLeftColor="#e5a83b";
   $("alertSummary").innerHTML=`<div class="summary-pulse" style="color:#f2bd54">●</div><div><b>Vigilance non vérifiée</b><p class="muted">${d.message||"Le flux officiel ne répond pas. Aucun niveau ne peut être confirmé."}</p></div>`;
   $("activeAlerts").innerHTML=`<div class="alert-unavailable"><b>Impossible de confirmer les niveaux actuellement.</b><br>Ne considère pas l’absence de données comme une absence de danger. Consulte la carte officielle de <a href="https://vigilance.meteofrance.fr/fr" target="_blank" rel="noopener" style="color:white">Météo-France Vigilance</a>.</div>`;
  }else if(!alerts.length){
   $("alertSummary").style.borderLeftColor="#35c46a";
   $("alertSummary").innerHTML=`<div class="summary-pulse" style="color:#70df91">●</div><div><b>Aucune vigilance en cours dans les bulletins reçus</b><p class="muted">Le flux consulté ne signale pas d’alerte en cours. Vérifie la carte officielle pour ta commune.</p></div>`;
   $("activeAlerts").innerHTML=`<div class="alert-empty"><b>Aucune vigilance active signalée par les sources consultées.</b><p class="muted">Cela ne remplace pas les consignes des autorités locales. Consulte Météo-France en cas de doute.</p></div>`;
  }else{
   const counts={rouge:0,orange:0,jaune:0,vert:0};
   alerts.forEach(a=>{if(counts[a.level]!==undefined)counts[a.level]++});
   const highest=counts.rouge?"rouge":counts.orange?"orange":counts.jaune?"jaune":"vert";
   const summaryText={rouge:"Au moins une vigilance rouge est signalée : danger exceptionnel.",orange:"Au moins une vigilance orange est signalée : phénomène dangereux.",jaune:"Des vigilances jaunes sont signalées : reste attentif à l’évolution.",vert:"Les bulletins reçus ne signalent que des phénomènes de niveau vert."};
   $("alertSummary").style.borderLeftColor={rouge:"#f04455",orange:"#ff8b32",jaune:"#f3d529",vert:"#35c46a"}[highest];
   const pulseColor={rouge:"#ff5363",orange:"#ff9b4d",jaune:"#f3d529",vert:"#70df91"}[highest];
   const fallbackNote=d.fallback?`<br><span class="muted">Source de secours : niveau maximal départemental ; consulte le bulletin officiel pour le détail des phénomènes et horaires.</span>`:"";
   $("alertSummary").innerHTML=`<div class="summary-pulse" style="color:${pulseColor}">●</div><div><b>${alerts.length} vigilance${alerts.length>1?"s":""} signalée${alerts.length>1?"s":""} · ${summaryText[highest]}</b><p class="muted">${counts.rouge} rouge · ${counts.orange} orange · ${counts.jaune} jaune · ${counts.vert} vert${fallbackNote}</p></div>`;
   $("activeAlerts").innerHTML=alerts.map(a=>`<article class="alert-item level-${a.level}"><div class="alert-item-top"><span class="alert-pill ${a.level||"inconnu"}">${a.level_fr||"Niveau non précisé"}</span><span class="alert-meta">${a.source||"Source officielle"}</span></div><h3>${a.emoji||"⚠️"} ${a.title}</h3><div class="alert-area">📍 ${a.area}</div><p>${a.description}</p><div class="alert-meta">${a.onset?`<span>Début : ${a.onset}</span>`:""}${a.expires?`<span>Fin prévue : ${a.expires}</span>`:""}</div><div class="alert-advice"><b>Conseil :</b> ${a.advice||"Suivez les consignes des autorités et consultez le bulletin officiel."}</div></article>`).join("");
  }
  const weatherNews=(newsData.items||[]).filter(n=>/météo|orage|tempête|pluie|inond|neige|verglas|canicule|vigilance|crue|vent violent|cyclone|froid|chaleur/i.test((n.title||"")+" "+(n.description||""))).slice(0,5);
  $("alertNews").innerHTML=weatherNews.length?weatherNews.map(n=>`<article class="alert-news-item"><span class="pill">${n.source}</span><p class="muted">${n.date||"Actualité récente"}</p><a href="${n.link}" target="_blank" rel="noopener">${n.title}</a><p>${n.description||""}</p></article>`).join(""):`<div class="muted">Aucun article météo récent n’a été trouvé dans les flux médias pour le moment. Consulte les actualités générales dans l’onglet Actualités.</div>`;
 }catch(e){
  $("alertSummary").style.borderLeftColor="#e5a83b";
  $("alertSummary").innerHTML="<div class='summary-pulse' style='color:#f2bd54'>●</div><div><b>Chargement impossible</b><p class='muted'>Vérifie ta connexion puis réessaie.</p></div>";
  $("activeAlerts").textContent="Impossible de récupérer les bulletins de vigilance.";
  $("alertNews").textContent="Actualités météo indisponibles.";
 }
}
async function loadNews(){if($("newsItems").dataset.loaded)return;try{const r=await fetch("/api/news"),d=await r.json();$("newsItems").innerHTML=d.items.length?d.items.map(n=>`<article class="panel newsitem"><span class="pill">${n.source}</span><p class="muted">${n.date||""}</p><a href="${n.link}" target="_blank" rel="noopener">${n.title}</a><p>${n.description}</p></article>`).join(""):"<div class='panel'>Aucune actualité reçue. Les flux RSS peuvent être temporairement indisponibles.</div>";$("newsItems").dataset.loaded="1"}catch{$("newsItems").textContent="Actualités indisponibles."}}
let timer; let lastSearchQuery=""; $("citySearch").addEventListener("input",()=>{clearTimeout(timer);timer=setTimeout(async()=>{let q=$("citySearch").value.trim();if(q.length<3){$("searchResults").style.display="none";return}if(q.toLocaleLowerCase()===lastSearchQuery)return;lastSearchQuery=q.toLocaleLowerCase();try{let r=await fetch("/api/search?q="+encodeURIComponent(q)),d=await r.json();$("searchResults").innerHTML=(d||[]).map((p,i)=>`<div class="result" data-i="${i}">${p.name} · ${p.admin} · ${p.country}</div>`).join("");$("searchResults").style.display="block";$("searchResults").querySelectorAll(".result").forEach(el=>el.onclick=()=>{let p=d[+el.dataset.i];p.lon=p.lon??p.longitude;p.lat=p.lat??p.latitude;current={lat:p.lat,lon:p.lon,name:p.name,timezone:p.timezone};$("searchResults").style.display="none";$("citySearch").value="";loadWeather(current)})}catch{}},600)});
$("todayDate").textContent=new Date().toLocaleDateString("fr-FR",{day:"numeric",month:"long"});initMini();loadWeather();
</script></body></html>
"""
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
