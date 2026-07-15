# AGENTS.md — Torre de control OpenBench (guía para agentes)

Guía operativa para cualquier agente de IA (o sesión futura) que use esta instancia de
OpenBench: hoy sirve a **Spell-Stockfish**; el siguiente inquilino es **Atomic-Stockfish**.
Escrita el 2026-07-12. Si algo de aquí contradice el código, gana el código — y actualiza
este documento.

## 1. Qué es esto

Fork de `sscg13/OpenBench@shatranj` (que a su vez es fork del OpenBench de AndyGrant),
extendido en la rama **`spell-runner`** para servir de instancia ÚNICA multi-proyecto:

- **Servidor Django** (SPRT, SPSA, redes por SHA, libros, colas de workers) — corre LOCAL
  en esta máquina (Windows 10, Ryzen 5950X).
- **Worker** (`Client/`) que compila el engine, corre el bench y arbitra partidas.
- **Ruteo variante→runner**: cada variante juega con el árbitro adecuado (ver §3).

Rutas en esta máquina:

| Cosa | Ruta |
|---|---|
| Este fork (server+client) | `C:\Users\djime\Documents\Chess_variants\Codex\Fairy-Stockfish organization\openbench-spell` |
| Fork publicado (workers remotos) | `https://github.com/Belzedar94/OpenBench` rama `spell-runner` (default) |
| Web pública | Túnel TryCloudflare EFÍMERO sobre el servidor local — la URL vigente está en `%TEMP%\ob_public_url.txt` del host (cambia en cada arranque del túnel; hosting estable pendiente de decisión) |
| venv del servidor | `<fork>\.venv` (Django 4.2.30, scipy 1.18, whitenoise) |
| Motor spell (público) | `https://github.com/Belzedar94/Spell-Stockfish` (branch de trabajo `phase-4-strength`, base `master`) |
| Docs de despliegue/diseño | `Spell-Stockfish\docs\openbench-server-runbook.md` y `docs\openbench-spell.md` |
| DB | `<fork>\db.sqlite3` (SQLite; suficiente para pocos workers) |

## 2. Arrancar y entrar

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
| `FRC`/`960`/`FISCHER` | cutechess-ob nativo | fischerandom |
| (ninguno) | cutechess estándar | ajedrez normal |

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
   — NUNCA en binario `'rb'` (con CRLF no coincide).
6. **`Config/config.json`**: añade tu engine a `"engines"` y tu libro a `"books"`.
   Reinicia el server.
7. **Redes**: súbelas por web (`/networks/`, engine correspondiente) o
   `python Scripts\upload_net.py -U <user> -P <pass> -S http://localhost:8000 -E <Engine> -N <nombre> -F <ruta>`.
   Se identifican por sha256; marca una default si quieres que los tests la usen por defecto.
8. **Primer test**: `/newTest/` → dev branch vs base branch del MISMO repo `source` (el
   server rechaza forks: `requests_illegal_fork`), bench declarado = el del commit, preset
   STC. Con un worker conectado, debería compilar, benchear y jugar.

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
- SQLite aguanta pocos workers reportando; si la flota crece → PostgreSQL.
- `Config/config.json` no se valida al arranque (bug upstream): revísalo a mano.

## 7. Estado y pendientes (2026-07-13)

- Hecho: server local operativo; Spell-Stockfish registrado (flujo público); red run5rl
  subida; libro `spell_openings.epd` publicado como release y manifestado; shim de Makefile
  en el repo del motor; `uci_pair_runner` integrado y endurecido (verificación adversarial).
- Hecho también (2026-07-12, tarde): fork publicado en `Belzedar94/OpenBench@spell-runner`
  (única rama, default; zipball verificado); servidor endurecido (SECRET_KEY/DEBUG por
  env + WhiteNoise) y expuesto vía túnel TryCloudflare.
- Hosting web: decisión del propietario (2026-07-12) = **quedarse con el túnel efímero
  por ahora**; hosting estable "ya veremos" (candidatos evaluados: VPS propio, Fly.io,
  túnel Cloudflare con dominio). **Vercel se evaluó y se DESCARTÓ**: serverless no encaja
  (sin disco persistente para SQLite/redes, límite de subida 4,5MB vs redes de 101MB, sin
  procesos residentes para los watchers) — no re-proponerlo.
- **E2E VERIFICADO** (2026-07-12 noche): test #1 (GAMES, mismo branch ambos lados) corrió
  el ciclo completo sin intervención — worker registrado → zipball del repo público →
  build nativo por el shim → gate de bench → libro del release → 40 partidas de spell
  arbitradas por uci_pair_runner → 19-20-1 (Elo ~0, como debe leer un smoke). La torre
  está OPERATIVA para SPRT/GAMES netless y SPSA.
- Tests CON red asignada: RESUELTO — el motor hornea `EVALFILE=` como default de la
  opción EvalFile (SPELL_EVALFILE_DEFAULT; bench con red = 11477541 para run5rl). Los
  engines que copien el patrón deben citar ese bench en sus commits cuando asignen red.
- Hecho: Atomic-Stockfish y el baseline Fairy congelado estan registrados con sus
  benches, red, libros y pin del corpus Atomic. Los cuatro presets Syzygy son tests
  `GAMES` de 2.000 partidas (STC/LTC por NNUE/clasico), sin LOS ni SPRT.
- El arranque Atomic usa copias de concurrencia 8 escalonadas 1,5 s. En la medición
  local de 48 handshakes reales (3 grupos de 16 procesos), la mediana fue 1,493 s,
  p95 2,179 s y máximo 2,462 s; la ráfaga única anterior rozaba 5 s.
- Pendiente: desplegar este onboarding, subir la red Atomic y ejecutar los cuatro
  workloads Syzygy; migrar a PostgreSQL si la flota crece.
- Histórico de decisiones y erratas verificadas: `Spell-Stockfish\docs\openbench-server-runbook.md`
  (despliegue) y `Spell-Stockfish\docs\openbench-spell.md` (diseño del ruteo).

## 8. DATAGEN distribuido genérico (rama `datagen-mode`, 2026-07-15)

- Contrato y runbook: `docs/datagen-mode.md`.
- OpenBench trata cada chunk como blob opaco; formato, merge y auditoría son del
  proyecto del motor. No introducir reglas Spell/Atomic en modelos o vistas.
- La plantilla usa `{SEED}`, `{COUNT}`, `{OUT}`, `{THREADS}` y opcionalmente
  `{BOOK}`. El proceso debe terminar con código cero dejando `{OUT}` completo.
- Dimensionar los chunks para 20–40 minutos. Los heartbeats mantienen un lease
  de cinco minutos durante build, bench, generación y upload; un fallo vuelve a
  dejar el chunk repartible.
- El SHA-256 y los bytes registrados corresponden al `.bz2` recibido y son
  recalculados por el servidor. Los archivos viven en
  `Media/datagen/<test_id>/chunk_<idx>.bz2`.
- En este host compartido, limitar builds con `OPENBENCH_BUILD_JOBS=8` y usar
  clientes dev pequeños; nunca mezclar la DB/Media/clientes de `:8000` con los
  ensayos de `:8001`.
