#!/usr/bin/env bash
# Installe / met à jour le trigger nb_observations_non_valide (module thalera).
# Usage :
#   ./for_install/install_trigger.sh
#   ./for_install/install_trigger.sh postgresql://user:pass@localhost:5432/geonature2db

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SQL="$ROOT/for_install/trigger_nb_observations_non_valide.sql"

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
echo "Trigger thalera nb_observations_non_valide installé."
