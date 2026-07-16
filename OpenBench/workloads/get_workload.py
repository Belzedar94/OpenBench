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
# >>> get_workload(Machine)
#
# Refer to: https://github.com/AndyGrant/OpenBench/wiki/Workload-Assignment

import math
import random
import re
import sys

import OpenBench.utils

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.datagen import claim_chunk, has_assignable_chunk, is_generic_datagen
from OpenBench.models import Result, Test

from django.db import transaction

def get_workload(request, machine):

    # Select a workload from the possible ones, if we can
    if not (test := select_workload(request, machine)):
        return {}

    chunk = claim_chunk(test, machine) if is_generic_datagen(test) else None
    if is_generic_datagen(test) and chunk is None:
        return {}

    # Avoid creating duplicate Result objects
    result, created = Result.objects.get_or_create(test=test, machine=machine)

    # Update the Machine's status and save everything
    machine.workload = test.id;
    machine.mnps = machine.dev_mnps = machine.base_mnps = 0.00
    machine.save(); result.save()

    return { 'workload' : workload_to_dictionary(test, result, machine, chunk) }

def select_workload(request, machine):

    # Step 1: Refine active workloads to the candidate assignments
    candidates, has_focus = filter_valid_workloads(request, machine)
    if not candidates:
        return None

    # Step 2: Count relevant threads on each candidate test
    worker_dist, engine_freq = compute_resource_distribution(candidates, machine, has_focus)

    # Step 3: Determine the effective-throughput for each workload
    if OPENBENCH_CONFIG['balance_engine_throughputs']:
        for id, data in worker_dist.items():
            data['throughput'] = data['throughput'] / engine_freq[data['engine']]

    # Step 4: Compute the Resource Ratios for each of the workloads, if we were assigned
    for id, data in worker_dist.items():
        data['ratio'] = (data['threads'] + machine.info['concurrency']) / data['throughput']

    # Step 5: Compute the idealized "Fair-Ratio" once our machine is added
    min_ratio      = min(x['ratio'] for x in worker_dist.values())
    thread_sum     = sum(x['threads'] for x in worker_dist.values()) + machine.info['concurrency']
    throughput_sum = sum(x['throughput'] for x in worker_dist.values())
    fair_ratio     = thread_sum / throughput_sum

    # Step 6: Repeat the same machine, if we are still within +- 25% fairness
    if machine.workload in worker_dist.keys():
        this_ratio = worker_dist[machine.workload]['ratio']
        if min_ratio / fair_ratio > 0.75 and this_ratio / fair_ratio < 1.25:
            return Test.objects.get(id=machine.workload)

    # Step 7: Pick a random test, amongst those who share the min_ratio, weighted by throughput
    choices = [id for id, data in worker_dist.items() if data['ratio'] == min_ratio]
    weights = [data['throughput'] for id, data in worker_dist.items() if data['ratio'] == min_ratio]
    return Test.objects.get(id=random.choices(choices, weights=weights)[0])

def filter_valid_workloads(request, machine):

    workloads = OpenBench.utils.get_active_tests()

    # Skip engines that the Machine cannot handle
    for engine in OPENBENCH_CONFIG['engines'].keys():
        if engine not in machine.info['supported']:
            workloads = workloads.exclude(dev_engine=engine)
            workloads = workloads.exclude(base_engine=engine)

    # Skip workloads that are blacklisted on the machine
    if blacklisted := request.POST.getlist('blacklist'):
        workloads = workloads.exclude(id__in=blacklisted)

    # Skip workloads that we have insufficient threads to play
    options = [
        x for x in workloads
        if valid_tablebase_assignment(x, machine)
        and valid_hardware_assignment(x, machine)
        and has_assignable_chunk(x)
    ]

    # Possible that no work exists for the machine
    if not options:
        return [], False

    # Refine to workloads of the highest priority
    priorities = [x.priority for x in options]
    candidates = [x for x in options if x.priority == max(priorities)]

    # Refine to workloads that match our focus, if applicable
    focuses    = machine.info.get('focus', [])
    has_focus  = any(x.dev_engine in focuses for x in candidates)

    if has_focus:
        candidates = list(filter(lambda x: x.dev_engine in focuses, candidates))

    return candidates, has_focus

def required_tablebase_size(setting):

    if setting in ['DISABLED', 'OPTIONAL']:
        return 0
    return int(setting.split('-')[0])

def machine_tablebase_max(machine, family):

    tablebases = machine.info.get('tablebases', {})
    if family in tablebases:
        capability = tablebases[family]
        return int(capability.get('max', 0) if isinstance(capability, dict)
                   else capability)
    if family == 'standard':
        return int(machine.info.get('syzygy_max', 0))
    return 0

def engine_tablebase_family(engine):

    return OPENBENCH_CONFIG['engines'][engine].get(
        'tablebase_family', 'standard'
    )

def machine_tablebase_manifest(machine, family):

    capability = machine.info.get('tablebases', {}).get(family, {})
    if not isinstance(capability, dict):
        return None
    value = capability.get('manifest_sha256')
    return value.lower() if isinstance(value, str) else None

def valid_tablebase_assignment(workload, machine):

    workload_families = {
        engine_tablebase_family(workload.dev_engine),
        engine_tablebase_family(workload.base_engine),
    }

    # cutechess-ob adjudication consumes orthodox .rtbw files. Never allow it
    # on an Atomic workload, even if the worker also has standard tables.
    if workload.syzygy_adj != 'DISABLED' and workload_families != {'standard'}:
        return False

    probe_size = required_tablebase_size(workload.syzygy_wdl)
    if probe_size:
        if any(machine_tablebase_max(machine, family) < probe_size
               for family in workload_families):
            return False

    # OPTIONAL may still enable every table found on the worker. A configured
    # corpus pin therefore applies whenever probing is allowed and this worker
    # actually advertises tables, not only to an explicit N-MAN requirement.
    if workload.syzygy_wdl != 'DISABLED':
        for engine in [workload.dev_engine, workload.base_engine]:
            engine_config = OPENBENCH_CONFIG['engines'][engine]
            expected = engine_config.get('tablebase_manifest_sha256')
            family = engine_tablebase_family(engine)
            if (expected and machine_tablebase_max(machine, family) > 0
                    and machine_tablebase_manifest(
                        machine, family) != expected.lower()):
                return False

    adjudication_size = required_tablebase_size(workload.syzygy_adj)
    if adjudication_size:
        if machine_tablebase_max(machine, 'standard') < adjudication_size:
            return False

    return True

def valid_hardware_assignment(workload, machine):

    # Generic DATAGEN explicitly receives all threads exposed by this worker;
    # it does not derive resource needs from chess-game engine options.
    if is_generic_datagen(workload):
        return machine.info['concurrency'] > 0

    # Extract thread requirements from the workload itself
    dev_threads  = int(OpenBench.utils.extract_option(workload.dev_options,  'Threads'))
    base_threads = int(OpenBench.utils.extract_option(workload.base_options, 'Threads'))

    # Extract the information from our machine
    threads      = machine.info['concurrency']
    hyperthreads = machine.info['physical_cores'] < threads

    # For core-odds tests, disable hyperthreads, by halving the thread count
    if hyperthreads and dev_threads != base_threads:
        threads = threads // 2

    # SPSA plays a pair at a time, not a game at a time
    is_spsa = workload.test_mode == 'SPSA'

    # Refuse if there are not enough threads for the test
    if (1 + is_spsa) * max(dev_threads, base_threads) > threads:
        return False

    # All Criteria have been met
    return True

def compute_resource_distribution(workloads, machine, has_focus):

    # Return a thread count, and engine name for each workload, as well as the throughput.
    # The throughput may be scaled down later, due to balance_engine_throughputs

    worker_dist = {
        workload.id : { 'threads' : 0, 'engine' : workload.dev_engine, 'throughput' : workload.throughput }
            for workload in workloads
    }

    # Ignore our own machine;
    # Ignore machines working on non-candidates;
    # Ignore focus-assigned machines when has_focus is false

    for x in OpenBench.utils.getRecentMachines():
        if x != machine and x.workload in worker_dist:
            if has_focus or worker_dist[x.workload]['engine'] not in x.info.get('focus', []):
                worker_dist[x.workload]['threads'] += x.info['concurrency']

    # Count of tests that exist for a particular dev_engine

    engine_freq = {}
    for workload in workloads:
        engine_freq[workload.dev_engine] = engine_freq.get(workload.dev_engine, 0) + 1

    return worker_dist, engine_freq

def workload_to_dictionary(test, result, machine, datagen_chunk=None):

    workload = {}

    workload['result'] = {
        'id'  : result.id,
    }

    workload['test'] = {
        'id'            : test.id,
        'type'          : test.test_mode,
        'syzygy_wdl'    : test.syzygy_wdl,
        'syzygy_adj'    : test.syzygy_adj,
        'win_adj'       : test.win_adj,
        'draw_adj'      : test.draw_adj,
        'workload_size' : test.workload_size,
        'upload_pgns'   : test.upload_pgns,
        'genfens_args'  : test.genfens_args,
        'play_reverses' : test.play_reverses,
    }

    book_config = OPENBENCH_CONFIG['books'].get(
        test.book_name, {'sha': None, 'source': None}
    )
    workload['test']['book'] = {
        'name'   : test.book_name,
        # ``sha`` preserves OpenBench's historical UTF-8/text-normalized
        # identity. DATAGEN generators consume the exact extracted bytes, so
        # books with CRLF content may also publish an explicit raw identity.
        'sha'     : book_config['sha'],
        'raw_sha' : book_config.get('raw_sha'),
        'source'  : book_config['source'],
    }

    workload['test']['dev'] = {
        'id'           : test.dev.id,
        'name'         : test.dev.name,
        'source'       : test.dev.source,
        'sha'          : test.dev.sha,
        'bench'        : test.dev.bench,
        'engine'       : test.dev_engine,
        'options'      : test.dev_options,
        'network'      : test.dev_network,
        'netname'      : test.dev_netname,
        'time_control' : test.dev_time_control,
        'nps'          : OPENBENCH_CONFIG['engines'][test.dev_engine]['nps'],
        'build'        : OPENBENCH_CONFIG['engines'][test.dev_engine]['build'],
        'private'      : OPENBENCH_CONFIG['engines'][test.dev_engine]['private'],
        'cutechess_launch_stagger_ms' : OPENBENCH_CONFIG['engines'][test.dev_engine].get(
            'cutechess_launch_stagger_ms', 0
        ),
        'tablebase_family' : engine_tablebase_family(test.dev_engine),
    }

    workload['test']['base'] = {
        'id'           : test.base.id,
        'name'         : test.base.name,
        'source'       : test.base.source,
        'sha'          : test.base.sha,
        'bench'        : test.base.bench,
        'engine'       : test.base_engine,
        'options'      : test.base_options,
        'network'      : test.base_network,
        'netname'      : test.base_netname,
        'time_control' : test.base_time_control,
        'nps'          : OPENBENCH_CONFIG['engines'][test.base_engine]['nps'],
        'build'        : OPENBENCH_CONFIG['engines'][test.base_engine]['build'],
        'private'      : OPENBENCH_CONFIG['engines'][test.base_engine]['private'],
        'cutechess_launch_stagger_ms' : OPENBENCH_CONFIG['engines'][test.base_engine].get(
            'cutechess_launch_stagger_ms', 0
        ),
        'tablebase_family' : engine_tablebase_family(test.base_engine),
    }

    if is_generic_datagen(test):
        assert datagen_chunk is not None
        workload['test']['datagen'] = {
            'command'             : test.datagen_command,
            'total_count'         : test.datagen_total_count,
            'positions_per_chunk' : test.datagen_positions_per_chunk,
            'base_seed'           : test.datagen_base_seed,
            'chunk_idx'           : datagen_chunk.idx,
            'chunk_count'         : datagen_chunk.position_count,
            'seed'                : test.datagen_base_seed + datagen_chunk.idx,
            'attempt'             : datagen_chunk.attempts,
            'producer_artifact_required': test.datagen_requires_producer_artifact(),
        }
        workload['distribution'] = None
        workload['spsa'] = None
        workload['reporting_type'] = None
        return workload

    workload['distribution']   = game_distribution(test, machine)
    workload['spsa']           = spsa_to_dictionary(test, workload)
    workload['reporting_type'] = test.spsa.get('reporting_type', 'BATCHED')

    with transaction.atomic():

        test = Test.objects.select_for_update().get(id=test.id)
        workload['test']['book_seed' ] = test.id
        workload['test']['book_index'] = test.book_index

        cutechess_cnt = workload['distribution']['cutechess-count']
        pairs_per_cnt = workload['distribution']['games-per-cutechess'] // 2
        per_opening   = 2 if (test.test_mode == 'DATAGEN' and not test.play_reverses) else 1

        test.book_index += cutechess_cnt * pairs_per_cnt * per_opening

        if test.test_mode == 'DATAGEN':
            workload['test']['genfens_seeds'] = [
                random.randint(0, 2**31 - 1) for x in range(machine.info['concurrency'])]

        test.save()

    return workload

def spsa_to_dictionary(test, workload):

    if test.test_mode != 'SPSA':
        return None

    # Only use one set of parameters if distribution is SINGLE.
    # Duplicate the params, even though they are the same, across all
    # Sockets on the machine, in the event of a singular SPSA distribution
    is_single    = test.spsa['distribution_type'] == 'SINGLE'
    permutations = 1 if is_single else workload['distribution']['cutechess-count']
    duplicates   = 1 if not is_single else workload['distribution']['cutechess-count']

    # C & R are scaled over the course of the iterations
    iteration     = 1 + (test.games / (test.spsa['pairs_per'] * 2))
    c_compression = iteration ** test.spsa['Gamma']
    r_compression = (test.spsa['A'] + iteration) ** test.spsa['Alpha']

    spsa = {}
    for name, param in test.spsa['parameters'].items():

        spsa[name] = {
            'dev'  : [], # One for each Permutation the Worker will run
            'base' : [], # One for each Permutation the Worker will run
            'flip' : [], # One for each Permutation the Worker will run
        }

        # C & R are constants for a particular assignment, for all Permutations
        spsa[name]['c'] = max(param['c'] / c_compression, 0.00 if param['float'] else 0.50)
        spsa[name]['r'] = param['a'] / r_compression / spsa[name]['c'] ** 2

        for f in range(permutations):

            # Adjust current best by +- C
            flip = 1 if random.getrandbits(1) else -1
            dev  = param['value'] + flip * spsa[name]['c']
            base = param['value'] - flip * spsa[name]['c']

            # Probabilistic rounding for Integer types
            if not param['float']:
                r    = random.uniform(0, 1)
                dev  = math.floor(dev  + r)
                base = math.floor(base + r)

            # Clip within [Min, Max]
            dev  = max(param['min'], min(param['max'], dev ))
            base = max(param['min'], min(param['max'], base))

            # Round integer values down
            if not param['float']:
                dev  = int(dev )
                base = int(base)

            # Append each permutation
            for g in range(duplicates):
                spsa[name]['dev' ].append(dev)
                spsa[name]['base'].append(base)
                spsa[name]['flip'].append(flip)


    return spsa

def extract_option(options, option):

    if (match := re.search('(?<=%s=")[^"]*' % (option), options)):
        return match.group()

    if (match := re.search('(?<=%s=\')[^\']*' % (option), options)):
        return match.group()

    if (match := re.search('(?<=%s=)[^ ]*' % (option), options)):
        return match.group()

def game_distribution(test, machine):

    dev_threads  = int(extract_option(test.dev_options, 'Threads'))
    base_threads = int(extract_option(test.base_options, 'Threads'))

    worker_threads = machine.info['concurrency']
    worker_sockets = machine.info['sockets']

    # For core-odds tests, disable hyperthreads, by halving the thread count
    if machine.info['physical_cores'] < worker_threads and dev_threads != base_threads:
        worker_threads = worker_threads // 2

    # Ignore sockets for concurrent cutechess, when playing with more than one thread
    if max(dev_threads, base_threads) > 1:
        worker_sockets = 1

    # Max possible concurrent engine games, per copy of cutechess
    max_concurrency = (worker_threads // worker_sockets) // max(dev_threads, base_threads)

    # Number of params being evaluated at a single time, if doing SPSA in SINGLE mode
    spsa_count = (worker_threads // max(dev_threads, base_threads)) // 2

    # SPSA is treated specially, if we are distributing many parameter sets at once
    is_multiple_spsa = (
        test.test_mode == 'SPSA'
        and test.spsa['distribution_type'] == 'MULTIPLE'
    )

    # Some engines initialize slowly enough that one large Cutechess copy can
    # hit the protocol-start timeout while launching every game at once. Split
    # that per-socket concurrency into equal smaller copies. Choosing a divisor
    # preserves the exact total worker concurrency and game/book accounting.
    concurrency_per = max_concurrency
    copies_per_socket = 1
    if not is_multiple_spsa:
        configured_limits = [
            OPENBENCH_CONFIG['engines'][engine].get(
                'cutechess_max_concurrency', 0
            )
            for engine in [test.dev_engine, test.base_engine]
        ]
        configured_limits = [limit for limit in configured_limits if limit]
        if configured_limits and max_concurrency:
            limit = min(min(configured_limits), max_concurrency)
            concurrency_per = max(
                divisor for divisor in range(1, limit + 1)
                if max_concurrency % divisor == 0
            )
            copies_per_socket = max_concurrency // concurrency_per

    return {
        'cutechess-count'     : spsa_count if is_multiple_spsa
                                else worker_sockets * copies_per_socket,
        'concurrency-per'     : 2 if is_multiple_spsa else concurrency_per,
        'games-per-cutechess' : 2 * test.workload_size
                                * (1 if is_multiple_spsa else concurrency_per),
    }
