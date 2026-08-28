# AGENTS.md — Torre de control OpenBench (guía para agentes)

Guía operativa para cualquier agente de IA (o sesión futura) que use esta instancia de
OpenBench. It currently serves **six engines**: Spell-Stockfish, Atomic-Stockfish,
Alice-Stockfish, Horde-Stockfish, Crazyhouse-Stockfish and
Fairy-Stockfish-Atomic-Baseline (frozen baseline).
Escrita el 2026-07-12; revisión mayor 2026-08-29. Si algo de aquí contradice el código,
gana el código — y actualiza este documento.

## 0. Estado operativo vigente (2026-08-12)

- La única instancia oficial es **https://belzedar.duckdns.org**, desplegada en el
  VPS (`/opt/openbench`, servicio systemd `openbench`, nginx delante, PostgreSQL
  como base de datos; la misma caja sirve AtomicDB). Todo test, DATAGEN, worker y
  cambio de prioridad debe quedar visible en esa web ANTES de arrancar nada.
- El server local en el 5950X es SOLO para pruebas desechables (`:8000`/`:8001`);
  nunca reproducir la cola viva ni producción en local (incidente 2026-07-21: un
  worker local T30 contra una copia de la DB duplicó trabajo de Spell).
- Workers: autorizados y en producción (la pausa de 2026-07-21 terminó hace
  semanas). El tope histórico de 8 hilos por worker ya no es global: el hilo
  máximo por máquina lo decide el propietario caso a caso, y el cliente (build
  42+) mide el RSS real de cada motor y baja su propia concurrencia si la RAM
  no da. La máquina t24 corre con SLA del agente; PC2 es discrecional del
  propietario y no se gestiona.
- Veredictos: antes de creerse un SPRT, desglosar el score POR MÁQUINA
  (`per-machine`): un worker que crashea regala partidas al lado dev y fabrica
  falsos passed. Ya pasó; no reaprenderlo.
- Los veredictos passed/failed se publican solos en Discord (categoría por motor)
  vía el feed del VPS; ver §4b.7 — un motor sin entrada en `HOOKS` publica en el
  canal de Atomic EN SILENCIO.

## 1. Qué es esto

Fork de `sscg13/OpenBench@shatranj` (que a su vez es fork del OpenBench de AndyGrant),
extendido en la rama **`spell-runner`** para servir de instancia ÚNICA multi-proyecto:

- **Servidor Django** (SPRT, SPSA, DATAGEN, redes por SHA, libros, colas de workers) —
  corre en el VPS (`/opt/openbench`, systemd `openbench`).
- **Worker** (`Client/`) que compila el engine, corre el bench y arbitra partidas.
- **Ruteo variante→runner**: cada variante juega con el árbitro adecuado (ver §3).

Rutas:

| Cosa | Ruta |
|---|---|
| Checkout de trabajo local (agentes) | `C:\Users\djime\Documents\Chess_variants\Codex\Fairy-Stockfish organization\Openbench Project\cpuflags-fix\repo` (rama `spell-runner`) |
| Fork publicado (workers remotos) | `https://github.com/Belzedar94/OpenBench` rama `spell-runner` (default) |
| Server de producción | VPS `/opt/openbench` (deploy: ver el runbook del final de este doc) |
| Web pública | `https://belzedar.duckdns.org` |
| DB | PostgreSQL en el VPS (migrada desde SQLite; los procesos Django NO se reconectan solos tras un reinicio de PG — reiniciar servicios) |
| Motor spell (público) | `https://github.com/Belzedar94/Spell-Stockfish` (rama viva `main`) |
| Docs de despliegue/diseño | `Spell-Stockfish\docs\openbench-server-runbook.md` (alta original, jul-2026) y `docs\openbench-spell.md` (diseño del ruteo) |
| Checkout histórico | `Openbench Project\OpenBench-spell-runner` (rama snapshot; su AGENTS.md quedó superseded por ESTE) |

## 2. Arrancar y entrar (HISTÓRICO: server local de pruebas)

> Esta sección describe cómo levantar una instancia LOCAL desechable. La
> producción vive en el VPS y se opera con el runbook del final de este doc.

```powershell
cd "<fork>"
# instancia PÚBLICA endurecida (la clave vive en Config\secret.key, gitignored):
$env:OPENBENCH_SECRET_KEY = (Get-Content Config\secret.key -Raw).Trim()
$env:OPENBENCH_DEBUG = 'False'
.\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000 --noreload
# túnel web (URL nueva en cada arranque; queda en %TEMP%\cf_err.log):
cloudflared tunnel --url http://localhost:8000
```

- OJO: `runserver` está sobreescrito y arranca dos watchers (artifacts y PGN). No usar
  gunicorn/otro WSGI sin replicarlos. Con `--noreload` los watchers arrancan igual
  (verificado); sin las env vars corre en modo dev local (DEBUG on, clave de fallback).
- Los estáticos con DEBUG=False los sirve WhiteNoise (`WHITENOISE_USE_FINDERS`), porque
  el runserver custom no acepta `--insecure`.
- Web: `http://localhost:8000/` · admin: `/admin/` · tests nuevos: `/newTest/` · redes:
  `/networks/`.
- Usuario: **`belzedar`** (superuser + Profile `enabled`+`approver`). La contraseña la
  tiene el propietario; si haces automatización, pídesela o crea un usuario propio en
  `/register/` y que el propietario lo habilite en `/admin/` (sin `enabled` no puedes nada,
  sin `approver` no puedes aprobar tests ni subir redes). NO escribas contraseñas en repos.
- La config (`Config/config.json`, `Engines/*.json`, `Books/*.json`) se carga AL ARRANCAR:
  cualquier cambio requiere reiniciar el server. La validación de arranque cubre
  Engines/Books/credenciales; `verify_general_config` es un no-op por un bug upstream —
  valida `Config/config.json` a mano.

## 3. Ruteo de variantes (lo que hace especial a este fork)

`Client/worker.py` decide el árbitro por TOKEN en el NOMBRE del libro de aperturas
(`VARIANTS`, en orden; primer match gana), con fallback por nombre de engine
(`ENGINE_VARIANTS`) cuando no hay libro (DATAGEN usa `book='None'`):

| Token en el libro | Árbitro | Nota |
|---|---|---|
| `SPELL` | `uci_pair_runner.py` | runner UCI puro nuestro, salida compatible cutechess |
| `SHATRANJ` | cutechess-ob nativo | herencia del fork sscg13 |
| `ATOMIC` | cutechess-ob nativo | **Atomic-Stockfish entra gratis por aquí** |
| `CRAZYHOUSE` | contract-specific `cutechess-cli` | requires `LICHESS_CRAZYHOUSE_2026_08_12`; Windows only |
| `FRC`/`960`/`FISCHER` | cutechess-ob nativo | fischerandom |
| (ninguno) | cutechess estándar | ajedrez normal |

Protected Crazyhouse and Horde workloads also require their exact explicit
`variant_contract` on the workload, both engines and the book. A matching name
token is routing evidence only and never authorizes a protected referee.

Para Atomic: pon `ATOMIC` en el nombre del libro (p.ej. `ATOMIC_8moves.epd`) y cutechess
arbitra la variante nativamente. Si tu variante NO la conoce cutechess, ese es el caso del
`uci_pair_runner` (añade tu token a `VARIANTS`; el runner no arbitra reglas — confía en los
motores — y emite el stdout/PGN exactos que el worker parsea).

## 4. Onboarding de un engine nuevo (checklist Atomic-Stockfish)

1. **Repo público en GitHub** (el flujo público compila EN el worker; el flujo privado
   necesita artifacts de GitHub Actions y un PAT — evítalo si puedes).
2. **Contrato de build**: el worker ejecuta, con cwd = `build.path` de tu json:
   `make -j EXE=<salida> GIT_SHA_FULL=<commit> [CC=<compiler>] [EVALFILE=<red>]`.
   Un DATAGEN genérico añade `OPENBENCH_DATAGEN=1`; el Makefile debe usarlo si
   necesita seleccionar un objetivo generador distinto. Las cachés de juego y
   DATAGEN públicas están separadas. DATAGEN genérico rechaza motores privados
   porque sus artifacts no declaran el rol play/generator. El worker sigue sin
   imponer target ni ARCH/COMP.
   Tu Makefile debe producir un binario nativo optimizado con `make` a pelo.
   Spell-Stockfish lo resuelve con un shim al final de `src/Makefile`
   (`.DEFAULT_GOAL := openbench` → `build COMP=<por-OS>`, ARCH ya es native) — cópialo.
   `EVALFILE=<ruta>` llega cuando el test tiene red asignada: si tu red es formato SF,
   embébela (el Makefile de SF lo soporta); si es formato propio, decide tu mecanismo
   (spell aún lo tiene pendiente — ver §7).
3. **Contrato de bench**: el worker corre `./binario bench` (sin args; con red asignada y
   engine PRIVADO antepone `setoption name EvalFile value <red>`). Requisitos: determinista,
   < 300 s (timeout ya subido en este fork; los workloads de juego lanzan UN bench
   POR HILO del worker EN PARALELO, mientras DATAGEN genérico verifica exactamente
   uno porque no usa NPS para escalar), imprime `Nodes searched: N`. Disciplina: los commits que se testeen llevan
   `Bench: <N>` al final del mensaje (regex del server: `(?:BENCH|NODES)[ :=]+([0-9,]+)`).
   Spell: `Bench: 13456297` (red default embebida).
4. **`Engines/Atomic-Stockfish.json`** — copia `Engines/Spell-Stockfish.json` (flujo
   público: `"private": false` + `build.path/compilers/cpuflags/systems`) y ajusta `nps`
   (mídelo: es el que escala TCs entre máquinas), presets y `book_name`. Los motores con
   arranque UCI costoso pueden declarar `"cutechess_max_concurrency": 8` y
   `"cutechess_launch_stagger_ms": 1500`: el servidor divide la concurrencia total en
   copias iguales y el worker escalona esas copias. Ambos defaults son 0 (desactivado) y
   los runners custom (Spell) conservan su distribución y arranque históricos.
5. **Libro**: `.epd` con token `ATOMIC` en el nombre → zip PÚBLICO (un release de GitHub
   vale: así está hosteado el de spell) que contenga el archivo con ese nombre EXACTO →
   `Books/<nombre>.json` con `source` y `sha`. **El sha es sha256 del TEXTO del .epd
   extraído con newlines universales** (así lo computa el worker):
   `python -c "import hashlib;print(hashlib.sha256(open('X.epd').read().encode()).hexdigest())"`
   — NUNCA sustituir este `sha` histórico por el binario (con CRLF no coincide).
   Si un generador valida los bytes exactos, añade además `raw_sha` calculado con
   `'rb'`; el cliente verificará ambas identidades.
6. **`Config/config.json`**: añade tu engine a `"engines"` y tu libro a `"books"`.
   Reinicia el server.
7. **Redes**: súbelas por web (`/networks/`, engine correspondiente) o
   `python Scripts\upload_net.py -U <user> -P <pass> -S http://localhost:8000 -E <Engine> -N <nombre> -F <ruta>`.
   Se identifican por sha256; marca una default si quieres que los tests la usen por defecto.
8. **Primer test**: `/newTest/` → dev branch vs base branch del MISMO repo `source` (el
   server rechaza forks: `requests_illegal_fork`), bench declarado = el del commit, preset
   STC. Con un worker conectado, debería compilar, benchear y jugar.

## 4b. Alta consolidada de un engine (proceso REAL, probado con Horde y Alice, ago-2026)

El §4 sigue siendo el contrato técnico; esto es el orden de operaciones completo con
todo lo que se aprendió en las altas de Atomic, Horde y Alice. Plantilla JSON de
referencia: `Engines/Alice-Stockfish.json` (la más reciente: flujo público,
`worker_max_concurrency: 8`, `cutechess_max_concurrency: 2`,
`cutechess_launch_stagger_ms: 500`, presets con `base_branch` PINEADO POR SHA y
`both_bench`/`both_network`).

1. **Motor**: repo público con shim de Makefile (§4.2), bench determinista con
   `Bench: <N>` en los commits (§4.3), y si usa red propia, el patrón
   `EVALFILE=` horneado como default de EvalFile (SPELL_EVALFILE_DEFAULT).
2. **Libro**: `.epd` con el token de variante en el nombre (§3: el token decide el
   árbitro), publicado como release pública + `Books/<nombre>.json` con el sha en
   modo TEXTO (§4.5).
3. **Server**: `Engines/<Motor>.json` + entrada en `"engines"` y `"books"` de
   `Config/config.json` → deploy al VPS (runbook del final) → restart. La config se
   carga al arrancar; sin restart no existe.
4. **Red**: subirla en `/networks/` (o `Scripts/upload_net.py`) y marcarla default
   si los tests deben usarla sin decirlo. Cambiarla luego en tests vivos exige
   tocar LOS DOS lados (gotcha §6).
5. **Baseline congelado** (opcional): si el proyecto mide contra una vara externa
   (patrón `Fairy-Stockfish-Atomic-Baseline`), es un engine más con su JSON y su
   pin. Puede retirarse de `"engines"` cuando ya no se use SIN borrar su JSON
   (así está hoy `Fairy-Stockfish-Hordetest-Baseline.json`: archivo presente,
   fuera de la lista).
6. **Binarios de release** (si el motor los distribuye): la coreografía es "merge
   con pin coherente" — el zip de release, el pin del cliente y el commit del
   repo deben señalar EXACTAMENTE el mismo árbol (lección Horde+Alice, jul-2026).
7. **Discord** (SIN esto los veredictos se pierden o van al canal equivocado):
   crear la categoría del motor con `#announcements`, `#contribute`, `#general` y
   `#test-bench-<motor>` (announcements y test-bench solo-lectura), webhook del
   test-bench guardado en el VPS junto a los demás, y **una entrada en el dict
   `HOOKS` de `/root/discord_sprt_feed.py`** — el ruteo es por subcadena de
   `dev_engine` y el default es Atomic, así que un motor nuevo sin entrada
   publica sus veredictos en el canal de Atomic en silencio (pasó con Horde y
   Alice: días de veredictos en el canal ajeno). Sembrar el histórico con
   `/root/siembra_feed.py <motor> <fichero-webhook>` (en seco primero,
   `--publicar` después). Al crear canales: NUNCA resolver por nombre a secas,
   filtrar por categoría padre (hay cinco `#announcements`).
8. **Primer test / tests por script**: el camino web (`/newTest/`) se valida solo.
   El camino Django (para clonar o crear en lote): clonar un test plantilla con
   `pk=None`, crear `Engine(name=<descriptivo>, source=<zip del sha COMPLETO>,
   sha, bench)` por lado, resetear contadores/flags del clon, y **validar cada
   bench contra un build limpio local ANTES de escribirlo** (reproducir primero
   un bench ya registrado del mismo engine para validar el entorno; si tu build
   de un engine registrado no reproduce su bench, tu firma nueva tampoco vale).
9. **Después del alta**: verificar el primer veredicto en el canal Discord del
   motor (no en el de Atomic), y el primer SPRT con el desglose por máquina (§0).

## 5. Workers

```powershell
cd "<fork>\Client"
pip install -r requirements.txt   # requests, psutil, py-cpuinfo
python client.py -U <user> -P <pass> -S http://localhost:8000 -T <hilos> -N 1
```

- El worker necesita `make` y `g++` en el PATH (en Windows: MSYS2 mingw64 —
  `C:\msys64\mingw64\bin` y `C:\msys64\usr\bin`).
- `credentials.<engine>` (PAT de GitHub) SOLO para engines privados. Público: nada.
- **Convivencia en el 5950X (32 hilos)**: esta máquina la comparten los agentes de Spell y
  Atomic y a veces corre granjas de datagen o entrenamientos. Antes de conectar un worker
  gordo: mira qué corre (`Get-Process stockfish*, python*`) y reparte (regla de la casa:
  ≤24 hilos de motor en total). Los benches del worker usan -T hilos de golpe.
- **Workers REMOTOS (quickstart)** — el fork ya está publicado:
  ```
  git clone -b spell-runner https://github.com/Belzedar94/OpenBench
  cd OpenBench/Client && pip install -r requirements.txt
  python client.py -U <user> -P <pass> -S <URL-publica-del-servidor> -T <hilos> -N 1
  ```
  Requisitos de la máquina: Python 3.9+, `make` + `g++` en PATH (Linux:
  build-essential; Windows: MSYS2 mingw64). Los tests SPELL corren con
  `uci_pair_runner.py` que viaja en `Client/` (usa el mismo python del worker). El
  auto-update descarga `client_repo_url`@`client_repo_ref` y asume carpeta raíz
  `OpenBench-<ref>` → por eso el repo se llama `OpenBench`. El kill-by-name del worker
  busca `cutechess-ob(.exe)` y no alcanza al runner python: el runner se auto-protege
  (pipe muerto → sale matando motores; 3 muertes instantáneas seguidas → aborta el lote).

## 5b. Metodología de testeo (fishtest-style, decisión del propietario 2026-07-13)

Vale para TODOS los engines de la torre (Spell-Stockfish hoy, Atomic-Stockfish después).
Es el procedimiento de fishtest/sscg13; los presets viven en `Engines/<Motor>.json`
(fuente de verdad — Atomic debe copiar la estructura del de Spell-Stockfish).

1. **Flujo por idea**: rama → **SPRT STC** (`8.0+0.08`, Threads=1 Hash=32) → si pasa →
   **SPRT LTC** (`40.0+0.4`, Threads=1 Hash=128) → si pasan AMBOS → **merge a master**
   con `Bench: <N>` al final del commit. Master solo avanza así.
   **Nomenclatura (corrección del propietario)**: el título del test en la web es
   `Engine-rama_dev vs Engine-rama_base` — la rama dev SIEMPRE lleva nombre descriptivo
   de la idea (`merged-ordering`, `razor-guard`...), NUNCA master-vs-master. Para tests
   por diff de opciones, crear igualmente una rama-etiqueta (commit vacío descriptivo
   con el `Bench:` de master) y pasar el toggle en dev_options: cero rebuilds de código,
   nombre legible.
2. **Bounds**: ganancia `[1.00, 6.00]` · simplificación/no-regresión `[-5.00, 0.00]` ·
   confianza `[0.05, 0.05]`. (Subidos de [0, 5] el 2026-07-13, decisión del propietario:
   en fase low-hanging-fruit los parches neutros deben morir rápido y solo pasar
   ganancias sustanciales — ojo, son nElo: con ~3% de tablas 1 nElo ≈ 2 Elo crudo.
   Volver a bounds finos cuando el gap con el baseline se cierre.)
   Adjudicación: win `movecount=4 score=800`, draw `movenumber=40 movecount=8 score=10`.
   (Win endurecida de 3/400 el 2026-07-13: con la eval aún verde, adjudicar a -400 corta
   partidas donde el bando "perdido" aún tiene salvaciones con spells — y sesga contra
   parches que justo mejoran la búsqueda de esas salvaciones. No aplicar
   retroactivamente a tests en curso.)
3. **Cambios no-funcionales** (bench idéntico, p.ej. toggles con default = comportamiento
   actual, refactors, docs): master directo estilo "No functional change", sin SPRT.
   Truco útil: implementar N ideas como opciones UCI default-off en UN commit
   no-funcional y lanzar N SPRTs por diff de opciones (dev_options vs base_options,
   misma rama) — cero ramas, cero rebuilds por idea.
4. **SMP**: si el cambio toca multithreading, presets SMP con Threads=8
   (STC 10+0.1 Hash=64 / LTC 30+0.3 Hash=256).
5. **SPSA**: presets `SPSA STC`/`SPSA VSTC`; los parámetros se exponen vía el TUNE de SF
   (el dump CSV de arranque del motor es EXACTAMENTE el formato de `spsa_inputs`
   anteponiendo el tipo: `Nombre, int, valor, min, max, c_end, r_end`). Aplicar
   resultados redondeados como defaults = commit FUNCIONAL (bench cambia) → en rigor
   pasa por SPRT; para paquetes SPSA grandes vale un test de confirmación
   paquete-vs-anterior.
6. **Progresión y releases**: cada tramo de Elo acumulado (~+30-50), test de partidas
   FIJAS a LTC (presets `progtest`) vs la **última release** — y para Spell, también vs
   el baseline FSF congelado (vara del hito M1, cross-engine, fuera de la torre).
   Release cuando el acumulado lo justifique.
7. **Operación web**: login en `/login/`; en la página de un test los approvers tienen
   botones STOP / RESTART / DELETE / MODIFY (prioridad/throughput). STOP suelta al
   worker tras el lote en curso. Prioridad más alta = el worker lo coge antes.
8. **Medición Atomic Syzygy**: es una comparación de feature, no un gate de parche.
   Usa cuatro tests `GAMES` fijos de 2.000 partidas (STC/LTC × NNUE on/off), según
   `docs/atomic-syzygy-openbench.md`; no usa SPRT ni un protocolo especial.

## 5c. Qué hacer cuando master avanza (rebases y mantenimiento de la cola)

Un SPRT mide **dev contra la base con la que se creó**, no contra el master de
ahora. Cuando un parche pasa STC+LTC y entra en master, el resto de tests en
vuelo NO quedan invalidados por ese mero hecho.

1. **Rebasar SOLO si se solapan.** La pregunta es si el parche recién mergeado
   y el test en vuelo tocan el mismo camino de código. Si son ortogonales
   (uno va en la eval y otro en el árbitro de tablas, pongamos), el veredicto
   del test sigue siendo válido y **se deja correr**: rebasar sin motivo tira
   miles de partidas ya jugadas y no compra nada.
2. **Si se solapan, se relanza contra el master nuevo.** Dos parches que
   podan el mismo nodo pueden ganar por separado y no sumar nada juntos —
   incluso restar. Ahí el resultado viejo no dice lo que hace falta saber, que
   es si el segundo aporta ENCIMA del primero. Se para el test, se rebasa la
   rama sobre master y se relanza desde cero.
3. **El caso peor es el que ya pasó**: un test que pasó pero cuya rama es
   anterior a un merge que se solapa. Antes de mergearlo, re-verificar contra
   el master nuevo, al menos a LTC. Mergear dos ganancias medidas contra la
   misma base vieja es la forma clásica de que el acumulado no aparezca luego
   en el test de progresión.
4. **Duda razonable = rebasar.** El coste de relanzar es partidas; el coste de
   mergear una ganancia que no existe es un motor que no mejora y semanas
   buscando por qué.

**Checklist de merge** (lo que hay que mirar SIEMPRE, con los incidentes que
lo motivaron):

- **El `Bench: <N>` del commit tiene que ser el de verdad.** El servidor lo
  compara y rechaza el test con `Wrong Bench` antes de jugar una sola partida.
  Es un guarda, no una molestia: una firma mal declarada significa que el
  binario probado no es el commit registrado.
- **Fuera el andamiaje antes de mergear.** Los ganchos `TUNE` de una campaña
  SPSA exponen cada parámetro como opción UCI, y la lista de opciones UCI es
  API pública que ve cualquier GUI. En Atomic se colaron trece durante
  diecinueve commits porque viajaron de polizón en un merge de linaje, y lo
  reportó un usuario. Al retirarlos se CONGELAN los valores vivos: revertir el
  commit de los ganchos habría devuelto valores ya tuneados y debilitado el
  motor en silencio.
- **Mergear un linaje no es mergear un diff mínimo.** Cuando se trae una rama
  larga, comparar además la SUPERFICIE (salida de `uci`, flags de build,
  ficheros nuevos), no solo que los tests pasen: los tests verdes no ven una
  opción UCI que no debería existir.

**Lo que NO se toca**: las prioridades de los tests las pone el propietario.
Cortan (el nivel más alto se lleva el 100% de las máquinas que casan), así que
subirse a 300 no es un ajuste fino, es decidir en qué trabaja la flota entera.
Si el orden parece mal, se dice con los números delante y se espera.

## 6. Gotchas que ya mordieron (no reaprender por las malas)

- **sha de libro en modo texto**, no binario (§4.5).
- **Nombres de engine = UN token** (el comando entero se `split()`ea por espacios; el
  parser de resultados lee `tokens[6]`). Nada de espacios en rutas de binarios/args.
- El PGN de cada partida se escribe SIEMPRE (aunque no haya errores) — el worker lo relee
  para contar crashes por cabecera `[Termination ...]`.
- Si cutechess falla antes de crear el PGN, el worker captura stdout/stderr y exit code,
  publica un único evento agregado y no intenta abrir el PGN inexistente.
- `option.<K>=<V>` en presets/tests para opciones UCI; en SPSA los parámetros se exponen
  como opciones UCI (TUNE de SF) y el server los mueve por lotes.
- Al medir benches a mano en PowerShell: las pipes de here-string meten un BOM que mata el
  PRIMER comando UCI en silencio. Redirige stdin desde archivo o usa Python.
- Los tests solo pueden apuntar al repo registrado en `source` (no a forks personales).
- SQLite aguantó lo que aguantó: la producción YA corre PostgreSQL en el VPS. Secuela
  operativa: tras cualquier reinicio de PG (incluye `unattended-upgrade` nocturnos),
  los procesos Django de larga vida quedan con la conexión rota y NO sanan solos —
  reiniciar `openbench` y los servicios `atomicdb-*`.
- `Config/config.json` no se valida al arranque (bug upstream): revísalo a mano.
- **El score de un SPRT se audita POR MÁQUINA antes de creerlo** (un worker que
  crashea regala partidas al dev). Y un veredicto sin canal Discord correcto es un
  veredicto que nadie vio: HOOKS primero (§4b.7).
- **Las ramas base de los tests envejecen**: la cola puede llevar meses corriendo
  contra un pin viejo aunque la rama viva haya avanzado (pasó con Spell: tests
  pineados a un ancestro de `main` mientras `main` acumulaba fixes de paridad).
  Al crear un test nuevo, decidir conscientemente el pin — y si toca estrenar
  base, registrar su bench validado (§4b.8).
- **Nombre del test en el índice = `prettyDevName` (mytags.py), y se rige por
  `Engine.name`, NO por un campo "título"**. Reglas del tag: (a) si
  `dev.name == base.name` y `dev_network != base_network` → muestra EL NOMBRE
  DE LA RED (así acabaron tres tests llamándose `spell-v2-XL-HARD.nnue`,
  corrección del propietario 2026-07-24); (b) en cualquier otro caso muestra
  `dev.name`. Convención (igual que fishtest/sscg13): **el nombre descriptivo
  de la idea vive en `Engine.name` del lado dev** (`capture-see-120`,
  `spsa3-spell-params`, `datagen-run9-hard-50m`). El camino web (formulario
  "Dev Branch") lo hace solo; **el camino Django/script debe crear un
  `Engine(name=<descriptivo>, source, sha, bench)` por test** — `Engine.name`
  es etiqueta pura: el build del worker se cachea por engine+sha+red, así que
  renombrar no fuerza rebuilds ni rompe leases en curso.
- **Al repuntar la red de un test dev-vs-base por Django: cambiar LOS DOS
  LADOS** (`dev_network/dev_netname/dev.bench` Y `base_network/base_netname/
  base.bench`). Si solo cambias dev, el SPRT mide red-vs-red en vez del
  parámetro (mordió el 2026-07-24 en capture-see-120 y spsa3; lo destapó la
  corrección de nombres del propietario).
- **Ciclo de vida del worker local (3 mordiscos el 2026-07-24)**: (1) el
  entrypoint es `client.py` — `worker.py` NO tiene `__main__` y sale con
  exit 0 EN SILENCIO (hasta con --help); (2) necesita PATH con
  `C:\msys64\mingw64\bin;C:\msys64\usr\bin` (make/g++) y los TBs atomic van
  con `--atomic-syzygy "<dtz;wdl;345>" --atomic-syzygy-manifest
  remote-inventory.json` (baseline-artifacts); (3) para PARARLO: crear
  `openbench.exit` en Client/ y **verificar por PID que el python de
  client.py murió ANTES de borrar el flag** — `tasklist` trunca los cmdline
  (un check con grep dio falso "parado", se borró el flag antes de que el
  worker lo viera y siguió produciendo una hora; lo cazó el propietario en
  la lista de machines). Verificación buena:
  `Get-CimInstance Win32_Process | ? { $_.CommandLine -match 'client.py' }`.

## 7. Estado (2026-08-29)

- **Servidos**: Spell-Stockfish, Atomic-Stockfish, Alice-Stockfish, Horde-Stockfish,
  Crazyhouse-Stockfish and Fairy-Stockfish-Atomic-Baseline (plus
  `Fairy-Stockfish-Hordetest-Baseline.json`
  presente pero fuera de la lista, ver §4b.5). Cada motor con su categoría Discord
  y su entrada en `HOOKS`.
- **Infra**: VPS con PostgreSQL, nginx y systemd; deploy por el runbook del final.
  Flota mixta (SLA propio solo en t24; el resto va y viene). El cliente 42+
  auto-regula la concurrencia por RSS medido.
- **Redes**: se identifican por sha256 y una default por engine (Spell hoy:
  `run5rl_e10_l07` — es la misma red publicada como `Spell_v1.nnue` en el
  release 1.0). Los benches de commit se citan CON la red asignada del momento.
- **Histórico completo del alta original** (túnel efímero, SQLite, decisiones de
  hosting, E2E del 12-jul): `Spell-Stockfish\docs\openbench-server-runbook.md` y
  las revisiones de este archivo en git — se retiró de aquí para que el estado
  no mienta.

## 8. DATAGEN distribuido genérico (rama `datagen-mode`, 2026-07-16)

- El protocolo v38 corrige la actualización en caliente de dependencias del
  worker y liga cada `machine_id` al usuario autenticado. Un cliente con un ID
  persistido de otra cuenta recibe `Bad Machine Id`, elimina `machine.txt` y se
  registra con una identidad nueva antes de poder reclamar un chunk. Desactivar
  el perfil revoca también las sesiones de worker que ya estaban conectadas.

- El protocolo v39 añade evidencia optativa del ejecutable productor. Una
  plantilla con `{PRODUCER_SHA256}` obliga al worker a subir el binario exacto
  antes de ejecutarlo; el servidor rehashea, guarda por contenido y liga
  SHA/tamaño/commit al chunk. No desplegar v39 a mitad de una campaña v38.

- El protocolo v40 añade el entorno Atomic Syzygy opt-in. El grupo
  `{SYZYGY}`, `{SYZYGY_MANIFEST_SHA256}`, `{SYZYGY_MAX}` y `{TEACHER_MODE}`
  exige pin de inventario, limite N-MAN y teacher byte-exacto `pure|true`.
  Scheduler, lease, upload receipt y manifest fallan cerrados; nunca persistir
  la ruta local. `syzygy_adj` permanece siempre `DISABLED`. No desplegar v40 ni
  lanzar sus canaries depth-7 antes del bridge/golden/A-B local verde.

- El protocolo v41 añade el contrato opt-in de publicación. Congela identidad
  de campaña/workload/role/cohort, motor+commit+bench, red completa, libro
  text/raw, comando/count/seed, productor y entorno. Cada chunk recibe lease y
  receipt ligados al hash del contrato incluso sin Syzygy; la API final publica
  schema/version, contrato completo y self-hash. La red se hashea desde sus
  bytes registrados y el libro congela sus identidades text/raw configuradas al
  crear el workload; cualquier drift posterior falla cerrado. No convertir
  workloads legacy a v41 por backfill.

- Contrato y runbook: `docs/datagen-mode.md`.
- OpenBench trata cada chunk como blob opaco; formato, merge y auditoría son del
  proyecto del motor. No introducir reglas Spell/Atomic en modelos o vistas.
- La plantilla usa `{SEED}`, `{COUNT}`, `{OUT}`, `{THREADS}` y opcionalmente
  `{BOOK}`, `{BOOK_SHA256}`, `{NETWORK}`, `{NETWORK_SHA256}` y
  `{PRODUCER_SHA256}`. v41 exige los cuatro placeholders de libro/red. Atomic Syzygy
  usa ademas el grupo v40 completo descrito arriba. `BOOK_SHA256` es la identidad raw de
  los bytes extraídos (o la identidad histórica si el manifiesto no publica
  `raw_sha`). El proceso debe terminar con código cero dejando `{OUT}` completo.
- Dimensionar los chunks para 20–40 minutos. Los heartbeats mantienen un lease
  de cinco minutos durante build, bench, generación y upload; un fallo vuelve a
  dejar el chunk repartible.
- El SHA-256 y los bytes registrados corresponden al `.bz2` recibido y son
  recalculados por el servidor. Los archivos viven en
  `Media/datagen/<test_id>/chunk_<idx>.bz2`.
- En este host compartido, limitar builds con `OPENBENCH_BUILD_JOBS=8` y usar
  clientes dev pequeños; nunca mezclar la DB/Media/clientes de `:8000` con los
  ensayos de `:8001`.

## Deploy del server (runbook, 27-jul)

En el VPS (/opt/openbench):
```
git pull --ff-only
.venv/bin/python manage.py collectstatic --noinput   # nginx sirve /static/ desde staticfiles/ — SIN esto, CSS/JS viejos
systemctl restart openbench                          # el token de cache se calcula al arrancar
```
Si hubo ventana con estaticos viejos servidos bajo token nuevo: `touch` de los fuentes en OpenBench/static/ + collectstatic + restart (nuevo token, invalida caches envenenadas).
