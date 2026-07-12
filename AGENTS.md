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
| venv del servidor | `<fork>\.venv` (Django 4.2.30, scipy 1.18) |
| Motor spell (público) | `https://github.com/Belzedar94/Spell-Stockfish` (branch de trabajo `phase-4-strength`, base `master`) |
| Docs de despliegue/diseño | `Spell-Stockfish\docs\openbench-server-runbook.md` y `docs\openbench-spell.md` |
| DB | `<fork>\db.sqlite3` (SQLite; suficiente para pocos workers) |

## 2. Arrancar y entrar

```powershell
cd "<fork>"
.\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000
```

- OJO: `runserver` está sobreescrito y arranca dos watchers (artifacts y PGN). No usar
  gunicorn/otro WSGI sin replicarlos.
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
   `make -j EXE=<salida> [CC=<compiler>] [EVALFILE=<red>]` — SIN target y SIN ARCH/COMP.
   Tu Makefile debe producir un binario nativo optimizado con `make` a pelo.
   Spell-Stockfish lo resuelve con un shim al final de `src/Makefile`
   (`.DEFAULT_GOAL := openbench` → `build COMP=<por-OS>`, ARCH ya es native) — cópialo.
   `EVALFILE=<ruta>` llega cuando el test tiene red asignada: si tu red es formato SF,
   embébela (el Makefile de SF lo soporta); si es formato propio, decide tu mecanismo
   (spell aún lo tiene pendiente — ver §7).
3. **Contrato de bench**: el worker corre `./binario bench` (sin args; con red asignada y
   engine PRIVADO antepone `setoption name EvalFile value <red>`). Requisitos: determinista,
   < 300 s (timeout ya subido en este fork; se lanza UN bench POR HILO del worker EN
   PARALELO), imprime `Nodes searched: N`. Disciplina: los commits que se testeen llevan
   `Bench: <N>` al final del mensaje (regex del server: `(?:BENCH|NODES)[ :=]+([0-9,]+)`).
   Spell: `Bench: 13456297` (red default embebida).
4. **`Engines/Atomic-Stockfish.json`** — copia `Engines/Spell-Stockfish.json` (flujo
   público: `"private": false` + `build.path/compilers/cpuflags/systems`) y ajusta `nps`
   (mídelo: es el que escala TCs entre máquinas), presets y `book_name`.
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
- Workers REMOTOS: pendiente de publicar este fork en GitHub (ver §7). El auto-update del
  cliente descarga `client_repo_url`@`client_repo_ref` y asume carpeta raíz
  `OpenBench-<ref>` → **el repo en GitHub debe llamarse `OpenBench`**. Además el
  kill-by-name del worker busca `cutechess-ob(.exe)`: para remotos, empaquetar
  `uci_pair_runner.py` como ejecutable con ese nombre (pyinstaller) — localmente no hace
  falta (el runner se auto-protege: pipe muerto → sale matando motores; 3 muertes
  instantáneas seguidas → aborta el lote).

## 6. Gotchas que ya mordieron (no reaprender por las malas)

- **sha de libro en modo texto**, no binario (§4.5).
- **Nombres de engine = UN token** (el comando entero se `split()`ea por espacios; el
  parser de resultados lee `tokens[6]`). Nada de espacios en rutas de binarios/args.
- El PGN de cada partida se escribe SIEMPRE (aunque no haya errores) — el worker lo relee
  para contar crashes por cabecera `[Termination ...]`.
- `option.<K>=<V>` en presets/tests para opciones UCI; en SPSA los parámetros se exponen
  como opciones UCI (TUNE de SF) y el server los mueve por lotes.
- Al medir benches a mano en PowerShell: las pipes de here-string meten un BOM que mata el
  PRIMER comando UCI en silencio. Redirige stdin desde archivo o usa Python.
- Los tests solo pueden apuntar al repo registrado en `source` (no a forks personales).
- SQLite aguanta pocos workers reportando; si la flota crece → PostgreSQL.
- `Config/config.json` no se valida al arranque (bug upstream): revísalo a mano.

## 7. Estado y pendientes (2026-07-12)

- Hecho: server local operativo; Spell-Stockfish registrado (flujo público); red run5rl
  subida; libro `spell_openings.epd` publicado como release y manifestado; shim de Makefile
  en el repo del motor; `uci_pair_runner` integrado y endurecido (verificación adversarial).
- Pendiente: **publicar este fork en GitHub** como `Belzedar94/OpenBench` rama
  `spell-runner` (config.json ya apunta ahí; hasta entonces, workers solo desde este
  checkout local); smoke SPRT E2E master-vs-master de spell; mecanismo EVALFILE→default
  UCI para tests de spell CON red asignada (los netless y SPSA funcionan ya); presets de
  Atomic cuando exista su json.
- Histórico de decisiones y erratas verificadas: `Spell-Stockfish\docs\openbench-server-runbook.md`
  (despliegue) y `Spell-Stockfish\docs\openbench-spell.md` (diseño del ruteo).
