#!/usr/bin/env python3
"""
Import incrémental EBMS (Indicia) → sous-module Monitoring GeoNature « thalera ».

Usage typique (cron nocturne) :
  python3 import_ebms.py
  python3 import_ebms.py --show-mapping
  python3 import_ebms.py --dry-run --limit 50

Variables d'environnement : voir import_ebms.env.example
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import psycopg2
    import psycopg2.extras
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "psycopg2 est requis : pip install psycopg2-binary"
    ) from exc


# ---------------------------------------------------------------------------
# Constantes / config
# ---------------------------------------------------------------------------

DEFAULT_WAREHOUSE_URL = "https://warehouse1.indicia.org.uk"
DEFAULT_MEDIA_PREFIX = "https://warehouse1.indicia.org.uk/upload/"
DEFAULT_USER = "rnfrance"
DEFAULT_PROJ_ID = "RNFRANCEMOTHS"
DEFAULT_BATCH_SIZE = 500
DEFAULT_MODULE_CODE = "thalera"
DEFAULT_STATE_FILE = Path(__file__).resolve().parent / ".import_ebms_state.json"
DEFAULT_TAXONS_CSV = Path("/home/zacharie/dev/thalera/ebms_rnfrance_taxons.csv")
DEFAULT_ENV_FILE = Path(__file__).resolve().parent / "import_ebms.env"


def load_env_file(path: Path, *, override: bool = True) -> None:
    """Charge un fichier KEY=VALUE dans os.environ (sans expansion shell).

    Par défaut, les valeurs du fichier priment sur l'environnement déjà présent
    (évite un EBMS_SECRET tronqué après un `source` bash qui interprète `$…`).
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # Retirer les guillemets éventuels, garder le contenu tel quel ($ inclus)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Sites pièges RN France
# ---------------------------------------------------------------------------
DEFAULT_LOCATION_IDS = [
    396800, 396795, 395885, 395886, 395441, 395812, 395813, 397629, 397630,
    397620, 397624, 396584, 396585, 395878, 396714, 396710, 395755, 396712,
    395877, 396715, 396020, 396022, 395763, 395765, 399051, 399055, 395447,
    398133, 398134, 396414, 396416, 394742, 397631, 395725, 395764, 395767,
    395768,
]

FIELD_MAPPING = """
================================================================================
CONCORDANCE DES CHAMPS — API EBMS → sous-module thalera
================================================================================

SITE (gn_monitoring.t_base_sites + t_site_complements)
--------------------------------------------------------------------------------
API EBMS                                      → GeoNature / Monitoring
location.location_id                          → base_site_code
location.name (sinon verbatim_locality)       → base_site_name
location.point (lat,lon) / location.geom      → geom (Point 4326)
location.verbatim_locality                    → base_site_description
(module thalera)                              → cor_site_module + types_site

VISITE (t_base_visits + t_visit_complements)  — 1 visite = 1 event EBMS
--------------------------------------------------------------------------------
API EBMS                                      → GeoNature / Monitoring
event.event_id                                → data.id_event_ebms (clé d'idempotence)
event.date_start                              → visit_date_min
event.date_end                                → visit_date_max (si présent)
identification.identified_by
  (sinon event.recorded_by)                   → observers_txt
event.source_system_key                       → data.event_source_system_key
metadata.tracking                             → data.last_tracking (info)

OBSERVATION (t_observations + t_observation_complements)  — 1 obs = 1 photo
--------------------------------------------------------------------------------
API EBMS                                      → GeoNature / Monitoring
occurrence.media[].path                       → data.id_media_ebms (clé d'idempotence)
id (occurrence)                               → data.id_occurrence_ebms (référence)
occurrence.source_system_key                  → data.occurrence_source_system_key
taxon.accepted_name / taxon.taxon_name
  (+ table de correspondance CSV)             → cd_nom (Taxref)
taxon.accepted_name / species / taxon_name    → data.taxon_ebms (libellé d'origine)
identification.classifier.current_determination.probability_given
                                              → data.score_ia
                                              (score au niveau occurrence EBMS ;
                                               dupliqué sur chaque photo de l'occurrence)
(non renseigné à l'import)                    → comments
(non renseigné à l'import)                    → data.taxon_valide (à valider en UI)
(non renseigné à l'import)                    → data.new_cd_nom

MÉDIAS (gn_commons.t_medias, 1 média attaché à chaque observation)
--------------------------------------------------------------------------------
API EBMS                                      → GeoNature
occurrence.media[].path                       → media_url =
  « https://warehouse1.indicia.org.uk/upload/ » + path
occurrence.media[].type                       → title_fr / description
event.recorded_by                             → author
nomenclature TYPE_MEDIA / Photo               → id_nomenclature_media_type
bib_tables_location t_observations            → id_table_location

INCREMENTAL (cron)
--------------------------------------------------------------------------------
metadata.tracking                             → fichier d'état .import_ebms_state.json
                                                (requête ES : tracking > dernier connu)
================================================================================
"""


# ---------------------------------------------------------------------------
# Client API
# ---------------------------------------------------------------------------

class EbmsClient:
    def __init__(
        self,
        warehouse_url: str,
        user: str,
        secret: str,
        proj_id: str,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.warehouse_url = warehouse_url.rstrip("/")
        self.search_url = (
            f"{self.warehouse_url}/index.php/services/rest/es-occurrences/_search/"
            f"?proj_id={proj_id}"
        )
        self.auth_header = f"USER:{user}:SECRET:{secret}"
        self.batch_size = batch_size

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.search_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": self.auth_header,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {error_body}") from exc

    def iter_occurrences(
        self,
        *,
        location_ids: list[int] | None,
        tracking_gt: int = 0,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Itère les occurrences triées par metadata.tracking (incrémental)."""
        search_after: list[Any] | None = None
        fetched = 0

        must: list[dict[str, Any]] = [
            {"range": {"metadata.tracking": {"gt": tracking_gt}}},
        ]
        if location_ids:
            must.append({"terms": {"location.location_id": location_ids}})

        while True:
            payload: dict[str, Any] = {
                "size": self.batch_size,
                "query": {"bool": {"must": must}},
                "sort": [{"metadata.tracking": {"order": "asc"}}, {"id": {"order": "asc"}}],
            }
            if search_after is not None:
                payload["search_after"] = search_after

            data = self._post(payload)
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                break

            for hit in hits:
                yield hit.get("_source", {})
                fetched += 1
                if limit is not None and fetched >= limit:
                    return

            search_after = hits[-1].get("sort")
            if search_after is None:
                break


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def load_taxon_cd_nom(csv_path: Path) -> dict[str, int]:
    """Index nom scientifique → cd_nom (clés normalisées lower)."""
    mapping: dict[str, int] = {}
    if not csv_path.is_file():
        print(f"Attention: CSV taxons introuvable ({csv_path})", file=sys.stderr)
        return mapping

    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            cd_nom = (row.get("cd_nom") or "").strip()
            if not cd_nom:
                continue
            try:
                cd_nom_int = int(cd_nom)
            except ValueError:
                continue
            for key in (
                row.get("accepted_name"),
                row.get("taxon_name"),
                row.get("taxref_scientific_name"),
                row.get("taxref_accepted_name"),
            ):
                if key and key.strip():
                    mapping[key.strip().lower()] = cd_nom_int
    return mapping


def parse_point(location: dict[str, Any]) -> tuple[float, float] | None:
    point = str(location.get("point") or "").strip()
    if "," in point:
        lat_s, lon_s = point.split(",", 1)
        try:
            return float(lat_s.strip()), float(lon_s.strip())
        except ValueError:
            pass
    geom = str(location.get("geom") or "")
    # POINT(lon lat)
    if geom.upper().startswith("POINT"):
        inner = geom[geom.find("(") + 1 : geom.find(")")]
        parts = inner.replace(",", " ").split()
        if len(parts) >= 2:
            try:
                return float(parts[1]), float(parts[0])  # lat, lon
            except ValueError:
                return None
    return None


def media_url_from_path(path: str, prefix: str) -> str:
    path = path.strip()
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def media_entries(occurrence: dict[str, Any], prefix: str) -> list[dict[str, str]]:
    """Une entrée = une photo (path Indicia + URL absolue + type)."""
    entries: list[dict[str, str]] = []
    for media in occurrence.get("media") or []:
        path = str(media.get("path") or "").strip()
        if not path:
            continue
        entries.append(
            {
                "path": path,
                "url": media_url_from_path(path, prefix),
                "type": str(media.get("type") or "Image"),
            }
        )
    return entries


def media_urls(occurrence: dict[str, Any], prefix: str) -> list[str]:
    return [m["url"] for m in media_entries(occurrence, prefix)]


def nested_get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


@dataclass
class ImportStats:
    sites_created: int = 0
    sites_existing: int = 0
    visits_created: int = 0
    visits_existing: int = 0
    observations_created: int = 0
    observations_updated: int = 0
    observations_skipped: int = 0
    medias_created: int = 0
    medias_existing: int = 0
    max_tracking: int = 0
    # Plus petit tracking non importé pour une raison rattrapable : le curseur
    # incrémental ne doit pas le dépasser, sinon l'occurrence est perdue.
    min_unresolved_tracking: int = 0
    cd_nom_from_csv: int = 0
    cd_nom_from_taxref: int = 0
    cd_nom_from_fallback: int = 0


# ---------------------------------------------------------------------------
# Import GeoNature
# ---------------------------------------------------------------------------

class ThaleraImporter:
    def __init__(
        self,
        dsn: str,
        *,
        module_code: str,
        id_dataset: int,
        id_digitiser: int | None,
        media_prefix: str,
        taxon_map: dict[str, int],
        dry_run: bool = False,
        cd_nom_fallback: int | None = None,
    ) -> None:
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = False
        self.module_code = module_code
        self.id_dataset = id_dataset
        self.id_digitiser = id_digitiser
        self.media_prefix = media_prefix
        self.taxon_map = taxon_map
        self.dry_run = dry_run
        self.cd_nom_fallback = cd_nom_fallback
        self.stats = ImportStats()
        # Cache des recherches Taxref (négatifs compris) + noms jamais résolus
        # par le CSV ni par Taxref, à reporter en fin de run.
        self._taxref_cache: dict[str, int | None] = {}
        self.unmapped_names: dict[str, int] = {}

        self.id_module = self._fetch_id_module()
        self.id_type_site = self._fetch_id_type_site()
        self.id_table_location_obs = self._fetch_id_table_location_observations()
        self.id_nomenclature_photo = self._fetch_id_nomenclature_photo()

    def close(self) -> None:
        self.conn.close()

    def _fetch_id_module(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id_module FROM gn_commons.t_modules WHERE module_code = %s",
                (self.module_code,),
            )
            row = cur.fetchone()
        if not row:
            raise RuntimeError(f"Module '{self.module_code}' introuvable dans gn_commons.t_modules")
        return int(row[0])

    def _fetch_id_type_site(self) -> int | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id_nomenclature
                FROM ref_nomenclatures.t_nomenclatures n
                JOIN ref_nomenclatures.bib_nomenclatures_types t
                  ON t.id_type = n.id_type
                WHERE t.mnemonique = 'TYPE_SITE'
                  AND n.cd_nomenclature = 'thalera'
                """
            )
            row = cur.fetchone()
        return int(row[0]) if row else None

    def _fetch_id_table_location_observations(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id_table_location
                FROM gn_commons.bib_tables_location
                WHERE schema_name = 'gn_monitoring'
                  AND table_name = 't_observations'
                """
            )
            row = cur.fetchone()
        if not row:
            raise RuntimeError("bib_tables_location t_observations introuvable")
        return int(row[0])

    def _fetch_id_nomenclature_photo(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT ref_nomenclatures.get_id_nomenclature('TYPE_MEDIA', '2')
                """
            )
            row = cur.fetchone()
        if not row or row[0] is None:
            # fallback label
            cur_alt = self.conn.cursor()
            cur_alt.execute(
                """
                SELECT n.id_nomenclature
                FROM ref_nomenclatures.t_nomenclatures n
                JOIN ref_nomenclatures.bib_nomenclatures_types t ON t.id_type = n.id_type
                WHERE t.mnemonique = 'TYPE_MEDIA'
                  AND (n.cd_nomenclature IN ('2', 'Photo', 'PHOTO')
                       OR n.label_default ILIKE '%%photo%%')
                LIMIT 1
                """
            )
            alt = cur_alt.fetchone()
            cur_alt.close()
            if not alt:
                raise RuntimeError("Nomenclature TYPE_MEDIA Photo introuvable")
            return int(alt[0])
        return int(row[0])

    def lookup_taxref(self, name: str) -> int | None:
        """Cherche un cd_ref dans taxonomie.taxref pour un nom scientifique.

        Complète le CSV, qui est un instantané figé : toute espèce apparue dans
        EBMS depuis son extraction est résolue ici à la volée. Les homonymes
        d'autres règnes sont écartés (regne = 'Animalia') et, à nom égal, on
        privilégie l'ordre Lepidoptera puis le nom valide (cd_nom = cd_ref).
        """
        clean = name.strip()
        key = clean.lower()
        if key in self._taxref_cache:
            return self._taxref_cache[key]

        # Passe 1 sur `lb_nom = %s` pour rester indexable (taxref ≈ 500 k lignes) ;
        # passe 2 insensible à la casse seulement si la première ne donne rien.
        row = None
        with self.conn.cursor() as cur:
            for where, param in (
                ("lb_nom = %s", clean),
                ("lower(lb_nom) = %s", key),
            ):
                cur.execute(
                    f"""
                    SELECT cd_ref
                    FROM taxonomie.taxref
                    WHERE {where}
                      AND regne = 'Animalia'
                    ORDER BY (COALESCE(ordre, '') = 'Lepidoptera') DESC,
                             (cd_nom = cd_ref) DESC,
                             cd_ref
                    LIMIT 1
                    """,
                    (param,),
                )
                row = cur.fetchone()
                if row:
                    break

        cd_ref = int(row[0]) if row and row[0] is not None else None
        self._taxref_cache[key] = cd_ref
        return cd_ref

    def resolve_cd_nom(self, source: dict[str, Any]) -> tuple[int | None, str | None]:
        """Retourne (cd_nom, origine), origine ∈ 'csv' | 'taxref' | 'fallback'.

        (None, None) si aucune piste : l'occurrence est alors ignorée et le
        curseur incrémental retenu (voir _mark_unresolved).
        """
        taxon = source.get("taxon") or {}
        names = [
            str(key).strip()
            for key in (
                taxon.get("accepted_name"),
                taxon.get("species"),
                taxon.get("taxon_name"),
            )
            if key and str(key).strip()
        ]

        for name in names:
            if name.lower() in self.taxon_map:
                self.stats.cd_nom_from_csv += 1
                return self.taxon_map[name.lower()], "csv"

        for name in names:
            cd_ref = self.lookup_taxref(name)
            if cd_ref is not None:
                self.stats.cd_nom_from_taxref += 1
                return cd_ref, "taxref"

        # Ni CSV ni Taxref : agrégats ("Noctua janthe/janthina"), groupes
        # informels ("Heterocera indet."), ou espèce absente de Taxref.
        label = names[0] if names else "(sans nom)"
        self.unmapped_names[label] = self.unmapped_names.get(label, 0) + 1

        if self.cd_nom_fallback is not None:
            self.stats.cd_nom_from_fallback += 1
            return self.cd_nom_fallback, "fallback"
        return None, None

    def _mark_unresolved(self, tracking_i: int) -> None:
        """Retient le plus petit tracking non importé pour une raison rattrapable.

        Le curseur sauvegardé en fin de run ne doit pas le dépasser : sinon
        l'occurrence ne sera plus jamais reproposée en mode incrémental.
        """
        if tracking_i <= 0:
            return
        if (
            not self.stats.min_unresolved_tracking
            or tracking_i < self.stats.min_unresolved_tracking
        ):
            self.stats.min_unresolved_tracking = tracking_i

    def upsert_site(self, source: dict[str, Any]) -> int | None:
        location = source.get("location") or {}
        location_id = str(location.get("location_id") or "").strip()
        if not location_id:
            return None

        name = str(location.get("name") or location.get("verbatim_locality") or location_id)
        description = str(location.get("verbatim_locality") or "")
        coords = parse_point(location)

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id_base_site
                FROM gn_monitoring.t_base_sites
                WHERE base_site_code = %s
                """,
                (location_id,),
            )
            row = cur.fetchone()
            if row:
                self.stats.sites_existing += 1
                return int(row[0])

            if self.dry_run:
                self.stats.sites_created += 1
                return -1

            if not coords:
                print(f"  Site {location_id}: pas de coordonnées, ignoré", file=sys.stderr)
                return None

            lat, lon = coords
            cur.execute(
                """
                INSERT INTO gn_monitoring.t_base_sites (
                    base_site_name, base_site_code, base_site_description,
                    geom, id_digitiser, uuid_base_site
                ) VALUES (
                    %s, %s, %s,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                    %s, %s
                )
                RETURNING id_base_site
                """,
                (name, location_id, description, lon, lat, self.id_digitiser, str(uuid.uuid4())),
            )
            id_base_site = int(cur.fetchone()[0])

            cur.execute(
                """
                INSERT INTO gn_monitoring.cor_site_module (id_base_site, id_module)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (id_base_site, self.id_module),
            )

            cur.execute(
                """
                INSERT INTO gn_monitoring.t_site_complements (id_base_site, data)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (id_base_site) DO NOTHING
                """,
                (id_base_site, json.dumps({"nb_observations_non_valide": 0})),
            )

            if self.id_type_site is not None:
                cur.execute(
                    """
                    INSERT INTO gn_monitoring.cor_site_type (id_base_site, id_type_site)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (id_base_site, self.id_type_site),
                )

        self.stats.sites_created += 1
        return id_base_site

    def upsert_visit(self, source: dict[str, Any], id_base_site: int) -> int | None:
        event = source.get("event") or {}
        event_id = str(event.get("event_id") or "").strip()
        if not event_id:
            # fallback : site + date
            event_id = f"{id_base_site}_{event.get('date_start')}"

        date_start = event.get("date_start")
        if not date_start:
            return None
        date_end = event.get("date_end") or date_start
        identified_by = str(nested_get(source, "identification", "identified_by") or "").strip()
        recorded_by = str(event.get("recorded_by") or "").strip()
        observers_txt = identified_by or recorded_by or None
        tracking = nested_get(source, "metadata", "tracking")

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id_base_visit
                FROM gn_monitoring.t_base_visits v
                JOIN gn_monitoring.t_visit_complements vc ON vc.id_base_visit = v.id_base_visit
                WHERE v.id_base_site = %s
                  AND v.id_module = %s
                  AND vc.data->>'id_event_ebms' = %s
                """,
                (id_base_site, self.id_module, event_id),
            )
            row = cur.fetchone()
            if row:
                id_base_visit = int(row[0])
                if not self.dry_run and observers_txt:
                    cur.execute(
                        """
                        UPDATE gn_monitoring.t_base_visits
                        SET observers_txt = %s
                        WHERE id_base_visit = %s
                          AND (observers_txt IS NULL OR observers_txt = '' OR observers_txt IS DISTINCT FROM %s)
                        """,
                        (observers_txt, id_base_visit, observers_txt),
                    )
                self.stats.visits_existing += 1
                return id_base_visit

            if self.dry_run:
                self.stats.visits_created += 1
                return -1

            data = {
                "id_event_ebms": event_id,
                "event_source_system_key": event.get("source_system_key"),
                "nb_observations_non_valide": 0,
            }
            if tracking is not None:
                data["last_tracking"] = tracking

            cur.execute(
                """
                INSERT INTO gn_monitoring.t_base_visits (
                    id_base_site, id_module, id_dataset, id_digitiser,
                    visit_date_min, visit_date_max, observers_txt, uuid_base_visit
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id_base_visit
                """,
                (
                    id_base_site,
                    self.id_module,
                    self.id_dataset,
                    self.id_digitiser,
                    date_start,
                    date_end,
                    observers_txt,
                    str(uuid.uuid4()),
                ),
            )
            id_base_visit = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO gn_monitoring.t_visit_complements (id_base_visit, data)
                VALUES (%s, %s::jsonb)
                """,
                (id_base_visit, json.dumps(data)),
            )

        self.stats.visits_created += 1
        return id_base_visit

    def upsert_observation_for_media(
        self,
        source: dict[str, Any],
        id_base_visit: int,
        media: dict[str, str],
        *,
        cd_nom: int,
        cd_nom_origine: str | None,
        score_ia_f: float | None,
        taxon_ebms: str | None,
    ) -> int | None:
        """Crée / met à jour une observation pour une photo (clé = id_media_ebms)."""
        occurrence_id = str(source.get("id") or "").strip()
        media_path = media["path"]
        media_url = media["url"]
        occ = source.get("occurrence") or {}
        comments = None
        author = str(nested_get(source, "event", "recorded_by") or "") or None

        complement = {
            "id_media_ebms": media_path,
            "id_occurrence_ebms": occurrence_id,
            "occurrence_source_system_key": occ.get("source_system_key"),
            "score_ia": score_ia_f,
            "taxon_ebms": taxon_ebms,
            # 'csv' | 'taxref' | 'fallback' : un 'fallback' signale un taxon
            # non résolu, à corriger en priorité lors de la validation.
            "cd_nom_origine": cd_nom_origine,
            # taxon_valide volontairement absent → observation non validée
        }

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.id_observation, o.uuid_observation
                FROM gn_monitoring.t_observations o
                JOIN gn_monitoring.t_observation_complements oc
                  ON oc.id_observation = o.id_observation
                WHERE oc.data->>'id_media_ebms' = %s
                """,
                (media_path,),
            )
            row = cur.fetchone()

            if row:
                id_observation, uuid_obs = int(row[0]), row[1]
                if not self.dry_run:
                    # Ne pas écraser taxon_valide / new_cd_nom déjà saisis
                    cur.execute(
                        """
                        UPDATE gn_monitoring.t_observations
                        SET cd_nom = %s,
                            comments = COALESCE(%s, comments),
                            id_base_visit = %s
                        WHERE id_observation = %s
                        """,
                        (cd_nom, comments, id_base_visit, id_observation),
                    )
                    cur.execute(
                        """
                        UPDATE gn_monitoring.t_observation_complements
                        SET data = (COALESCE(data, '{}'::jsonb)
                                    - 'score_ia'
                                    - 'taxon_ebms'
                                    - 'cd_nom_origine'
                                    - 'id_media_ebms'
                                    - 'id_occurrence_ebms'
                                    - 'occurrence_source_system_key')
                          || %s::jsonb
                        WHERE id_observation = %s
                        """,
                        (json.dumps(complement), id_observation),
                    )
                self.stats.observations_updated += 1
            else:
                if self.dry_run:
                    self.stats.observations_created += 1
                    self.stats.medias_created += 1
                    return -1

                uuid_obs = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO gn_monitoring.t_observations (
                        id_base_visit, cd_nom, comments, id_digitiser, uuid_observation
                    ) VALUES (%s, %s, %s, %s, %s)
                    RETURNING id_observation
                    """,
                    (id_base_visit, cd_nom, comments, self.id_digitiser, uuid_obs),
                )
                id_observation = int(cur.fetchone()[0])
                cur.execute(
                    """
                    INSERT INTO gn_monitoring.t_observation_complements (id_observation, data)
                    VALUES (%s, %s::jsonb)
                    """,
                    (id_observation, json.dumps(complement)),
                )
                self.stats.observations_created += 1

        self._upsert_one_media(str(uuid_obs), media_url, author, title=media.get("type") or "Photo EBMS")
        return id_observation

    def _upsert_one_media(
        self,
        uuid_observation: str,
        url: str,
        author: str | None,
        *,
        title: str = "Photo EBMS",
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id_media FROM gn_commons.t_medias
                WHERE uuid_attached_row = %s::uuid AND media_url = %s
                """,
                (uuid_observation, url),
            )
            if cur.fetchone():
                self.stats.medias_existing += 1
                return

            if self.dry_run:
                self.stats.medias_created += 1
                return

            cur.execute(
                """
                INSERT INTO gn_commons.t_medias (
                    id_nomenclature_media_type,
                    id_table_location,
                    uuid_attached_row,
                    title_fr,
                    media_url,
                    author,
                    is_public,
                    unique_id_media
                ) VALUES (%s, %s, %s::uuid, %s, %s, %s, true, %s)
                """,
                (
                    self.id_nomenclature_photo,
                    self.id_table_location_obs,
                    uuid_observation,
                    title[:255] if title else "Photo EBMS",
                    url,
                    author,
                    str(uuid.uuid4()),
                ),
            )
            self.stats.medias_created += 1

    def import_occurrence(self, source: dict[str, Any]) -> None:
        tracking = nested_get(source, "metadata", "tracking")
        try:
            tracking_i = int(tracking) if tracking is not None else 0
        except (TypeError, ValueError):
            tracking_i = 0
        if tracking_i > self.stats.max_tracking:
            self.stats.max_tracking = tracking_i

        medias = media_entries(source.get("occurrence") or {}, self.media_prefix)
        if not medias:
            # Occurrence sans photo : il n'y a rien à importer, jamais. Skip
            # définitif assumé, le curseur peut avancer.
            self.stats.observations_skipped += 1
            return

        cd_nom, cd_nom_origine = self.resolve_cd_nom(source)
        if cd_nom is None:
            self.stats.observations_skipped += 1
            self._mark_unresolved(tracking_i)
            occurrence_id = source.get("id")
            name = nested_get(source, "taxon", "accepted_name") or nested_get(source, "taxon", "taxon_name")
            print(
                f"  Occurrence {occurrence_id}: pas de cd_nom pour '{name}', ignorée "
                f"(curseur retenu ; définir THALERA_CD_NOM_FALLBACK pour l'importer à valider)",
                file=sys.stderr,
            )
            return

        score_ia = nested_get(
            source, "identification", "classifier", "current_determination", "probability_given"
        )
        try:
            score_ia_f = float(score_ia) if score_ia is not None and score_ia != "" else None
        except (TypeError, ValueError):
            score_ia_f = None

        taxon = source.get("taxon") or {}
        taxon_ebms = (
            str(
                taxon.get("accepted_name")
                or taxon.get("species")
                or taxon.get("taxon_name")
                or ""
            ).strip()
            or None
        )

        id_base_site = self.upsert_site(source)
        if not id_base_site:
            self.stats.observations_skipped += 1
            self._mark_unresolved(tracking_i)
            return
        id_base_visit = self.upsert_visit(source, id_base_site)
        if not id_base_visit:
            self.stats.observations_skipped += 1
            self._mark_unresolved(tracking_i)
            return

        for media in medias:
            self.upsert_observation_for_media(
                source,
                id_base_visit,
                media,
                cd_nom=cd_nom,
                cd_nom_origine=cd_nom_origine,
                score_ia_f=score_ia_f,
                taxon_ebms=taxon_ebms,
            )

    def commit(self) -> None:
        if self.dry_run:
            self.conn.rollback()
        else:
            self.conn.commit()


# ---------------------------------------------------------------------------
# État incrémental
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"last_tracking": 0}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, last_tracking: int) -> None:
    path.write_text(
        json.dumps(
            {
                "last_tracking": last_tracking,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Import EBMS → Monitoring thalera")
    p.add_argument("--show-mapping", action="store_true", help="Affiche la concordance des champs et quitte")
    p.add_argument("--dry-run", action="store_true", help="Parcourt l'API sans écrire en base")
    p.add_argument("--limit", type=int, default=None, help="Limiter le nombre d'occurrences")
    p.add_argument("--full", action="store_true", help="Ignore l'état et repart de tracking=0")
    p.add_argument("--state-file", type=Path, default=Path(os.environ.get("THALERA_STATE_FILE", DEFAULT_STATE_FILE)))
    p.add_argument("--taxons-csv", type=Path, default=Path(os.environ.get("THALERA_TAXONS_CSV", DEFAULT_TAXONS_CSV)))
    p.add_argument("--batch-size", type=int, default=int(os.environ.get("EBMS_BATCH_SIZE", DEFAULT_BATCH_SIZE)))
    p.add_argument("--warehouse-url", default=os.environ.get("EBMS_WAREHOUSE_URL", DEFAULT_WAREHOUSE_URL))
    p.add_argument("--media-prefix", default=os.environ.get("EBMS_MEDIA_PREFIX", DEFAULT_MEDIA_PREFIX))
    p.add_argument("--user", default=os.environ.get("EBMS_USER", DEFAULT_USER))
    p.add_argument("--secret", default=os.environ.get("EBMS_SECRET", ""))
    p.add_argument("--proj-id", default=os.environ.get("EBMS_PROJ_ID", DEFAULT_PROJ_ID))
    p.add_argument("--module-code", default=os.environ.get("THALERA_MODULE_CODE", DEFAULT_MODULE_CODE))
    p.add_argument("--dsn", default=os.environ.get("GEONATURE_DB_DSN", ""))
    p.add_argument("--id-dataset", type=int, default=int(os.environ.get("THALERA_ID_DATASET", "0") or 0))
    p.add_argument("--id-digitiser", type=int, default=int(os.environ.get("THALERA_ID_DIGITISER", "0") or 0) or None)
    p.add_argument(
        "--cd-nom-fallback",
        type=int,
        default=int(os.environ.get("THALERA_CD_NOM_FALLBACK", "0") or 0) or None,
        help="cd_nom utilisé si aucun match Taxref (défaut: ignorer l'occurrence)",
    )
    p.add_argument(
        "--all-locations",
        action="store_true",
        help="Ne filtre pas sur la liste des location_id RN France",
    )
    return p


def main() -> int:
    load_env_file(DEFAULT_ENV_FILE)
    args = build_parser().parse_args()

    if args.show_mapping:
        print(FIELD_MAPPING)
        return 0

    if not args.secret:
        print("EBMS_SECRET (ou --secret) obligatoire", file=sys.stderr)
        return 1
    if not args.dsn and not args.dry_run:
        print("GEONATURE_DB_DSN (ou --dsn) obligatoire hors --dry-run", file=sys.stderr)
        return 1
    if not args.id_dataset and not args.dry_run:
        print("THALERA_ID_DATASET (ou --id-dataset) obligatoire hors --dry-run", file=sys.stderr)
        return 1

    print(FIELD_MAPPING)

    state = {"last_tracking": 0} if args.full else load_state(args.state_file)
    tracking_gt = int(state.get("last_tracking") or 0)
    print(f"Import incrémental depuis metadata.tracking > {tracking_gt}")

    taxon_map = load_taxon_cd_nom(args.taxons_csv)
    print(f"Correspondances Taxref chargées : {len(taxon_map)} clés ({args.taxons_csv})")

    client = EbmsClient(
        warehouse_url=args.warehouse_url,
        user=args.user,
        secret=args.secret,
        proj_id=args.proj_id,
        batch_size=args.batch_size,
    )

    location_ids = None if args.all_locations else DEFAULT_LOCATION_IDS
    if location_ids:
        print(f"Filtre location_id : {len(location_ids)} sites")

    # dry-run sans DSN : on simule juste le parcours API
    importer: ThaleraImporter | None = None
    if args.dsn:
        importer = ThaleraImporter(
            args.dsn,
            module_code=args.module_code,
            id_dataset=args.id_dataset,
            id_digitiser=args.id_digitiser,
            media_prefix=args.media_prefix,
            taxon_map=taxon_map,
            dry_run=args.dry_run,
            cd_nom_fallback=args.cd_nom_fallback,
        )
    elif args.dry_run:
        print("Mode dry-run sans DSN : parcours API uniquement (pas de résolution DB)")
    else:
        return 1

    started = time.time()
    count = 0
    max_tracking = tracking_gt

    try:
        for source in client.iter_occurrences(
            location_ids=location_ids,
            tracking_gt=tracking_gt,
            limit=args.limit,
        ):
            count += 1
            tracking = nested_get(source, "metadata", "tracking")
            try:
                tracking_i = int(tracking) if tracking is not None else 0
            except (TypeError, ValueError):
                tracking_i = 0
            if tracking_i > max_tracking:
                max_tracking = tracking_i

            if importer is not None:
                importer.import_occurrence(source)
            else:
                # dry-run API only : affiche un extrait des médias
                urls = media_urls(source.get("occurrence") or {}, args.media_prefix)
                if count <= 3:
                    print(
                        f"  ex. occurrence {source.get('id')} "
                        f"→ {len(urls)} photo(s) "
                        f"site={nested_get(source, 'location', 'location_id')} "
                        f"date={nested_get(source, 'event', 'date_start')} "
                        f"medias={urls}"
                    )

            if count % 100 == 0:
                print(f"... {count} occurrences traitées")
                if importer is not None and not args.dry_run:
                    importer.commit()

        if importer is not None:
            importer.commit()
            if importer.stats.max_tracking > max_tracking:
                max_tracking = importer.stats.max_tracking

        # Le curseur ne doit jamais dépasser une occurrence non importée pour
        # une raison rattrapable, sinon elle est perdue définitivement en
        # incrémental. On repart juste avant : les reprises sont idempotentes
        # (clé id_media_ebms).
        safe_tracking = max_tracking
        unresolved = importer.stats.min_unresolved_tracking if importer else 0
        if unresolved:
            safe_tracking = min(safe_tracking, unresolved - 1)

        if not args.dry_run and safe_tracking > tracking_gt:
            save_state(args.state_file, safe_tracking)
            print(f"État sauvegardé : last_tracking={safe_tracking} → {args.state_file}")
        elif not args.dry_run and unresolved:
            print(
                f"État inchangé (last_tracking={tracking_gt}) : occurrence non importée "
                f"à tracking={unresolved}, elle sera reproposée au prochain run."
            )

        if unresolved and safe_tracking < max_tracking:
            print(
                f"Curseur retenu à {safe_tracking} au lieu de {max_tracking} "
                f"(occurrence non importée à tracking={unresolved}).",
                file=sys.stderr,
            )

    except Exception as exc:
        if importer is not None:
            importer.conn.rollback()
        print(f"Erreur: {exc}", file=sys.stderr)
        return 1
    finally:
        if importer is not None:
            importer.close()

    elapsed = time.time() - started
    print(f"\nTerminé en {elapsed:.1f}s — {count} occurrence(s) parcourue(s)")
    if importer is not None:
        s = importer.stats
        print(
            f"  sites      : +{s.sites_created} / existants {s.sites_existing}\n"
            f"  visites    : +{s.visits_created} / existantes {s.visits_existing}\n"
            f"  observations: +{s.observations_created} / maj {s.observations_updated} / skip {s.observations_skipped}\n"
            f"  médias     : +{s.medias_created} / existants {s.medias_existing}\n"
            f"  cd_nom     : csv {s.cd_nom_from_csv} / taxref {s.cd_nom_from_taxref}"
            f" / fallback {s.cd_nom_from_fallback}"
        )
        if importer.unmapped_names:
            print("\n  Taxons non résolus (ni CSV ni Taxref) :")
            for name, nb in sorted(
                importer.unmapped_names.items(), key=lambda kv: (-kv[1], kv[0])
            ):
                print(f"    {nb:>4} × {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
