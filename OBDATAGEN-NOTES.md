# OBDATAGEN-NOTES

Fecha de validación: 2026-07-15 (Europe/Madrid).

## Aislamiento y resultado

Todo el trabajo se hizo en el clon
`C:\Users\djime\Documents\Chess_variants\Codex\Fairy-Stockfish organization\openbench-spell-dev`,
rama local `datagen-mode`. La instancia de producción `../openbench-spell`, su
SQLite, Media, servidor `:8000` y sus procesos no se modificaron. Después del
`git clone` requerido no se ejecutó ninguna escritura contra ese árbol. Los
procesos finalizados durante los gates fueron exclusivamente clientes dev
identificados por PID y ruta dentro de `openbench-spell-dev`. No hubo push.

Recursos usados para el E2E: servidor `127.0.0.1:8001`, DB SQLite nueva,
cliente dev `-T 2 -N 1` y `OPENBENCH_BUILD_JOBS=8`.

## Commits de la rama

| Commit | Motivo |
|---|---|
| `440898b` | Establecer un baseline explícito de migraciones antes de cambiar el esquema. |
| `b948035` | Añadir el scheduler, modelo, upload, API y UI DATAGEN opacos y genéricos. |
| `ec3c78a` | Añadir ejecución, heartbeat, compresión y upload DATAGEN al cliente. |
| `d6b679b` | Corregir la ruta Windows del ejecutable descubierta por el E2E y fijarla con test. |
| `a0e38cc` | Documentar contrato operativo, sizing y despliegue. |

## Gates

| Gate | Estado | Evidencia cuantitativa |
|---|---|---|
| 1. DB nueva + server `:8001` | **PASS** | `0001_initial` y `0002_...datagen...` aplicadas; `manage.py check` = 0 issues; HTTP `/` = 200; listener exclusivo en `127.0.0.1:8001`. |
| 2. DATAGEN Spell 16k/8 chunks | **PASS** | Test #1, branch `nnue-v2`, commit `4c7e8d1305028ecac7ef453a8e10ec6f0284f846`, bench `12231192`; 16.000/16.000 posiciones, 8/8 filas `COMPLETED`, 8 archivos, 329.045 bytes bzip2. |
| 3. Merge + auditor run7 | **PASS** | Cabecera merged `count=16000`, `source_count=27456`, tamaño 704.032 = 32 + 16.000×44, SHA-256 `8a253ee1b9ba174da5bf55e39982aed0ee81360d75561687a610d9e5368ddbaa`; `audit_run7.py` exit 0. |
| 4. Regresión SPRT | **PASS** | Test #2 creado por la vista (`302 /index/`), cliente aceptó workload, verificó dos benches `12231192` y lanzó `uci_pair_runner` con concurrencia 2; primer lote registrado: 4 partidas, 0-3-1. |
| 5. Fallo y reparto | **PASS** | Test #3, comando UCI inexistente: dos asignaciones reales, `attempts=2`; tras cada fallo volvió a `PENDING`, dos eventos de error, cliente vivo y pidiendo trabajo. |

No hubo un gate fallido. El auditor emitió una advertencia de distribución
informativa propia de un piloto de 16k (fase de pociones 3 = 0,025%, por debajo
del 5%). El propio auditor clasifica el tamaño como `pilot/partial`, devuelve
código 0 sin `--strict` y validó limpiamente estructura, count y registros. No
se presentó como un pase `--strict` de distribución de una producción de 50M.

## Gate 1: migraciones, servidor y UI

La DB `db.sqlite3` se creó vacía en el clon y se ejecutó `python manage.py
migrate`. Estado final:

```text
OpenBench
 [X] 0001_initial
 [X] 0002_test_datagen_base_seed_test_datagen_command_and_more
```

El servidor de desarrollo arrancó con:

```text
manage.py runserver 127.0.0.1:8001 --noreload
```

Durante la verificación, PID launcher 6324 / PID Python 1344 y listener PID
1344. `/`, `/test/1/` y la descarga de chunks respondieron 200. La página del
test contenía `8 / 8` y enlaces para chunk 0 y chunk 7. La página de creación
mostró los modos SPRT/GAMES/DATAGEN y los campos de plantilla, total, tamaño de
chunk y semilla.

`manage.py makemigrations --check --dry-run` terminó con `No changes detected`.

## Gate 2: E2E DATAGEN

Configuración real del test #1:

```text
engine: Spell-Stockfish
branch: nnue-v2
commit: 4c7e8d1305028ecac7ef453a8e10ec6f0284f846
bench: 12231192
template: datagen book {BOOK} nodes 1000 count {COUNT} threads {THREADS} seed {SEED} out {OUT}
book: spell_openings.epd
total: 16000
positions_per_chunk: 2000
base_seed: 410000
client threads: 2
```

El cliente ejecutó semillas 410000..410007. Desde la primera asignación útil
del chunk 0 hasta completar el chunk 7 transcurrieron 246,1 s; incluye polling y
un bench antes de cada chunk. Los DATAGEN pequeños de gate tardaron 2,94–8,00 s
por chunk y son deliberadamente más cortos que el sizing recomendado de
producción.

| Chunk | Count | Attempts | Bytes `.bz2` | SHA-256 |
|---:|---:|---:|---:|---|
| 0 | 2.000 | 2 | 42.483 | `688d91e7ccd0ccd691f93a8ab5aea26436a5cf2e512287dc469f5caebae09461` |
| 1 | 2.000 | 1 | 41.225 | `b2660641fb4da418894ffc158ce9a57eba3b5173d6c0081b9fcd6da197c458c5` |
| 2 | 2.000 | 1 | 41.356 | `8fbc946eeca4eb4af77417e4a448b7d6fc4c76efb2793a0b7c82b19d8726b423` |
| 3 | 2.000 | 1 | 39.710 | `8d7c944986ecb50be0e182ce71a193af899b4ec7fd4257665e2a37bf4aa9e7d8` |
| 4 | 2.000 | 1 | 41.036 | `95b9b8f4342ab75ee27db9d1fe88cdc581107e7cac590c5b40ae94728b3ccadf` |
| 5 | 2.000 | 1 | 40.423 | `ae9ae26ce5c94473aa3cd17bab2145eeb640c6c0a307d01b5c29eb19daa8b68d` |
| 6 | 2.000 | 1 | 41.836 | `baf92c1b628f8bd68a39701058352839da4d3815ed49b8c31098afded85661f9` |
| 7 | 2.000 | 1 | 40.976 | `62b4ba74f64e7d311ac864684c40c1119ff3363ff121bc889464c4aac7b51c23` |

Se recalcularon de forma independiente los ocho SHA y tamaños desde
`Media/datagen/1`; todos coincidieron con `DatagenChunk`. La descarga HTTP del
chunk 0 devolvió 42.483 bytes y el mismo SHA. El test terminó automáticamente
con `finished=True`, `passed=True` y `games=16000` (columna histórica usada como
contador visible de posiciones).

El segundo intento del chunk 0 es evidencia de una incidencia real encontrada
durante el gate: el cliente intentaba abrir el basename desde `Client/` aunque
el binario estaba en `Client/Engines/`, produjo `WinError 2` y el servidor lo
reencoló correctamente. Se corrigió en `d6b679b`; el resto del E2E se completó
con la ruta corregida.

## Gate 3: merge offline run7

El helper ad hoc quedó en `.scratch/merge_run7_chunks.py`; no forma parte del
servidor. Para cada `.bz2` validó magic `RUN7`, versión 1, record size 44, count
declarado y tamaño del payload. Conservó flags comunes, sumó `source_count` y
reescribió una única cabecera `<4sHHQQQ>`.

```text
8 × count 2000 = 16000
8 × payload 88000 = 704000 bytes
header = 32 bytes
merged = 704032 bytes
source_count sum = 27456
merged SHA-256 = 8a253ee1b9ba174da5bf55e39982aed0ee81360d75561687a610d9e5368ddbaa
```

Se ejecutó el auditor original, sin modificar el repo del motor:

```text
Spell-Stockfish/tools/spellnnue-pytorch/audit_run7.py .scratch/merged_run7.bin
```

Resultado: exit 0, `records 16,000`, `file bytes 704,032 (32 + 16,000 x 44)` y
ningún error de estructura o registro. La advertencia piloto se conserva arriba
para no confundir este smoke con la auditoría estadística strict de 50M.

## Gate 4: compatibilidad SPRT

El test #2 se creó en la misma DB y por el mismo flujo de vista que un test
normal. Dev/base apuntaron a `nnue-v2`, opciones `Threads=1 Hash=32` y TC
`0.10+0.01`. El cliente reutilizó el engine cacheado, verificó ambos benches,
calculó sus escalas y lanzó:

```text
uci_pair_runner.py ... -variant spell-chess -concurrency 2 -games 4 ...
```

OpenBench registró 4 partidas: 0 wins, 3 losses, 1 draw, 1 timeloss. El test se
marcó finalizado manualmente después de demostrar aceptación y arranque; no es
un resultado de fuerza. No apareció ningún error nuevo en el scheduler SPRT.

## Gate 5: fallo limpio y reasignación

Plantilla del test #3:

```text
definitely_not_datagen seed {SEED} count {COUNT} threads {THREADS} out {OUT}
```

En ambas sesiones el motor terminó sin crear `{OUT}`. El cliente reportó
`DATAGEN chunk 0 failed: DATAGEN command completed without creating {OUT}`; el
servidor soltó machine/lease, conservó el error y dejó el chunk `PENDING`. La
primera sesión siguió viva y pidió otro workload. Tras reiniciar sólo el cliente
dev, el servidor asignó el mismo chunk otra vez (`attempts=2`); volvió a fallar,
se reencoló y el proceso cliente continuó vivo. La traza se escribe en el log de
errores, pero la excepción queda capturada por el loop del worker y no tumba el
cliente. El test se cerró manualmente dejando la fila pendiente como evidencia.

## Regresión automatizada final

```text
python manage.py test OpenBench.tests -v 1
Found 28 test(s) ... Ran 28 tests ... OK

python -m unittest discover -s UnitTests -v
Ran 25 tests ... OK

python manage.py check
System check identified no issues (0 silenced).

python manage.py makemigrations --check --dry-run
No changes detected
```

Los 28 tests Django cubren creación/validación, chunking, asignación, lease,
heartbeat, upload, idempotencia/conflicto, error/requeue, restart y rutas Atomic
existentes. Los 25 tests cliente incluyen 7 DATAGEN y las regresiones anteriores
de onboarding/runner Atomic.

## Decisiones de diseño añadidas al brief

1. **Lease de cinco minutos.** El heartbeat se emite cada 30 s; cinco minutos
   toleran cortes breves y recuperan trabajo abandonado sin intervención.
2. **Upload idempotente.** Repetir exactamente un chunk completado devuelve
   éxito; intentar reemplazarlo por bytes distintos se rechaza.
3. **Hash del transporte.** SHA-256 y bytes son los del `.bz2` recibido y el
   servidor los recalcula. El formato descomprimido nunca entra en la frontera
   universal.
4. **Validación estricta de plantilla.** Una línea, máximo 4096 chars, whitelist
   de placeholders, sin format specs/conversiones, counts positivos y rango
   signed-64 para todas las semillas.
5. **Blacklist local después de error.** Evita que un único cliente recoja en
   bucle el mismo chunk defectuoso; el servidor no lo bloquea para otros.
6. **Reclaim por ownership.** Heartbeat, error y upload exigen test/chunk/machine
   coherentes; un cliente con lease antiguo recibe stop y no puede pisar datos.
7. **Paralelismo de build configurable.** `OPENBENCH_BUILD_JOBS` permite `-j8`
   en este host sin alterar el `make -j` histórico cuando la variable no existe.
8. **Compatibilidad legacy.** Sólo es genérico `DATAGEN` con plantilla no vacía;
   workloads DATAGEN históricos sin plantilla conservan el flujo previo.
9. **Progreso dual.** La UI muestra chunks completos/totales y posiciones
   completas/totales; el test sólo finaliza cuando no falta ningún chunk.
10. **Presets como datos.** El ejemplo Spell vive exclusivamente en
    `Engines/Spell-Stockfish.json`; el scheduler y las vistas no contienen
    nombres, comandos ni formatos Spell/Atomic.

## Qué falta antes de producción

1. Ensayar sobre copias de la DB y `Media`; medir tiempo/espacio de la migración
   SQLite y validar un rollback por restauración.
2. Publicar primero una ref del cliente que incluya v36. El servidor anuncia
   v36 y un cliente viejo intentará auto-update; ref y versión deben desplegarse
   de forma atómica.
3. En ventana de mantenimiento, parar ordenadamente servidor/workers activos y
   hacer backup. La producción no tiene el baseline de migraciones explícito:
   verificar esquema, ejecutar `migrate OpenBench 0001 --fake`, después aplicar
   **de verdad** `migrate OpenBench 0002` y finalmente `migrate`.
4. Verificar permisos/capacidad/backup de `Media/datagen`; definir retención,
   cuotas y alertas de disco antes de cargas grandes.
5. Smoke de un chunk, download, fallo/requeue y un SPRT después del despliegue;
   subir clientes gradualmente.
6. Para upstream a sscg13: rebase sobre su HEAD, adaptar la numeración de
   migraciones y cualquier evolución de modelos, mantener juntos servidor,
   cliente v36, tests y docs, y explicar explícitamente la coexistencia con el
   DATAGEN legacy del fork.

El procedimiento ampliado está en `docs/datagen-mode.md`.

## Estado al cerrar la sesión

Después de capturar todas las evidencias se detuvieron únicamente el cliente y
el servidor dev `:8001`. Se limpiaron el binario cacheado, la copia local del
libro, PGN, sidecars y `__pycache__` generados en `Client/`. Se conservaron la
DB dev, los ocho chunks canónicos de `Media/datagen/1` y las evidencias bajo
`.scratch/`. `git status -sb` quedó sin cambios y no se hizo push.

## Cabecera de métricas del index (2026-07-16)

### Alcance y commit

La cabecera estilo fishtest se implementó en el clon dev, rama `datagen-mode`,
en el commit local `08bf920` (`feat(ui): add cached index capacity metrics`). No
se escribió en `../openbench-spell`, no se lanzó ningún build/engine, no se
tocaron procesos ajenos y no hubo push. La implementación no añade modelos ni
campos: reutiliza `Machine`, `Result`, `Test` y `DatagenChunk`.
La hoja nueva fuerza `OPENBENCH_STATIC_VERSION = 'v6'` para no reutilizar el
CSS `v5` que ya había servido DATAGEN.

### Cómputo de los cuatro cajetines

1. **Cores.** Suma `Machine.info.concurrency` de máquinas cuyo `updated` está
   dentro de los últimos 3 minutos. El nombre visible sigue fishtest, pero el
   valor representa hilos de worker, tal como pide el contrato.
2. **Nodes/sec.** `clientSubmitNPS` guarda en `Machine.mnps` la media por hilo de
   los benches concurrentes. La capacidad total es
   `Σ(mnps × 1e6 × concurrency)` sólo para las mismas máquinas vivas. El
   formateador usa sufijos compactos (`1.06G`, `850M`, etc.).
3. **Games/min.** `Result` es acumulativo y conserva sólo el último `updated`,
   no un evento por submit. Sin migración, el servidor mantiene snapshots
   proceso-locales de `Result.games` y calcula el delta real entre el primer y
   el último snapshot de una ventana móvil de hasta 10 minutos. Tras reiniciar,
   el primer muestreo sólo puede atribuir con seguridad el acumulado de filas
   actualizadas cuyos tests también nacieron dentro de la ventana; los tests
   antiguos arrancan en 0 hasta disponer del segundo snapshot. Es una
   aproximación explícita y conservadora, documentada en el tooltip.
4. **Time remaining.** El tooltip comienza por `ESTIMATE` y aplica:
   - DATAGEN genérico: para cada test,
     `rate = posiciones_completadas / (ahora - primer_chunk_completado)` y
     `segundos = posiciones_restantes / rate`. Ésta es la fórmula exacta sobre
     el progreso persistido y el ritmo medido solicitado.
   - SPRT: trabajo restante =
     `max(mediana_histórica_resuelta - games, 0)`. La mediana se carga una vez
     por proceso usando sólo SPRT finalizados cuyo `currentllr` alcanzó un
     bound; sin historia válida usa 15.000.
   - SPSA: si existen `iterations` y `pairs_per`, quedan
     `max(2 × iterations × pairs_per - games, 0)` partidas. Un SPSA sin esos
     campos se excluye y el número excluido aparece en el tooltip.
   - GAMES y DATAGEN legacy usan el resto exacto `max_games - games`.
   - Total: suma los segundos DATAGEN por test y añade
     `60 × partidas_restantes / games_per_min`. Se formatea como minutos,
     horas o días. Sin máquinas vivas muestra `—`; con trabajo pendiente pero
     ritmo todavía nulo/desconocido muestra `∞`.

Todo el cálculo completo está detrás de una variable módulo con TTL de 30 s y
lock; los pageviews dentro del TTL reutilizan el mismo resultado y no vuelven a
consultar la DB. La mediana SPRT queda además memoizada durante toda la vida del
proceso.

### Gates automatizados

| Gate | Estado | Evidencia |
|---|---|---|
| Unit tests nuevos | **PASS** | 6/6: máquinas viva/muerta, NPS `1.06G`, DATAGEN 500/1.000 a 5 pos/s con ETA 100 s, mediana SPRT 2.000 y delta 100 games/min, SPSA y caché/render. |
| Suite Django completa | **PASS** | `Found 34 test(s)`; `Ran 34 tests in 3.546s`; `OK`. |
| Suite cliente completa | **PASS** | `Ran 27 tests in 0.100s`; `OK`. |
| Django check | **PASS** | `System check identified no issues (0 silenced).` |
| Esquema | **PASS** | `manage.py makemigrations --check --dry-run` → `No changes detected`. |

### Gate E2E en `:8001`

Se comprobó el puerto antes de cada arranque. Hubo dos pasadas secuenciales del
mismo servidor dev: la primera validó el HTML y se apagó (PID 22784); tras
detectar y corregir el cache-buster, la pasada final fue:

```text
python manage.py runserver 127.0.0.1:8001 --noreload
listener PID 24404
```

`GET /index/` respondió **200**, se detectaron exactamente cuatro cards y
`GET /static/style.css?v6` respondió **200** con la cuadrícula de cuatro
columnas. La DB dev conservada tenía DATAGEN #1 terminado y aprobado con
16.000/16.000 posiciones (8 chunks), DATAGEN #4 con 4/4 (1 chunk), y el SPRT
#2 terminado manualmente con 4 partidas pero LLR sin resolver. Ninguna máquina
tenía heartbeat en los últimos 3 minutos y ningún `Result` se había actualizado
en los últimos 10; por eso los valores reales y plausibles fueron Cores `0`,
Nodes/sec `0`, Games/min `0` y Time remaining `—`.

HTML relevante devuelto por el servidor:

```html
<div aria-label="OpenBench capacity metrics" class="index-metrics">
<section class="index-metric" data-metric="cores" title="Worker threads from machines updated in the last 3 minutes.">
<div class="index-metric-title">Cores</div>
<div class="index-metric-value">0</div>
</section>
<section class="index-metric" data-metric="nodes-sec" title="Sum of clientSubmitNPS across live worker threads (reported per-thread speed multiplied by concurrency).">
<div class="index-metric-title">Nodes/sec</div>
<div class="index-metric-value">0</div>
</section>
<section class="index-metric" data-metric="games-min" title="10-minute rolling delta of Result.games sampled in memory; the database has only each row's latest updated timestamp.">
<div class="index-metric-title">Games/min</div>
<div class="index-metric-value">0</div>
</section>
<section class="index-metric" data-metric="time-remaining" title="ESTIMATE: generic DATAGEN uses remaining positions divided by each test's measured positions/sec; gameplay uses remaining expected games divided by the 10-minute games/min rate. Active SPRTs assume 15000.0 total games (resolved-history median; 15000 fallback). No machine heartbeat was seen in the last 3 minutes.">
<div class="index-metric-title">Time remaining</div>
<div class="index-metric-value">—</div>
</section>
</div>
```

Al terminar la pasada final se validó que el listener seguía siendo exactamente
el PID 24404 y que su command line seguía apuntando a
`manage.py runserver 127.0.0.1:8001
--noreload`; se detuvo sólo ese PID. Estado final: proceso detenido y
`PORT_8001_LISTENER_AFTER=none`. Producción `:8000` no se inspeccionó ni tocó.
