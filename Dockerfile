FROM python:3.12-slim

# hora local de Perú para que RUN_TIME se interprete en horario del negocio
ENV TZ=America/Lima \
    PYTHONUNBUFFERED=1

# libs de sistema para WeasyPrint (export a PDF) + fuentes
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml config.yaml entrypoint.sh ./
COPY src ./src
COPY templates ./templates

# editable: el código queda en /app/src y el paquete resuelve templates/ y
# config.yaml relativos a /app (igual que en desarrollo)
RUN pip install --no-cache-dir -e '.[pdf]' && chmod +x entrypoint.sh

EXPOSE 8080
CMD ["./entrypoint.sh"]
