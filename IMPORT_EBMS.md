# Import EBMS → Thalera

Script : `import_ebms.py`

## Modèle métier

| Niveau Monitoring | Correspondance EBMS |
|-------------------|---------------------|
| Site | `location` (piège) |
| Visite | `event` |
| **Photo** (observation) | **1 entrée = 1 image** (`occurrence.media[]`) |

Une occurrence EBMS avec N photos produit N observations GeoNature (taxon / score IA dupliqués si l’API ne fournit qu’un score au niveau occurrence).

## Concordance des champs

```bash
python3 import_ebms.py --show-mapping
```

| Niveau Monitoring | Champ GeoNature | Source API EBMS |
|-------------------|-----------------|-----------------|
| **Site** | `base_site_code` | `location.location_id` |
| | `base_site_name` | `location.name` |
| | `geom` | `location.point` / `location.geom` |
| | `base_site_description` | `location.verbatim_locality` |
| **Visite** | `visit_date_min` | `event.date_start` |
| | `observers_txt` | `identification.identified_by` (sinon `event.recorded_by`) |
| | `data.id_event_ebms` | `event.event_id` |
| **Photo** | `cd_nom` | taxon EBMS → CSV Taxref |
| | `data.taxon_ebms` | `taxon.accepted_name` / `species` / `taxon_name` |
| | `data.score_ia` | `identification.classifier…probability_given` |
| | `data.id_media_ebms` | `occurrence.media[].path` (clé d’idempotence) |
| | `data.id_occurrence_ebms` | `id` (référence) |
| | `comments` | *(non importé)* |
| | `taxon_valide` / `new_cd_nom` | *(non importés — saisie UI)* |
| **Média** | `media_url` | `https://warehouse1.indicia.org.uk/upload/` + `path` |

## Prérequis

1. Module `thalera` installé dans GeoNature Monitoring
2. Jeu de données (`THALERA_ID_DATASET`)
3. CSV taxons : `/home/zacharie/dev/thalera/ebms_rnfrance_taxons.csv`
4. `pip install psycopg2-binary`

## Configuration

```bash
cp import_ebms.env.example import_ebms.env
# éditer les secrets / DSN / id_dataset
```

Le script charge `import_ebms.env` automatiquement.

## Lancement

```bash
# Afficher le mapping
python3 import_ebms.py --show-mapping

# Test sans écriture
python3 import_ebms.py --dry-run --limit 20

# Import incrémental (cron)
python3 import_ebms.py

# Reprendre depuis le début (idempotent sur id_media_ebms)
python3 import_ebms.py --full
```

## Purge des données de test

```bash
cd /home/zacharie/dev/monitorings-geonature/thalera
set -a && . ./import_ebms.env && set +a
psql "$GEONATURE_DB_DSN" -f for_install/purge_import_ebms_test.sql
rm -f .import_ebms_state.json
```

## Cron (tous les soirs)

```cron
30 2 * * * cd /home/zacharie/dev/monitorings-geonature/thalera && /chemin/venv/bin/python import_ebms.py >> /var/log/thalera_import_ebms.log 2>&1
```

Appeler **directement le python du venv** : cron utilise `/bin/sh` (dash), qui ne
connaît pas le builtin bash `source` (`/bin/sh: 1: source: not found`). Le script
charge lui-même `import_ebms.env`, l'activation ne sert qu'à fournir `psycopg2`.

L'import est **incrémental** via `metadata.tracking` (fichier `.import_ebms_state.json`).  
`--full` force un reparcours depuis le début (idempotent sur la clé `id_media_ebms`).

Le curseur n'avance **jamais** au-delà d'une occurrence non importée pour une raison
rattrapable (taxon non résolu, échec site/visite) : elle est reproposée au run suivant
au lieu d'être perdue. Une occurrence sans photo n'a rien à importer et ne bloque pas
la progression.

## Résolution du cd_nom

CSV `ebms_rnfrance_taxons.csv` → `taxonomie.taxref` (recherche sur `lb_nom`, règne
`Animalia`, ordre `Lepidoptera` privilégié en cas d'homonyme) → `THALERA_CD_NOM_FALLBACK`.
L'origine retenue est tracée dans `data.cd_nom_origine` (`csv` | `taxref` | `fallback`).
Le bilan de fin de run liste les taxons que ni le CSV ni Taxref ne résolvent — c'est
la liste à ajouter au CSV.