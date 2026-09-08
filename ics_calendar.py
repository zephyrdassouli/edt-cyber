"""
ics_calendar.py
================

Récupère l'EDT (emploi du temps) IMT Atlantique publié au format ICS
(lien "PASSCAL") et l'extrait sous une forme facilement réutilisable.

Sortie finale (les seules informations extraites/stockées par cours,
objet `Course`) :
    - date        : "dd/mm" (ex: "07/09")
    - code_ue     : NETCRY / CYBLAW / SECNOS / SHS / CPRO / DIV CYBER
    - start       : heure de début, ex "9h30"
    - end         : heure de fin, ex "12h"
    - duration    : durée, ex "2h30"

Le code UE est déterminé en best-effort par `classify_ue()` à partir de
l'intitulé (SUMMARY) de l'évènement : correspondance directe avec le
code (ex: "NETCRY") si présent tel quel, sinon correspondance par
mots-clés sur l'intitulé complet de l'UE. Si rien ne correspond ->
"DIV CYBER".

Conçu pour être robuste face aux particularités du flux observé :
- champs vides représentés par "-" (LOCATION, COMMENT...)
- propriété CATEGORIES dupliquée (peu importe, ignorée)
- LOCATION au format libre et parfois mal formé (parenthèses non
  fermées, plusieurs groupes entre parenthèses, etc.)
- SUMMARY contenant parfois une URL de visioconférence
- réseau capricieux (timeouts, erreurs 5xx) -> retries + cache local

Par défaut, seuls les évènements à partir du 1er septembre 2026 sont
conservés (voir DEFAULT_EXTRACTION_START / paramètre `since`), le flux
PASSCAL pouvant contenir plusieurs années d'historique.

Utilisation rapide
-------------------
    export ICS_CALENDAR_URL="https://inpass.imt-atlantique.fr/passcal/getics?login=...&check=..."
    python3 ics_calendar.py                      # résumé + répartition par code UE
    python3 ics_calendar.py --json cours.json     # exporte les cours (5 champs) en JSON
    python3 ics_calendar.py --next 5              # 5 prochains cours
    python3 ics_calendar.py --since 2026-01-01    # change la période d'extraction
    python3 ics_calendar.py --all                 # pas de filtre de date

Utilisation dans un autre script Python
-----------------------------------------
    from ics_calendar import get_courses

    courses = get_courses()                # lit ICS_CALENDAR_URL par défaut
    for c in courses[:10]:
        print(c.date, c.code_ue, c.start, c.end, c.duration)

La seule dépendance tierce nécessaire est le paquet `icalendar`
(pip install icalendar). `requests` est utilisé s'il est disponible,
sinon on retombe sur `urllib` de la stdlib.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Iterable, Optional

try:
    from icalendar import Calendar
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Le paquet 'icalendar' est requis : pip install icalendar --break-system-packages"
    ) from exc

# requests est optionnel : on s'en sert si présent (gestion des erreurs un
# peu plus pratique), sinon on utilise urllib qui est toujours disponible.
try:
    import requests  # type: ignore

    _HAS_REQUESTS = True
except ImportError:  # pragma: no cover
    _HAS_REQUESTS = False


logger = logging.getLogger("ics_calendar")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------
# Modèle de données
# --------------------------------------------------------------------------

# Mots-clés fréquents dans les intitulés de séance, utilisés pour deviner
# un "type d'activité" exploitable (Cours / TD / TP / Evaluation / ...).
# Best-effort uniquement : le résumé brut (`summary`) reste toujours
# disponible pour un parsing plus fin si besoin.
_ACTIVITY_KEYWORDS = [
    "Evaluation", "Évaluation", "QCM", "TP", "TD", "BE", "Cours",
    "Colloque", "Episode", "Épisode", "Tournoi", "Atelier", "Conférence",
    "Présentation", "Simulation", "Oraux", "Introduction", "Vacances",
]
_ACTIVITY_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _ACTIVITY_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# Repère un préfixe "campus" du type "NA-", "RE-", "BR-" en tête de LOCATION.
_CAMPUS_RE = re.compile(r"^(?P<campus>[A-Z]{2})-(?P<rest>.*)$")
# Extrait les groupes entre parenthèses (non imbriqués) sans planter si une
# parenthèse fermante est en trop (ex: "NA-B016 (V- 26 - 13 PC))").
_PAREN_RE = re.compile(r"\(([^()]*)\)")


@dataclass
class Location:
    """Représentation best-effort d'un champ LOCATION brut."""

    raw: str
    campus: Optional[str] = None       # ex: "NA", "RE", "BR"
    building_room: Optional[str] = None  # texte avant la 1re parenthèse
    tags: list[str] = field(default_factory=list)  # contenus entre parenthèses

    @classmethod
    def parse(cls, raw: Optional[str]) -> Optional["Location"]:
        if not raw:
            return None
        raw = raw.strip()
        if raw in ("", "-"):
            return None

        campus = None
        rest = raw
        m = _CAMPUS_RE.match(raw)
        if m:
            campus = m.group("campus")
            rest = m.group("rest").strip()

        tags = [t.strip() for t in _PAREN_RE.findall(rest) if t.strip()]
        building_room = _PAREN_RE.sub("", rest).strip(" -")

        return cls(raw=raw, campus=campus, building_room=building_room or None, tags=tags)


@dataclass
class Event:
    """Un évènement de l'EDT, sous une forme directement exploitable."""

    uid: str
    summary: str
    start: datetime
    end: Optional[datetime]
    all_day: bool
    location: Optional[Location]
    last_modified: Optional[datetime] = None

    # Champs dérivés (best-effort, à partir de `summary`)
    module: Optional[str] = None        # ex: "ATSA S1-N"
    description: Optional[str] = None   # reste du summary après le module
    activity_type: Optional[str] = None  # ex: "Cours", "TD", "TP", "Evaluation"
    meeting_url: Optional[str] = None   # URL de visio trouvée dans le summary

    @property
    def duration(self) -> Optional[timedelta]:
        if self.start and self.end:
            return self.end - self.start
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat() if self.start else None
        d["end"] = self.end.isoformat() if self.end else None
        d["last_modified"] = self.last_modified.isoformat() if self.last_modified else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        d = dict(d)
        d["start"] = datetime.fromisoformat(d["start"]) if d.get("start") else None
        d["end"] = datetime.fromisoformat(d["end"]) if d.get("end") else None
        d["last_modified"] = (
            datetime.fromisoformat(d["last_modified"]) if d.get("last_modified") else None
        )
        loc = d.get("location")
        d["location"] = Location(**loc) if loc else None
        return cls(**d)


_URL_RE = re.compile(r"https?://\S+")


def _derive_fields(ev: Event) -> None:
    """Remplit module/description/activity_type/meeting_url à partir du summary."""
    summary = ev.summary or ""

    url_match = _URL_RE.search(summary)
    if url_match:
        ev.meeting_url = url_match.group(0)

    if " - " in summary:
        module, description = summary.split(" - ", 1)
        ev.module = module.strip() or None
        ev.description = description.strip() or None
    else:
        ev.description = summary.strip() or None

    kw_match = _ACTIVITY_RE.search(summary)
    if kw_match:
        ev.activity_type = kw_match.group(1)


# --------------------------------------------------------------------------
# Récupération réseau (avec retries + cache local optionnel)
# --------------------------------------------------------------------------

DEFAULT_TIMEOUT = 15
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 1.5
USER_AGENT = "ics-calendar-fetcher/1.0 (+python)"


def _mask_url(url: str) -> str:
    """Masque les paramètres sensibles (login/check/token) dans les logs."""
    return re.sub(r"(?i)(login|check|token|key)=[^&]+", r"\1=***", url)


def fetch_ics_bytes(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> bytes:
    """Télécharge le flux ICS avec quelques tentatives en cas d'erreur réseau."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            logger.debug("Fetch ICS (tentative %d/%d): %s", attempt, retries, _mask_url(url))
            if _HAS_REQUESTS:
                resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
                resp.raise_for_status()
                return resp.content
            else:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read()
        except Exception as exc:  # noqa: BLE001 - on veut tout capturer pour retry
            last_exc = exc
            logger.warning("Échec du téléchargement (tentative %d/%d): %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(backoff ** attempt)
    raise RuntimeError(f"Impossible de récupérer l'ICS après {retries} tentatives") from last_exc


def fetch_ics_with_cache(
    url: Optional[str],
    cache_path: Optional[Path] = None,
    cache_max_age: Optional[int] = 900,
    force_refresh: bool = False,
    **fetch_kwargs,
) -> bytes:
    """
    Récupère l'ICS, avec un cache disque local en secours.

    - Si le cache existe et a moins de `cache_max_age` secondes, il est
      utilisé directement (pas d'appel réseau, `url` peut même être None)
      sauf si `force_refresh=True`.
    - Si le réseau échoue mais qu'un cache existe (même périmé), on
      retombe dessus plutôt que de planter (utile en mode "robuste").
    - Si aucun cache utilisable n'existe, `url` devient obligatoire.
    """
    if cache_path is not None:
        cache_path = Path(cache_path)
        if not force_refresh and cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if cache_max_age is not None and age < cache_max_age:
                logger.info("Utilisation du cache local (%.0fs, < %ss): %s", age, cache_max_age, cache_path)
                return cache_path.read_bytes()

    if not url:
        if cache_path is not None and cache_path.exists():
            logger.warning("Pas d'URL fournie, utilisation du cache existant (peut-être périmé): %s", cache_path)
            return cache_path.read_bytes()
        raise ValueError(
            "Aucune URL fournie et aucun cache utilisable. Passe `url=...` ou "
            "définis la variable d'environnement ICS_CALENDAR_URL."
        )

    try:
        data = fetch_ics_bytes(url, **fetch_kwargs)
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(data)
        return data
    except Exception as exc:
        if cache_path is not None and cache_path.exists():
            logger.error(
                "Récupération réseau impossible (%s), utilisation du cache périmé: %s", exc, cache_path
            )
            return cache_path.read_bytes()
        raise


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _to_datetime(value, default_tz=None) -> tuple[Optional[datetime], bool]:
    """
    Normalise une valeur DTSTART/DTEND icalendar en (datetime, all_day).
    Les évènements "DATE" seuls (sans heure) sont considérés all_day=True.
    """
    if value is None:
        return None, False
    dt = value.dt
    if isinstance(dt, datetime):
        return dt, False
    if isinstance(dt, date):
        # date pure (VALUE=DATE) -> évènement "journée entière"
        return datetime(dt.year, dt.month, dt.day, tzinfo=default_tz), True
    return None, False


def parse_ics(ics_bytes: bytes) -> list[Event]:
    """Parse le contenu ICS brut et renvoie une liste d'Event triée par date."""
    try:
        cal = Calendar.from_ical(ics_bytes)
    except Exception as exc:
        raise ValueError(f"ICS invalide ou corrompu : {exc}") from exc

    events: list[Event] = []
    skipped = 0

    for component in cal.walk("VEVENT"):
        try:
            uid = str(component.get("UID", "")).strip()
            summary = str(component.get("SUMMARY", "")).strip()

            dtstart_prop = component.get("DTSTART")
            start, all_day = _to_datetime(dtstart_prop)
            if start is None:
                logger.warning("Évènement sans DTSTART ignoré (uid=%s)", uid)
                skipped += 1
                continue

            dtend_prop = component.get("DTEND")
            end, _ = _to_datetime(dtend_prop)

            if end is None:
                duration_prop = component.get("DURATION")
                if duration_prop is not None:
                    try:
                        end = start + duration_prop.dt
                    except Exception:  # noqa: BLE001
                        end = None

            last_modified_prop = component.get("LAST-MODIFIED")
            last_modified, _ = _to_datetime(last_modified_prop)

            raw_location = component.get("LOCATION")
            location = Location.parse(str(raw_location).strip() if raw_location else None)

            ev = Event(
                uid=uid or f"noid-{len(events)}",
                summary=summary,
                start=start,
                end=end,
                all_day=all_day,
                location=location,
                last_modified=last_modified,
            )
            _derive_fields(ev)
            events.append(ev)
        except Exception as exc:  # noqa: BLE001 - un évènement cassé ne doit pas tout faire planter
            skipped += 1
            logger.warning("Évènement ignoré à cause d'une erreur de parsing: %s", exc)
            continue

    if skipped:
        logger.info("%d évènement(s) ignoré(s) pendant le parsing.", skipped)

    events.sort(key=lambda e: e.start)
    logger.info("%d évènement(s) extrait(s).", len(events))
    return events


# --------------------------------------------------------------------------
# Classification en code UE
# --------------------------------------------------------------------------

# code -> (alias exacts à repérer tels quels dans l'intitulé, phrases-clés
# à repérer en best-effort si l'alias n'apparaît pas littéralement)
_UE_CATEGORIES: list[tuple[str, list[str], list[str]]] = [
    (
        "NETCRY",
        ["NETCRY"],
        [
            "NETWORK AND CRYPTOGRAPHY",
            "RESEAUX ET CRYPTOGRAPHIE",
            "RESEAU ET CRYPTOGRAPHIE",
            "CRYPTOGRAPHIE",
        ],
    ),
    (
        "CYBLAW",
        ["CYBLAW"],
        [
            "LAW, ORGANIZATION AND GEOPOLITICS",
            "LAW ORGANIZATION AND GEOPOLITICS",
            "GEOPOLITICS OF CYBERSECURITY",
            "GEOPOLITIQUE DE LA CYBERSECURITE",
            "DROIT DE LA CYBERSECURITE",
            "DROIT ET GEOPOLITIQUE",
        ],
    ),
    (
        "SECNOS",
        ["SECNOS"],
        [
            "FUNDAMENTALS FOR NETWORK AND OPERATING SYSTEMS SECURITY",
            "NETWORK AND OPERATING SYSTEMS SECURITY",
            "OPERATING SYSTEMS SECURITY",
            "SECURITE DES RESEAUX ET DES SYSTEMES D'EXPLOITATION",
            "SECURITE DES SYSTEMES D'EXPLOITATION",
            "SECURITE RESEAUX ET SYSTEMES",
        ],
    ),
    (
        "SHS",
        ["SHS"],
        [
            "SCIENCES SOCIALES",
            "société",
         ],
    ),
    (
        "CPRO",
        ["CPRO"],
        [
            "UE CONTRAT PRO",
            "CONTRAT DE PROFESSIONNALISATION",
            "CONTRAT PRO",
            "PRO"
        ],
    ),
]
FALLBACK_UE_CODE = "DIV CYBER"


def _normalize(text: str) -> str:
    """Majuscules + suppression des accents, pour un matching robuste."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.upper()


def classify_ue(text: Optional[str]) -> str:
    """
    Détermine le code UE (NETCRY/CYBLAW/SECNOS/SHS/CPRO) à partir d'un
    intitulé d'évènement, en best-effort. Renvoie FALLBACK_UE_CODE
    ("DIV CYBER") si rien ne correspond.

    Stratégie (dans l'ordre) :
      1. l'alias du code apparaît tel quel comme mot entier (ex: "NETCRY")
      2. une phrase-clé caractéristique de l'UE apparaît dans le texte
         (comparaison insensible aux accents/majuscules)
    """
    if not text:
        return FALLBACK_UE_CODE
    norm = _normalize(text)

    for code, aliases, _phrases in _UE_CATEGORIES:
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias)}\b", norm):
                return code

    for code, _aliases, phrases in _UE_CATEGORIES:
        for phrase in phrases:
            if _normalize(phrase) in norm:
                return code

    return FALLBACK_UE_CODE


# --------------------------------------------------------------------------
# Sortie finale : Course (les 5 champs à extraire/stocker)
# --------------------------------------------------------------------------

def _format_time_fr(dt: datetime) -> str:
    """9h30, 12h (pas de minutes affichées si :00)."""
    if dt.minute == 0:
        return f"{dt.hour}h"
    return f"{dt.hour}h{dt.minute:02d}"


def _format_duration_fr(td: timedelta) -> str:
    """2h30, 2h (pas de minutes affichées si pile un compte d'heures)."""
    total_minutes = int(round(td.total_seconds() / 60))
    hours, minutes = divmod(max(total_minutes, 0), 60)
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h{minutes:02d}"


@dataclass
class Course:
    """Les seules informations extraites/stockées pour un cours."""

    date: str        # "dd/mm", ex "07/09"
    code_ue: str      # NETCRY / CYBLAW / SECNOS / SHS / CPRO / DIV CYBER
    start: str        # ex "9h30"
    end: str          # ex "12h"
    duration: str     # ex "2h30"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Course":
        return cls(**d)


def event_to_course(e: Event) -> Optional[Course]:
    """Convertit un Event brut en Course. None si l'heure de fin manque
    (impossible de calculer end/duration de façon fiable)."""
    if e.end is None:
        logger.warning(
            "Évènement sans heure de fin ignoré (uid=%s, résumé=%r).", e.uid, e.summary
        )
        return None
    return Course(
        date=e.start.strftime("%d/%m"),
        code_ue=classify_ue(e.summary),
        start=_format_time_fr(e.start),
        end=_format_time_fr(e.end),
        duration=_format_duration_fr(e.end - e.start),
    )


def events_to_courses(events: Iterable[Event]) -> list[Course]:
    courses = []
    for e in events:
        c = event_to_course(e)
        if c is not None:
            courses.append(c)
    return courses


# --------------------------------------------------------------------------
# API haut niveau
# --------------------------------------------------------------------------

# Le flux ICS PASSCAL peut contenir plusieurs années d'historique (l'export
# testé remontait à 2023). Par défaut on ne garde que ce qui est utile pour
# l'année en cours : à partir du 1er septembre 2026.
DEFAULT_EXTRACTION_START = date(2026, 9, 1)


def filter_since(events: Iterable[Event], min_start: Optional[date]) -> list[Event]:
    """Ne garde que les évènements dont la date de début >= min_start (inclus).

    `min_start` peut être une `date` ou un `datetime` ; None désactive le filtre.
    """
    if min_start is None:
        return list(events)
    cutoff = min_start.date() if isinstance(min_start, datetime) else min_start
    return [e for e in events if e.start.date() >= cutoff]


def get_schedule(
    url: Optional[str] = None,
    cache_path: Optional[str] = "schedule_cache.ics",
    cache_max_age: Optional[int] = 900,
    force_refresh: bool = False,
    since: Optional[date] = DEFAULT_EXTRACTION_START,
) -> list[Event]:
    """
    Point d'entrée principal : renvoie la liste d'Event à jour.

    `url` par défaut : variable d'environnement ICS_CALENDAR_URL
    (on évite de coder en dur un lien contenant un token personnel).

    `since` : date de début de la période d'extraction (par défaut
    `DEFAULT_EXTRACTION_START`, soit le 1er septembre 2026). Les évènements
    antérieurs sont exclus du résultat. Passer `since=None` pour tout garder.
    """
    url = url or os.environ.get("ICS_CALENDAR_URL")
    cache = Path(cache_path) if cache_path else None
    data = fetch_ics_with_cache(
        url, cache_path=cache, cache_max_age=cache_max_age, force_refresh=force_refresh
    )
    events = parse_ics(data)
    events = filter_since(events, since)
    logger.info("%d évènement(s) après filtre depuis %s.", len(events), since or "le début")
    return events


def get_courses(
    url: Optional[str] = None,
    cache_path: Optional[str] = "schedule_cache.ics",
    cache_max_age: Optional[int] = 900,
    force_refresh: bool = False,
    since: Optional[date] = DEFAULT_EXTRACTION_START,
) -> list[Course]:
    """
    Point d'entrée recommandé : renvoie directement la liste de `Course`
    (les 5 champs à extraire/stocker), classés par code UE en best-effort.
    """
    events = get_schedule(
        url=url,
        cache_path=cache_path,
        cache_max_age=cache_max_age,
        force_refresh=force_refresh,
        since=since,
    )
    return events_to_courses(events)


def events_between(events: Iterable[Event], start: datetime, end: datetime) -> list[Event]:
    return [e for e in events if e.start < end and (e.end or e.start) >= start]


def events_on_date(events: Iterable[Event], day: date) -> list[Event]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=next(iter(events), None) and next(iter(events)).start.tzinfo)
    end = start + timedelta(days=1)
    return events_between(events, start, end)


def upcoming_events(events: Iterable[Event], now: Optional[datetime] = None, limit: int = 10) -> list[Event]:
    now = now or datetime.now().astimezone()
    future = [e for e in events if e.start >= now]
    return sorted(future, key=lambda e: e.start)[:limit]


def save_json(records: Iterable, path: str) -> None:
    """Générique : fonctionne pour une liste de Course ou d'Event (les deux
    exposent `.to_dict()`)."""
    Path(path).write_text(
        json.dumps([r.to_dict() for r in records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_json(path: str, record_cls=Course) -> list:
    """Générique : passer `record_cls=Event` pour recharger un export d'Event."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [record_cls.from_dict(d) for d in data]


def courses_to_dataframe(courses: Iterable[Course]):
    """Conversion optionnelle vers pandas.DataFrame (import paresseux)."""
    import pandas as pd  # import local : pandas n'est pas une dépendance obligatoire

    return pd.DataFrame([c.to_dict() for c in courses])


def events_to_dataframe(events: Iterable[Event]):
    """Conversion optionnelle (Event complet, pour debug) vers pandas.DataFrame."""
    import pandas as pd  # import local : pandas n'est pas une dépendance obligatoire

    rows = []
    for e in events:
        rows.append(
            {
                "uid": e.uid,
                "start": e.start,
                "end": e.end,
                "all_day": e.all_day,
                "module": e.module,
                "activity_type": e.activity_type,
                "description": e.description,
                "summary": e.summary,
                "location_raw": e.location.raw if e.location else None,
                "campus": e.location.campus if e.location else None,
                "meeting_url": e.meeting_url,
                "code_ue": classify_ue(e.summary),
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Récupère et extrait l'EDT IMT Atlantique (format ICS).")
    p.add_argument("--url", help="URL du flux ICS (sinon: variable d'env ICS_CALENDAR_URL)")
    p.add_argument("--cache", default="schedule_cache.ics", help="Chemin du cache local ICS")
    p.add_argument("--cache-max-age", type=int, default=900, help="Âge max du cache en secondes")
    p.add_argument("--force-refresh", action="store_true", help="Ignore le cache et retélécharge")
    p.add_argument("--json", metavar="PATH", help="Exporte les cours (5 champs) en JSON vers ce fichier")
    p.add_argument("--next", type=int, metavar="N", help="Affiche les N prochains cours")
    p.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        default=DEFAULT_EXTRACTION_START.isoformat(),
        help=f"Début de la période d'extraction (défaut: {DEFAULT_EXTRACTION_START.isoformat()})",
    )
    p.add_argument("--all", action="store_true", help="Ne filtre pas par date (ignore --since)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    since = None if args.all else date.fromisoformat(args.since)

    try:
        events = get_schedule(
            url=args.url,
            since=since,
            cache_path=args.cache,
            cache_max_age=args.cache_max_age,
            force_refresh=args.force_refresh,
        )
    except Exception as exc:
        logger.error("Échec: %s", exc)
        return 1

    courses = events_to_courses(events)

    if args.json:
        save_json(courses, args.json)
        logger.info("Export JSON écrit: %s", args.json)

    if args.next:
        upcoming = events_to_courses(upcoming_events(events, limit=args.next))
        print(f"\n--- {args.next} prochain(s) cours ---")
        for c in upcoming:
            print(f"{c.date}  {c.start}-{c.end} ({c.duration})  [{c.code_ue}]")
    else:
        print(f"\nTotal: {len(courses)} cours extraits.")
        if courses:
            print(f"Période: {courses[0].date} -> {courses[-1].date}")
        print("\nRépartition par code UE :")
        for code, n in sorted(Counter(c.code_ue for c in courses).items()):
            print(f"  {code:<10} {n}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())