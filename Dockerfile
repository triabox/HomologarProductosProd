FROM python:3.12-slim

# hora local de Perú para que RUN_TIME se interprete en horario del negocio
ENV TZ=America/Lima \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml config.yaml entrypoint.sh ./
COPY src ./src
COPY templates ./templates

# editable: el código queda en /app/src y el paquete resuelve templates/ y
# config.yaml relativos a /app (igual que en desarrollo)
RUN pip install --no-cache-dir -e . && chmod +x entrypoint.sh

EXPOSE 8080
CMD ["./entrypoint.sh"]
