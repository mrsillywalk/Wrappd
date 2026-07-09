"""
Top-30 Films & TV Shows — Backend Proof of Concept
Draait als lichtgewicht API-service op Proxmox (LXC of Docker).

Databron: TMDB API (https://www.themoviedb.org/settings/api — gratis key).
Optioneel: OMDb API voor IMDb/Rotten Tomatoes-scores per titel.

Start:  uvicorn main:app --host 0.0.0.0 --port 8000
Docs:   http://<host>:8000/docs  (interactieve Swagger UI)

Zonder API-key testen: MOCK_MODE=1 uvicorn main:app ...
"""

import os
import json
import asyncio
import logging
import sqlite3
from typing import Optional, Literal, Any

import httpx
from cachetools import TTLCache
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

try:
    import redis.asyncio as aioredis  # redis-py (pip install "redis>=5")
except ImportError:
    aioredis = None  # zonder pakket draait alles gewoon op geheugen

log = logging.getLogger("wrappd")

TMDB_KEY = os.environ.get("TMDB_API_KEY", "")
OMDB_KEY = os.environ.get("OMDB_API_KEY", "")  # optioneel
MOCK_MODE = os.environ.get("MOCK_MODE", "0") == "1"
DB_PATH = os.environ.get("DB_PATH", "watched.db")  # SQLite-bestand voor kijklijsten
DEFAULT_LANG = os.environ.get("DEFAULT_LANG", "en-US")  # standaardtaal als er geen wordt meegegeven
BAYES_M = float(os.environ.get("BAYES_M", "500"))  # drempel voor het Bayesiaanse gemiddelde (IMDb-methode)
REDIS_URL = os.environ.get("REDIS_URL", "")  # bijv. redis://localhost:6379/0; leeg = alleen geheugen
POOL_PAGES = int(os.environ.get("POOL_PAGES", "5"))  # TMDB-pagina's (20 per pagina) voor de pool om uit bij te vullen

TMDB_BASE = "https://api.themoviedb.org/3"

app = FastAPI(
    title="Top-30 Films & Series API",
    description="PoC-backend: top 30 per genre met filters op jaar, acteur, regisseur en rating.",
    version="0.1.0",
)

# ---------------------------------------------------------------- cache

# Eén Redis-client voor de hele app (lazy: pas verbinden als hij nodig is).
_redis = None


def get_redis():
    """Geef de gedeelde Redis-client, of None als Redis niet is ingesteld."""
    global _redis
    if not REDIS_URL or aioredis is None:
        return None
    if _redis is None:
        # decode_responses=True: we werken met strings (JSON), niet met bytes.
        _redis = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    return _redis


# Sentinel om een écht cache-gemis te onderscheiden van een opgeslagen None.
# (Een 'niet gevonden'-persoon cachen we bewust als None, zodat we die lookup
#  niet telkens opnieuw doen. Zonder deze truc zou None 'gemis' lijken.)
MISS = object()


class Cache:
    """
    Cache met Redis als hoofdopslag en automatische terugval op geheugen.

    - Staat Redis aan en is hij bereikbaar, dan is Redis leidend.
    - Valt Redis weg (of is hij niet ingesteld), dan gebruikt dezelfde code
      een lokale TTLCache. Geen crash, geen configuratie nodig.
    - Waarden worden als JSON opgeslagen, verpakt in {"v": ...}, zodat een
      opgeslagen None netjes terugkomt en niet als 'gemis' geldt.
    """

    def __init__(self, namespace: str, ttl: int, maxsize: int):
        self.namespace = namespace
        self.ttl = ttl
        self.memory = TTLCache(maxsize=maxsize, ttl=ttl)  # de vangnet-opslag

    def _key(self, key: Any) -> str:
        raw = key if isinstance(key, str) else json.dumps(key, default=str)
        return f"{self.namespace}:{raw}"

    async def get(self, key: Any) -> Any:
        """Geef de waarde terug, of de MISS-sentinel als hij er niet is."""
        r = get_redis()
        if r is not None:
            try:
                raw = await r.get(self._key(key))
                return json.loads(raw)["v"] if raw is not None else MISS
            except Exception as e:  # Redis onbereikbaar -> vangnet
                log.warning("Redis get faalde (%s); val terug op geheugen", e)
        return self.memory.get(key, MISS)

    async def set(self, key: Any, value: Any) -> None:
        r = get_redis()
        if r is not None:
            try:
                await r.set(self._key(key), json.dumps({"v": value}, default=str), ex=self.ttl)
                return
            except Exception as e:  # Redis onbereikbaar -> vangnet
                log.warning("Redis set faalde (%s); val terug op geheugen", e)
        self.memory[key] = value


# Cache: genres 24u, zoekresultaten 1u, persoon-lookups 24u
genre_cache = Cache("genre", ttl=86400, maxsize=8)
list_cache = Cache("list", ttl=3600, maxsize=512)
person_cache = Cache("person", ttl=86400, maxsize=1024)

MediaType = Literal["movie", "tv"]


# ---------------------------------------------------------------- scores

def bayesian_score(R: Optional[float], v: Optional[int], C: float, m: float) -> Optional[float]:
    """
    Bayesiaans gemiddelde (IMDb-methode).
        score = (v/(v+m))*R + (m/(v+m))*C
    R = rauwe rating, v = aantal stemmen, C = lijstgemiddelde, m = drempel (BAYES_M).
    Titels met weinig stemmen worden naar C toe getrokken; titels met veel stemmen
    houden bijna hun eigen R. Zo verslaat '9,5 uit 12 stemmen' geen klassieker.
    """
    if R is None or v is None:
        return None
    v = float(v)
    return (v / (v + m)) * R + (m / (v + m)) * C


# Elke bron is een LOS, UITSCHAKELBAAR blokje met eigen gewicht.
# Nu alleen TMDB actief; Trakt/RT/IMDb later inpluggen MÉT licentie
# (zet dan enabled=True en geef een gewicht). "value" geeft een 0-10-waarde
# terug voor een titel, of None als die bron geen cijfer heeft.
SCORE_SOURCES = [
    {
        "key": "tmdb",
        "enabled": True,
        "weight": 1.0,
        "value": lambda it, ctx: bayesian_score(
            it.get("tmdb_rating"), it.get("vote_count"), ctx["tmdb_mean"], ctx["bayes_m"]
        ),
    },
    # Voorbeeld voor later (uit tot je een licentie + databron hebt):
    # {"key": "trakt", "enabled": False, "weight": 0.0,
    #  "value": lambda it, ctx: it.get("trakt_rating")},
]


def composite_score(it: dict, ctx: dict) -> tuple[Optional[float], dict]:
    """
    Combineer alle ingeschakelde bronnen tot één eindcijfer (gewogen gemiddelde).
    Geeft (eindcijfer, losse-bronscores) terug. Bronnen zonder cijfer tellen niet mee,
    zodat het gewicht netjes herverdeeld wordt over de bronnen die er wél zijn.
    """
    acc = 0.0
    total_w = 0.0
    per_source: dict[str, float] = {}
    for src in SCORE_SOURCES:
        if not src["enabled"] or src["weight"] <= 0:
            continue
        val = src["value"](it, ctx)
        if val is None:
            continue
        per_source[src["key"]] = round(val, 3)
        acc += val * src["weight"]
        total_w += src["weight"]
    if total_w == 0:
        return None, per_source
    return round(acc / total_w, 3), per_source


def apply_scores(items: list[dict]) -> list[dict]:
    """
    Reken voor elke titel een samengesteld 'score'-veld uit en bewaar de losse
    bronscores in 'score_sources' (voor controle). Werkt op KOPIEËN, zodat de
    cache met rauwe TMDB-data niet vervuild raakt.
    """
    ratings = [it["tmdb_rating"] for it in items if it.get("tmdb_rating") is not None]
    tmdb_mean = sum(ratings) / len(ratings) if ratings else 0.0
    ctx = {"tmdb_mean": tmdb_mean, "bayes_m": BAYES_M}

    scored = []
    for it in items:
        it = dict(it)  # kopie: raakt de cache niet aan
        score, sources = composite_score(it, ctx)
        it["score"] = score
        it["score_sources"] = sources
        scored.append(it)
    return scored


# ---------------------------------------------------------------- helpers

async def tmdb_get(client: httpx.AsyncClient, path: str, **params) -> dict:
    params["api_key"] = TMDB_KEY
    r = await client.get(f"{TMDB_BASE}{path}", params=params, timeout=15)
    if r.status_code == 401:
        raise HTTPException(502, "TMDB API-key ongeldig of ontbreekt (zet TMDB_API_KEY).")
    r.raise_for_status()
    return r.json()


async def find_person_id(client: httpx.AsyncClient, name: str) -> Optional[int]:
    """Zoek TMDB persoon-ID op naam (acteur of regisseur)."""
    key = name.strip().lower()
    cached = await person_cache.get(key)
    if cached is not MISS:
        return cached
    data = await tmdb_get(client, "/search/person", query=name)
    results = data.get("results", [])
    pid = results[0]["id"] if results else None
    await person_cache.set(key, pid)  # ook None cachen: 'niet gevonden' onthouden
    return pid


async def enrich_with_omdb(client: httpx.AsyncClient, imdb_id: str) -> dict:
    """Haal IMDb/RT/Metacritic-scores op via OMDb (optioneel)."""
    if not OMDB_KEY or not imdb_id:
        return {}
    try:
        r = await client.get(
            "https://www.omdbapi.com/",
            params={"apikey": OMDB_KEY, "i": imdb_id},
            timeout=10,
        )
        d = r.json()
        ratings = {x["Source"]: x["Value"] for x in d.get("Ratings", [])}
        return {
            "imdb_rating": d.get("imdbRating"),
            "rotten_tomatoes": ratings.get("Rotten Tomatoes"),
            "metacritic": ratings.get("Metacritic"),
        }
    except Exception:
        return {}


def mock_items(media_type: str) -> list[dict]:
    base_year = 1994
    return [
        {
            "id": 1000 + i,
            "title": f"Mock {'Film' if media_type == 'movie' else 'Serie'} {i + 1}",
            "year": base_year + i,
            "tmdb_rating": round(9.3 - i * 0.1, 1),
            "vote_count": 5000 - i * 100,
            "genre_ids": [18],
            "overview": "Voorbeeldtitel voor testen zonder API-key.",
            "poster": None,
        }
        for i in range(30)
    ]


# ---------------------------------------------------------------- watched (gezien)

def db() -> sqlite3.Connection:
    """Open de SQLite-database en zorg dat de tabel bestaat."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS watched (
               device_id  TEXT NOT NULL,
               media_type TEXT NOT NULL,
               item_id    INTEGER NOT NULL,
               PRIMARY KEY (device_id, media_type, item_id)
           )"""
    )
    return conn


class WatchedIn(BaseModel):
    device_id: str       # uniek ID dat de APP aanmaakt en op het toestel bewaart
    media_type: MediaType
    item_id: int         # TMDB-id van de film/serie
    watched: bool = True # True = aanvinken, False = vinkje weghalen


# ---------------------------------------------------------------- endpoints

@app.get("/api/health")
async def health():
    # Live checken of Redis echt bereikbaar is; anders draait de cache op geheugen.
    cache_backend = "memory"
    r = get_redis()
    if r is not None:
        try:
            await r.ping()
            cache_backend = "redis"
        except Exception:
            cache_backend = "memory (redis onbereikbaar)"
    return {
        "status": "ok",
        "mock_mode": MOCK_MODE,
        "omdb_enabled": bool(OMDB_KEY),
        "cache_backend": cache_backend,
    }


@app.post("/api/watched")
def set_watched(w: WatchedIn):
    """Markeer een titel als gezien (of haal het vinkje weg) voor dit device."""
    conn = db()
    if w.watched:
        conn.execute(
            "INSERT OR IGNORE INTO watched (device_id, media_type, item_id) VALUES (?, ?, ?)",
            (w.device_id, w.media_type, w.item_id),
        )
    else:
        conn.execute(
            "DELETE FROM watched WHERE device_id=? AND media_type=? AND item_id=?",
            (w.device_id, w.media_type, w.item_id),
        )
    conn.commit()
    conn.close()
    return {"ok": True, "item_id": w.item_id, "watched": w.watched}


@app.get("/api/watched")
def get_watched(device_id: str, media_type: MediaType = "movie"):
    """Geef de lijst met gezien-item-id's voor dit device terug."""
    conn = db()
    rows = conn.execute(
        "SELECT item_id FROM watched WHERE device_id=? AND media_type=?",
        (device_id, media_type),
    ).fetchall()
    conn.close()
    return {"device_id": device_id, "media_type": media_type,
            "watched": [r[0] for r in rows]}


@app.get("/api/genres")
async def genres(
    media_type: MediaType = "movie",
    language: str = Query(DEFAULT_LANG, description="Taalcode, bijv. en-US, nl-NL, de-DE, fr-FR, es-ES"),
):
    """Lijst van beschikbare genres voor films of tv-series, in de gevraagde taal."""
    if MOCK_MODE:
        return {"genres": [{"id": 18, "name": "Drama"}, {"id": 35, "name": "Comedy"}]}
    cache_key = (media_type, language)
    cached = await genre_cache.get(cache_key)
    if cached is not MISS:
        return cached
    async with httpx.AsyncClient() as client:
        data = await tmdb_get(client, f"/genre/{media_type}/list", language=language)
    await genre_cache.set(cache_key, data)
    return data


@app.get("/api/top30")
async def top30(
    media_type: MediaType = "movie",
    genre_id: Optional[int] = Query(None, description="Genre-ID uit /api/genres"),
    actor: Optional[str] = Query(None, description="Naam van acteur"),
    director: Optional[str] = Query(None, description="Naam van regisseur (alleen films)"),
    year_min: Optional[int] = Query(None, ge=1900),
    year_max: Optional[int] = Query(None, le=2100),
    rating_min: float = Query(0.0, ge=0, le=10),
    rating_max: float = Query(10.0, ge=0, le=10),
    sort_by: Literal["rating", "year_asc", "year_desc"] = "rating",
    min_votes: int = Query(200, description="Minimum aantal stemmen (filtert obscure titels)"),
    enrich: bool = Query(False, description="IMDb/RT-scores toevoegen via OMDb (langzamer)"),
    device_id: Optional[str] = Query(None, description="Device-ID; nodig voor exclude_watched"),
    exclude_watched: bool = Query(False, description="Verberg titels die dit device al gezien heeft"),
    limit: int = Query(30, ge=1, le=100, description="Aantal titels in de lijst (bijv. 10 of 30)"),
    language: str = Query(DEFAULT_LANG, description="Taalcode, bijv. en-US, nl-NL, de-DE, fr-FR, es-ES"),
):
    """
    Genereer een top-lijst op basis van de opgegeven filters.
    Titels en omschrijvingen komen in de gevraagde taal (language).
    Ratings komen van TMDB; met enrich=true ook IMDb/Rotten Tomatoes (OMDb-key vereist).
    Met exclude_watched=true + device_id worden gezien titels weggelaten en schuift
    de lijst op, zodat je altijd een volle top krijgt van wat je nog niet zag.
    """
    if MOCK_MODE:
        items = mock_items(media_type)
    else:
        cache_key = (media_type, genre_id, actor, director, year_min, year_max,
                     rating_min, rating_max, sort_by, min_votes, language)
        cached = await list_cache.get(cache_key)
        if cached is not MISS:
            items = cached
        else:
            items = await fetch_from_tmdb(media_type, genre_id, actor, director,
                                          year_min, year_max, rating_min, rating_max,
                                          min_votes, language)
            await list_cache.set(cache_key, items)

    # Samengesteld eindcijfer berekenen (werkt op kopieën; cache blijft schoon)
    items = apply_scores(items)

    # Sorteren
    if sort_by == "rating":
        # Op het samengestelde 'score', niet op de rauwe TMDB-rating.
        # Titels zonder score belanden onderaan (-1).
        items = sorted(items, key=lambda x: x["score"] if x["score"] is not None else -1.0,
                       reverse=True)
    elif sort_by == "year_asc":
        items = sorted(items, key=lambda x: x["year"] or 0)
    else:
        items = sorted(items, key=lambda x: x["year"] or 0, reverse=True)

    # Gezien titels eruit filteren (vóór het afkappen, zodat de lijst opschuift)
    if exclude_watched and device_id:
        conn = db()
        rows = conn.execute(
            "SELECT item_id FROM watched WHERE device_id=? AND media_type=?",
            (device_id, media_type),
        ).fetchall()
        conn.close()
        seen_ids = {r[0] for r in rows}
        items = [it for it in items if it["id"] not in seen_ids]

    items = items[:limit]

    # Optioneel verrijken met OMDb
    if enrich and not MOCK_MODE and OMDB_KEY:
        async with httpx.AsyncClient() as client:
            ids = await asyncio.gather(*[
                tmdb_get(client, f"/{media_type}/{it['id']}/external_ids") for it in items
            ], return_exceptions=True)
            extra = await asyncio.gather(*[
                enrich_with_omdb(client, (d.get("imdb_id") or "") if isinstance(d, dict) else "")
                for d in ids
            ])
        for it, ex in zip(items, extra):
            it.update(ex)

    return {"count": len(items), "results": items}


async def fetch_from_tmdb(media_type, genre_id, actor, director,
                          year_min, year_max, rating_min, rating_max,
                          min_votes, language=DEFAULT_LANG) -> list[dict]:
    date_field = "primary_release_date" if media_type == "movie" else "first_air_date"
    params = {
        "sort_by": "vote_average.desc",
        "vote_count.gte": min_votes,
        "vote_average.gte": rating_min,
        "vote_average.lte": rating_max,
        "language": language,
    }
    if genre_id:
        params["with_genres"] = genre_id
    if year_min:
        params[f"{date_field}.gte"] = f"{year_min}-01-01"
    if year_max:
        params[f"{date_field}.lte"] = f"{year_max}-12-31"

    async with httpx.AsyncClient() as client:
        if actor:
            pid = await find_person_id(client, actor)
            if pid is None:
                raise HTTPException(404, f"Acteur '{actor}' niet gevonden.")
            params["with_cast" if media_type == "movie" else "with_people"] = pid
        if director and media_type == "movie":
            pid = await find_person_id(client, director)
            if pid is None:
                raise HTTPException(404, f"Regisseur '{director}' niet gevonden.")
            params["with_crew"] = pid

        # Haal een diepere pool op (POOL_PAGES × 20 titels) zodat de app kan
        # bijvullen als je gezien titels verbergt. Alle pagina's parallel.
        pages = await asyncio.gather(*[
            tmdb_get(client, f"/discover/{media_type}", page=p, **params)
            for p in range(1, POOL_PAGES + 1)
        ])

    items = []
    seen_ids = set()  # dedup: TMDB kan bij weinig pagina's de laatste herhalen
    for page in pages:
        for r in page.get("results", []):
            if r["id"] in seen_ids:
                continue
            seen_ids.add(r["id"])
            date = r.get("release_date") or r.get("first_air_date") or ""
            items.append({
                "id": r["id"],
                "title": r.get("title") or r.get("name"),
                "year": int(date[:4]) if date[:4].isdigit() else None,
                "tmdb_rating": r.get("vote_average"),
                "vote_count": r.get("vote_count"),
                "genre_ids": r.get("genre_ids", []),
                "overview": r.get("overview"),
                "poster": f"https://image.tmdb.org/t/p/w342{r['poster_path']}" if r.get("poster_path") else None,
            })
    return items
