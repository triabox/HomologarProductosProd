# Homologador de Productos CoRD ↔ VTEX (Oechsle)

Valida que los productos cargados en **CoRD** (`oechsle.cord.pe`) estén **bien cargados y
actualizados** comparándolos contra **VTEX** (`oechsle.pe`), la fuente de verdad de la migración.

**Dirección:** CoRD es el driver. Se recorre el catálogo de CoRD por categoría y, por cada
producto, se busca su equivalente en VTEX **por SKU**. Lo que está en CoRD debe coincidir; que
falten productos de VTEX en CoRD no es error en esta etapa.

## Cómo funciona

```
CoRD PLP (crawl)  ─►  productos (SKU+URL)  ─►  scrape detalle CoRD
                                                      │
SKU  ─►  VTEX API (lookup por skuId)  ─────────────►  motor de comparación
                                                      │   (comparadores pluggables)
                                          SQLite (histórico + cursor) ─► dashboard HTML
```

- **VTEX**: API pública de catálogo. Lookup por SKU:
  `GET /api/catalog_system/pub/products/search?fq=skuId:<sku>`. Árbol de categorías:
  `/api/catalog_system/pub/category/tree/50`.
- **CoRD**: Next.js server-rendered. Los datos vienen en el payload RSC
  (`self.__next_f.push`): precio, especificaciones `{label,value}`, marca, categoría. El nombre
  sale del `<h1>` y el path de categoría del breadcrumb JSON-LD. **No** hay sitemap ni API
  pública (entorno interno); las PLP se descubren derivando el slug del path de categoría
  (`/Tecnologia/Telefonia/Celulares/` → `/tecnologia/telefonia/celulares`).

## Instalación

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
```

## Uso

```bash
# Comparar un único producto (verificación rápida)
homologador seed "https://oechsle.cord.pe/celular-motorola-moto-g06-4-256gb-azul-3043308/p"

# Corrida acotada por tiempo (30 min) — reanuda donde quedó la vez anterior
homologador run --max-runtime 30

# Corrida de una categoría puntual
homologador run --category celulares --limit-categories 1

# Corrida completa desde cero (ignora el cursor)
homologador run --no-resume

# Regenerar dashboards desde la base
homologador report
```

Salidas en `reports/`: `run-<id>-*.html` (dashboard de la corrida) y `trends.html`
(evolución de KPIs entre corridas).

## Corrida diaria por tiempo (cron)

El estado vive en SQLite, así que cada corrida retoma desde el cursor. Para correr 30 min
todos los días a las 02:00:

```cron
0 2 * * * cd /ruta/HomologarProductosProd && ./.venv/bin/homologador run --max-runtime 30 >> data/cron.log 2>&1
```

Cuando se completa todo el catálogo (~1705 categorías), el cursor se reinicia solo y empieza un
nuevo ciclo de re-validación.

## KPIs

Por categoría y global, con histórico y deltas vs la corrida previa:
cobertura (SKU de CoRD hallados en VTEX), match por campo (precio/nombre/descripción/atributos),
score de homologación 0–100 ponderado, y conteo de discrepancias por severidad.

## Extender: agregar un comparador

Crear `src/homologador/comparators/<algo>.py`:

```python
from ..models import FieldResult, Product, Severity
from .base import Comparator, register

@register
class StockComparator(Comparator):
    key = "stock"
    label = "Stock"
    def compare(self, cord: Product, vtex: Product) -> FieldResult:
        ...
        return FieldResult(field=self.key, ok=..., score=..., severity=...)
```

Se registra solo y aparece automáticamente en el motor, la base y el dashboard. Opcional: darle
peso en `score_weights` dentro de `config.yaml`.

## Configuración

Todo en `config.yaml`: URLs, concurrencia/rate-limit/caché HTTP, tamaño de muestra por
categoría (≥20), umbrales de los comparadores, pesos del score, objetivos por campo
(`goals`) y presupuesto de tiempo.

**Muestreo (`sampling.rotate`)**: con `true` (default) cada corrida valida productos
**distintos** por categoría (rota con un offset persistido por categoría, cubriendo todo el
set descubrible en pocas corridas). Con `false` usa siempre la misma muestra — útil para
comparar tendencias "manzana con manzana".

## Limitaciones conocidas (etapa 1)

- **Paginación de PLP**: CoRD pagina del lado del cliente; la carga server-rendered trae ~32
  productos por categoría, suficiente para muestrear ≥20. Cubrir el 100% del catálogo de una
  categoría requeriría la API interna de CoRD o renderizado headless (`--render`, pendiente).
- **Descripción**: hoy ambos sistemas exponen el mismo meta descriptivo; el comparador de
  descripción es por eso poco discriminante. Se mantiene para detectar divergencias futuras.
- **Mapeo por SKU**: válido para productos vendidos por Oechsle (mismo SKU). Productos de
  terceros/marketplace pueden no resolver en VTEX y se reportan como "sin equivalente".
