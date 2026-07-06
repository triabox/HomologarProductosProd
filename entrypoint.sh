#!/bin/sh
# Entrypoint del contenedor: sirve el dashboard por HTTP y corre el homologador
# una vez por día a la hora configurada (hora America/Lima).
#
# Env vars:
#   PORT             puerto del dashboard (default 8080)
#   RUN_TIMES        horas locales de corrida, separadas por coma (default "02:00")
#                    ej. "02:00,12:00" = madrugada y mediodía
#   RUN_TIME         (compat) usado si RUN_TIMES no está definida
#   MAX_RUNTIME_MIN  presupuesto de tiempo por corrida (default 30)
#   RUN_ON_START     "true" = correr también al arrancar el contenedor
set -u

PORT="${PORT:-8080}"
RUN_TIMES="${RUN_TIMES:-${RUN_TIME:-02:00}}"
MAX_RUNTIME_MIN="${MAX_RUNTIME_MIN:-30}"
RUN_ON_START="${RUN_ON_START:-false}"

mkdir -p data reports

echo "[entrypoint] verificación de conectividad:"
python - <<'PY'
import httpx
for url in ("https://www.oechsle.pe/api/catalog_system/pub/category/tree/1",
            "https://oechsle.cord.pe/"):
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"})
        print(f"  {url} -> HTTP {r.status_code}")
    except Exception as e:
        print(f"  {url} -> ERROR {type(e).__name__}: {e}")
PY

# regenerar dashboard desde la base persistida (si la hay)
homologador report >/dev/null 2>&1 || true
if [ ! -f reports/index.html ]; then
  cat > reports/index.html <<'HTML'
<!doctype html><meta charset="utf-8"><title>Homologador CoRD - VTEX</title>
<body style="font-family:system-ui;background:#0f172a;color:#e2e8f0;display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center"><h1>Homologador CoRD &harr; VTEX</h1>
<p>Aún no hay corridas registradas. La corrida programada generará el primer dashboard.</p></div>
HTML
fi

echo "[entrypoint] dashboard en puerto ${PORT}; corridas diarias a las ${RUN_TIMES} (TZ=${TZ:-UTC})"
python -m http.server "${PORT}" --directory reports &

if [ "${RUN_ON_START}" = "true" ]; then
  echo "[entrypoint] RUN_ON_START: corrida inicial de ${MAX_RUNTIME_MIN} min"
  homologador run --max-runtime "${MAX_RUNTIME_MIN}" || true
fi

while :; do
  now=$(date +%s)
  # próxima corrida = el horario más cercano entre todos los configurados
  next=""
  for t in $(echo "${RUN_TIMES}" | tr ',' ' '); do
    target=$(date -d "today ${t}" +%s) || continue
    if [ "${target}" -le "${now}" ]; then
      target=$((target + 86400))
    fi
    if [ -z "${next}" ] || [ "${target}" -lt "${next}" ]; then
      next=${target}
    fi
  done
  if [ -z "${next}" ]; then
    echo "[entrypoint] RUN_TIMES inválido (${RUN_TIMES}); duermo 1h"
    sleep 3600
    continue
  fi
  echo "[entrypoint] próxima corrida: $(date -d "@${next}")"
  sleep $((next - now))
  homologador run --max-runtime "${MAX_RUNTIME_MIN}" || true
done
