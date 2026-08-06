"""Re-baseline de numeros de prueba: drena lo que un ciclo dejo saturado.

Existe por el mismo motivo que ``recascade_backed``: la regla de repeticion de
``proof._cycling_edges`` (invariante 6 de docs/value-semantics.md) corrige el
calculo HACIA DELANTE, pero los ``ProofNode`` que el trinquete ya clavo en
``PROOF_INFINITY`` se quedan tal cual hasta que algo recompute su familia.
Caso Eclipsia (6-ago): 534 abiertos con dn = 2^62 y 1.177 con pn = 2^62,
repartidos por todo el arbol.

MECANICA.  Cada pasada siembra ``refresh_proof_numbers`` con los ABIERTOS
saturados de ese momento (§ ``proof.saturated_open_count``: pn o dn en
``PROOF_INFINITY`` sobre posiciones UNKNOWN).  Con la regla nueva delante, la
arista que cierra el bucle puntua ``(INF, 0)`` en vez del numero del ciclo, la
realimentacion se corta y el veneno drena un salto de espina por pasada; el
cono de ancestros lo arrastra el propio refresco.  Se repite hasta que una
pasada no escriba ninguna fila o se agote el tope.

QUE NO PROMETE, y hay que decirlo antes de leer el informe.  Saturados CERO
no es el punto fijo, y menos con el invariante 6 en vigor: un ``pn`` infinito
es corriente y HONESTO — dice "por aqui no se prueba el objetivo", que es lo
que vale un nodo con todas sus continuaciones refutadas, incluida la
refutacion POR REPETICION que la regla nueva introduce a proposito.  El
comando informa de las dos columnas por separado por eso mismo: lo que tiene
que bajar es el ``dn`` saturado en nodos abiertos, que es el estado imposible
(dn infinito significa probado, y probado es pn = 0).  El criterio de parada
es "nada cambio", nunca "nada saturado".

Idempotente y seguro en vivo: recomputar un nodo sano no escribe (el corte
"sin cambios" de ``_refresh_campaign``), y el churn concurrente solo añade
trabajo que la pasada siguiente absorbe.  Manual a proposito — la decision de
convertirlo en barrido nocturno es del propietario, no de este comando.
"""

from django.core.management.base import BaseCommand
from django.db.models import Q

from atomicdb import proof
from atomicdb.models import ProofNode


class Command(BaseCommand):
    help = ('Recalcula los pn/dn saturados de nodos abiertos con la regla '
            'de repeticion vigente, hasta punto fijo')

    def add_arguments(self, parser):
        parser.add_argument('--chunk', type=int, default=500)
        parser.add_argument('--max-passes', type=int, default=12)
        parser.add_argument(
            '--keys', nargs='*', default=None,
            help='Siembra explicita (claves de posicion); por defecto, '
                 'todos los abiertos saturados de cada pasada')

    def _saturated_keys(self):
        return list(ProofNode.objects.filter(
            position__status='UNKNOWN').filter(
            Q(pn__gte=proof.PROOF_INFINITY)
            | Q(dn__gte=proof.PROOF_INFINITY)).values_list(
            'position_id', flat=True).distinct())

    def _census(self):
        return (proof.saturated_open_count(column='dn'),
                proof.saturated_open_count(column='pn'))

    def handle(self, *args, **opts):
        chunk, cap = opts['chunk'], opts['max_passes']
        explicit = opts['keys'] or None
        before_dn, before_pn = self._census()
        self.stdout.write(
            'abiertos saturados antes: dn=%d (el sintoma) pn=%d (honesto)'
            % (before_dn, before_pn))
        for n in range(1, cap + 1):
            seeds = explicit if explicit is not None else \
                self._saturated_keys()
            if not seeds:
                self.stdout.write(self.style.SUCCESS(
                    'nada saturado que sembrar en la pasada %d' % n))
                break
            written = 0
            for start in range(0, len(seeds), chunk):
                written += proof.refresh_proof_numbers(
                    seeds[start:start + chunk])
            now_dn, now_pn = self._census()
            self.stdout.write(
                'pasada %d: %d semillas, %d filas escritas, '
                'quedan dn=%d pn=%d' % (n, len(seeds), written,
                                        now_dn, now_pn))
            if written == 0:
                self.stdout.write(self.style.SUCCESS(
                    'punto fijo en la pasada %d' % n))
                break
        else:
            self.stdout.write(self.style.WARNING(
                'tope de pasadas (%d) sin punto fijo' % cap))
        after_dn, after_pn = self._census()
        self.stdout.write(
            'abiertos saturados despues: dn=%d (antes %d) pn=%d (antes %d)'
            % (after_dn, before_dn, after_pn, before_pn))
