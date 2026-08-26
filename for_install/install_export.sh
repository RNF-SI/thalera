#!/usr/bin/env bash
# Installe / met à jour la vue d'export CSV Thalera (1 ligne = 1 photo).
# Usage :
#   ./for_install/install_export.sh
#   ./for_install/install_export.sh postgresql://user:pass@localhost:5432/geonature2db

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SQL="$ROOT/exports/csv/export_csv.sql"

if [[ -f "$ROOT/import_ebms.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/import_ebms.env"
  set +a
fi

DSN="${1:-${GEONATURE_DB_DSN:-}}"
if [[ -z "$DSN" ]]; then
  echo "GEONATURE_DB_DSN manquant (import_ebms.env ou argument)." >&2
  exit 1
fi

psql "$DSN" -v ON_ERROR_STOP=1 -f "$SQL"
echo "Vues d'export Thalera installées (standard + recap_especes)."
