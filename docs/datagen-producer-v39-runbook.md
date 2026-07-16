# Runbook del CAS de productores DATAGEN v39

## Invariantes

- La presencia de `{PRODUCER_SHA256}` y el hash del contrato se congelan en la
  misma transacción que crea el mapa de chunks. Editar después el comando no
  cambia la autorización de una campaña activa.
- El worker copia el ejecutable desde un descriptor regular sin symlink a un
  snapshot privado del intento, conserva `.exe` cuando corresponde, lo hashea
  durante y después de la copia, sube esos bytes y ejecuta ese mismo snapshot.
- Un `DatagenProducerBuild` reserva un artefacto para una campaña. La reserva
  sobrevive a requeue/reclaim; sólo se borra el binding local del chunk. Por
  tanto upload -> error -> requeue no renueva ninguna cuota.
- `UNVERIFIED`, `STAGING`, `AVAILABLE` y `CORRUPT` son estados explícitos. Sólo `AVAILABLE`
  puede completar un chunk o descargarse. La promoción usa rename atómico y
  fsync del archivo (y del directorio en POSIX).
- Las cuotas se serializan mediante filas bloqueables: global física del CAS,
  lógica por propietario y por campaña. Los defaults son 4096/256 GiB global,
  1024/64 GiB por propietario y 256/16 GiB por campaña; cada binario tiene un
  máximo de 2 GiB.
- Manifest y descarga del productor requieren un `Profile.enabled`. Se admite
  sesión web o HTTP Basic sobre HTTPS. La petición valida estado/tamaño desde
  metadata cacheada y transmite el mismo descriptor abierto; el scrub periódico
  hace el costoso SHA-256 fuera del request path.

## Reverse proxy

Django rechaza por `Content-Length` antes de parsear multipart por encima de
`2 GiB + 8 MiB` de overhead. El proxy debe imponer el mismo límite también a
requests chunked antes de que alcancen WSGI. Ejemplo nginx:

```nginx
location = /clientSubmitDatagenProducer/ {
    client_max_body_size 2056m;
    client_body_timeout 30m;
    proxy_request_buffering on;
    proxy_pass http://openbench;
}
```

HTTP Basic se rechaza si Django no considera segura la petición. Si el TLS
termina en el proxy, configure `OPENBENCH_TRUST_X_FORWARDED_PROTO=True` y haga
que el proxy elimine cualquier `X-Forwarded-Proto` entrante antes de fijarlo a
`https`; no exponga directamente el puerto de la aplicación. Mantenga límites
separados para chunks de datos, que pueden tener otra política de tamaño.

## Reconciliación, scrub y retención

Ensayo sin mutaciones:

```text
python manage.py reconcile_datagen_producers --dry-run --scrub \
  --staging-max-age-hours=24 --retention-days=30
```

Ejecución periódica recomendada:

```text
python manage.py reconcile_datagen_producers --scrub \
  --staging-max-age-hours=24 --retention-days=30
```

El comando es idempotente. No toca `STAGING` reciente, reanuda un staging
antiguo válido, descubre canonical corrupto/ausente, elimina `.staging`
huérfano tras el TTL, retira reservas sólo de campañas finalizadas/eliminadas
que superaron la retención, elimina CAS sin referencias y reconstruye todos los
contadores desde las FK. Ejecútelo diariamente; para CAS grandes puede separar
una pasada frecuente sin `--scrub` y una pasada de SHA completa semanal.

Si el comando informa `CORRUPT` con referencias activas, no borre la fila:
vuelva a publicar el hash exacto desde un lease autenticado o restaure el blob
desde backup y repita `--scrub`.

## Migración v38 -> v39

1. Detener de forma coordinada servidor y workers; v38 se rechaza expresamente
   una vez configurado `client_version=39`.
2. Respaldar DB y `Media/datagen-producers` y verificar restauración.
3. Desplegar servidor+cliente v39 y dependencias, sin usar `--fake` para `0006`.
4. Ejecutar `python manage.py migrate` y después
   `python manage.py makemigrations --check --dry-run` y `python manage.py check`.
5. Ejecutar el reconciler en `--dry-run --scrub`; revisar bytes, estados y
   reservas del backfill. Después ejecutar la pasada real.
6. Arrancar servidor, probar autenticación del manifest/producer y arrancar
   workers v39 gradualmente. No mezclar workers v38 y v39.

El backfill congela contratos existentes, marca blobs v39 previos como
`UNVERIFIED` y materializa campaign-build/FK desde los bindings de chunks. El
scrub posterior valida los bytes, los promueve a `AVAILABLE` y corrige contadores.

## PostgreSQL

Defina `OPENBENCH_POSTGRES_DB`, `OPENBENCH_POSTGRES_USER`,
`OPENBENCH_POSTGRES_PASSWORD`, `OPENBENCH_POSTGRES_HOST` y
`OPENBENCH_POSTGRES_PORT`. El workflow `datagen-postgres.yml` levanta PostgreSQL
16 y ejecuta migraciones, carreras de claim/upload, suite Django y worker. Las
reservas usan `select_for_update`; SQLite conserva el CAS condicional y retries
acotados para desarrollo/instalaciones existentes.

## Diagnóstico rápido

- `upload_required=true`: el CAS no está `AVAILABLE`; haga el upload completo.
- Estado `UNVERIFIED`: esperado tras el backfill; ejecute `--scrub` antes de
  reabrir la publicación/descarga.
- HTTP 409 de cuota: revise filas global/owner/Test y ejecute reconciler
  `--dry-run`; no borre blobs manualmente.
- HTTP 409 stale lease: esperado tras reclaim; no crea LogEvent ni archivo.
- Estado `STAGING` > TTL: ejecute reconciler; puede promover el staging o el
  canonical ya durable.
- Estado `CORRUPT`: restaure/republique y haga scrub; ningún chunk nuevo podrá
  completar con ese productor hasta volver a `AVAILABLE`.
