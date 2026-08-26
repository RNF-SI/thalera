-- Purge des données importées EBMS pour le module thalera (tests).
-- À lancer sur la base GeoNature, ex. :
--   psql "$GEONATURE_DB_DSN" -f for_install/purge_import_ebms_test.sql
-- Puis réinitialiser l'état incrémental :
--   rm -f .import_ebms_state.json

BEGIN;

CREATE TEMP TABLE _thalera_sites ON COMMIT DROP AS
SELECT DISTINCT csm.id_base_site
FROM gn_monitoring.cor_site_module csm
JOIN gn_commons.t_modules mod ON mod.id_module = csm.id_module
WHERE mod.module_code = 'thalera';

CREATE TEMP TABLE _thalera_visits ON COMMIT DROP AS
SELECT v.id_base_visit, v.uuid_base_visit
FROM gn_monitoring.t_base_visits v
JOIN gn_commons.t_modules mod ON mod.id_module = v.id_module
WHERE mod.module_code = 'thalera';

CREATE TEMP TABLE _thalera_obs ON COMMIT DROP AS
SELECT o.id_observation, o.uuid_observation
FROM gn_monitoring.t_observations o
JOIN _thalera_visits v ON v.id_base_visit = o.id_base_visit;

-- Médias (observations, visites, sites)
DELETE FROM gn_commons.t_medias m
USING _thalera_obs o
WHERE m.uuid_attached_row = o.uuid_observation;

DELETE FROM gn_commons.t_medias m
USING _thalera_visits v
WHERE m.uuid_attached_row = v.uuid_base_visit;

DELETE FROM gn_commons.t_medias m
USING gn_monitoring.t_base_sites s
JOIN _thalera_sites ts ON ts.id_base_site = s.id_base_site
WHERE m.uuid_attached_row = s.uuid_base_site;

DELETE FROM gn_monitoring.t_observation_complements oc
USING _thalera_obs o
WHERE oc.id_observation = o.id_observation;

DELETE FROM gn_monitoring.t_observations o
USING _thalera_obs t
WHERE o.id_observation = t.id_observation;

DELETE FROM gn_monitoring.t_visit_complements vc
USING _thalera_visits v
WHERE vc.id_base_visit = v.id_base_visit;

DELETE FROM gn_monitoring.cor_visit_observer cvo
USING _thalera_visits v
WHERE cvo.id_base_visit = v.id_base_visit;

DELETE FROM gn_monitoring.t_base_visits v
USING _thalera_visits t
WHERE v.id_base_visit = t.id_base_visit;

DELETE FROM gn_monitoring.cor_site_type cst
USING _thalera_sites ts
WHERE cst.id_base_site = ts.id_base_site;

DELETE FROM gn_monitoring.t_site_complements sc
USING _thalera_sites ts
WHERE sc.id_base_site = ts.id_base_site;

DELETE FROM gn_monitoring.cor_site_module csm
USING gn_commons.t_modules mod
WHERE csm.id_module = mod.id_module
  AND mod.module_code = 'thalera';

-- Sites qui n'appartiennent plus à aucun module
DELETE FROM gn_monitoring.t_base_sites s
USING _thalera_sites ts
WHERE s.id_base_site = ts.id_base_site
  AND NOT EXISTS (
      SELECT 1
      FROM gn_monitoring.cor_site_module csm
      WHERE csm.id_base_site = s.id_base_site
  );

COMMIT;
