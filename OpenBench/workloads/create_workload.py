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
# >>> create_workload(request, type)
#
# A Workload can be a "TEST", which is an SPRT, or FIXED type.
# A Workload can be a "TUNE", which is an SPSA tuning session
#
# This module will either create the workload and return the user to the index,
# which will display their newly created test. Or it will return them to index,
# with a list of errors that need to be fixed. A warning may also be displayed,
# if the Base branch appears ahead of the Dev branch.

import math

import OpenBench.utils
import OpenBench.views
import OpenBench.datagen_publication
import OpenBench.variant_contract

from django.conf import settings
from django.db import IntegrityError, transaction
from OpenBench.datagen import MAX_LEGACY_DATAGEN_GAMES, initialize_chunks
from OpenBench.models import *
from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.workloads.verify_workload import verify_workload

def create_workload(request, workload_type):

    assert workload_type in [ 'TEST', 'TUNE', 'DATAGEN' ]

    if not request.user.is_authenticated:
        return OpenBench.views.redirect(request, '/login/', error='Only enabled users can create tests')

    if not Profile.objects.get(user=request.user).enabled:
        return OpenBench.views.redirect(request, '/login/', error='Only enabled users can create tests')

    if request.method == 'GET':

        data = { 'networks' : list(Network.objects.all().values()) }

        if workload_type == 'TEST':
            data['workload']        = workload_type
            data['dev_text']        = 'Dev'
            data['dev_title_text']  = 'Dev'
            data['submit_text']     = 'Create Engine Test'
            data['submit_endpoint'] = '/newTest/'

        if workload_type == 'TUNE':
            data['workload']        = workload_type
            data['dev_text']        = ''
            data['dev_title_text']  = 'Engine'
            data['submit_text']     = 'Create SPSA Tune'
            data['submit_endpoint'] = '/newTune/'

        if workload_type == 'DATAGEN':
            data['workload']        = workload_type
            data['dev_text']        = 'Dev'
            data['dev_title_text']  = 'Dev'
            data['submit_text']     = 'Create Datagen'
            data['submit_endpoint'] = '/newDatagen/'

        return OpenBench.views.render(request, 'create_workload.html', data)

    if workload_type == 'TEST':
        workload, errors = create_new_test(request)

    elif workload_type == 'TUNE':
        workload, errors = create_new_tune(request)

    elif workload_type == 'DATAGEN':
        workload, errors = create_new_datagen(request)

    if errors != [] and errors != None:
        paths = { 'TEST' : '/newTest/', 'TUNE' : '/newTune/', 'DATAGEN' : '/newDatagen/' }
        return OpenBench.views.redirect(request, paths[workload_type], error='\n'.join(errors))

    warning = None
    if not workload.is_generic_datagen() and OpenBench.utils.branch_is_out_of_date(workload):
        warning = 'Consider Rebasing: Dev (%s) appears behind Base (%s)' % (workload.dev.name, workload.base.name)

    username = request.user.username
    profile  = Profile.objects.get(user=request.user)
    summary  = 'CREATE P=%d TP=%d' % (workload.priority, workload.throughput)
    LogEvent.objects.create(author=username, summary=summary, log_file='', test_id=workload.id)

    if not OPENBENCH_CONFIG['use_cross_approval'] and profile.approver:
        workload.approved = True; workload.save()

    return OpenBench.views.redirect(request, '/index/', warning=warning)

def create_new_test(request):

    # Generic DATAGEN is available from the regular Create Test page. Keep the
    # dedicated /newDatagen/ endpoint as a convenient preselected form.
    if request.POST.get('test_mode') == 'DATAGEN':
        return create_new_datagen(request)

    # Collects erros, and collects all data from the Github API
    errors, engine_info = verify_workload(request, 'TEST')
    dev_info, dev_has_all = engine_info[0]
    base_ingo, base_has_all = engine_info[1]

    if errors:
        return None, errors

    test                   = Test()
    test.author            = request.user.username
    test.book_name         = request.POST['book_name']
    test.upload_pgns       = request.POST['upload_pgns']

    test.dev               = get_engine(*dev_info)
    test.dev_repo          = request.POST['dev_repo']
    test.dev_engine        = request.POST['dev_engine']
    test.dev_options       = request.POST['dev_options']
    test.dev_network       = request.POST['dev_network']
    test.dev_time_control  = OpenBench.utils.TimeControl.parse(request.POST['dev_time_control'])

    test.base              = get_engine(*base_ingo)
    test.base_repo         = request.POST['base_repo']
    test.base_engine       = request.POST['base_engine']
    test.base_options      = request.POST['base_options']
    test.base_network      = request.POST['base_network']
    test.base_time_control = OpenBench.utils.TimeControl.parse(request.POST['base_time_control'])

    test.variant_contract = OpenBench.variant_contract.configured_variant_contract(
        OPENBENCH_CONFIG, test.dev_engine, test.base_engine, test.book_name
    ) or ''

    test.workload_size     = int(request.POST['workload_size'])
    test.priority          = int(request.POST['priority'])
    test.throughput        = int(request.POST['throughput'])

    test.syzygy_wdl        = request.POST['syzygy_wdl']
    test.syzygy_adj        = request.POST['syzygy_adj']
    test.win_adj           = request.POST['win_adj']
    test.draw_adj          = request.POST['draw_adj']

    test.test_mode         = request.POST['test_mode']
    test.awaiting          = not (dev_has_all and base_has_all)

    if test.test_mode == 'SPRT':
        test.elolower = float(request.POST['test_bounds'].split(',')[0].lstrip('['))
        test.eloupper = float(request.POST['test_bounds'].split(',')[1].rstrip(']'))
        test.alpha    = float(request.POST['test_confidence'].split(',')[1].rstrip(']'))
        test.beta     = float(request.POST['test_confidence'].split(',')[0].lstrip('['))
        test.lowerllr = math.log(test.beta / (1.0 - test.alpha))
        test.upperllr = math.log((1.0 - test.beta) / test.alpha)

    if test.test_mode == 'GAMES':
        test.max_games = int(request.POST['test_max_games'])

    if test.dev_network:
        test.dev_netname = Network.objects.get(engine=test.dev_engine, sha256=test.dev_network).name

    if test.base_network:
        test.base_netname = Network.objects.get(engine=test.base_engine, sha256=test.base_network).name

    test.save()

    profile = Profile.objects.get(user=request.user)
    profile.tests += 1
    profile.save()

    return test, None

def create_new_tune(request):

    # Collects erros, and collects all data from the Github API
    errors, engine_info = verify_workload(request, 'TUNE')
    dev_info, dev_has_all = engine_info

    if errors:
        return None, errors

    test                  = Test()
    test.author           = request.user.username
    test.book_name        = request.POST['book_name']
    test.upload_pgns      = request.POST['upload_pgns']

    test.dev              = test.base              = get_engine(*dev_info)
    test.dev_repo         = test.base_repo         = request.POST['dev_repo']
    test.dev_engine       = test.base_engine       = request.POST['dev_engine']
    test.dev_options      = test.base_options      = request.POST['dev_options']
    test.dev_network      = test.base_network      = request.POST['dev_network']
    test.dev_time_control = test.base_time_control = OpenBench.utils.TimeControl.parse(request.POST['dev_time_control'])

    test.variant_contract = OpenBench.variant_contract.configured_variant_contract(
        OPENBENCH_CONFIG, test.dev_engine, test.base_engine, test.book_name
    ) or ''

    test.workload_size    = int(request.POST['spsa_pairs_per'])
    test.priority         = int(request.POST['priority'])
    test.throughput       = int(request.POST['throughput'])

    test.syzygy_wdl       = request.POST['syzygy_wdl']
    test.syzygy_adj       = request.POST['syzygy_adj']
    test.win_adj          = request.POST['win_adj']
    test.draw_adj         = request.POST['draw_adj']

    test.test_mode        = 'SPSA'
    test.spsa             = extract_spas_params(request)

    test.awaiting         = not dev_has_all

    if test.dev_network:
        name = Network.objects.get(engine=test.dev_engine, sha256=test.dev_network).name
        test.dev_netname = test.base_netname = name

    test.save()

    profile = Profile.objects.get(user=request.user)
    profile.tests += 1
    profile.save()

    return test, None

def create_new_datagen(request):

    # Collects erros, and collects all data from the Github API
    errors, engine_info = verify_workload(request, 'DATAGEN')
    dev_info, dev_has_all = engine_info

    if errors:
        return None, errors

    publication_network = None
    publication_book = None
    if OpenBench.datagen_publication.publication_requested(request.POST):
        try:
            selected_network = Network.objects.get(
                engine=request.POST['dev_engine'],
                sha256=request.POST['dev_network'],
            )
            publication_network = (
                OpenBench.datagen_publication.capture_network_identity(
                    selected_network, settings.MEDIA_ROOT
                )
            )
            publication_book = (
                OpenBench.datagen_publication.capture_book_identity(
                    request.POST['book_name'], OPENBENCH_CONFIG['books']
                )
            )
        except (
            KeyError,
            Network.DoesNotExist,
            OpenBench.datagen_publication.PublicationContractError,
        ) as error:
            return None, [str(error) or 'Unable to freeze DATAGEN publication assets']

        campaign_id = request.POST['datagen_campaign_id'].strip()
        workload_id = request.POST['datagen_external_workload_id'].strip()
        role = request.POST['datagen_role'].strip()
        cohort = request.POST['datagen_cohort'].strip()
        if Test.objects.filter(
            datagen_publication_protocol=41,
            datagen_campaign_id=campaign_id,
            datagen_external_workload_id=workload_id,
        ).exists():
            return None, ['DATAGEN campaign already contains this external workload id']
        if Test.objects.filter(
            datagen_publication_protocol=41,
            datagen_campaign_id=campaign_id,
            datagen_role=role,
            datagen_cohort=cohort,
        ).exists():
            return None, ['DATAGEN campaign already contains this role/cohort slot']

    try:
        with transaction.atomic():
            test = Test()
            test.author = request.user.username
            test.book_name = request.POST['book_name']
            test.upload_pgns = 'FALSE'

            engine = get_engine(*dev_info)
            test.dev = test.base = engine
            test.dev_repo = test.base_repo = request.POST['dev_repo']
            test.dev_engine = test.base_engine = request.POST['dev_engine']
            test.dev_options = test.base_options = request.POST.get('dev_options', '')
            test.dev_network = test.base_network = request.POST.get('dev_network', '')
            test.dev_time_control = test.base_time_control = ''
            test.variant_contract = OpenBench.variant_contract.configured_variant_contract(
                OPENBENCH_CONFIG, test.dev_engine, test.base_engine, test.book_name
            ) or ''

            test.datagen_command = request.POST['datagen_command'].strip()
            test.datagen_total_count = int(request.POST['datagen_total_count'])
            test.datagen_positions_per_chunk = int(request.POST['datagen_positions_per_chunk'])
            test.datagen_base_seed = int(request.POST['datagen_base_seed'])

            # Generic DATAGEN completion uses the 64-bit position counters below.
            # max_games is only a legacy signed-32-bit summary, so preserve exact
            # values while representable and saturate that non-canonical mirror.
            test.max_games = min(
                test.datagen_total_count, MAX_LEGACY_DATAGEN_GAMES
            )
            test.workload_size = 1
            test.priority = int(request.POST['priority'])
            test.throughput = int(request.POST['throughput'])

            template_fields = Test.datagen_template_fields(test.datagen_command)
            tablebase_required = bool(
                template_fields & DATAGEN_TABLEBASE_PLACEHOLDERS
            )
            engine_config = OPENBENCH_CONFIG['engines'][test.dev_engine]
            if tablebase_required:
                test.syzygy_wdl = request.POST['syzygy_wdl']
                tablebase_family = engine_config.get(
                    'tablebase_family', 'standard'
                )
                tablebase_max = int(test.syzygy_wdl.split('-')[0])
                tablebase_manifest = engine_config[
                    'tablebase_manifest_sha256'
                ].lower()
            else:
                test.syzygy_wdl = 'DISABLED'
                tablebase_family = ''
                tablebase_max = 0
                tablebase_manifest = ''
            test.syzygy_adj = 'DISABLED'
            test.win_adj = 'None'
            test.draw_adj = 'None'

            teacher_mode = (
                request.POST.get('datagen_teacher_mode', '')
                if 'TEACHER_MODE' in template_fields else ''
            )
            test.freeze_datagen_environment_contract(
                tablebase_family,
                tablebase_max,
                tablebase_manifest,
                teacher_mode,
            )

            test.test_mode = 'DATAGEN'
            test.awaiting = not dev_has_all
            test.use_tri = False
            test.use_penta = False

            if test.dev_network:
                name = Network.objects.get(
                    engine=test.dev_engine, sha256=test.dev_network
                ).name
                test.dev_netname = test.base_netname = name

            if publication_network is not None:
                test.datagen_publication_protocol = 41
                test.datagen_campaign_id = request.POST[
                    'datagen_campaign_id'
                ].strip()
                test.datagen_external_workload_id = request.POST[
                    'datagen_external_workload_id'
                ].strip()
                test.datagen_role = request.POST['datagen_role'].strip()
                test.datagen_cohort = request.POST['datagen_cohort'].strip()
                test.freeze_datagen_producer_contract()
                test.freeze_datagen_publication_contract(
                    publication_network, publication_book
                )

            test.save()
            initialize_chunks(test)

            profile = Profile.objects.get(user=request.user)
            profile.tests += 1
            profile.save()
    except OpenBench.datagen_publication.PublicationContractError as error:
        return None, [str(error)]
    except IntegrityError:
        if publication_network is None:
            raise
        return None, ['DATAGEN publication campaign slot was created concurrently']

    return test, None

def extract_spas_params(request):

    spsa = {} # SPSA Hyperparams
    spsa['Alpha'  ] = float(request.POST['spsa_alpha'])
    spsa['Gamma'  ] = float(request.POST['spsa_gamma'])
    spsa['A_ratio'] = float(request.POST['spsa_A_ratio'])

    # Tuning durations
    spsa['iterations'] = int(request.POST['spsa_iterations'])
    spsa['pairs_per' ] = int(request.POST['spsa_pairs_per'])
    spsa['A'         ] = spsa['A_ratio'] * spsa['iterations']

    # Tuning Methodologies
    spsa['reporting_type'   ] = request.POST['spsa_reporting_type']
    spsa['distribution_type'] = request.POST['spsa_distribution_type']

    # Each individual tuning parameter
    spsa['parameters'] = {}
    for index, line in enumerate(request.POST['spsa_inputs'].split('\n')):

        # Comma-seperated values, already verified in verify_workload()
        name, data_type, value, minimum, maximum, c_end, r_end = line.split(',')

        # Recall the original order of inputs
        param          = {}
        param['index'] = index

        # Raw extraction
        param['float'] = data_type.strip() == 'float'
        param['start'] = float(value)
        param['value'] = float(value)
        param['min'  ] = float(minimum)
        param['max'  ] = float(maximum)
        param['c_end'] = float(c_end)
        param['r_end'] = float(r_end)

        # Verbatim Fishtest logic for computing these
        param['c']     = param['c_end'] * spsa['iterations'] ** spsa['Gamma']
        param['a_end'] = param['r_end'] * param['c_end'] ** 2
        param['a']     = param['a_end'] * (spsa['A'] + spsa['iterations']) ** spsa['Alpha']

        spsa['parameters'][name] = param

    return spsa

def get_engine(source, name, sha, bench):

    engine = Engine.objects.filter(name=name, source=source, sha=sha, bench=bench)
    if engine.first() != None:
        return engine.first()

    return Engine.objects.create(name=name, source=source, sha=sha, bench=bench)
