# Thalera

Sous-module Monitoring GeoNature pour le contrôle / validation des photos de papillons de nuit (données EBMS / Indicia).

**Arbre métier :** `site` (piège) → `visite` (event) → `photo` (observation = 1 image)

Doc import EBMS détaillée : [IMPORT_EBMS.md](./IMPORT_EBMS.md)

---

## Prérequis serveur

- GeoNature + module Monitoring déjà installés
- Accès shell au compte applicatif (souvent `geonatureadmin`)
- PostgreSQL (`psql`)
- Python 3 + `psycopg2` (dans le venv GeoNature ou système)
- Un **jeu de données** GeoNature pour Thalera
- Une **liste TaxHub** des taxons concernés (pour le widget de correction)
- Compte API EBMS (`rnfrance` + secret)
- CSV de correspondance taxons EBMS → Taxref (`ebms_rnfrance_taxons.csv`)

---

## 1) Déployer le code du sous-module

```bash
# Exemple : dépôt dédié ou clone dans un dossier de protocoles
sudo mkdir -p /home/geonatureadmin/modules_monitoring
cd /home/geonatureadmin/modules_monitoring
git clone <URL_DU_REPO_THALERA> thalera
# ou : git pull si déjà présent
```

Créer le lien symbolique attendu par Monitoring :

```bash
mkdir -p ~/geonature/backend/media/monitorings
ln -sfn /home/geonatureadmin/modules_monitoring/thalera \
        ~/geonature/backend/media/monitorings/thalera
```

> Le **nom du dossier** lié doit être `thalera` (= `module_code`).

---

## 2) Installer / mettre à jour le sous-module Monitoring

```bash
source ~/geonature/backend/venv/bin/activate
geonature monitorings install thalera
# en mise à jour de config JSON déjà installée :
# geonature monitorings update thalera
sudo systemctl restart geonature
deactivate
```

Référence officielle :  
[Installation d'un sous-module](https://github.com/PnX-SI/gn_module_monitoring#installation-dun-sous-module)

---

## 3) Configurer le sous-module dans l’UI

Dans GeoNature → **Monitorings** → **Thalera** → **Éditer** :

| Paramètre | Valeur |
|-----------|--------|
| Jeu(x) de données | celui dédié Thalera |
| Liste des observateurs | liste UsersHub |
| Liste des taxons | liste TaxHub (correction `new_cd_nom`) |
| Afficher dans le menu latéral | oui (`active_frontend`) |
| Types de sites | type `thalera` (créé via `nomenclature.json` à l’install) |

Vérifier aussi en admin / droits :

- permission **R** (accès) + **E** (exports) sur le sous-module
- CRUD sites / visites / observations selon les validateurs

Noter l’**`id_dataset`** et un **`id_role`** (digitiser d’import) pour l’étape 4.

---

## 4) Configurer l’import EBMS (`.env`)

```bash
cd /home/geonatureadmin/modules_monitoring/thalera
cp import_ebms.env.example import_ebms.env
chmod 600 import_ebms.env
nano import_ebms.env
```

Renseigner au minimum :

```bash
EBMS_USER=rnfrance
EBMS_SECRET='LE_SECRET_AVEC_DOLLAR'   # guillemets simples obligatoires si source bash
GEONATURE_DB_DSN=postgresql://USER:PASS@localhost:5432/geonature2db
THALERA_ID_DATASET=<id_dataset>
THALERA_ID_DIGITISER=<id_role>
THALERA_TAXONS_CSV=/chemin/absolu/ebms_rnfrance_taxons.csv
THALERA_STATE_FILE=/home/geonatureadmin/modules_monitoring/thalera/.import_ebms_state.json
THALERA_CD_NOM_FALLBACK=<cd_nom Lepidoptera>
```

Déposer le CSV taxons sur le serveur et adapter `THALERA_TAXONS_CSV`.

### Résolution du `cd_nom`

Trois niveaux, dans l'ordre :

1. **CSV** `ebms_rnfrance_taxons.csv` — instantané figé, correspondances validées.
2. **`taxonomie.taxref`** — recherche à la volée sur `lb_nom` (règne `Animalia`,
   ordre `Lepidoptera` privilégié en cas d'homonyme). Rattrape toute espèce
   apparue dans EBMS depuis l'extraction du CSV.
3. **`THALERA_CD_NOM_FALLBACK`** — dernier recours pour les agrégats
   (`Noctua janthe/janthina`) et groupes informels (`Heterocera indet.`), qui
   n'ont pas de `cd_nom` Taxref par nature.

L'origine retenue est tracée dans `data.cd_nom_origine`
(`csv` | `taxref` | `fallback`) ; les `fallback` sont à corriger en priorité lors
de la validation. Récupérer le `cd_nom` générique :

```sql
SELECT cd_nom, lb_nom FROM taxonomie.taxref WHERE lb_nom = 'Lepidoptera';
```

**Sans fallback**, une occurrence non résolue est ignorée *et* le curseur
incrémental est retenu à sa valeur : elle sera reproposée à chaque run tant que
le taxon reste non résoluble (rien n'est perdu, mais la progression est bloquée).
Le bilan de fin de run liste les taxons concernés, à ajouter au CSV.

Installer la dépendance Python si besoin :

```bash
source ~/geonature/backend/venv/bin/activate
pip install psycopg2-binary
# ou utiliser /usr/bin/python3 si psycopg2 y est déjà
```

> **Ne pas** faire `source import_ebms.env` sans guillemets sur le secret : bash coupe au `$`.  
> Le script `import_ebms.py` charge le fichier **lui-même** (sans expansion shell).

---

## 5) Installer trigger + exports SQL

Les scripts lisent `GEONATURE_DB_DSN` dans `import_ebms.env` (étape 4) :

```bash
cd /home/geonatureadmin/modules_monitoring/thalera

./for_install/install_trigger.sh
./for_install/install_export.sh

# équivalent psql direct :
# psql "$GEONATURE_DB_DSN" -f for_install/trigger_nb_observations_non_valide.sql
# psql "$GEONATURE_DB_DSN" -f exports/csv/export_csv.sql
```

Cela installe :

- trigger `nb_observations_non_valide` (visite + site)
- vues d’export :
  - `gn_monitoring.v_export_thalera_standard` → export photos
  - `gn_monitoring.v_export_thalera_recap_especes` → récap validation par espèce

---

## 6) Premier import (test puis prod)

```bash
cd /home/geonatureadmin/modules_monitoring/thalera

# Contrôle mapping
python3 import_ebms.py --show-mapping

# Test API sans écriture
python3 import_ebms.py --dry-run --limit 20

# Petit import réel
python3 import_ebms.py --limit 50

# Import complet (premier chargement ou reprise)
python3 import_ebms.py --full
```

Vérifier ensuite dans l’UI : sites, visites, photos + médias, compteurs non validés, exports.

---

## 7) Cron (import incrémental)

```cron
# Tous les soirs à 2h30
30 2 * * * cd /home/geonatureadmin/modules_monitoring/thalera && /home/geonatureadmin/geonature/backend/venv/bin/python import_ebms.py >> /var/log/thalera_import_ebms.log 2>&1
```

> Appeler **directement le python du venv**, ne pas activer l'environnement :
> cron exécute les commandes avec `/bin/sh` (dash), qui ne connaît pas le builtin
> bash `source` (`/bin/sh: 1: source: not found`). Le script charge lui-même
> `import_ebms.env`, l'activation ne sert donc qu'à fournir `psycopg2`.

Créer le fichier de log et droits d’écriture pour l’utilisateur du cron :

```bash
sudo touch /var/log/thalera_import_ebms.log
sudo chown geonatureadmin:geonatureadmin /var/log/thalera_import_ebms.log
```

L’état incrémental est dans `.import_ebms_state.json` (`metadata.tracking`).  
`--full` reparcours tout en restant **idempotent** sur `id_media_ebms` (1 photo = 1 observation).

---

## 8) Mise à jour en prod (changelog code / config)

```bash
cd /home/geonatureadmin/modules_monitoring/thalera
git pull

source ~/geonature/backend/venv/bin/activate
geonature monitorings install thalera   # ou update selon version Monitoring
sudo systemctl restart geonature
deactivate

./for_install/install_trigger.sh   # si le SQL trigger a changé
./for_install/install_export.sh    # si les vues d'export ont changé
```

---

## 9) Purge données de test (jamais en prod métier sans accord)

```bash
cd /home/geonatureadmin/modules_monitoring/thalera
# le script lit GEONATURE_DB_DSN dans import_ebms.env
psql "$GEONATURE_DB_DSN" -f for_install/purge_import_ebms_test.sql
rm -f .import_ebms_state.json
```

---

## Checklist go-live

- [ ] Lien `media/monitorings/thalera` OK
- [ ] `geonature monitorings install thalera` OK
- [ ] Module configuré (JDD, listes, frontend actif)
- [ ] Permissions validateurs + export (E)
- [ ] `import_ebms.env` en place (secret entre quotes)
- [ ] CSV taxons présent
- [ ] `THALERA_CD_NOM_FALLBACK` renseigné
- [ ] Trigger installé
- [ ] Exports installés (2 boutons CSV visibles)
- [ ] Import test (`--limit`) OK
- [ ] Cron planifié + log
- [ ] Médias visibles sur une photo dans l’UI

---

## Dépannage rapide

| Symptôme | Cause fréquente |
|----------|-----------------|
| HTTP 401 Incorrect secret | Secret tronqué (`$…`) : remettre des `'…'` dans `.env`, `unset EBMS_SECRET` |
| Sites invisibles | Type de site `thalera` / `cor_site_type` / module non actif frontend |
| Photo sans Taxref | Nom absent du CSV *et* de `taxonomie.taxref` → renseigner `THALERA_CD_NOM_FALLBACK` (voir bilan de fin de run) |
| Import qui repasse toujours les mêmes occurrences | Curseur retenu par une occurrence non résolue : voir stderr « Curseur retenu à … » |
| Taxon manifestement faux | `data.cd_nom_origine = 'fallback'` → à corriger en validation |
| Compteur non validé à 0 | Trigger non installé |
| Export 404 | Vue SQL absente → `./for_install/install_export.sh` |
