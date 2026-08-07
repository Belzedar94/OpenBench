"""Republica las cifras compartidas de la portada, fuera del camino del visitante.

QUE CALIENTA, DICHO SIN ADORNOS.  Las CINCO entradas compartidas que la
portada lee (§ metrics.PUBLIC_SNAPSHOTS mas la flota de § contributors).  No
calienta la cache de PAGINA, y no es un descuido: la entrada de pagina de la
portada varia por ``Cookie`` (§ urls, ``vary_on_cookie``) y el token CSRF es
distinto para cada visitante, asi que la clave de pagina tambien lo es.  Un
calentador con su propio tarro de cookies escribiria SU entrada y ningun
visitante la leeria jamas.  Estas cinco, en cambio, no llevan visitante en la
clave: las lee todo el mundo, con cookie o sin ella.

POR QUE EXISTE.  Medido el 7-ago sobre la base real, medirlas cuesta 23 s —
14,4 s el barrido de ``Position`` y 6,8 s la atribucion de cierres.  Que las
mida este comando y no una peticion es la diferencia entre gastar ese tiempo
en un servicio con ``Nice=10`` o gastarlo con un visitante mirando.

QUE NO ES.  No es una dependencia de nada.  Si este comando falla, o no
corre, la portada sigue sirviendose: las entradas se renuevan solas por
detras cuando un lector las encuentra viejas (§ metrics.shared_snapshot), y
lo unico que se pierde es que la cifra sea un poco mas reciente.  Por eso el
timer no lleva ``Requires=`` de nadie y por eso esto sale con codigo 0
incluso cuando una medida revienta: un calentador que tumba lo que calienta
seria peor que no tenerlo.
"""

import time

from django.core.management.base import BaseCommand

from atomicdb import metrics


class Command(BaseCommand):
    help = ('Republica las cifras compartidas de la portada (contadores, '
            'atribucion, actividad, campanas y flota).')

    def add_arguments(self, parser):
        parser.add_argument(
            '--quiet', action='store_true',
            help='No imprimir nada salvo que falle algo.')

    def handle(self, *args, **options):
        started = time.monotonic()
        try:
            keys = metrics.refresh_public_snapshot()
        except Exception as exc:            # noqa: BLE001
            # A stderr y con codigo 0: el journal lo recoge y sale en
            # ``systemctl status``, pero un calentador que falla no puede
            # marcar el arranque como fallido ni disparar alarmas de nadie.
            self.stderr.write('warm_public_snapshot: no se pudo republicar '
                              '(%s: %s)' % (type(exc).__name__, exc))
            return
        if not options['quiet']:
            self.stdout.write('warm_public_snapshot: %d entradas en %.1fs'
                              % (len(keys), time.monotonic() - started))
