-- Recalcule nb_observations_non_valide sur la visite ET le site Thalera
-- (= observations sans taxon_valide renseigné)
-- à chaque création / modification / suppression d'observation (ou de ses compléments).
--
-- Installation :
--   ./for_install/install_trigger.sh
-- ou :
--   psql "$GEONATURE_DB_DSN" -f for_install/trigger_nb_observations_non_valide.sql
--
-- Prérequis : le module GeoNature doit avoir module_code = 'thalera'.

CREATE OR REPLACE FUNCTION gn_monitoring.fct_tri_thalera_nb_observations_non_valide()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  _id_base_visit integer;
  _id_base_site integer;
  _module_code text;
  _nb_visit integer;
  _nb_site integer;
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF TG_TABLE_NAME = 't_observation_complements' THEN
      SELECT o.id_base_visit
      INTO _id_base_visit
      FROM gn_monitoring.t_observations o
      WHERE o.id_observation = OLD.id_observation;
    ELSE
      _id_base_visit := OLD.id_base_visit;
    END IF;
  ELSE
    IF TG_TABLE_NAME = 't_observation_complements' THEN
      SELECT o.id_base_visit
      INTO _id_base_visit
      FROM gn_monitoring.t_observations o
      WHERE o.id_observation = NEW.id_observation;
    ELSE
      _id_base_visit := NEW.id_base_visit;
    END IF;
  END IF;

  IF _id_base_visit IS NULL THEN
    RETURN COALESCE(NEW, OLD);
  END IF;

  SELECT v.id_base_site, m.module_code
  INTO _id_base_site, _module_code
  FROM gn_monitoring.t_base_visits v
  JOIN gn_commons.t_modules m ON m.id_module = v.id_module
  WHERE v.id_base_visit = _id_base_visit;

  IF _module_code IS DISTINCT FROM 'thalera' THEN
    RETURN COALESCE(NEW, OLD);
  END IF;

  -- Compteur au niveau visite
  SELECT COUNT(*)::integer
  INTO _nb_visit
  FROM gn_monitoring.t_observations o
  LEFT JOIN gn_monitoring.t_observation_complements oc
    ON oc.id_observation = o.id_observation
  WHERE o.id_base_visit = _id_base_visit
    AND NULLIF(TRIM(oc.data->>'taxon_valide'), '') IS NULL;

  INSERT INTO gn_monitoring.t_visit_complements AS vc (id_base_visit, data)
  VALUES (
    _id_base_visit,
    jsonb_build_object('nb_observations_non_valide', _nb_visit)
  )
  ON CONFLICT (id_base_visit) DO UPDATE
  SET data = (COALESCE(vc.data, '{}'::jsonb) - 'nb_observations_valide')
    || jsonb_build_object('nb_observations_non_valide', _nb_visit);

  -- Compteur au niveau site (toutes les visites du site dans le module thalera)
  IF _id_base_site IS NOT NULL THEN
    SELECT COUNT(*)::integer
    INTO _nb_site
    FROM gn_monitoring.t_observations o
    LEFT JOIN gn_monitoring.t_observation_complements oc
      ON oc.id_observation = o.id_observation
    JOIN gn_monitoring.t_base_visits v
      ON v.id_base_visit = o.id_base_visit
    JOIN gn_commons.t_modules m
      ON m.id_module = v.id_module
    WHERE v.id_base_site = _id_base_site
      AND m.module_code = 'thalera'
      AND NULLIF(TRIM(oc.data->>'taxon_valide'), '') IS NULL;

    UPDATE gn_monitoring.t_site_complements
    SET data = (COALESCE(data, '{}'::jsonb) - 'nb_observations_valide')
      || jsonb_build_object('nb_observations_non_valide', _nb_site)
    WHERE id_base_site = _id_base_site;

    IF NOT FOUND THEN
      INSERT INTO gn_monitoring.t_site_complements (id_base_site, data)
      VALUES (
        _id_base_site,
        jsonb_build_object('nb_observations_non_valide', _nb_site)
      );
    END IF;
  END IF;

  RETURN COALESCE(NEW, OLD);
END;
$$;

-- Nettoyage de l'ancienne version "valide"
DROP TRIGGER IF EXISTS tri_thalera_nb_obs_valide_oc
  ON gn_monitoring.t_observation_complements;
DROP TRIGGER IF EXISTS tri_thalera_nb_obs_valide_obs
  ON gn_monitoring.t_observations;
DROP FUNCTION IF EXISTS gn_monitoring.fct_tri_thalera_nb_observations_valide();

DROP TRIGGER IF EXISTS tri_thalera_nb_obs_non_valide_oc
  ON gn_monitoring.t_observation_complements;
CREATE TRIGGER tri_thalera_nb_obs_non_valide_oc
  AFTER INSERT OR UPDATE OR DELETE
  ON gn_monitoring.t_observation_complements
  FOR EACH ROW
  EXECUTE PROCEDURE gn_monitoring.fct_tri_thalera_nb_observations_non_valide();

DROP TRIGGER IF EXISTS tri_thalera_nb_obs_non_valide_obs
  ON gn_monitoring.t_observations;
CREATE TRIGGER tri_thalera_nb_obs_non_valide_obs
  AFTER INSERT OR DELETE
  ON gn_monitoring.t_observations
  FOR EACH ROW
  EXECUTE PROCEDURE gn_monitoring.fct_tri_thalera_nb_observations_non_valide();
