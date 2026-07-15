# DATAGEN distribuido

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

`SEED`, `COUNT`, `OUT` y `THREADS` son obligatorios. `BOOK` es opcional. No se
admiten placeholders desconocidos, conversiones, formatos, saltos de línea,
NUL ni plantillas mayores de 4096 caracteres.

El cliente escribe por stdin:

```text
<comando ya sustituido>
quit
```

El contrato de éxito es deliberadamente pequeño: el proceso termina con código
cero y `{OUT}` existe como archivo. El motor puede crear shards internos, pero
debe producir el archivo merged final antes de terminar. El cliente comprime
ese archivo con bzip2 y lo sube. Un código no-cero, la ausencia de `{OUT}`, un
fallo de compresión o un fallo definitivo de upload se reportan por el flujo de
errores existente y liberan el chunk para otro cliente.

Antes de ejecutar el comando, el cliente descarga/compila únicamente la rama
dev del motor y comprueba su bench por el mecanismo normal de OpenBench. El
formato del motor, la variante y el contenido del libro no están codificados en
los modelos ni en las vistas.

## Creación y reparto

En `/newTest/`, seleccionar `DATAGEN` y completar:

- motor, repositorio, rama y bench dev;
- plantilla de comando;
- número total de posiciones;
- posiciones por chunk;
- semilla base;
- libro o `NONE`;
- prioridad y throughput.

También se mantiene `/newDatagen/` como acceso directo al mismo modo. Los
presets son datos del motor en `Engines/<Motor>.json`, bajo
`datagen_presets`; añadir otro motor o variante no requiere tocar Python.

El servidor crea el mapa completo de chunks al guardar el test. Cada fila tiene
índice, count real, estado, intentos, propietario actual, SHA-256, bytes y
fechas. El lease dura cinco minutos y cada heartbeat lo renueva. Si el cliente
desaparece, un lease vencido vuelve a ser asignable. Si el servidor indica que
el lease ya no pertenece al cliente, este detiene sólo su proceso DATAGEN y no
sube el resultado obsoleto.

Tras un error, el servidor devuelve el chunk a `PENDING`. El cliente que falló
pone ese workload en una blacklist local hasta reiniciarse para evitar un bucle
caliente; otro cliente puede recogerlo inmediatamente. `attempts` permite
auditar las reasignaciones.

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

Una repetición con contenido idéntico es idempotente. Un segundo contenido para
un chunk ya completado se rechaza. El test pasa y finaliza sólo cuando todos los
chunks están `COMPLETED`. La página del test muestra chunks y progreso; cada
archivo se descarga por `GET /api/datagen/<test_id>/<chunk_idx>/`.

La validación universal termina en SHA-256 y bytes. Descompresión, validación de
registros, combinación de cabeceras, deduplicación y auditoría quedan fuera de
OpenBench. Por ejemplo, un proyecto Spell puede concatenar offline sus registros
run7 y ejecutar `audit_run7.py`; un proyecto Atomic puede usar otro formato y
otro auditor sin cambiar el servidor.

## Compatibilidad y pruebas

Un workload histórico con `test_mode=DATAGEN` pero sin `datagen_command` conserva
el flujo anterior. SPRT, GAMES y SPSA siguen usando su scheduler, resultados,
PGN y cutechess sin entrar en ninguna ruta genérica DATAGEN.

Comprobaciones locales mínimas:

```powershell
python manage.py migrate
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test OpenBench.tests
python -m unittest discover -s UnitTests -v
```

## Paso a producción

La instancia actual es anterior al historial explícito de migraciones de la app
`OpenBench`. Hacer primero un ensayo completo sobre una copia de `db.sqlite3` y
de `Media/`. En una ventana coordinada:

1. Publicar en el fork una ref de cliente que contenga la versión 36; verificar
   que el zip de auto-update incluye el worker DATAGEN.
2. Parar de forma ordenada el servidor y los workers de producción y respaldar
   DB y Media. No desplegar a mitad de workloads activos.
3. Verificar que el esquema existente coincide con el snapshot pre-DATAGEN.
4. Marcar sólo el baseline ya existente con
   `python manage.py migrate OpenBench 0001 --fake`.
5. Aplicar la migración real con `python manage.py migrate OpenBench 0002` y
   después `python manage.py migrate`.
6. Crear/verificar permisos de `Media/datagen`, arrancar servidor, ejecutar
   `check`, probar login, un download y un DATAGEN de un chunk.
7. Arrancar clientes versión 36 de forma gradual y vigilar logs, disco y leases.

No usar `--fake` para `0002`: esa migración crea las columnas DATAGEN y la tabla
de chunks. Para upstream hacia sscg13 hay que rebasar estos commits sobre su
HEAD, resolver posibles diferencias de modelos/migraciones y enviar servidor,
cliente, tests y documentación juntos. Conviene acordar además política de
retención/cuotas para blobs antes de abrir DATAGEN a una flota grande.

## Adopción por otra variante (checklist para Atomic y futuras)

El server no sabe nada de tu formato: adoptar el modo son tres pasos del lado
del proyecto de cada motor.

1. **Comando datagen in-engine** que cumpla el contrato de arriba: invocable
   por stdin UCI en una línea, acepta al menos semilla/count/salida/hilos (los
   nombres de flags son tuyos — la plantilla los mapea), y al terminar deja UN
   archivo final en la ruta de salida y sale con código cero. Referencia
   completa: `src/datagen.cpp` de Spell-Stockfish (multihilo con shards por
   hilo + merge final, filtros al escribir, sidecar de metadata, `--resume`).
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
