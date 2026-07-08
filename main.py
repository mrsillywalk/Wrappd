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
import sqlite3
from typing import Optional, Literal

import httpx
from cachetools import TTLCache
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

TMDB_KEY = os.environ.get("TMDB_API_KEY", "")
OMDB_KEY = os.environ.get("OMDB_API_KEY", "")  # optioneel
MOCK_MODE = os.environ.get("MOCK_MODE", "0") == "1"
DB_PATH = os.environ.get("DB_PATH", "watched.db")  # SQLite-bestand voor kijklijsten
DEFAULT_LANG = os.environ.get("DEFAULT_LANG", "en")  # standaardtaal (korte code) als er geen wordt meegegeven

TMDB_BASE = "https://api.themoviedb.org/3"

# Korte taalcodes die de APP stuurt -> volledige TMDB-codes.
# De app hoeft zo niets te weten van TMDB's xx-XX-notatie; onbekende of lege
# invoer valt terug op de standaard, zodat een typefout nooit een crash geeft.
LANG_MAP = {
    "en": "en-US",
    "nl": "nl-NL",
    "de": "de-DE",
    "fr": "fr-FR",
    "es": "es-ES",
}


def resolve_lang(code: Optional[str]) -> str:
    """Zet een korte taalcode (en, nl, ...) om naar TMDB-formaat (en-US, ...).

    Accepteert ook al-volledige codes (en-US) en hoofdletters (EN), en valt bij
    onbekende of ontbrekende invoer terug op de standaardtaal.
    """
    if not code:
        return LANG_MAP[DEFAULT_LANG]
    key = code.strip().lower()[:2]        # 'nl-NL' of 'NL' -> 'nl'
    return LANG_MAP.get(key, LANG_MAP[DEFAULT_LANG])

app = FastAPI(
    title="Top-30 Films & Series API",
    description="PoC-backend: top 30 per genre met filters op jaar, acteur, regisseur en rating.",
    version="0.1.0",
)

# Cache: genres 24u, zoekresultaten 1u, persoon-lookups 24u
# Persistent via Redis als REDIS_URL gezet is; anders in-memory (verdwijnt bij herstart).
TTL_GENRE = 86400
TTL_LIST = 3600
TTL_PERSON = 86400

# In-memory fallback-caches: gebruikt als Redis niet is geconfigureerd of onbereikbaar.
_mem_genre = TTLCache(maxsize=8, ttl=TTL_GENRE)
_mem_list = TTLCache(maxsize=512, ttl=TTL_LIST)
_mem_person = TTLCache(maxsize=1024, ttl=TTL_PERSON)

# Redis-verbinding: leeg = uit (dan puur in-memory).
# Voorbeeld: REDIS_URL=redis://192.168.1.108:6379/0
REDIS_URL = os.environ.get("REDIS_URL", "")
_redis = None
_MISS = object()  # sentinel: onderscheidt 'niet in cache' van een gecachte None


async def get_redis():
    """Geef de Redis-client terug, of None als Redis uit staat / de lib ontbreekt."""
    global _redis
    if not REDIS_URL:
        return None
    if _redis is None:
        try:
            import redis.asyncio as aioredis
            # Korte timeouts: als Redis plat ligt, val snel terug op geheugen
            # i.p.v. elk verzoek te laten hangen.
            _redis = aioredis.from_url(
                REDIS_URL, decode_responses=True,
                socket_connect_timeout=2, socket_timeout=2,
            )
        except Exception:
            return None
    return _redis


async def cache_get(key: str, mem_store: TTLCache):
    """Haal een waarde uit Redis; val bij storing terug op de geheugen-cache."""
    r = await get_redis()
    if r is not None:
        try:
            raw = await r.get(key)
            return json.loads(raw) if raw is not None else _MISS
        except Exception:
            pass  # Redis-hik -> geheugen proberen
    return mem_store.get(key, _MISS)


async def cache_set(key: str, value, ttl: int, mem_store: TTLCache):
    """Schrijf naar Redis (met vervaltijd); val bij storing terug op geheugen."""
    r = await get_redis()
    if r is not None:
        try:
            await r.set(key, json.dumps(value), ex=ttl)
            return
        except Exception:
            pass  # Redis-hik -> geheugen gebruiken
    mem_store[key] = value

MediaType = Literal["movie", "tv"]


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
    key = "person:" + name.strip().lower()
    cached = await cache_get(key, _mem_person)
    if cached is not _MISS:
        return cached
    data = await tmdb_get(client, "/search/person", query=name)
    results = data.get("results", [])
    pid = results[0]["id"] if results else None
    await cache_set(key, pid, TTL_PERSON, _mem_person)
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


# ---------------------------------------------------------------- scoring

# Ratingbronnen als losse, uitschakelbare blokjes met eigen gewicht.
# Nu alleen TMDB; later voeg je hier bijv. "trakt" of "imdb" toe (mét licentie).
# Elke bron leest z'n rauwe cijfer (0-10) en aantal stemmen uit een item.
SCORE_SOURCES = {
    "tmdb": {
        "weight": 1.0,
        "enabled": True,
        "rating": lambda it: it.get("tmdb_rating"),
        "votes": lambda it: it.get("vote_count") or 0,
    },
    # Voorbeeld voor later (uitgeschakeld):
    # "trakt": {"weight": 0.5, "enabled": False,
    #           "rating": lambda it: it.get("trakt_rating"),
    #           "votes":  lambda it: it.get("trakt_votes") or 0},
}

# Bayes-drempel: hoeveel stemmen een bron "vertrouwt" voordat een titel
# z'n eigen cijfer mag houden. Hoger = strenger voor obscure titels.
BAYES_M = int(os.environ.get("BAYES_M", "500"))


def bayesian_score(rating: float, votes: int, mean: float, m: int = BAYES_M) -> float:
    """Trek titels met weinig stemmen naar het lijstgemiddelde toe (IMDb-methode).

    score = (v/(v+m))*R + (m/(v+m))*C  — met R=cijfer, v=stemmen, C=gemiddelde.
    """
    v = max(votes, 0)
    if v + m == 0:
        return mean
    return (v / (v + m)) * rating + (m / (v + m)) * mean


def compute_composite(items: list[dict]) -> None:
    """Bereken per item een samengesteld 'score'-veld over alle actieve bronnen.

    Per bron wordt eerst het lijstgemiddelde (C) bepaald, dan een Bayes-cijfer,
    en die worden gewogen samengevoegd. Werkt met 1 bron of met meerdere.
    """
    active = {name: s for name, s in SCORE_SOURCES.items() if s.get("enabled")}

    # Lijstgemiddelde C per bron (alleen over titels die die bron hebben).
    means = {}
    for name, s in active.items():
        vals = [s["rating"](it) for it in items if s["rating"](it) is not None]
        means[name] = (sum(vals) / len(vals)) if vals else 0.0

    for it in items:
        total_w = 0.0
        acc = 0.0
        for name, s in active.items():
            r = s["rating"](it)
            if r is None:
                continue  # bron ontbreekt voor deze titel -> sla over
            b = bayesian_score(r, s["votes"](it), means[name])
            acc += b * s["weight"]
            total_w += s["weight"]
        it["score"] = round(acc / total_w, 1) if total_w else it.get("tmdb_rating")


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
    return {"status": "ok", "mock_mode": MOCK_MODE, "omdb_enabled": bool(OMDB_KEY)}


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
    language: str = Query(DEFAULT_LANG, description="Korte taalcode: en, nl, de, fr, es (standaard en)"),
):
    """Lijst van beschikbare genres voor films of tv-series, in de gevraagde taal."""
    if MOCK_MODE:
        return {"genres": [{"id": 18, "name": "Drama"}, {"id": 35, "name": "Comedy"}]}
    lang = resolve_lang(language)
    key = f"genre:{media_type}:{lang}"
    cached = await cache_get(key, _mem_genre)
    if cached is not _MISS:
        return cached
    async with httpx.AsyncClient() as client:
        data = await tmdb_get(client, f"/genre/{media_type}/list", language=lang)
    await cache_set(key, data, TTL_GENRE, _mem_genre)
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
    language: str = Query(DEFAULT_LANG, description="Korte taalcode: en, nl, de, fr, es (standaard en)"),
):
    """
    Genereer een top-lijst op basis van de opgegeven filters.
    Titels en omschrijvingen komen in de gevraagde taal (language).
    Ratings komen van TMDB; met enrich=true ook IMDb/Rotten Tomatoes (OMDb-key vereist).
    Met exclude_watched=true + device_id worden gezien titels weggelaten en schuift
    de lijst op, zodat je altijd een volle top krijgt van wat je nog niet zag.
    """
    lang = resolve_lang(language)
    if MOCK_MODE:
        items = mock_items(media_type)
    else:
        key = "list:" + ":".join(str(x) for x in (
            media_type, genre_id, actor, director, year_min, year_max,
            rating_min, rating_max, sort_by, min_votes, lang))
        cached = await cache_get(key, _mem_list)
        if cached is not _MISS:
            items = cached
        else:
            items = await fetch_from_tmdb(media_type, genre_id, actor, director,
                                          year_min, year_max, rating_min, rating_max,
                                          min_votes, lang)
            await cache_set(key, items, TTL_LIST, _mem_list)

    # Samengesteld eindcijfer per titel berekenen (Bayesiaans, over actieve bronnen)
    compute_composite(items)

    # Sorteren
    if sort_by == "rating":
        items = sorted(items, key=lambda x: x.get("score") or 0, reverse=True)
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
        "language": resolve_lang(language),  # accepteert korte code én al-volledige code
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

        # Haal 2 pagina's op (40 titels) zodat er na filtering genoeg overblijft
        pages = await asyncio.gather(
            tmdb_get(client, f"/discover/{media_type}", page=1, **params),
            tmdb_get(client, f"/discover/{media_type}", page=2, **params),
        )

    items = []
    for page in pages:
        for r in page.get("results", []):
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
