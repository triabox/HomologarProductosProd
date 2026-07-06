#!/bin/sh
# Entrypoint del contenedor: sirve el dashboard por HTTP y corre el homologador
# una vez por día a la hora configurada (hora America/Lima).
#
# Env vars:
#   PORT             puerto del dashboard (default 8080)
#   RUN_TIME         hora local de la corrida diaria HH:MM (default 02:00)
#   MAX_RUNTIME_MIN  presupuesto de tiempo por corrida (default 30)
#   RUN_ON_START     "true" = correr también al arrancar el contenedor
set -u

PORT="${PORT:-8080}"
RUN_TIME="${RUN_TIME:-02:00}"
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

echo "[entrypoint] dashboard en puerto ${PORT}; corrida diaria a las ${RUN_TIME} (TZ=${TZ:-UTC})"
python -m http.server "${PORT}" --directory reports &

if [ "${RUN_ON_START}" = "true" ]; then
  echo "[entrypoint] RUN_ON_START: corrida inicial de ${MAX_RUNTIME_MIN} min"
  homologador run --max-runtime "${MAX_RUNTIME_MIN}" || true
fi

while :; do
  now=$(date +%s)
  target=$(date -d "today ${RUN_TIME}" +%s)
  if [ "${target}" -le "${now}" ]; then
    target=$((target + 86400))
  fi
  echo "[entrypoint] próxima corrida: $(date -d "@${target}")"
  sleep $((target - now))
  homologador run --max-runtime "${MAX_RUNTIME_MIN}" || true
done
