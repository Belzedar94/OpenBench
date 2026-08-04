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

## Modo delta (implementado, 4-ago-2026)

La palanca del punto 5, tirada: la lectura ya estaba acotada por la bola, pero
la fase de ESCRITURA seguía recorriendo el universo UNKNOWN entero — 206 s con
la base tranquila y hasta 2.400 s bajo tormenta de ingesta, por contención con
el procesador de la cola sobre la misma tabla.

Una pasada delta re-puntúa tres conjuntos y nada más:

1. **la bola entera**, siempre: es donde vive la cima, es lo único que alguien
   consume, y ya está calculada y en memoria;
2. **lo tocado** desde la última pasada (`updated > since`): fuera de la bola
   el regret es una constante (30 conectado / 5 suelto), así que la prioridad
   de un nodo de ahí fuera sólo cambia si cambian *sus* columnas;
3. **los padres directos de lo tocado**, que es el único agujero real de la
   marca y está medido en el código: `backup_backed_evals` escribe a los
   padres con `bulk_update(dirty, _BACKED_FIELDS)` y esa lista no lleva
   `updated`, así que un padre puede estrenar `backed_eval` — un término de la
   fórmula — sin que su marca se mueva. Un ply de padres cose el lado de las
   hojas (que cubre `updated`) con el lado de la raíz (que cubre la bola).

**No cambia la fórmula, ni los precios de fuera de la bola, ni las lápidas, ni
el filtro de vivas.** El delta decide *qué filas se reescriben*, nunca *con qué
número*: el ancla de paridad (`test_selector_v2`) sigue comparando v1 contra v2
en modo completo, y `DeltaAgreesWithTheCompletePassTests` compara los dos modos
del mismo motor.

**Precio declarado:** una fila que nadie tocó y que quedó fuera de la bola
conserva la prioridad de una pasada anterior. Vive sólo en el fondo de la
tabla, que es la parte que ningún consumidor lee.

**Estado y fallback:** el timestamp de la última pasada vive en el proceso
(`ingest._selector_delta_state`), igual que `_priority_refresh_cache` — el
servicio no persiste nada en la base y esto no iba a ser lo primero. Primera
pasada tras arrancar: completa. Más de `SELECTOR_DELTA_MAX_GAP` (10 min) sin
pasada: completa. Modo sombra (`top_k`): nunca delta. `ATOMICDB_SELECTOR_DELTA
= False` devuelve la pasada completa sin desplegar.

**Se arregló de camino** lo que el delta destapó: dos sitios escribían términos
de la fórmula con un `update` de queryset, que no dispara `auto_now`, así que
cambiaban el precio de una fila a espaldas de la marca — `_revive_tombstones`
(una lápida levantada vuelve con prioridad 0,0, que es *alta*) y
`_tag_campaign_subtree` (el bono de campaña son hasta 40 unidades). Los dos
ponen ya `updated` a mano, como ya hacían los cierres y la siembra de eval.

**Instrumentación:** cada pasada del servicio publica `selector_mode`,
`selector_rows` y `selector_seconds` en su línea de JSON, visible en
journalctl. Una racha de `full` no es un fallo del delta: es el proceso
diciendo que lleva reiniciándose.

**Migración 0038** (`Position.updated` indexada). No hace falta para que el
delta sea *correcto* — sin índice la consulta da las mismas filas y sólo tarda
más — así que no hay ninguna prisa que justifique una ventana de bloqueo mala
en Postgres. De paso arregla el `order_by('-updated')` de
`enqueue_coverage_completion`, que sin índice ordenaba la tabla entera cada
pasada del mismo servicio.

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
