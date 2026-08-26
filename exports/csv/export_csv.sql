-- Export CSV Thalera
-- - v_export_thalera_standard      : 1 ligne = 1 photo
-- - v_export_thalera_recap_especes : récap validation par espèce (IA)
--
-- Installation :
--   ./for_install/install_export.sh
-- ou :
--   psql "$GEONATURE_DB_DSN" -f exports/csv/export_csv.sql

DROP VIEW IF EXISTS gn_monitoring.v_export_thalera_standard;

CREATE OR REPLACE VIEW gn_monitoring.v_export_thalera_standard AS
SELECT
    -- Site (piège)
    s.base_site_code AS code_site,
    s.base_site_name AS nom_site,
    s.base_site_description AS description_site,
    ST_Y(s.geom) AS latitude,
    ST_X(s.geom) AS longitude,

    -- Visite (event EBMS)
    v.id_base_visit,
    v.visit_date_min AS date_visite,
    v.visit_date_max AS date_visite_fin,
    COALESCE(v.observers_txt, '') AS observateurs,
    v.comments AS commentaire_visite,
    vc.data->>'id_event_ebms' AS id_event_ebms,
    vc.data->>'event_source_system_key' AS event_source_system_key,

    -- Photo (observation)
    o.id_observation,
    o.uuid_observation,
    oc.data->>'id_media_ebms' AS id_media_ebms,
    oc.data->>'id_occurrence_ebms' AS id_occurrence_ebms,
    oc.data->>'occurrence_source_system_key' AS occurrence_source_system_key,
    m.media_url AS url_photo,

    -- Taxon IA / EBMS
    o.cd_nom AS cd_nom_ia,
    tx_ia.lb_nom AS nom_taxon_ia,
    tx_ia.nom_vern AS nom_vernaculaire_ia,
    oc.data->>'taxon_ebms' AS taxon_ebms,
    NULLIF(oc.data->>'score_ia', '')::numeric AS score_ia,

    -- Validation
    oc.data->>'taxon_valide' AS taxon_valide,
    NULLIF(oc.data->>'new_cd_nom', '')::integer AS cd_nom_valide,
    tx_val.lb_nom AS nom_taxon_valide,
    tx_val.nom_vern AS nom_vernaculaire_valide,
    o.comments AS commentaire_photo

FROM gn_monitoring.t_observations o
JOIN gn_monitoring.t_base_visits v
  ON v.id_base_visit = o.id_base_visit
JOIN gn_commons.t_modules mod
  ON mod.id_module = v.id_module
 AND mod.module_code = 'thalera'
JOIN gn_monitoring.t_base_sites s
  ON s.id_base_site = v.id_base_site
LEFT JOIN gn_monitoring.t_visit_complements vc
  ON vc.id_base_visit = v.id_base_visit
LEFT JOIN gn_monitoring.t_site_complements sc
  ON sc.id_base_site = s.id_base_site
LEFT JOIN gn_monitoring.t_observation_complements oc
  ON oc.id_observation = o.id_observation
LEFT JOIN taxonomie.taxref tx_ia
  ON tx_ia.cd_nom = o.cd_nom
LEFT JOIN taxonomie.taxref tx_val
  ON tx_val.cd_nom = NULLIF(oc.data->>'new_cd_nom', '')::integer
LEFT JOIN LATERAL (
    SELECT med.media_url
    FROM gn_commons.t_medias med
    WHERE med.uuid_attached_row = o.uuid_observation
    ORDER BY med.id_media
    LIMIT 1
) m ON TRUE
ORDER BY
    s.base_site_code,
    v.visit_date_min,
    o.id_observation;


-- ---------------------------------------------------------------------------
-- Recapitulatif par espece (proposition IA) : validations + pourcentages
-- Vue : gn_monitoring.v_export_thalera_recap_especes
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS gn_monitoring.v_export_thalera_recap_especes;

CREATE OR REPLACE VIEW gn_monitoring.v_export_thalera_recap_especes AS
WITH photos AS (
    SELECT
        o.cd_nom AS cd_nom_ia,
        COALESCE(tx.lb_nom, oc.data->>'taxon_ebms', '(taxon inconnu)') AS nom_taxon_ia,
        tx.nom_vern AS nom_vernaculaire_ia,
        NULLIF(TRIM(oc.data->>'taxon_valide'), '') AS taxon_valide,
        NULLIF(oc.data->>'score_ia', '')::numeric AS score_ia
    FROM gn_monitoring.t_observations o
    JOIN gn_monitoring.t_base_visits v
      ON v.id_base_visit = o.id_base_visit
    JOIN gn_commons.t_modules mod
      ON mod.id_module = v.id_module
     AND mod.module_code = 'thalera'
    LEFT JOIN gn_monitoring.t_observation_complements oc
      ON oc.id_observation = o.id_observation
    LEFT JOIN taxonomie.taxref tx
      ON tx.cd_nom = o.cd_nom
),
par_espece AS (
    SELECT
        cd_nom_ia,
        nom_taxon_ia,
        nom_vernaculaire_ia,
        COUNT(*)::integer AS nb_photos,
        COUNT(*) FILTER (
            WHERE taxon_valide = 'Bon taxon identifié'
        )::integer AS nb_bien_identifiees,
        COUNT(*) FILTER (
            WHERE taxon_valide = 'Mauvais taxon identifié'
        )::integer AS nb_mal_identifiees,
        COUNT(*) FILTER (
            WHERE taxon_valide IS NULL
        )::integer AS nb_non_validees,
        COUNT(*) FILTER (
            WHERE taxon_valide IS NOT NULL
        )::integer AS nb_validees,
        ROUND(AVG(score_ia)::numeric, 6) AS score_ia_moyen
    FROM photos
    GROUP BY cd_nom_ia, nom_taxon_ia, nom_vernaculaire_ia
),
totaux AS (
    SELECT
        COALESCE(SUM(nb_photos), 0)::integer AS nb_photos_total
    FROM par_espece
)
SELECT
    e.cd_nom_ia,
    e.nom_taxon_ia,
    e.nom_vernaculaire_ia,
    e.nb_photos,
    e.nb_bien_identifiees,
    e.nb_mal_identifiees,
    e.nb_non_validees,
    e.nb_validees,
    CASE
        WHEN e.nb_photos > 0
        THEN ROUND(100.0 * e.nb_bien_identifiees / e.nb_photos, 2)
        ELSE 0
    END AS pct_bien_identifiees,
    CASE
        WHEN e.nb_photos > 0
        THEN ROUND(100.0 * e.nb_mal_identifiees / e.nb_photos, 2)
        ELSE 0
    END AS pct_mal_identifiees,
    CASE
        WHEN e.nb_photos > 0
        THEN ROUND(100.0 * e.nb_non_validees / e.nb_photos, 2)
        ELSE 0
    END AS pct_non_validees,
    CASE
        WHEN e.nb_validees > 0
        THEN ROUND(100.0 * e.nb_bien_identifiees / e.nb_validees, 2)
        ELSE NULL
    END AS pct_precision_parmi_validees,
    CASE
        WHEN t.nb_photos_total > 0
        THEN ROUND(100.0 * e.nb_photos / t.nb_photos_total, 2)
        ELSE 0
    END AS pct_du_total_photos,
    e.score_ia_moyen
FROM par_espece e
CROSS JOIN totaux t
ORDER BY e.nb_photos DESC, e.nom_taxon_ia;

