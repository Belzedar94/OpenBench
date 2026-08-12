# Política de asignación del solver (5-ago-2026)

> «Las campañas deberían funcionar, y si se establece una campaña, el auto
> debería ir por esos árboles, sino no sirve de nada.»

Ese es el enunciado entero. Este documento explica por qué hoy no se cumple,
qué dos reglas lo arreglan, con qué constantes, y qué NO hacen.

Recibos: auditoría de solo lectura del 5-ago sobre la base viva
(`/root/q1_camp.py`, `q3.py`, `q4.py`, `q6_orclamp.py` en el servidor) y el
código de `atomicdb/ingest.py`, `atomicdb/proof.py`.

## 1. Por qué las campañas son inertes hoy

Producción corre con `ATOMICDB_SELECTOR=pn` (la unidad de systemd del
selector lo pone en su bloque `Environment=`). Y con eso, `next_tasks`
cortocircuita en su primera línea:

    if proof.selector_mode() == 'pn':
        return _next_tasks_by_proof(n)

`_next_tasks_by_proof` baja por df-pn desde la raíz de cada `ProofCampaign`
activa con el repertorio blando 80/15/5, y **jamás lee `Position.priority`**.
El bono de campaña — `CAMPAIGN_BONUS = 40·ln(1+votos)`, sumado en
`priority_of` — vive entero dentro de esa columna. O sea: la única palanca que
la comunidad tiene sobre el trabajo del solver alimenta un número que el
selector desplegado no mira.

No es una teoría; se ve en la base:

| medida (5-ago) | valor |
| --- | --- |
| campañas ACTIVE | 1 (id=7, «While this is no mate eval…», de wolfie, 4 votos) |
| posiciones etiquetadas con la campaña 7 | 251.512 (216.358 todavía UNKNOWN) |
| bono que le da la fórmula | 40 · ln(5) = **64,4 unidades** |
| top-5 de `priority` GLOBAL | las cinco son de la campaña 7 (116,378) |
| tareas PENDING AUTO sobre la campaña 7 | **0** |

La columna dice que la campaña gana la cola por goleada, y la cola que de
verdad se sirve ni la consulta. Un usuario que vota ve el número subir en la
portada y no ve moverse ni una tarea: eso es exactamente lo que el propietario
llama «no sirve de nada».

## 2. Regla (a): descensos que ARRANCAN en la raíz de una campaña

**Qué cambia.** Antes de cada descenso, se sortea si ese descenso arranca en
la raíz global de la `ProofCampaign` (lo de siempre) o en la raíz de una
campaña ACTIVE. A partir de ahí el descenso es el MISMO: primaria pegajosa,
reparto blando 80/15/5, reserva del lote, tope de plies. La campaña no cambia
las reglas de la prueba; cambia dónde empieza a mirar.

**Con qué probabilidad.** Peso de cada campaña ACTIVE = `ln(1+votos)`, el
mismo logaritmo que ya usaba el bono — el voto veinte pesa mucho menos que el
segundo, y un brigadeo de cookies mueve el orden, no lo compra. Los pesos se
normalizan entre sí y se multiplican por un TOPE GLOBAL:

    CAMPAIGN_DESCENT_SHARE = 0.35

Como mucho el 35 % de los descensos arrancan en campañas, sumadas todas. El
tope es la mitad importante de la regla: sin él, tres campañas votadas se
llevarían el solver entero y el teorema de la raíz —que es lo que este
proyecto está probando— se quedaría sin motor. Con él, dos de cada tres
descensos siguen saliendo de la raíz global pase lo que pase.

**El suelo de activación.** `ln(1+0) = 0`: una campaña ACTIVE sin votos
pesaría cero y no recibiría nada, que es el mismo fallo que este paquete viene
a arreglar, solo que con otra cara. Por eso una campaña ACTIVE cuenta como
mínimo con un voto:

    CAMPAIGN_ACTIVATION_VOTES = 1   # activar es el voto del propietario

Es el reparto de poder del modelo `Campaign` escrito en la asignación:
cualquiera propone y vota, **solo el propietario activa**, y una activación no
puede valer cero. PROPOSED y PAUSED siguen valiendo exactamente cero
descensos: la línea entre «la comunidad pide» y «el propietario concede» no se
mueve.

**El sorteo es determinista.** Mismo hash del contador que el reparto
80/15/5 (`_bucket`), con otro dominio (`campaign:`) para que las dos
decisiones no queden correlacionadas. Nada de `random`: el mismo estado
produce la misma cola y un replay sigue siendo reproducible, que es la
propiedad por la que este selector eligió un contador y no un generador.

**Qué NO hace.**

- No es un cupo garantizado ni una cola aparte. Es dónde ARRANCA el descenso;
  si bajo esa raíz no queda nada abierto, el descenso vuelve con las manos
  vacías y el intento se pierde — el bucle de `_next_tasks_by_proof` tiene
  presupuesto de intentos y sigue.
- No adopta el subárbol. La etiqueta `Position.campaign_id` y el BFS de
  activación siguen siendo lo que eran.
- No toca `priority` ni `CAMPAIGN_BONUS`. Con el selector en `regret` todo
  sigue igual que ayer, byte a byte; el bono de la columna sigue siendo la
  palanca de ese otro motor.
- Una campaña cuya raíz ya está cerrada no entra en el sorteo (y además el
  cierre la pasa a DONE por el camino de siempre).

## 3. Regla (b): clamp de nodos OR con la respuesta ya en la mano

**El caso que lo motiva**, medido. Línea `1.c3 e6 2.e4 Qf6 3.f4 Qd4`: nodo
con blancas al turno, o sea nodo OR de la campaña `root-white-win`. Tiene 30
hijos. Hoy 29 están cerrados y **23 de ellos costaron ~8M nodos cada uno**
(`nodes=8.00M` repetido, tarea a tarea, entre las 12:03 y las 12:07 del 5-ago;
otros cuatro salieron gratis por PV de mate y dos se cerraron por debajo del
peldaño). Del orden de **185M nodos de motor** en hermanos, mientras la única
jugada que la prueba necesita de ese nodo — `cxd4`, que se come la dama —
seguía siendo el único hijo UNKNOWN.

**Y la parte honesta de este ejemplo**: el clamp NO habría pillado ese nodo
concreto. Sus 29 hermanos son refutaciones (`BLACK_WIN`), no hermanos de un
ganador, y mientras `cxd4` siga sin probarse nadie puede afirmar que el nodo
está resuelto. Lo que el ejemplo enseña es la FORMA del gasto — un nodo OR
enumerando a precio completo hermanos que ninguna prueba va a usar — y de esa
forma hay un trozo que sí es decidible sin adivinar nada: cuando el ganador ya
está probado. Eso es lo que implementa el clamp, y lo de abajo es lo que vale
hoy.

**La regla.** En un nodo OR mueve el atacante del objetivo: le basta UNA
jugada buena. Si ese nodo YA tiene un hijo con `status == goal` —una victoria
probada, no una eval optimista— el nodo está probado y sus demás hijos no le
deben nada a la prueba. Las tareas sobre esos hermanos caen a presupuesto
mínimo:

    OR_CLAMP_NODES   = 250_000   # vs BUDGET_LADDER[0] = 8_000_000
    OR_CLAMP_MULTIPV = 1

Treinta y dos veces más barato que el primer peldaño de la escalera. No es
cero, y eso es deliberado: un hermano sin ninguna eval es un agujero en el
DAG —lo mira el explorador, lo respalda la cascada, lo ordena la tabla de
jugadas— así que se le compra una mirada barata, no el silencio.

**Es la generalización de `_short_mate_clamp` a distancia desconocida.** Aquel
dice: «reclamas mate en ≤3, no te compro la excavación de 128M, te compro la
verificación». Este dice: «la victoria por aquí ya está probada, a distancia
desconocida, así que a tus hermanos no les compro nada más que una mirada».
Mismo par `(presupuesto, multipv)`, mismo camino por `multipv_for`, misma
idea: el presupuesto lo fija PARA QUÉ es el análisis, no cuántas veces se ha
visitado el nodo.

**Cuánto vale hoy**, sobre la base viva (`q6_orclamp.py`, muestra de los 507
nodos con `pn=0` y posición todavía UNKNOWN):

| medida | valor |
| --- | --- |
| nodos OR con un hijo YA PROBADO (WHITE_WIN) | 151 |
| hermanos suyos todavía UNKNOWN | 3.936 |
| a 8M por tarea | **31,5 G nodos** de motor |
| con el clamp (250k) | 0,98 G |

Treinta veces menos, sobre trabajo que la prueba no necesita. Y la muestra es
un tope de 4.000 candidatos sobre una base de millones: el número real es
mayor, no menor.

**Por qué NO toca nodos AND.** En un nodo AND mueve el defensor y hay que
refutar TODAS sus respuestas. Que una de ellas esté probada no libera a las
demás — al revés, son la obligación entera de la prueba. Abaratarlas ahí sería
comprar una prueba falsa. El clamp mira el FEN del padre y el objetivo de la
campaña (`is_or_node`) precisamente para no cruzar esa línea.

**Transposiciones.** El DAG tiene varios padres por nodo, así que la condición
es sobre TODOS ellos: un nodo se abarata solo si CADA padre suyo está resuelto
—cerrado, o nodo OR con hijo probado—. Basta un padre vivo que todavía lo
necesite para que el presupuesto vuelva a ser el de siempre. Es la misma
lógica que `_still_reachable` («con todos los padres cerrados, analizarlo ya
no influye arriba») un paso antes: un nodo OR con hijo probado está cerrado
para la prueba aunque el DAG no haya llegado todavía a escribirle el cierre.

**Dónde se aplica.** En el camino que mintea bajo `pn` (`_next_tasks_by_proof`)
y solo ahí. El clamp es una afirmación sobre LA PRUEBA, y la prueba es quien
mintea en ese modo; el selector `regret` tiene otra política de interés y no
se toca. Un click de visitante tampoco se ve afectado: `_request_rung` compra
por `max(...)` sobre la escalera de peticiones, que empieza en 128M.

## 4. Conmutadores

Los dos van ENCENDIDOS por defecto y se apagan desde el entorno, con el patrón
de `ATOMICDB_SELECTOR_DELTA` (cualquier cosa que huela a «no» lo apaga):

| variable | qué apaga | default |
| --- | --- | --- |
| `ATOMICDB_CAMPAIGN_DESCENT` | los descensos con raíz de campaña (a) | ON |
| `ATOMICDB_OR_CLAMP` | el presupuesto mínimo de hermanos (b) | ON |

Al lado de estos dos vive `ATOMICDB_DESCENT`, que no apaga nada sino que
elige POR DÓNDE se baja (§6). Su default es el comportamiento histórico y
está en el código, no en el entorno: `value` se pone a mano en el env file y
quitar esa línea es el rollback entero.

| variable | qué elige | default |
| --- | --- | --- |
| `ATOMICDB_DESCENT` | `proof` (df-pn) o `value` (walker de la espina) | `proof` |

Encendidos por defecto y no en sombra, a diferencia de `ATOMICDB_SELECTOR_V2`,
porque ninguno de los dos estrena un motor: (a) mueve el punto de arranque de
una fracción acotada de los descensos y (b) mueve un presupuesto hacia abajo.
Los dos son reversibles con una línea en el drop-in de systemd y un reinicio,
sin desplegar código, y ninguno deja residuo en la base: no escriben columnas
nuevas, no reescriben pn/dn, no cierran nada.

Con `ATOMICDB_CAMPAIGN_DESCENT` apagado —o sin ninguna campaña ACTIVE— la cola
que sale de `_next_tasks_by_proof` es la de siempre, nodo a nodo.

## 5. Cómo se comprueba que está vivo

Una sombra de N descensos, sin escribir nada, cuenta qué fracción arranca en
una campaña ACTIVE:

    for counter in range(100):
        proof.campaign_start(counter)     # None = raíz global

Con la campaña 7 en pausa el resultado tiene que ser 0 %, y ese cero es la
prueba de que el cableado respeta el estado. Con una campaña ACTIVE y votos,
la fracción tiene que rondar el 35 % del tope. Además, las tareas minteadas
desde una raíz de campaña salen marcadas con `arm='campaign'`: quien vota
puede ver su línea en la cola, que es la mitad visible de «las campañas
deberían funcionar».

## 6. El descenso por VALOR (12-ago-2026)

Las dos reglas de arriba deciden dónde ARRANCA un descenso y cuánto vale un
hermano ya resuelto. Ninguna toca la pregunta de en medio: **por dónde se
baja**. Esta sección sí.

### 6.1 El síntoma, con recibos

Auditoría del 11-ago sobre la base viva, campaña `root-white-win`
(WHITE_WIN desde startpos), única activa:

| medida | valor |
| --- | --- |
| `pn` de `1.e3` en la raíz | 119 |
| `pn` de `g1f3` en la raíz | 475 |
| valor respaldado de `g1f3` | **+812** (la mejor jugada del tablero) |
| últimas 40 tareas AUTO bajo `1.e3` | 26 |
| últimas 40 tareas AUTO bajo `g1f3` | 1 |
| tareas de esas 40 a 8M (`BUDGET_LADDER[0]`) | 40 |
| reparto del GASTO AUTO bajo `1.e3` / `g1f3` | 65 % / 2,5 % |

Dos tercios del motor donado se estaban gastando en sondas de reconocimiento
sobre una línea mediocre, y la jugada que sostiene el valor de la raíz recibía
una tarea de cada cuarenta.

### 6.2 El diagnóstico

`pn` es una estimación del coste de COMPLETAR la prueba formal. A la distancia
a la que está esta prueba —nadie va a cerrar Atomic desde startpos esta
década— todos los `pn` del árbol son ficción, y un mínimo sobre ficciones no
elige la línea prometedora: elige **la línea floja más barata de enumerar**.
Un `pn` de 119 no dice que `1.e3` gane; dice que a `1.e3` todavía le quedan
pocos hijos informados.

Dos defectos concretos salieron de la misma auditoría:

- el 5 % «explore» del reparto 80/15/5 bajaba por `ranked[-1]`, o sea **el peor
  hijo del nodo**: explorar lo que ya se sabe malo;
- el presupuesto lo ponía `budget_for`, que cuenta VISITAS, y un nodo recién
  encontrado siempre tiene cero: 8M en la jugada que sostiene un +812 a 22
  plies no mueve el número, sólo repite lo que la siembra del padre ya decía.

### 6.3 La regla: seguir la espina respaldada

El enunciado del propietario, entero: «cogería el mejor opening (`g1f3`, +812
backed), miraría la mejor respuesta negra y bajaría los 22 plies hasta analizar
las jugadas concretas que sostienen el 812. Buen análisis ahí, y al siguiente
bottleneck. Como mucho una cascada rápida a 8M para encontrar el siguiente
cuello.»

La infraestructura ya existía: la cadena de `backed_move` **es** esa espina —
el negamax de los dos bandos sobre búsqueda real, calculado por
`ingest.backup_backed_evals`, el mismo que el explorador pinta con el chip de
respaldo. El walker (`proof.descend_value`) la recorre:

1. **Primaria** de cada nodo: su `backed_move`. Si apunta a un hijo que ese
   paseo no puede seguir —cerrado desde la última cascada, o ya visitado—
   manda el orden por valor (`best_known` en POV blanca; blancas maximizan,
   negras minimizan) con la histéresis de `selected_child`. `pn` sobrevive
   como **desempate** y sólo como desempate: dos hijos que valen lo mismo para
   el que mueve siguen diferenciándose en lo que cuesta cerrarlos, y esa es la
   pregunta que `pn` contesta bien.
2. **Terminación**: el nodo SIN `backed_move` es la hoja cuyo `eval_cp` crudo
   sostiene todo lo de arriba. Ahí se compra.
3. **Revisitas**, para que el walker no vuelva eternamente a la misma punta:
   una punta que ya lleva ≥128M encima no se recompra. El objetivo pasa al
   primer nodo de la espina —de abajo arriba— con respuestas SIN JUZGAR
   (`is_unjudged`): ese es el cuello de botella. Una espina entera sin cuellos
   sí es una línea que hay que profundizar, y la punta cobra el peldaño
   siguiente al que ya tiene.
4. **Ciclos**: el paseo lleva visited-set —el DAG cierra ciclos de verdad, que
   `1.Nf3 Nf6 2.Ng1 Ng8` ES startpos— y una espina que vuelve sobre sí misma
   deja como objetivo el nodo pre-ciclo, por encima de lo que ya tiene el hijo
   que cierra el bucle. Mismo espíritu que `_queue_cycle_disambiguation`.
5. **Reserva**: una punta que este mismo lote ya reparte, o que ya tiene tarea
   PENDING o LEASED, no se entrega por duplicado; el paseo se va por la
   alternativa competitiva más profunda (Δ < `VALUE_ALTERNATIVE_MARGIN_CP`,
   150cp) y sigue la espina desde ahí.

### 6.4 El 80/15/5, re-significado

El mismo `_bucket` determinista de siempre, con el mismo hash, sorteado **una
vez por paseo** y no una por ply: ahora el bucket describe el paseo entero
—punta, desvío o hijo virgen— y no una elección repetida en cada nodo.

| bucket | a dónde va | presupuesto |
| --- | --- | --- |
| 80 % primary | la punta de la espina principal | 128M (`REQUEST_BUDGET_LADDER[0]`) |
| 15 % backup | el desvío competitivo más profundo | 32M |
| 5 % explore | el mejor hijo SIN EXPLORAR del camino | 8M |

El 5 % ya no compra el peor hijo: compra la **medida** que una eval sembrada
por el MultiPV del padre no es.

### 6.5 Presupuestos

La escalera de exploración deja de gobernar la línea principal. El objetivo del
paseo entra por el primer peldaño de PETICIONES —el mismo que compra un click
humano— porque es la misma clase de pregunta: «qué pasa de verdad aquí», no una
mirada de reconocimiento.

| brazo del walker | presupuesto |
| --- | --- |
| punta de la espina / cuello de botella | 128M |
| punta saturada sin cuello | siguiente peldaño de `REQUEST_BUDGET_LADDER` |
| nodo pre-ciclo | `_rung_at_least(nodos del hijo que cicla + 1)` |
| desvío competitivo | 32M |
| hijo sin explorar | 8M |
| cascada del cuello | 8M, cupo 16 |

**La cascada** (`_queue_value_cascade`) corre tras el respaldo de cada ingesta:
las respuestas sin juzgar del nodo recién analizado entran a precio de mirada,
con `arm='cascade'` para poder auditarlas y con cupo propio de 16 —un cuarto
del colchón de 64— para que no pueda convertir el reparto en el mar de sondas
de 8M del que venimos. Con el descenso df-pn no hace nada.

**Los clamps siguen mandando por encima de todo.** Una reclamación de mate
corto con distancia conocida compra su verificación y no la excavación de
128M (`_short_mate_clamp`), exactamente igual que la compraba por el descenso
de siempre; y un hermano de un ganador ya probado sigue cayendo a
`OR_CLAMP_NODES` (§3). Lo que este paquete sube es el precio de lo que la
prueba SÍ necesita; no toca el de lo que ya dejó de necesitar.

### 6.6 Las campañas de la comunidad

Sin cambios en el reparto acotado (§2): la campaña sigue decidiendo dónde
ARRANCA el descenso. Lo que cambia es que, desde esa raíz, el walker hace
**exactamente el mismo bucle** que desde startpos: sigue la espina de ESA
posición hasta la hoja que sostiene SU evaluación, la compra, y en el descenso
siguiente relee la espina fresca desde la misma raíz para encontrar el cuello
nuevo. Espina, punta, cuello y peldaño se resuelven DENTRO de su subárbol —
salirse de él a comprar en la espina global sería el «no sirve de nada» que las
campañas vienen a arreglar. Lo que una campaña refina es el número de su raíz.

### 6.7 Qué NO cambia

- `Position.priority`, el selector v2 y los brazos de calidad
  (`QUALITY_ARM`, convergencia, desambiguación) siguen igual.
- La fairness de la cola: AUTO sigue detrás de USER, y el cupo de leases
  profundos sigue limitando cuántas tareas gordas hay a la vez.
- No hace falta `recascade`: esto no toca el backprop, sólo lo LEE.
- No escribe columnas nuevas ni reordena `pn`/`dn`. Apagarlo no deja residuo.

### 6.8 Cómo se comprueba que está vivo

Sobre las `AnalysisTask` AUTO PENDING recién minteadas:

- la mayoría cuelgan del mejor opening por respaldo (hoy `g1f3`; antes, 2,5 %);
- los presupuestos están dominados por 128M, con 8M sólo en `arm='cascade'` y
  en los desvíos del 5 %;
- ninguna tarea nueva del tipo «1.e3 a 8M».

Criterio de aceptación a 24h: >60 % del gasto AUTO bajo el mejor opening y
presupuesto medio por tarea AUTO ≥ ~100M (hoy: 8M).
