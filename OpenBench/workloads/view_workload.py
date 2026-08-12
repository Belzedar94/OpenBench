# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
#   OpenBench is a chess engine testing framework authored by Andrew Grant.   #
#   <https://github.com/AndyGrant/OpenBench>           <andrew@grantnet.us>   #
#                                                                             #
#   OpenBench is free software: you can redistribute it and/or modify         #
#   it under the terms of the GNU General Public License as published by      #
#   the Free Software Foundation, either version 3 of the License, or         #
#   (at your option) any later version.                                       #
#                                                                             #
#   OpenBench is distributed in the hope that it will be useful,              #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of            #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the             #
#   GNU General Public License for more details.                              #
#                                                                             #
#   You should have received a copy of the GNU General Public License         #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.     #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# Module serves a singular purpose, to invoke:
# >>> view_workload(request, workload, type)
#
# A Workload can be a "TEST", which is an SPRT, or FIXED type
# A Workload can be a "TUNE", which is an SPSA tuning session
# A Workload can be a "DATAGEN", which is a Data Generation session

import datetime
import OpenBench.live_elo
import OpenBench.views

from django.core.paginator import Paginator
from django.utils import timezone
from OpenBench.models import *

DATAGEN_CHUNKS_PER_PAGE = 100

def view_workload(request, workload, workload_type):

    assert workload_type in [ 'TEST', 'TUNE', 'DATAGEN' ]

    data = {
        'workload' : workload,
        'results'  : [],
    }

    for result in Result.objects.filter(test=workload):
        data['results'].append({ 'data' : result, 'active' : is_active(result) })

    if workload_type == 'TEST':
        data['type']            = workload_type
        data['dev_text']        = 'Dev'

        # Dials on top of the page, for the workloads that have an Elo to show
        if OpenBench.live_elo.has_live_elo(workload):
            data['live_elo'] = OpenBench.live_elo.live_elo_payload(workload)

    if workload_type == 'TUNE':
        data['type']            = workload_type
        data['dev_text']        = ''

    if workload_type == 'DATAGEN':
        data['type']            = workload_type
        data['dev_text']        = 'Engine'
        if workload.is_generic_datagen():
            chunks = workload.datagen_chunks.select_related(
                'machine__user'
            ).order_by('idx')
            chunk_page = Paginator(chunks, DATAGEN_CHUNKS_PER_PAGE).get_page(
                request.GET.get('chunks_page')
            )
            data['datagen_chunks'] = chunk_page
            data['datagen_chunk_page'] = chunk_page
            data['datagen_completed'] = workload.datagen_completed_chunks
            data['datagen_total'] = workload.datagen_total_chunks()

    return OpenBench.views.render(request, 'workload.html', data)

def is_active(result):

    # One minute prior to now
    target = datetime.datetime.utcnow()
    target = target.replace(tzinfo=timezone.utc)
    target = target - datetime.timedelta(minutes=1)

    return result.test.id == result.machine.workload and result.machine.updated >= target
