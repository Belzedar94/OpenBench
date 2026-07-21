# Operación de la instancia permanente

- **URL única para todo**: https://belzedar.duckdns.org (HTTPS, cert auto-renovado). El server local de la torre (127.0.0.1:8000) está RETIRADO — no lo levantes; su db es solo backup.
- **Workers** (cualquier motor): `python client.py -U <usuario> -P <password> -S https://belzedar.duckdns.org -T 8 -N 1` desde openbench-spell/Client. En esta torre, **8 threads es el máximo operativo** salvo orden posterior explícita del propietario. Las cuentas/máquinas existentes migraron intactas.
- **Pausa temporal (2026-07-21)**: todos los workers deben permanecer parados.
  Se pueden publicar/configurar workloads en la web oficial, pero no arrancar
  clientes hasta nueva autorización explícita del propietario.
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
  (fetch+reset+pip+migrate+restart+health-check). Copias de los scripts del
  server en `Scripts/deploy.sh` y `Scripts/archive_manifest.py`.
- **Archivado automático**: tarea de Windows "OpenBench-Archive-Pull" (cada
  hora) ejecuta `Scripts/archive_pull.py` en la torre: descarga los chunks
  COMPLETED a `F:\OpenBench\archive\datagen\<test>\`, verifica sha256 contra
  el manifiesto del server y solo entonces purga el fichero remoto
  (fail-closed). Log en `F:\OpenBench\archive\archive.log`. El disco del VPS
  (40GB) queda así acotado aunque se encolen datasets de miles de millones.
