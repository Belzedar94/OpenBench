# Operación de la instancia permanente

- **URL única para todo**: https://belzedar.duckdns.org (HTTPS, cert auto-renovado). El server local de la torre (127.0.0.1:8000) está RETIRADO — no lo levantes; su db es solo backup.
- **Worker AtomicDB de la torre**: el propietario lo mantiene deliberadamente
  en **T24**. Un deploy, una comprobación o una migración no autorizan a
  pararlo, reiniciarlo ni cambiar sus threads. Si un cutover offline exige una
  pausa, hay que obtener autorización explícita, registrar el comando/PID y
  relanzarlo exactamente en T24 al terminar, verificando que reconecta.
- **Crear tests**: vía web (login → Create Test / Create Tune / Create Datagen) o POST a `/scripts/` con action=CREATE_TEST como siempre, contra la URL nueva.
- **Datagen distribuido**: contrato del motor y checklist de adopción por variante (Atomic incluido) en `openbench-spell/docs/datagen-mode.md`. El binario del motor debe exponer un comando datagen (referencia: src/datagen.cpp de Spell-Stockfish).
- **Disco del server (40GB)**: los chunks de datagen completados se descargan a la torre y se PURGAN del server tras cada merge. Datasets archivados (p.ej. Atomic #68, 5GB) viven solo en la torre.
- **Cambios en el server**: editar en el repo local openbench-spell (torre), probar en el clon dev (:8001), y desplegar delta por scp + `systemctl restart openbench` (llave SSH: scratchpad sesión 90548ab1; server /opt/openbench en 178.104.66.19).

## Regla fail-closed: producción frente a local

- Un clon local con una copia de `db.sqlite3`/`Media` no es una cola compartida ni
  una réplica operativa. No iniciar allí workers con ciencia o DATAGEN oficial.
- `:8001` se reserva exclusivamente para smoke tests rápidos, aislados y
  desechables. `:8000` local sigue retirado.
- Antes de arrancar un worker oficial, confirmar que el workload existe y tiene
  la prioridad correcta en **https://belzedar.duckdns.org**.
- Nunca copiar una cola viva y dejar que un worker local reclame sus chunks: eso
  duplica trabajo sin reflejarlo en producción.

### Incidente 2026-07-21

Se creó una instancia v41 en `:8001` desde una copia de DB/Media y se lanzó un
worker T30 para Spell→Atomic. Fue un error de destino: el Atomic local no podía
verse en la web permanente y el worker duplicaba chunks de Spell. Los procesos
locales se detuvieron; no llegaron a generar posiciones Atomic. Acción
preventiva: URL permanente obligatoria, visibilidad web previa y límite T8.

Al parar el worker se detectó además un generador Spell huérfano (descendiente
del T30 local) que seguía consumiendo CPU y unos 4,9 GB de RAM; también se
terminó. Al detener workers, verificar siempre la ausencia de sus procesos hijo.

El servicio permanente debe definir
`OPENBENCH_TRUST_X_FORWARDED_PROTO=True` y
`OPENBENCH_TRUSTED_ORIGINS=https://belzedar.duckdns.org`; nginx debe eliminar
cualquier `X-Forwarded-Proto` enviado por el cliente y escribir el suyo. No se
debe activar la confianza del proxy en procesos locales o de test.

El servicio permanente es un entorno separado. El desarrollo y la validación
de migraciones deben usar un clon, DB, árbol Media y endpoint aislados
(convencionalmente `:8001`). Nunca apuntar un worker de desarrollo a producción
ni incluir credenciales en comandos guardados en Git, logs, manifests, receipts
o archivos de estado.

## Split staged de AtomicDB en SQLite

El split separa las escrituras intensivas de AtomicDB de la base operativa de
OpenBench. Es un cutover **offline**, no una migración que deba ejecutarse
durante un deploy ordinario. Hasta completar el split autenticado, el alias
AtomicDB sigue siendo `default` y todo conserva el comportamiento legacy.

### Activación fail-closed

La ruta recomendada y única para la producción actual es
`/opt/openbench/atomicdb.sqlite3` (`BASE_DIR/atomicdb.sqlite3`), sin variable de
entorno. Un proceso nuevo selecciona el alias `atomicdb` únicamente cuando
existen la base y el sidecar
`/opt/openbench/atomicdb.sqlite3.split-receipt.json`, y el receipt valida
exactamente:

```text
schema=atomicdb.sqlite.split.v1
status=verified
destination=<realpath absoluto de atomicdb.sqlite3>
migration_sentinel=atomicdb.0013_progresssnapshot
```

El sidecar verificado es la configuración durable del split. Un fichero vacío,
un JSON truncado, una ruta diferente, un sentinel distinto o una sola de las
dos piezas hace que el proceso falle cerrado o permanezca en compatibilidad;
nunca debe abrir ni crear una SQLite para decidir el alias.

`OPENBENCH_ATOMICDB_PATH` queda reservado para un destino custom avanzado. Solo
puede configurarse **después** de que ya existan una DB y receipt válidos para
esa misma ruta, y debe llegar con idéntico valor tanto al servicio systemd como
a cada invocación de deploy. La producción actual no lo necesita; usar la ruta
por defecto evita drift entre el servicio y el shell de despliegue. Nunca se
debe apuntar la variable a una ruta vacía para “inicializarla”.

### Cutover offline

1. Fusionar y desplegar primero el código en modo compatibilidad, sin DB ni
   receipt de split. Confirmar que `ATOMICDB_DATABASE_ALIAS` continúa siendo
   `default`.
2. Reservar ventana offline y obtener autorización explícita antes de tocar el
   worker AtomicDB T24. Registrar su comando, PID y estado de conexión. No
   detener workers, DATAGEN ni procesos ajenos.
3. Detener las escrituras web de AtomicDB y, solo si fue autorizado, pausar el
   T24. Verificar que no quedan leases/submits en tránsito.
4. Crear fuera del checkout un backup consistente de la SQLite `default`,
   registrar tamaño y SHA-256, y conservar también commit/configuración. No
   seguir si el backup o `PRAGMA integrity_check` falla.
5. Exigir que tanto `/opt/openbench/atomicdb.sqlite3` como su
   `.split-receipt.json` estén ausentes. El comando raw no usa ni activa el
   alias destino:

   ```bash
   ./.venv/bin/python manage.py split_atomicdb_sqlite \
     --destination /opt/openbench/atomicdb.sqlite3
   ```

   El comando debe abortar si cualquiera de las dos rutas ya existe. Publica el
   receipt verificado al final; un fallo previo no se convierte en activación.
6. En un proceso nuevo, autenticar el destino permitiendo únicamente
   migraciones de código aún pendientes, migrar explícitamente el app en su
   alias y repetir la verificación estricta:

   ```bash
   ./.venv/bin/python manage.py verify_atomicdb_database \
     --allow-pending-migrations
   ./.venv/bin/python manage.py migrate atomicdb \
     --database atomicdb --no-input
   ./.venv/bin/python manage.py verify_atomicdb_database
   ```

7. Ejecutar `/opt/openbench/deploy.sh`. Si detecta el alias `atomicdb`, el
   script autentica primero el destino sin escribir; sólo después migra
   `default`, migra `atomicdb` y repite la verificación estricta antes de
   `collectstatic` y del restart. Ningún fallo debe reiniciar el servicio.
8. Verificar health-check, alias, conteos de filas representativos, integridad,
   foreign keys y una operación de lectura. Si se pausó el worker, relanzarlo
   inmediatamente con su comando exacto en T24 y confirmar que reconecta.

No borrar las tablas AtomicDB legacy de `default` durante el cutover ni durante
el periodo de observación. Son una red de rollback, no evidencia de que el
router siga escribiendo allí.

### Rollback

- Antes de aceptar nuevas escrituras en la DB separada, el rollback consiste en
  mantener todo offline, retirar el sidecar de la ruta de activación
  (conservarlo junto a la DB, no destruirlo), iniciar un proceso nuevo en
  `default` y verificarlo antes de reabrir tráfico.
- Después de cualquier escritura post-cutover, **no** basta con retirar el
  sidecar: las tablas legacy ya están atrasadas. Detener escrituras, respaldar
  ambas bases y usar un procedimiento revisado de reconciliación inversa o
  restaurar el par de backups del mismo instante.
- Nunca borrar ni truncar las tablas legacy, la DB separada o su receipt como
  parte de un rollback. Conservar hashes, timestamps y logs de verificación.
- La autorización temporal para pausar el T24 termina con la intervención:
  restaurarlo en T24 y verificar su conexión aunque el rollback sea la opción
  elegida.

## Atomic Syzygy DATAGEN v40/v41 gate

Before any production rollout:

1. Back up the development DB and Media tree, then run `python manage.py
   migrate`, `python manage.py check`, and `python manage.py makemigrations
   --check --dry-run`.
2. Run the focused server and client suites documented in
   `docs/datagen-mode.md`, including the legacy DATAGEN cases.
3. Confirm a matching Atomic worker receives a v40 lease and a wrong family,
   insufficient cardinality, or mismatched manifest receives no work.
4. Confirm upload creates a receipt, identical retry is idempotent, requeue
   clears attempt evidence, and the final manifest contains no local path.
5. Keep `syzygy_adj=DISABLED` and run both depth-7 canaries locally only after
   the engine bridge and golden tests pass. Compare `pure` and `true`
   (legacy-playing) output with the format auditor before approval.
6. For publication protocol v41, confirm creation authenticates the full
   network SHA/bytes and book text/raw hashes, duplicate campaign slots fail,
   every chunk carries a publication lease/receipt, and the completed API
   verifies and publishes its `manifest_sha256`.

Production deployment is a separate, explicitly authorized operation. It
requires an idle-window DB/Media backup, migrations through `0009`, server restart,
gradual worker-v41 restart, and a one-chunk smoke test. Do not use `--fake` for
`0007` or `0008`, do not deploy midway through an active campaign, and retain the prior
application and backup for rollback. Migration `0009` is the empty merge node
that joins the production profile-default branch with the v41 DATAGEN branch.

## Deploy y archivado (desde 2026-07-21)

- **Deploy de un comando**: `/opt/openbench` es clon git de
  github.com/Belzedar94/OpenBench (rama spell-runner). Publicar = merge a esa
  rama en GitHub + `ssh root@178.104.66.19 /opt/openbench/deploy.sh`
  (fetch+reset+pip+migraciones verificadas+restart+health-check). En modo split
  el deploy autentica una DB ya inicializada antes de migrarla y vuelve a
  verificarla antes del restart; nunca crea el destino. Copias de los scripts
  del server en `Scripts/deploy.sh` y `Scripts/archive_manifest.py`.
- **Archivado automático**: tarea de Windows "OpenBench-Archive-Pull" (cada
  hora) ejecuta `Scripts/archive_pull.py` en la torre: descarga los chunks
  COMPLETED a `F:\OpenBench\archive\datagen\<test>\`, verifica sha256 contra
  el manifiesto del server y solo entonces purga el fichero remoto
  (fail-closed). Log en `F:\OpenBench\archive\archive.log`. El disco del VPS
  (40GB) queda así acotado aunque se encolen datasets de miles de millones.
