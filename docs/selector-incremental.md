# Selector incremental: diseño (P1.6, 3-ago-2026)

Recibos de código = mapa del 3-ago sobre `refresh_selector.py` / `ingest.py`.

## El problema, con números

`refresh_priorities(force=True)` corre cada pasada del servicio (intervalo
nominal 60 s) y hace DOS full scans sin `.iterator()` (`ingest.py:1698-1709`):
5,9 M posiciones a dicts (`val`, `white_stm`, `settled`) y 6,2 M aristas a
adyacencia (`children`), más un Dijkstra completo y un `dirty` que acumula
INSTANCIAS ORM enteras (queryset sin `.only()`, `ingest.py:1802-1830`) antes
del primer `bulk_update`. Resultado medido en cx43: 4,7 GB de arranque,
+0,26 GB/h hasta 10,6 GB (arenas de CPython que no vuelven al SO), 3-4 h de
CPU por ciclo, y una vez arrastró a PostgreSQL al OOM. El propio docstring lo
predijo ("ten times bigger it is the first thing that breaks"); vamos 13×.

Carrera conocida de regalo: las dos fotos (posiciones, aristas) son
secuenciales y sin transacción; una posición creada entre ambas aparece en
`children` pero no en `white_stm` → `KeyError: <hash>` en `ingest.py:1746`,
absorbido por la red de `step()` unas veces por hora.

## La observación que desbloquea

Nadie consume la prioridad de 5,9 M filas. Los consumidores reales:

- `next_tasks` (`ingest.py:1899`): top `4·n` por `-priority`.
- `choose_pending` (`views.py:362`): banda USER aparte; el resto ordena por
  la priority ya escrita.
- un widget de portada (`views.py:2593`): top 12.

Solo importa LA CIMA. Y la fórmula (`ingest.py:1808-1828`) acota lo que un
nodo puede sumar: cercanía ≤15, mate-band +50, sin-expandir +2, campaña
+40·log1p(votos); y resta 3·runits (regret saturado a 30 unidades) y
1,5·visits. Un nodo con runits altos NO puede competir con la cima salvo
mate-band o campaña — y eso da un **horizonte de poda dinámico**:

    H = (bonos_máximos_posibles − corte_actual_del_topN) / REGRET_WEIGHT

donde `corte_actual` es la prioridad del N-ésimo mejor ya conocido
(N = 4·TASK_REFILL_COUNT o 1.000, lo que sea mayor). Todo nodo cuyo mejor
caso quede bajo el corte no se expande. En la práctica H ≪ 30 unidades: la
bola competitiva es una fracción pequeña del grafo.

## Arquitectura

1. **Dijkstra acotado por lotes, sin snapshot global.** Frontera con heap
   como hoy; la adyacencia y los valores se leen POR LOTES al expandir
   (`Edge.objects.filter(parent_id__in=lote).values_list(...)` +
   `Position.objects.filter(key__in=necesarios).values_list(...)`), con un
   caché local solo de la bola explorada. Memoria O(bola). La carrera del
   KeyError desaparece estructuralmente: una clave que no está en el lote es
   frontera nueva o nodo recién nacido — se salta y la siguiente pasada lo ve.
2. **`reachable` persistente e incremental.** Booleano/época en `Position`,
   propagado en `expand()` (`ingest.py:299-301`: al crear la arista, si el
   padre es reachable el hijo lo es) + backfill one-shot. Distingue
   "conectado lejano" (runits 30) de "desconectado" (5, `DISCONNECTED_REGRET`)
   sin recorrer el grafo. Migración pequeña con índice parcial.
3. **Escritura ligera.** El refresh recorre UNKNOWN con
   `values_list('key','priority','visits','expanded','campaign_id')` +
   `iterator()`, calcula, y hace `bulk_update` sobre shells
   `Position(key=k)` con solo `priority` — nada de instancias completas
   retenidas.
4. **Fórmula intacta.** Mismos pesos, mismos términos (`best_known_eval`,
   mate-band, expanded, visits, campañas ACTIVE con log1p). Fuera de la bola:
   runits = 30 si reachable, 5 si no — exactamente la semántica actual de
   lejos/desconectado.
5. **Cadencia real de 60 s.** La pasada acotada debe caber en segundos. Si
   el crecimiento vuelve a comerse el margen, siguiente palanca ya prevista:
   modo delta (solo posiciones con `updated > última_pasada` y sus padres;
   el patrón ya existe en `enqueue_coverage_completion`, `ingest.py:2934`).

## Lo que NO cambia

Banda USER del lease, inline fallback (`ATOMICDB_INLINE_SELECTOR`), el orden
de pasos del servicio (`refresh_selector.py:163-208`), tombstones
(`priority__gt=DEAD/2`) y el contrato de `next_tasks`.

## Validación antes de conmutar (obligatoria)

- **Modo sombra**: N pasadas con ambos motores (viejo y nuevo) sobre la
  misma base, comparando el top-1000 por prioridad: Jaccard del conjunto y
  Kendall τ del orden; se exige Jaccard ≥ 0,95 y τ ≥ 0,9 sostenidos, y se
  publican los números.
- Conmutador por setting (`ATOMICDB_SELECTOR_V2=1`) con vuelta atrás
  inmediata; el servicio viejo queda una semana como plan B.
- El `RuntimeMaxSec=6h` se queda de cinturón hasta ver una semana de RSS
  plano; objetivo: pasada < 300 MB y < 60 s.

## Estimación de efecto

RAM del servicio: 4,7-10,6 GB → cientos de MB. CPU: 3-4 h/ciclo → segundos.
La web deja de compartir máquina con un monstruo; el KeyError muere; el
recycle de 6 h pasa de necesidad a paranoia sana.
