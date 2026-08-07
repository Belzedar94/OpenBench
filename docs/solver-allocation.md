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
