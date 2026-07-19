# DATAGEN distribuido

> Worker protocol v39 reloads hot-updated Client dependencies before running
> a workload and binds persisted machine IDs to their authenticated owner. This
> prevents mixed-version book helpers and cross-account lease collisions.
> Disabling a worker profile also revokes its existing session immediately and
> asks an active workload to stop at its next heartbeat. It additionally
> supports opt-in content-addressed producer evidence for publication-grade
> datasets.

> Worker protocol v40 adds an opt-in authenticated Atomic-tablebase
> environment contract. It freezes the corpus family, exact manifest,
> cardinality limit, and byte-exact teacher mode at campaign creation; freezes
> the assigned worker capability in every chunk lease; and stores a
> server-generated receipt that binds that lease to the uploaded bytes. Worker
> filesystem paths are never sent to or persisted by the server.

Este fork añade un workload `DATAGEN` genérico. OpenBench distribuye trabajo,
verifica el artefacto recibido como un blob opaco y conserva cada chunk. No
conoce el formato de entrenamiento ni concatena archivos; esas responsabilidades
pertenecen al proyecto del motor.

## Contrato del motor

El creador del test proporciona una plantilla UCI de una sola línea. Se permiten
estos placeholders:

- `{SEED}`: `datagen_base_seed + chunk_idx`.
- `{COUNT}`: posiciones de este chunk. El último puede ser menor.
- `{OUT}`: ruta local única donde el motor debe escribir el archivo final.
- `{THREADS}`: concurrencia `-T` del cliente asignado.
- `{BOOK}`: ruta local del libro, o `NONE` cuando el test no usa libro.
- `{BOOK_SHA256}`: SHA-256 de los bytes exactos del libro extraído, o `NONE`.
  OpenBench conserva además su SHA histórico normalizado como texto; el cliente
  verifica ambas identidades antes de iniciar el generador.
- `{NETWORK}`: ruta local de la red dev ya descargada y verificada, o `NONE`
  cuando el workload no tiene red.
- `{PRODUCER_SHA256}`: SHA-256 de los bytes exactos del ejecutable generador.
  Su presencia activa el protocolo de evidencia v39: el worker sube primero el
  binario y el servidor recalcula su identidad, lo conserva en CAS y liga hash,
  tamaño y commit dev al lease antes de ejecutar el comando.
- `{SYZYGY}`: ruta local del corpus Atomic autenticado. Solo se sustituye en el
  worker y nunca forma parte del lease, receipt o manifest del servidor.
- `{SYZYGY_MANIFEST_SHA256}`: pin SHA-256 del inventario Atomic autenticado.
- `{SYZYGY_MAX}`: limite cardinal congelado del workload (por ejemplo `6`).
- `{TEACHER_MODE}`: modo explicito y byte-exact del generador. Los unicos
  valores permitidos son `pure` y `true`; `true` conserva el comportamiento
  legacy-playing. No existe un default semantico del servidor.

`SEED`, `COUNT`, `OUT` y `THREADS` son obligatorios. `BOOK`, `BOOK_SHA256`,
`NETWORK` y `PRODUCER_SHA256` son opcionales. Los tres placeholders Syzygy se
usan juntos o no se usa ninguno. Cuando se usan, `{TEACHER_MODE}` tambien es
obligatorio, `syzygy_wdl` debe ser un limite explicito `3-MAN`--`6-MAN`, el
motor debe declarar la familia `atomic` y un manifest pin, y
`syzygy_adj=DISABLED` se fuerza siempre. No se admiten placeholders desconocidos,
conversiones, formatos, saltos de línea, NUL ni plantillas mayores de 4096
caracteres. `NETWORK` no modifica el mecanismo existente que pasa `EVALFILE`
durante el build.

El cliente escribe por stdin:

```text
<comando ya sustituido>
quit
```

El contrato de éxito es deliberadamente pequeño: el proceso termina con código
cero y `{OUT}` existe como archivo. El motor puede crear shards internos, pero
debe producir el archivo merged final antes de terminar. El cliente comprime
ese archivo con bzip2 y lo sube. Un código no-cero o la ausencia de `{OUT}` son
fallos deterministas del motor: se reportan, liberan el chunk y ponen este
workload en la blacklist local del cliente. Descargas y fallos de setup que no
sean errores explícitos de configuración, además de compresión, upload y sus
reportes, se tratan como transitorios: el chunk se reencola sin bloquear todo el
workload. Los errores de configuración, build, artefacto ausente y bench siguen
siendo deterministas. Si tampoco se puede notificar al servidor, el lease de
cinco minutos sigue siendo la red de seguridad.

Cuando la plantilla contiene `{PRODUCER_SHA256}`, el contrato se endurece antes
del launch. El worker calcula el hash sobre un handle estable del ejecutable y
primero intenta ligar sólo sus metadatos en
`/clientSubmitDatagenProducer/`. Si el CAS no contiene esos bytes, el servidor
responde `upload_required=true`; sólo entonces el worker sube el binario y el
servidor vuelve a calcular hash y tamaño. El blob se almacena por contenido bajo
`Media/datagen-producers/sha256/<prefijo>/<sha256>`; el chunk conserva además el
commit dev exacto. Reintentar los mismos bytes o reutilizarlos desde otro chunk
es idempotente, pero un segundo productor para el mismo lease se rechaza.
Distintas arquitecturas o toolchains pueden aportar un build-set de productores
para la misma campaña: todos deben corresponder al mismo commit, con un máximo
de 256 binarios únicos, 2 GiB por binario y 16 GiB agregados. Reencolar el chunk
borra su binding de intento, no el blob CAS reutilizable. Heartbeats, errores,
binding del productor y publicación final repiten el número de intento del
lease; una respuesta atrasada no puede afectar a un reclaim de la misma máquina.
El upload final del chunk debe repetir exactamente su identidad de productor o
falla cerrado. El canal CAS nunca sustituye el canal, cuotas ni formato de los
chunks.

### Contrato de entorno Atomic v40

La ruta Syzygy DATAGEN es opt-in y falla cerrada en cuatro puntos:

1. Creacion: la plantilla debe contener el grupo Syzygy completo y
   `{TEACHER_MODE}`. OpenBench congela familia `atomic`, limite N-MAN, pin del
   inventario y modo `pure|true` en un hash de contrato.
2. Scheduling: solo un worker que anuncia el mismo pin y al menos ese limite
   puede reclamar un chunk. Las tablas ortodoxas no satisfacen la familia
   Atomic.
3. Lease: al reclamar, el servidor congela el capability autenticado del
   worker, incluyendo su maximo anunciado, en un documento hasheado. Cambiar
   despues `Machine.info` no cambia la autorizacion de ese intento.
4. Upload: el worker repite la identidad congelada, nunca la ruta local. El
   servidor la compara con el lease antes de hashear el archivo y crea un
   receipt que liga contrato, lease, test/chunk/attempt/machine, identidad del
   corpus, teacher mode, productor opcional y SHA-256/bytes del resultado.

Requeue o restart elimina lease y receipt del intento abandonado. Un retry
identico del upload completado es idempotente; evidencia distinta se rechaza.
El manifest final rehashea todos los leases y receipts antes de publicarlos.
La plantilla generica anterior, sin el grupo Syzygy, conserva exactamente el
flujo legacy y deja vacios estos campos.

Antes de ejecutar el comando, el cliente descarga/compila únicamente la rama
dev del motor y comprueba su bench una sola vez, independientemente de
`{THREADS}`. Ese NPS es sólo informativo: DATAGEN no escala trabajo ni parámetros
con él. Los workloads de juego conservan sus benches por hilo y su escalado
normal. El formato del motor, la variante y el contenido del libro no están
codificados en los modelos ni en las vistas.

Los builds públicos reciben siempre `GIT_SHA_FULL=<sha>` para que el binario
pueda conservar procedencia exacta aunque el archive de GitHub no incluya
`.git`. Un DATAGEN genérico recibe además `OPENBENCH_DATAGEN=1`; el Makefile
puede usar esa variable para seleccionar su objetivo generador. Las cachés de
binarios de juego y generación usan nombres distintos, de modo que nunca se
reutiliza accidentalmente un ejecutable público del rol equivocado. DATAGEN
genérico rechaza motores privados tanto al crear el workload como en el cliente:
el metadata de artifacts privado actual no declara el rol play/generator y no
es seguro adivinarlo. Los Makefiles que ignoran las variables mantienen el
comportamiento anterior para builds públicos.

## Creación y reparto

En `/newTest/`, seleccionar `DATAGEN` y completar:

- motor, repositorio, rama y bench dev;
- plantilla de comando;
- número total de posiciones;
- posiciones por chunk;
- semilla base;
- libro o `NONE`;
- para Atomic Syzygy: limite N-MAN y teacher mode explicito `pure|true`;
- prioridad y throughput.

También se mantiene `/newDatagen/` como acceso directo al mismo modo. Los
presets son datos del motor en `Engines/<Motor>.json`, bajo
`datagen_presets`; añadir otro motor o variante no requiere tocar Python.

El servidor acepta como máximo 100.000 chunks por workload (el mismo techo que
el manifiesto Atomic BIN V2), valida el límite antes de crear filas y las
inserta en lotes acotados. Después crea el mapa completo al guardar el test.
Cada fila tiene índice, count real, estado, intentos, propietario actual,
SHA-256, bytes y fechas. El lease dura cinco minutos y cada heartbeat lo
renueva. Si el cliente desaparece, un lease vencido vuelve a ser asignable. Si
el servidor indica que el lease ya no pertenece al cliente, este detiene sólo
su proceso DATAGEN y no sube el resultado obsoleto.

Tras un error, el servidor devuelve el chunk a `PENDING`. Sólo los fallos
deterministas de build, bench o generador ponen ese workload en una blacklist
local hasta reiniciarse para evitar un bucle caliente. Los fallos transitorios
de empaquetado/transporte no lo hacen: cualquier cliente puede recoger el chunk
reencolado. `attempts` permite auditar las reasignaciones.

Dimensionar `positions_per_chunk` para unos 20–40 minutos en el hardware típico.
Chunks mucho más cortos amplifican builds, benches y uploads; chunks mucho más
largos aumentan el trabajo perdido ante un corte. El heartbeat continúa durante
descarga, build, bench, generación y upload.

En hosts compartidos se puede limitar el paralelismo de compilación sin cambiar
el default histórico:

```powershell
$env:OPENBENCH_BUILD_JOBS = "8"
python client.py -U <worker> -P <password> -S http://127.0.0.1:8001 -T 2 -N 1
```

## Upload, integridad y descarga

`POST /clientSubmitDatagen/` recibe el chunk bzip2. El servidor vuelve a calcular
SHA-256 y tamaño sobre los bytes comprimidos; no confía en los valores enviados
por el cliente. El nombre canónico es:

```text
Media/datagen/<test_id>/chunk_<idx>.bz2
```

El upload se escribe primero con un nombre de staging único dentro de
`Media/datagen`. Esa copia potencialmente grande ocurre fuera de la transacción;
después un compare-and-swap del lease, un `rename` atómico en el mismo volumen y
los contadores `games/completed_chunks` se confirman juntos. Así SQLite no
mantiene su lock global durante la copia y dos chunks simultáneos no pierden
progreso. La migración de estos contadores hace backfill de campañas existentes.

Una repetición con contenido idéntico es idempotente. Un segundo contenido para
un chunk ya completado se rechaza. El test pasa y finaliza sólo cuando todos los
chunks están `COMPLETED`. La página del test muestra chunks y progreso; cada
archivo se descarga por `GET /api/datagen/<test_id>/<chunk_idx>/`.

Una campaña ya completa expone su mapa inmutable en
`GET /api/datagen/<test_id>/`. Cada entrada incluye semilla, posiciones, hash y
tamaño del chunk y, cuando se solicitó evidencia, hash/tamaño/commit del
productor. Los ejecutables se descargan por
`GET /api/datagen-producer/<sha256>/`. Estos metadatos son transporte: el
servidor rehashea cada productor contra su identidad CAS antes de autorizar un
chunk, publicar el manifest o servir la descarga. El manifest hace un solo hash
por build distinto y la descarga reutiliza el mismo descriptor verificado. El
publicador del formato sigue obligado a descargar y reautenticar todos los bytes
y a aplicar sus propios trust pins.

En una campaña Atomic Syzygy v40, el mismo manifest incluye el contrato de
entorno y, por chunk, el lease y receipt canonicos con sus SHA-256. Esos
documentos contienen identidad de corpus, cardinalidad y teacher mode, pero
nunca la ruta local del worker.

La validación universal termina en SHA-256 y bytes. Descompresión, validación de
registros, combinación de cabeceras, deduplicación y auditoría quedan fuera de
OpenBench. Por ejemplo, un proyecto Spell puede concatenar offline sus registros
run7 y ejecutar `audit_run7.py`; un proyecto Atomic puede usar otro formato y
otro auditor sin cambiar el servidor.

## Operación del CAS v39

La operación y recuperación del CAS de productores v39 (cuotas, estados,
autenticación, límites del proxy, scrub/GC, migración y rollback) está descrita
en `docs/datagen-producer-v39-runbook.md`. Los manifiestos y ejecutables de
productor requieren siempre un usuario habilitado, incluso en una instancia
pública; los clientes CLI usan HTTP Basic exclusivamente sobre TLS.

## Compatibilidad y pruebas

Un workload histórico con `test_mode=DATAGEN` pero sin `datagen_command` conserva
el flujo anterior. SPRT, GAMES y SPSA siguen usando su scheduler, resultados,
PGN y cutechess sin entrar en ninguna ruta genérica DATAGEN.

Comprobaciones locales mínimas:

```powershell
python manage.py migrate
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test OpenBench.tests.test_datagen_mode OpenBench.tests.test_atomic_syzygy_support
python manage.py test UnitTests.test_datagen_worker UnitTests.test_atomic_onboarding
```

## Paso a producción

La instancia actual es anterior al historial explícito de migraciones de la app
`OpenBench`. Hacer primero un ensayo completo sobre una copia de `db.sqlite3` y
de `Media/`. En una ventana coordinada:

1. Publicar en el fork una ref de cliente que contenga la versión 40; verificar
   que el zip de auto-update incluye el worker DATAGEN.
2. Parar de forma ordenada el servidor y los workers de producción y respaldar
   DB y Media. No desplegar a mitad de workloads activos.
3. Verificar que el esquema existente coincide con el snapshot pre-DATAGEN.
4. Marcar sólo el baseline ya existente con
   `python manage.py migrate OpenBench 0001 --fake`.
5. Aplicar todas las migraciones reales hasta `0007` con
   `python manage.py migrate`. `0006` congela contratos, crea reservas/cuotas
   y hace backfill de evidencia de productor ya existente; `0007` añade el
   contrato, lease y receipt de entorno v40.
6. Crear/verificar permisos de `Media/datagen`, arrancar servidor, ejecutar
   `check`, probar login, un download y un DATAGEN de un chunk.
7. Arrancar clientes versión 40 de forma gradual y vigilar logs, disco y leases.

No usar `--fake` para `0002`–`0007`: crean el esquema DATAGEN, CAS y sus
relaciones de integridad. Para upstream hacia sscg13 hay que rebasar estos commits sobre su
HEAD, resolver posibles diferencias de modelos/migraciones y enviar servidor,
cliente, tests y documentación juntos. Conviene acordar además política de
retención/cuotas para blobs antes de abrir DATAGEN a una flota grande.

Los presets `Atomic Syzygy depth-7 pure canary` y
`Atomic Syzygy depth-7 true canary` son deliberadamente independientes del
preset legacy depth-6. No crear ni aprobar esos workloads en produccion hasta
que el bridge del motor acepte los cuatro argumentos v40, pasen sus golden
tests y una prueba A/B local valide ambos teacher modes y el formato de salida.

## Adopción por otra variante (checklist para Atomic y futuras)

El server no sabe nada de tu formato: adoptar el modo son tres pasos del lado
del proyecto de cada motor.

1. **Comando datagen in-engine** que cumpla el contrato de arriba: invocable
   por stdin UCI en una línea, acepta al menos semilla/count/salida/hilos (los
   nombres de flags son tuyos — la plantilla los mapea), y al terminar deja UN
   archivo final en la ruta de salida y sale con código cero. Referencia
   completa: `src/datagen.cpp` de Spell-Stockfish (multihilo con shards por
   hilo + merge final, filtros al escribir, sidecar de metadata, `--resume`).
   Si el generador no es el objetivo por defecto del motor, haz que el Makefile
   seleccione ese objetivo cuando recibe `OPENBENCH_DATAGEN=1`. Usa
   `GIT_SHA_FULL` para rellenar el commit de procedencia y `{NETWORK}` en la
   plantilla cuando el comando necesite la ruta de la red en runtime. Para un
   dataset publicable, incluye `{PRODUCER_SHA256}` y escríbelo dentro del
   manifiesto de chunk; OpenBench no permite iniciar el generador hasta haber
   autenticado y ligado esos bytes al lease.
2. **Formato y auditoría propios**: decide tu registro binario y escribe tu
   merge/auditoría OFFLINE (los chunks se descargan de `Media/datagen/<test>/`;
   son bzip2 del archivo que tu motor escribió). Referencia:
   `tools/spellnnue-pytorch/run7.py` (formato de 44 B con round-trip motor↔
   python) y `audit_run7.py` (informe de distribución con umbrales).
3. **Crear el test** en `/newDatagen/`: motor+rama+bench (el cliente compila y
   verifica el bench como en cualquier SPRT), plantilla con placeholders,
   count total, posiciones por chunk (dimensiona para ~20-40 min por chunk en
   un worker típico), semilla base y libro si aplica. La reproducibilidad por
   chunk (seed = base + idx) es gratis; consérvala.

Consejo operativo de la primera producción (Spell, test #66): el bench del
motor a nodos bajos NO predice el ritmo de escritura — mide posiciones/s con
un piloto local de tu datagen antes de dimensionar chunks.
