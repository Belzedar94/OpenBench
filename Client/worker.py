# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                           #
#   OpenBench is a chess engine testing framework by Andrew Grant.          #
#   <https://github.com/AndyGrant/OpenBench>  <andrew@grantnet.us>          #
#                                                                           #
#   OpenBench is free software: you can redistribute it and/or modify       #
#   it under the terms of the GNU General Public License as published by    #
#   the Free Software Foundation, either version 3 of the License, or       #
#   (at your option) any later version.                                     #
#                                                                           #
#   OpenBench is distributed in the hope that it will be useful,            #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#   GNU General Public License for more details.                            #
#                                                                           #
#   You should have received a copy of the GNU General Public License       #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.   #
#                                                                           #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

import argparse
import bz2
from collections import deque
import cpuinfo
import glob
import hashlib
import importlib
import inspect
import json
import multiprocessing
import os
import platform
import psutil
import queue
import re
import requests
import shutil
import stat
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile

from subprocess import PIPE, Popen, call, STDOUT
from itertools import combinations_with_replacement
from concurrent.futures import ThreadPoolExecutor, wait

## Local imports

import bench
import genfens as client_genfens
import pgn_util as client_pgn_util
import utils as client_utils

# ``client.py`` is intentionally not replaced by the hot updater.  Older
# bootstraps therefore reload this module after copying the new Client files,
# while already-imported dependency modules would otherwise remain stale in
# ``sys.modules``.  Reload them in dependency order before importing their
# public helpers so a worker and its support code always come from the same
# downloaded client version.
client_utils = importlib.reload(client_utils)
bench = importlib.reload(bench)
client_genfens = importlib.reload(client_genfens)
client_pgn_util = importlib.reload(client_pgn_util)

from client import BadVersionException
from client import url_join
from client import try_forever

from utils import *
from pgn_util import compress_list_of_pgns
from genfens import create_genfens_opening_book

## Basic configuration of the Client. These timeouts can be changed at will

CLIENT_VERSION   = 41 # Client version to send to the Server
# 90s rides out shared-sqlite write-lock waits on the server (AtomicDB batch
# jobs hold multi-second transactions; heartbeats were dying at 30s while the
# server would have answered shortly after).
TIMEOUT_HTTP     = 90 # Timeout in seconds for HTTP requests
TIMEOUT_ERROR    = 10 # Timeout in seconds when any errors are thrown
TIMEOUT_WORKLOAD = 30 # Timeout in seconds between workload requests
REPORT_INTERVAL  = 30 # Seconds between reports to the Server
# Six attempts with 45s spacing ride out flaky residential-upstream windows
# during ~180MB chunk uploads; the heartbeat thread keeps the lease fresh
# throughout, so patience here is free (observed 3x SSLWantWriteError bursts
# on 2026-07-23 that a 3x5s policy could not survive).
DATAGEN_TRANSFER_RETRIES = 6
DATAGEN_TRANSFER_RETRY_DELAY = 45
ATOMIC_DATAGEN_TABLEBASE_MIN = 3
ATOMIC_DATAGEN_TABLEBASE_MAX = 6

IS_WINDOWS = platform.system() == 'Windows' # Don't touch this
IS_LINUX   = platform.system() != 'Windows' # Don't touch this


def download_opening_book_compatible(
    book_sha, book_source, book_name, book_raw_sha=None
):
    """Download a book across an in-process Client hot update.

    Version 37 added the fourth raw-SHA argument.  Reloading ``utils`` above is
    the durable fix, while this narrow adapter lets a process recover if an
    embedded/legacy bootstrap prevents that reload.  Signature inspection is
    deliberate: catching ``TypeError`` from the call would also hide a real
    error raised inside a modern four-argument implementation.
    """

    try:
        parameters = list(
            inspect.signature(download_opening_book).parameters.values()
        )
    except (TypeError, ValueError):
        parameters = ()

    positional_parameters = [
        parameter for parameter in parameters
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    supports_positional_raw_sha = not parameters or any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in parameters
    ) or len(positional_parameters) >= 4

    if supports_positional_raw_sha:
        return download_opening_book(
            book_sha, book_source, book_name, book_raw_sha
        )

    supports_keyword_raw_sha = any(
        parameter.name == 'book_raw_sha'
        or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    if supports_keyword_raw_sha:
        return download_opening_book(
            book_sha,
            book_source,
            book_name,
            book_raw_sha=book_raw_sha,
        )

    print('[Note] Legacy opening-book helper detected after Client hot update.')
    result = download_opening_book(book_sha, book_source, book_name)

    # Preserve the v37 raw-byte contract even when adapting a legacy helper.
    # The old function has already verified the normalized identity here.
    if book_raw_sha and book_name.upper() != 'NONE':
        book_path = os.path.join('Books', book_name)
        with open(book_path, 'rb') as book_file:
            raw_sha256 = hashlib.sha256(book_file.read()).hexdigest()
        print('Correct Raw  %s' % book_raw_sha.upper())
        print('Download Raw %s\n' % raw_sha256.upper())
        if book_raw_sha.upper() != raw_sha256.upper():
            os.remove(book_path)
            raise OpenBenchCorruptedBookException(
                'Invalid raw sha for %s' % book_name
            )

    return result


class Configuration:

    ## Handles configuring the worker with the server. This means collecting
    ## information about the system, as well as holding any of the command line
    ## arguments provided. Lastly, a Configuration() object holds the Workload

    def __init__(self, args):

        # Basic init of every piece of System specific information
        self.compilers      = {}
        self.git_tokens     = {}
        self.cpu_flags      = []
        self.cpu_name       = ''
        self.os_name        = platform.system()
        self.os_ver         = platform.release()
        self.python_ver     = platform.python_version()
        self.mac_address    = hex(uuid.getnode()).upper()[2:]
        self.logical_cores  = psutil.cpu_count(logical=True)
        self.physical_cores = psutil.cpu_count(logical=False)
        self.ram_total_mb   = psutil.virtual_memory().total // (1024 ** 2)
        self.machine_name   = 'None'
        self.machine_id     = 'None'
        self.secret_token   = 'None'
        self.syzygy_max     = 2
        self.atomic_syzygy_max = 2
        self.blacklist      = []

        self.process_args(args) # Rest of the command line settings
        self.init_client()      # Create folder structure and verify Syzygy
        self.validate_setup()   # Check the threads and sockets values provided

    def process_args(self, args):

        # Extract all of the options
        self.username    = args.username
        self.password    = args.password
        self.server      = args.server
        self.threads     = int(args.threads)
        self.sockets     = int(args.nsockets)
        self.identity    = args.identity if args.identity else 'None'
        self.syzygy_path = args.syzygy   if args.syzygy   else None
        self.atomic_syzygy_path = (
            args.atomic_syzygy if args.atomic_syzygy else None
        )
        self.atomic_syzygy_manifest = (
            args.atomic_syzygy_manifest if args.atomic_syzygy_manifest else None
        )
        self.atomic_syzygy_manifest_sha256 = None
        if self.atomic_syzygy_manifest and not self.atomic_syzygy_path:
            raise ValueError('--atomic-syzygy-manifest requires --atomic-syzygy')
        if self.atomic_syzygy_path and not self.atomic_syzygy_manifest:
            raise ValueError('--atomic-syzygy requires --atomic-syzygy-manifest')
        self.fleet       = args.fleet    if args.fleet    else False
        self.focus       = args.focus    if args.focus    else []

    def init_client(self):

        # Verify that we have make installed
        print('\nLooking for Make... [v%s]' % locate_utility('make'))

        # Use Client.py's path as the base pathway
        os.chdir(os.path.dirname(os.path.abspath(__file__)))

        # Ensure the folder structure for ease of coding
        for folder in ['PGNs', 'Engines', 'Networks', 'Books', 'Datagen']:
            if not os.path.isdir(folder):
                os.mkdir(folder)

        # Check until we stop finding valid N-man tables
        if self.syzygy_path:
            while validate_syzygy_exists(
                self.syzygy_path, self.syzygy_max + 1, '.rtbw'
            ):
                self.syzygy_max = self.syzygy_max + 1

        if self.atomic_syzygy_path:
            while validate_syzygy_exists(
                self.atomic_syzygy_path,
                self.atomic_syzygy_max + 1,
                ('.atbw', '.atbz'),
            ):
                self.atomic_syzygy_max += 1
            if self.atomic_syzygy_manifest:
                self.atomic_syzygy_manifest_sha256 = validate_tablebase_inventory(
                    self.atomic_syzygy_path,
                    self.atomic_syzygy_manifest,
                    ('.atbw', '.atbz'),
                )

        # 1-man and 2-man tables are not a thing
        if self.syzygy_max < 3:
            self.syzygy_max = 0
        if self.atomic_syzygy_max < 3:
            self.atomic_syzygy_max = 0

        # Report highest complete depth that we found
        print('Looking for Syzygy... [%d-Man]' % (self.syzygy_max))
        print('Looking for Atomic Syzygy... [%d-Man]' % (self.atomic_syzygy_max))

    def validate_setup(self):

        assert self.threads >= self.sockets
        assert self.threads % self.sockets == 0
        assert min(self.threads, self.sockets) >= 1

    def scan_for_compilers(self, data):

        print ('\nScanning for Compilers...')

        # For each engine, attempt to find a valid compiler
        for engine, build_info in data.items():

            # Local patch: this worker's Atomic engines stall under load and
            # kill the whole client (see Spell-Stockfish AUDIT 2026-07-14).
            # With OB_SKIP_ATOMIC set we do not advertise Atomic compilers,
            # so the server never assigns Atomic workloads to this machine.
            if os.environ.get('OB_SKIP_ATOMIC') and 'Atomic' in engine: continue

            # Private engines don't need to be compiled
            if build_info['private']: continue

            # Try to find at least one working compiler
            for compiler in build_info['compilers']:

                # Compilers may require a specific version
                if '>=' in compiler:
                    compiler, version = compiler.split('>=')
                    version = tuple(map(int, version.split('.')))
                else: version = (0, 0, 0)

                # Try to confirm this compiler is present, and new enough
                try:
                    match = get_version(compiler)
                    if tuple(map(int, match.split('.'))) >= version:
                        print('%-16s | %-8s (%s)' % (engine, compiler, match))
                        self.compilers[engine] = (compiler, match)
                        break
                except: continue # Unable to execute compiler

            # Report missing engines in case the User is not expecting it
            if engine not in self.compilers:
                print('%-16s | Missing %s' % (engine, data[engine]['compilers']))

    def scan_for_private_tokens(self, data):

        print ('\nScanning for Private Tokens...')

        # For each engine, attempt to find a valid compiler
        for engine, build_info in data.items():

            # Public engines don't need access tokens
            if not build_info['private']: continue

            # Private engines expect a credentials.engine file for the main repo
            has_token = os.path.exists('credentials.%s' % (engine.replace(' ', '').lower()))
            print('%-16s | %s' % (engine, ['Missing', 'Found'][has_token]))
            if has_token: self.git_tokens[engine] = True

    def scan_for_cpu_flags(self, data):

        print('\nScanning for CPU Flags...')

        # Get all flags, and for sanity uppercase them
        info   = cpuinfo.get_cpu_info()
        actual = [x.replace("_", "").replace(".", "").upper() for x in info.get('flags', [])]

        # Set the CPU name which has to be done via global
        self.cpu_name = info.get('brand_raw', info.get('brand', 'Unknown'))

        # This should cover virtually all compiler flags that we would care about
        desired  = ['POPCNT', 'BMI2']
        desired += ['SSSE3', 'SSE41', 'SSE42', 'SSE4A', 'AVX', 'AVX2', 'FMA']
        desired += ['AVX512VNNI', 'AVX512BW', 'AVX512DQ', 'AVX512F']

        # Add any custom flags from the OpenBench configs, just in case we missed one
        requested = set(sum([info['cpuflags'] for engine, info in data.items()], []))
        for flag in [x for x in requested if x not in desired]: desired.append(flag)
        self.cpu_flags = [x for x in desired if x in actual]

        # Report the results of our search, including any "missing flags
        print ('Found   |', ' '.join(self.cpu_flags))
        print ('Missing |', ' '.join([x for x in desired if x not in actual]))

    def scan_for_machine_id(self):

        if os.path.isfile('machine.txt'):
            with open('machine.txt') as fin:
                for line in fin.readlines():
                    self.machine_id = line.rstrip(); break

class ServerReporter:

    ## Handles reporting things to the server, which are not intended to send a great
    ## deal of information back. Reports to the server can hit various endpoints, with
    ## differing payloads. Payloads must always include the machine id, and secret token

    @staticmethod
    def report(config, endpoint, payload, files=None):

        datagen = (config.workload or {}).get('test', {}).get('datagen')
        if datagen:
            payload.setdefault('chunk_idx', datagen['chunk_idx'])
            payload.setdefault('attempt', datagen['attempt'])

        payload['machine_id'] = config.machine_id
        payload['secret']     = config.secret_token

        target   = url_join(config.server, endpoint)
        response = requests.post(target, data=payload, files=files, timeout=TIMEOUT_HTTP)

        # Check for a json repsone, to look for Client Version Errors
        try: as_json = response.json()
        except: return response

        # Throw all the way back to the client.py
        if 'Bad Client Version' in as_json.get('error', ''):
            raise BadVersionException()

        return response

    @staticmethod
    def report_nps(config, dev_nps, base_nps):

        payload = {
            'nps'      : (dev_nps + base_nps) // 2,
            'dev_nps'  : int(dev_nps),
            'base_nps' : int(base_nps),
        }

        return ServerReporter.report(config, 'clientSubmitNPS', payload)

    @staticmethod
    def report_missing_artifact(config, artifact_name, artifact_json):

        payload = {
            'test_id'    : config.workload['test']['id'],
            'error'      : 'Artifact %s missing' % (artifact_name),
            'logs'       : json.dumps(artifact_json, indent=2),
        }

        return ServerReporter.report(config, 'clientSubmitError', payload)

    @staticmethod
    def report_build_fail(config, branch, output):

        branch_name = config.workload['test'][branch]['name']
        engine_name = config.workload['test'][branch]['engine']
        final_name  = '[%s] %s' % (engine_name, branch_name)

        payload = {
            'test_id'    : config.workload['test']['id'],
            'error'      : '%s build failed' % (final_name),
            'logs'       : output,
        }

        return ServerReporter.report(config, 'clientSubmitError', payload)

    @staticmethod
    def report_engine_error(config, error, pgn=None):

        payload = {
            'test_id'    : config.workload['test']['id'],
            'error'      : error,
            'logs'       : pgn or '',
        }

        return ServerReporter.report(config, 'clientSubmitError', payload)

    @staticmethod
    def report_bad_bench(config, error):

        payload = {
            'test_id'    : config.workload['test']['id'],
            'error'      : error,
        }

        return ServerReporter.report(config, 'clientBenchError', payload)

    @staticmethod
    def report_results(config, batches):

        payload = {

            'test_id'      : config.workload['test']['id'],
            'result_id'    : config.workload['result']['id'],

            'trinomial'    : [0, 0, 0],       # LDW
            'pentanomial'  : [0, 0, 0, 0, 0], # LL DL DD DW WW

            'crashes'      : 0, # " disconnect" or "connection stalls"
            'timelosses'   : 0, # " loses on time "
            'illegals'     : 0, # " illegal move "
        }

        for batch in batches:

            payload['trinomial'  ] = [x+y for x,y in zip(payload['trinomial'  ], batch['trinomial'  ])]
            payload['pentanomial'] = [x+y for x,y in zip(payload['pentanomial'], batch['pentanomial'])]

            payload['crashes'   ] += batch['crashes'   ]
            payload['timelosses'] += batch['timelosses']
            payload['illegals'  ] += batch['illegals'  ]

            if config.workload['test']['type'] == 'SPSA':

                # Pairs can be added one at a time, or in bulk
                result = batch['trinomial'][2] - batch['trinomial'][0]

                # For each param compute the update step for the Server
                for name, param in config.workload['spsa'].items():
                    delta = param['r'] * param['c'] * result * param['flip'][batch['cutechess_idx']]
                    payload['spsa_%s' % (name)] = payload.get('spsa_%s' % (name), 0.0) + delta

        # Collapse into a JSON friendly format for Django
        payload['trinomial'  ] = ' '.join(map(str, payload['trinomial'  ]))
        payload['pentanomial'] = ' '.join(map(str, payload['pentanomial']))

        print (payload)

        return ServerReporter.report(config, 'clientSubmitResults', payload)

    @staticmethod
    def report_heartbeat(config):

        payload = {
            'test_id' : config.workload['test']['id']
        }

        return ServerReporter.report(config, 'clientHeartbeat', payload)

    @staticmethod
    def report_pgn(config, compressed_pgn_text):

        payload = {
            'test_id'      : config.workload['test']['id'],
            'result_id'    : config.workload['result']['id'],
            'book_index'   : config.workload['test']['book_index'],
            'Content-Type' : 'application/octet-stream',
        }

        files = {
            'file' : ('games.pgn', compressed_pgn_text)
        }

        return ServerReporter.report(config, 'clientSubmitPGN', payload, files)

    @staticmethod
    def report_datagen_producer(
        config, path, sha256, byte_count, commit, metadata_only=False
    ):

        payload = {
            'test_id': config.workload['test']['id'],
            'chunk_idx': config.workload['test']['datagen']['chunk_idx'],
            'attempt': config.workload['test']['datagen']['attempt'],
            'sha256': sha256,
            'bytes': byte_count,
            'commit': commit,
            'metadata_only': int(metadata_only),
        }

        target = url_join(config.server, 'clientSubmitDatagenProducer')
        payload['machine_id'] = config.machine_id
        payload['secret'] = config.secret_token

        if metadata_only:
            response = requests.post(
                target,
                data=payload,
                timeout=TIMEOUT_HTTP,
            )
        else:
            with open(path, 'rb') as data:
                response = requests.post(
                    target,
                    data=payload,
                    files={
                        'file': (
                            os.path.basename(path), data, 'application/octet-stream'
                        )
                    },
                    timeout=(TIMEOUT_HTTP, 600),
                )

        try:
            body = response.json()
        except Exception:
            response.raise_for_status()
            raise RuntimeError(
                'DATAGEN producer upload returned a non-JSON response'
            )

        if 'Bad Client Version' in body.get('error', ''):
            raise BadVersionException()
        if response.status_code != 200 or 'error' in body:
            raise RuntimeError(
                body.get('error', 'DATAGEN producer upload failed')
            )
        return body

    @staticmethod
    def report_datagen(
        config, path, sha256, byte_count, producer=None, tablebase=None
    ):

        payload = {
            'test_id': config.workload['test']['id'],
            'chunk_idx': config.workload['test']['datagen']['chunk_idx'],
            'attempt': config.workload['test']['datagen']['attempt'],
            'sha256': sha256,
            'bytes': byte_count,
        }
        if producer is not None:
            payload.update({
                'producer_sha256': producer['sha256'],
                'producer_bytes': producer['bytes'],
                'producer_commit': producer['commit'],
            })
        if tablebase is not None:
            payload.update({
                'environment_contract_sha256': (
                    tablebase['environment_contract_sha256']
                ),
                'environment_lease_sha256': tablebase[
                    'environment_lease_sha256'
                ],
                'tablebase_family': tablebase['family'] or '',
                'tablebase_max': tablebase['required_max'],
                'tablebase_worker_max': tablebase['worker_max'],
                'tablebase_manifest_sha256': (
                    tablebase['manifest_sha256'] or ''
                ),
                'teacher_mode': tablebase['teacher_mode'] or '',
            })
            if tablebase.get('publication_contract_sha256'):
                payload['publication_contract_sha256'] = tablebase[
                    'publication_contract_sha256'
                ]

        target = url_join(config.server, 'clientSubmitDatagen')
        payload['machine_id'] = config.machine_id
        payload['secret'] = config.secret_token

        with open(path, 'rb') as data:
            response = requests.post(
                target,
                data=payload,
                files={'file': (os.path.basename(path), data, 'application/x-bzip2')},
                timeout=(TIMEOUT_HTTP, 600),
            )

        try:
            body = response.json()
        except Exception:
            response.raise_for_status()
            raise RuntimeError('DATAGEN upload returned a non-JSON response')

        if 'Bad Client Version' in body.get('error', ''):
            raise BadVersionException()
        if response.status_code != 200 or 'error' in body:
            raise RuntimeError(body.get('error', 'DATAGEN upload failed'))
        return body


## Variant -> Runner routing table.
##
## The variant is inferred from tokens in the Opening Book name, generalizing
## the old "SHATRANJ in book_name" hack. Each entry maps a token found in the
## book name to a (runner, variant_id) pair:
##
##   'cutechess'       : native cutechess-ob arbitration ( -variant <id> )
##   'uci-pair-runner' : uci_pair_runner.py, a pure-UCI pair runner for
##                       variants that cutechess does not arbitrate. It accepts
##                       the exact same command line flags as cutechess-ob and
##                       emits cutechess-compatible output ("Started game",
##                       "Score of", "Finished game ... {reason}", -pgnout with
##                       Termination headers), so parsing does not change.
##
## Tokens are checked in insertion order; the first match wins. No match means
## standard chess through cutechess, exactly as before.

VARIANTS = {
    'SPELL'    : ('uci-pair-runner', 'spell-chess' ),  # first: wins over FRC/960 in combined names
    'ALICE'    : ('uci-pair-runner', 'alice'       ),
    'SHATRANJ' : ('cutechess'      , 'shatranj'    ),
    'ATOMIC'   : ('cutechess'      , 'atomic'      ),
    'FRC'      : ('cutechess'      , 'fischerandom'),
    '960'      : ('cutechess'      , 'fischerandom'),
    'FISCHER'  : ('cutechess'      , 'fischerandom'),
}

# Fallback when the book name carries no token (DATAGEN runs use book='None';
# genfens builds the openings by calling the engine): route by the dev
# engine's registered variant instead.
ENGINE_VARIANTS = {
    'SPELL-STOCKFISH'                    : ('uci-pair-runner', 'spell-chess'),
    'ALICE-STOCKFISH'                    : ('uci-pair-runner', 'alice'      ),
    'ATOMIC-STOCKFISH'                   : ('cutechess'      , 'atomic'     ),
    'FAIRY-STOCKFISH-ATOMIC-BASELINE'    : ('cutechess'      , 'atomic'     ),
}

# Pair-runner script distributed alongside worker.py, inside Client/. The
# worker os.chdir()s to the Client/ directory on startup, so a bare relative
# name is always resolvable here.
UCI_PAIR_RUNNER = 'uci_pair_runner.py'

def variant_routing(config):

    # Returns (runner, variant_id), inferred from the Opening Book name
    book_name = config.workload['test']['book']['name'].upper()

    for token, (runner, variant_id) in VARIANTS.items():
        if token in book_name:
            return (runner, variant_id)

    # No token in the book name (e.g. DATAGEN's book='None'): fall back to
    # the dev engine's registered variant
    dev_engine = config.workload['test']['dev']['engine'].upper()
    if dev_engine in ENGINE_VARIANTS:
        return ENGINE_VARIANTS[dev_engine]

    return ('cutechess', 'standard')

def runner_base_command(config):

    # Everything cutechess arbitrates natively keeps the original binary
    runner, variant = variant_routing(config)
    if runner == 'cutechess':
        binary = ['cutechess-ob.exe', 'cutechess-ob'][IS_LINUX]
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), binary)
        return '"%s"' % path

    # uci-pair-runner: same flags, cutechess-compatible output. Prefer the
    # interpreter running the worker (same venv). The assembled command still
    # uses quoted tokens only for UCI options, so a spaced executable path must
    # be converted to its 8.3 short form (Windows) or dropped for a bare name.
    python = sys.executable
    if ' ' in python and IS_WINDOWS:
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.kernel32.GetShortPathNameW(python, buf, 260):
                python = buf.value
        except Exception:
            pass
    if ' ' in python:
        python = ['python', 'python3'][IS_LINUX]
        print('WARNING: venv python path contains spaces; falling back to '
              'bare "%s" from PATH for the uci-pair-runner' % python)
    return '%s %s' % (python, UCI_PAIR_RUNNER)


class Cutechess:

    ## Handles building the very long string of arguments that need to be passed
    ## to cutechess in order to launch a set of games. Operates on the Configuration,
    ## and a small number of secondary arguments that are not housed in the Configuration

    @staticmethod
    def basic_settings(config):

        # Variant and runner are both inferred from the Opening Book name,
        # via the VARIANTS routing table ( see variant_routing() )
        runner, variant = variant_routing(config)

        # Only include -repeat if not skipping the reverses in DATAGEN
        is_datagen = config.workload['test']['type'] == 'DATAGEN'
        no_reverse = is_datagen and not config.workload['test']['play_reverses']

        # Always include -recover and -variant
        return ['-repeat', ''][no_reverse] + ' -recover -variant %s' % (variant)

    @staticmethod
    def concurrency_settings(config):

        # Already computed for us by the Server
        return '-concurrency %d -games %d' % (
            config.workload['distribution']['concurrency-per'],
            config.workload['distribution']['games-per-cutechess'],
        )

    @staticmethod
    def launch_stagger_seconds(config, cutechess_idx):

        # The bundled Cutechess launches every concurrent game immediately.
        # Server-side distribution can split a slow-starting engine into
        # smaller copies; stagger those copy launches here. Custom runners
        # retain their historical startup behavior.
        runner, _ = variant_routing(config)
        if runner != 'cutechess':
            return 0.0

        stagger_ms = max(
            config.workload['test'][branch].get(
                'cutechess_launch_stagger_ms', 0
            )
            for branch in ['dev', 'base']
        )
        return cutechess_idx * stagger_ms / 1000.0

    @staticmethod
    def adjudication_settings(config):

        # All three possible adjudication settings
        win_adj    = config.workload['test']['win_adj'   ]
        draw_adj   = config.workload['test']['draw_adj'  ]
        syzygy_adj = config.workload['test']['syzygy_adj']

        # Empty, unless specified in the settings
        win_flags    = ['', '-resign ' + win_adj ][win_adj  != 'None']
        draw_flags   = ['', '-draw '   + draw_adj][draw_adj != 'None']
        syzygy_flags = ''

        # Set the tb path if we have them, and are allowed to use them
        if syzygy_adj != 'DISABLED' and config.syzygy_max:
            syzygy_flags = '-tb %s' % (config.syzygy_path.replace('\\', '\\\\'))

        # We would only get a test we can do; specify a limit if needed
        if syzygy_adj != 'DISABLED' and syzygy_adj != 'OPTIONAL':
            syzygy_flags += ' -tbpieces %s' % (syzygy_adj.split('-')[0])

        return '%s %s %s' % (win_flags, draw_flags, syzygy_flags)

    @staticmethod
    def book_settings(config, cutechess_idx):

        # DATAGEN creates their own book
        if config.workload['test']['type'] == 'DATAGEN':

            # -repeat might not be applied, so handle the book offsets
            no_reverse = not config.workload['test']['play_reverses']
            pairs      = config.workload['distribution']['games-per-cutechess'] // 2
            start      = 1 + (cutechess_idx * pairs * (1 + no_reverse))
            return '-openings file=Books/openbench.genfens.epd format=epd order=sequential start=%d' % (start)

        # Can handle EPD and PGN Books, which must be specified
        book_name   = config.workload['test']['book']['name']
        book_suffix = book_name.split('.')[-1]

        # Start position is determined partially by cutechess index
        pairs = config.workload['distribution']['games-per-cutechess'] // 2
        start = config.workload['test']['book_index'] + cutechess_idx * pairs

        return '-openings file=Books/%s format=%s order=random start=%d -srand %d' % (
            book_name, book_suffix, start, config.workload['test']['book_seed'])

    @staticmethod
    def engine_settings(config, command, branch, scale_factor, cutechess_idx):

        # Extract configuration from the Workload
        options = config.workload['test'][branch]['options']
        network = config.workload['test'][branch]['network']
        private = config.workload['test'][branch]['private']
        engine  = config.workload['test'][branch]['engine']
        syzygy  = config.workload['test']['syzygy_wdl']
        family  = config.workload['test'][branch].get(
            'tablebase_family', 'standard'
        )
        path, maximum = tablebase_capability(config, family)
        required = (
            0 if syzygy in ['DISABLED', 'OPTIONAL']
            else int(syzygy.split('-')[0])
        )
        if required and (not path or maximum < required):
            raise RuntimeError(
                '%s requires %d-man %s tablebases; worker has %d-man'
                % (engine, required, family, maximum)
            )

        # Human-readable name, and scale the time control
        name    = command.replace('.exe', '')
        control = scale_time_control(config.workload, scale_factor, branch)

        # Private engines, when using Networks, must set them via UCI
        if private and network and network != 'None':
            options += ' EvalFile=%s' % (os.path.join('../Networks', network))
            name    += '-%s' % (network)

        # Set the SyzygyPath if we have them, and are allowed to use them
        if syzygy != 'DISABLED' and maximum:
            options += ' SyzygyPath=%s' % (path.replace('\\', '\\\\'))

        # Set a SyzygyProbeLimit if we may only use up-to N-Man
        if (syzygy != 'DISABLED' and syzygy != 'OPTIONAL'
                and not has_uci_option(options, 'SyzygyProbeLimit')):
            options += ' SyzygyProbeLimit=%s' % (syzygy.split('-')[0])

        # Add any of the custom SPSA settings
        if config.workload['test']['type'] == 'SPSA':
            for param, data in config.workload['spsa'].items():
                options += ' %s=%s' % (param, str(data[branch][cutechess_idx]))

        # Join options together in the Cutechess format. Quoted workload
        # tokens allow UCI option names to contain spaces (for example
        # "Use NNUE=true"). Keep the complete option.NAME=VALUE expression
        # quoted until the final argv serialization so it reaches cutechess
        # as one argument.
        option_tokens = re.findall(r'"[^"]*"|\S+', options)
        option_args = []
        for option in option_tokens:
            if len(option) >= 2 and option[0] == option[-1] == '"':
                option = option[1:-1]
            argument = 'option.' + option
            option_args.append(
                '"%s"' % argument if any(c.isspace() for c in argument)
                else argument
            )
        options = (' ' + ' '.join(option_args)) if option_args else ''
        return '-engine dir=Engines/ cmd=./%s proto=uci %s%s name=%s-%s' % (
            command, control, options, engine, branch
        )

    @staticmethod
    def pgnout_settings(config, timestamp, cutechess_idx):
        return '-pgnout %s' % (Cutechess.pgn_name(config, timestamp, cutechess_idx))

    @staticmethod
    def update_results(results, line):

        # Given any game #, find the other in the pair
        def game_to_pair(g):
            return (g, g+1) if g % 2 else (g-1, g)

        # Find the Pentanomial index given a game pair
        def pair_to_penta(r1, r2):
            lookup = { '0-1' : 0, '1/2-1/2' : 1, '1-0' : 2 }
            return lookup[r1] + 2 - lookup[r2]

        # Find the Trinomial indices, from our POV, for a give game pair
        def pair_to_trinomial(r1, r2):
            lookup = { '0-1' : 0, '1/2-1/2' : 1, '1-0' : 2 }
            return lookup[r1], 2 - lookup[r2]

        # Extract the game # and result str from a Cutechess line
        def parse_finished_game(line):
            tokens = line.split()
            return int(tokens[2]), tokens[6]

        # Parse for errors resulting in adjudication
        reason = line.split(':')[1]
        results['crashes'   ] += 'disconnect' in reason or 'stalls' in reason
        results['timelosses'] += 'on time' in reason
        results['illegals'  ] += 'illegal' in reason

        # Parse Game # and result, and save
        game, result = parse_finished_game(line)
        results['games'][game] = result

        # Check to see if the Pair has finished
        first, second = game_to_pair(game)
        if first not in results['games'] or second not in results['games']:
            return

        # Get the indices for the Pentanomial, and the two for Trinomial
        p = pair_to_penta(results['games'][first], results['games'][second])
        t1, t2 = pair_to_trinomial(results['games'][first], results['games'][second])

        # Update everything
        results['trinomial'  ][t1] += 1
        results['trinomial'  ][t2] += 1
        results['pentanomial'][p ] += 1

        # Clean up results['games']
        del results['games'][first]
        del results['games'][second]

    @staticmethod
    def kill_everything(dev_process, base_process):

        if IS_LINUX:
            kill_process_by_name('cutechess-ob')

        if IS_WINDOWS:
            kill_process_by_name('cutechess-ob.exe')

        kill_process_by_name(dev_process)
        kill_process_by_name(base_process)

    @staticmethod
    def pgn_name(config, timestamp, cutechess_idx):

        test_id   = int(config.workload['test']['id'])
        result_id = int(config.workload['result']['id'])

        # Format: <Test>-<Result>-<Time>-<Index>.pgn
        return 'PGNs/%d.%d.%d.%d.pgn' % (test_id, result_id, timestamp, cutechess_idx)


class PGNHelper:

    @staticmethod
    def slice_pgn_file(file):

        with open(file) as pgn:

            while True:

                headers = list(iter(lambda: pgn.readline().rstrip(), ''))
                moves   = list(iter(lambda: pgn.readline().rstrip(), ''))

                if not headers or not moves:
                    break

                yield (headers, moves)

    @staticmethod
    def get_pgn_header(sliced_headers, header):
        for line in sliced_headers:
            if line.startswith('[%s ' % header):
                return line.split('"')[1]

    @staticmethod
    def get_error_reason(sliced_headers):

        reason = PGNHelper.get_pgn_header(sliced_headers, 'Termination')

        if reason and 'abandoned' in reason:
            return 'Disconnect'

        if reason and 'stalled' in reason:
            return 'Stalled'

        if reason and 'illegal' in reason:
            return 'Illegal Move'

    @staticmethod
    def pretty_format(headers, moves):
        return '\n'.join(headers + [''] + moves)

class ResultsReporter(object):

    ## Handles idle looping while reading from the results Queue that the Cutechess
    ## workers place results into. Once finished, this class can be used to collect
    ## all of the errors in the PGN, and send htem back to the server.

    def __init__(self, config, tasks, results_queue, abort_flag):
        self.config        = config
        self.tasks         = tasks
        self.results_queue = results_queue
        self.abort_flag    = abort_flag

    def process_until_finished(self):

        self.last_report = 0
        self.pending     = []

        # Don't report until finished, for BULK SPSA tests
        self.bulk = self.config.workload['test']['type'] == 'SPSA'
        self.bulk = self.bulk and self.config.workload['reporting_type'] == 'BULK'

        # Block up-to 5 seconds to get a new result
        def get_next_result():
            try: return self.results_queue.get(timeout=5)
            except queue.Empty: return False

        # Collect results until all Tasks are done
        while any(not task.done() for task in self.tasks):

            result = get_next_result()
            if result:
                self.pending.append(result)

            # Send results, or a heartbeat, every REPORT_INTERVAL seconds until done
            if self.send_results(report_interval=REPORT_INTERVAL):
                return

            # Kill everything if openbench.exit is created
            if os.path.isfile('openbench.exit'):
                return self.abort_flag.set()

        # Exhaust the Results Queue completely since Tasks are done
        while True:
            result = get_next_result()
            if result:
                self.pending.append(result)
            else:
                break

        # Send any remaining results immediately
        self.send_results(report_interval=0, final_report=True)

    def send_results(self, report_interval, final_report=False):

        # Do not send more often than report_interval dictates
        if self.last_report + report_interval > time.time():
            return False

        try:

            # Heartbeat when no results, or still awaiting bulk results
            if not self.pending or (self.bulk and not final_report):
                response = ServerReporter.report_heartbeat(self.config).json()
                self.last_report = time.time()

            else: # Send all of the queued Results at once
                response = ServerReporter.report_results(self.config, self.pending).json()
                self.last_report = time.time()
                self.pending = []

            # If the test ended, kill all tasks
            if 'stop' in response:
                self.abort_flag.set()

            # Signal an exit if the test ended
            return 'stop' in response

        except BadVersionException:
            self.abort_flag.set()
            return True

        except Exception:
            traceback.print_exc()
            print ('[Note] Failed to upload results to server...')
            self.last_report = time.time()

    def send_errors(self, timestamp, cutechess_cnt):

        for x in range(cutechess_cnt):

            # Reuse logic that was given to Cutechess to decide the PGN name
            fname = Cutechess.pgn_name(self.config, timestamp, x)

            # A runner may fail during engine initialization, before it has a
            # chance to create its PGN. The runner failure is reported from
            # send_runner_errors(); do not mask it with FileNotFoundError.
            if not os.path.isfile(fname):
                print('[#%d] No PGN was created by the game runner' % x)
                continue

            # For any game with weird Termination, report it
            for header, moves in PGNHelper.slice_pgn_file(fname):
                error = PGNHelper.get_error_reason(header)
                if error:
                    as_str = PGNHelper.pretty_format(header, moves)
                    ServerReporter.report_engine_error(self.config, error, as_str)

    def send_runner_errors(self):

        # A server/user stop is intentional. process_until_finished() may
        # return while runner futures are still winding down, so never block
        # on task.result() or report those cancellations as engine failures.
        if self.abort_flag.is_set():
            return 0

        failures = []
        for cutechess_idx, task in enumerate(self.tasks):
            if not task.done():
                failures.append({
                    'cutechess_idx': cutechess_idx,
                    'returncode': None,
                    'started_games': 0,
                    'finished_games': 0,
                    'message': 'game runner task did not terminate',
                    'logs': '',
                })
                continue
            try:
                failure = task.result()
            except Exception as error:
                failure = {
                    'cutechess_idx': cutechess_idx,
                    'returncode': None,
                    'started_games': 0,
                    'finished_games': 0,
                    'message': 'game runner raised %s: %s'
                               % (type(error).__name__, error),
                    'logs': ''.join(traceback.format_exception(
                        type(error), error, error.__traceback__
                    )),
                }

            if failure:
                failures.append(failure)

        if not failures:
            return 0

        pregame = sum(failure['finished_games'] == 0 for failure in failures)
        summary = (
            'Game runner failure: %d/%d copies failed; '
            '%d before the first completed game'
            % (len(failures), len(self.tasks), pregame)
        )

        # One failed high-concurrency workload can contain dozens of identical
        # traces. Keep the server event useful and bounded while retaining the
        # first eight independent runner diagnostics.
        reports = []
        for failure in failures[:8]:
            reports.append(
                '[runner #%d] exit=%s started=%d finished=%d\n%s\n%s'
                % (
                    failure['cutechess_idx'],
                    failure['returncode'],
                    failure['started_games'],
                    failure['finished_games'],
                    failure['message'],
                    failure['logs'][-8192:],
                )
            )
        if len(failures) > len(reports):
            reports.append(
                '[%d additional runner failures omitted]'
                % (len(failures) - len(reports))
            )

        ServerReporter.report_engine_error(
            self.config, summary, '\n\n'.join(reports)
        )
        return len(failures)


def get_version(program):

    # Try to execute the program from the command line
    # First with `--version`, and again with just `version`

    try:
        process = Popen([program, '--version'], stdout=PIPE, stderr=PIPE)
        stdout  = process.communicate()[0].decode('utf-8')
        return re.search(r'\d+\.\d+(\.\d+)?', stdout).group()

    except:
        process = Popen([program, 'version'], stdout=PIPE, stderr=PIPE)
        stdout  = process.communicate()[0].decode('utf-8')
        return re.search(r'\d+\.\d+(\.\d+)?', stdout).group()

def locate_utility(util, force_exit=True, report_error=True):

    try: return get_version(util)

    except Exception:
        if report_error: print('[Error] Unable to locate %s' % (util))
        if force_exit: sys.exit()

def set_cutechess_permissions():

    status = os.system('sudo -n chmod 777 cutechess-ob > /dev/null 2>&1')

    if status != 0:
        status = os.system('chmod 777 cutechess-ob > /dev/null 2>&1')

    if status != 0:
        print ('[ERROR] Unable to set execute permissions on cutechess-ob')


def cleanup_stale_path(path, max_age, remove_directories=False):

    try:
        if time.time() - os.path.getmtime(path) <= max_age:
            return

        # Datagen is a worker-owned cache and generators may leave sidecar
        # directories behind. Never recurse through a symlink, though: remove
        # only the link itself so cleanup cannot escape the cache directory.
        if remove_directories and os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)

    except OSError as error:
        # Antivirus scanners and delayed Windows handles may keep stale paths
        # locked. Cleanup is best effort and must not stop workload requests.
        print('[Note] Cleanup deferred for %s: %s' % (path, error))


def cleanup_client():

    SECONDS_PER_DAY   = 60 * 60 * 24
    SECONDS_PER_WEEK  = SECONDS_PER_DAY * 7
    SECONDS_PER_MONTH = SECONDS_PER_WEEK * 4

    for file in os.listdir('PGNs'):
        cleanup_stale_path(os.path.join('PGNs', file), SECONDS_PER_DAY)

    for file in os.listdir('Engines'):
        cleanup_stale_path(os.path.join('Engines', file), SECONDS_PER_WEEK)

    for file in os.listdir('Networks'):
        cleanup_stale_path(os.path.join('Networks', file), SECONDS_PER_MONTH)

    for file in os.listdir('Datagen'):
        cleanup_stale_path(
            os.path.join('Datagen', file),
            SECONDS_PER_DAY,
            remove_directories=True,
        )

def has_uci_option(options, name):

    pattern = r'(?<!\S)%s=(?:"[^"]*"|\'[^\']*\'|\S+)' % re.escape(name)
    return re.search(pattern, options, re.IGNORECASE) is not None


def tablebase_capability(config, family):

    if family == 'standard':
        return config.syzygy_path, config.syzygy_max
    if family == 'atomic':
        return config.atomic_syzygy_path, config.atomic_syzygy_max
    raise ValueError('Unknown tablebase family: %s' % (family))


def tablebase_manifest(config, family):

    if family == 'atomic':
        return config.atomic_syzygy_manifest_sha256
    # The legacy standard-Syzygy capability has no authenticated inventory
    # contract.  It remains valid for gameplay, but cannot satisfy the new
    # provenance-bearing DATAGEN path.
    if family == 'standard':
        return None
    raise ValueError('Unknown tablebase family: %s' % (family))


def split_tablebase_paths(path_value):

    return path_value.split(':' if IS_LINUX else ';')


def validate_syzygy_exists(path_value, K, required_suffixes=('.rtbw',)):

    if isinstance(required_suffixes, str):
        required_suffixes = (required_suffixes,)

    letters = ['', 'Q', 'R', 'B', 'N', 'P']

    # Generate many potential K[] v K[], including all valid ones
    candidates = ['K%svK%s' % (''.join(lhs), ''.join(rhs))
        for N in range(1, K - 1)
            for lhs in combinations_with_replacement(letters, N)
                for rhs in combinations_with_replacement(letters, K - N - 2)]

    # Syzygy does LHS having more pieces first, stronger pieces second
    def valid_filename(name):
        for i, letter in enumerate(letters[1:]):
            name = name.replace(letter, str(9 - i))
        lhs, rhs = name.replace('K', '9').split('v')
        return int(lhs) >= int(rhs) and name != 'KvK'

    # Split paths, using ":" on Unix, and ";" on Windows
    paths = split_tablebase_paths(path_value)

    # Check to see if each Syzygy File exists as desired
    for filename in list(filter(valid_filename, set(candidates))):
        for suffix in required_suffixes:
            if not any(
                os.path.isfile(os.path.join(path, filename + suffix))
                for path in paths
            ):
                return False

    return True


def validate_tablebase_inventory(path_value, manifest_path, required_suffixes):

    with open(manifest_path, 'rb') as fin:
        raw_manifest = fin.read()
    manifest_sha256 = hashlib.sha256(raw_manifest).hexdigest()

    try:
        inventory = json.loads(raw_manifest.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('Invalid tablebase inventory JSON') from error
    if not isinstance(inventory, list) or not inventory:
        raise ValueError('Tablebase inventory must be a non-empty JSON list')

    paths = [os.path.abspath(path) for path in split_tablebase_paths(path_value)]
    if any(not os.path.isdir(path) for path in paths):
        raise ValueError('Every tablebase runtime path must be a directory')
    suffixes = tuple(suffix.lower() for suffix in required_suffixes)
    observed = set()
    groups = {}
    paths_by_label = {}

    def source_directory(label):
        key = label.casefold()
        if key in paths_by_label:
            return paths_by_label[key]

        candidates = {
            os.path.realpath(path)
            for path in paths
            if os.path.basename(path).casefold() == key
        }
        candidates.update(
            os.path.realpath(candidate)
            for path in paths
            for candidate in [os.path.join(os.path.dirname(path), label)]
            if os.path.isdir(candidate)
        )
        if len(candidates) != 1:
            raise ValueError(
                'Cannot resolve one authenticated source directory for %s'
                % (label,)
            )
        paths_by_label[key] = candidates.pop()
        return paths_by_label[key]

    for entry in inventory:
        if not isinstance(entry, dict):
            raise ValueError('Tablebase inventory entries must be JSON objects')
        name = entry.get('name')
        directory = entry.get('directory')
        size = entry.get('bytes')
        if (not isinstance(name, str) or os.path.basename(name) != name
                or not name.lower().endswith(suffixes)):
            raise ValueError('Invalid tablebase inventory name: %r' % (name,))
        if (not isinstance(directory, str) or not directory
                or os.path.basename(directory) != directory
                or directory in ['.', '..']):
            raise ValueError('Invalid directory for tablebase %s' % (name,))
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError('Invalid byte count for tablebase %s' % (name,))
        if name.casefold() in observed:
            raise ValueError('Duplicate tablebase inventory name: %s' % (name,))
        observed.add(name.casefold())

        source_file = os.path.join(source_directory(directory), name)
        runtime_files = [
            os.path.join(path, name)
            for path in paths
            if os.path.isfile(os.path.join(path, name))
        ]
        if not os.path.isfile(source_file) or len(runtime_files) != 1:
            raise ValueError(
                'Expected one authenticated source and runtime file for %s'
                % (name,)
            )
        if not os.path.samefile(source_file, runtime_files[0]):
            raise ValueError(
                'Runtime tablebase is not the authenticated hardlink for %s'
                % (name,)
            )
        if os.path.getsize(source_file) != size:
            raise ValueError('Tablebase byte count mismatch for %s' % (name,))

        group = groups.setdefault(
            directory, {'files': 0, 'bytes': 0, 'mtime_ns': 0}
        )
        group['files'] += 1
        group['bytes'] += size
        group['mtime_ns'] = max(
            group['mtime_ns'], os.stat(source_file).st_mtime_ns
        )

    actual = set()
    for path in paths:
        for name in os.listdir(path):
            if name.lower().endswith(suffixes):
                actual.add(name.casefold())
    if actual != observed:
        missing = sorted(observed - actual)
        extra = sorted(actual - observed)
        raise ValueError(
            'Tablebase inventory mismatch: missing=%r extra=%r'
            % (missing[:5], extra[:5])
        )

    for directory, expected in groups.items():
        marker_path = os.path.join(
            source_directory(directory), '.acquisition-complete.json'
        )
        try:
            with open(marker_path, encoding='utf-8-sig') as fin:
                marker = json.load(fin)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(
                'Missing or invalid Atomic Syzygy acquisition marker: %s'
                % (marker_path,)
            ) from error
        valid_marker = (
            isinstance(marker, dict)
            and marker.get('schema') == 'atomic-syzygy-acquisition-v1'
            and marker.get('directory') == directory
            and marker.get('files') == expected['files']
            and marker.get('bytes') == expected['bytes']
            and str(marker.get('source_inventory_sha256', '')).lower()
                == manifest_sha256
            and marker.get('official_md5_verification') == 'pass'
            and os.stat(marker_path).st_mtime_ns >= expected['mtime_ns']
        )
        if not valid_marker:
            raise ValueError(
                'Invalid or stale Atomic Syzygy acquisition marker: %s'
                % (marker_path,)
            )

    return manifest_sha256


def scale_time_control(workload, scale_factor, branch):

    # Extract everything from the workload dictionary
    reference_nps = workload['test'][branch]['nps']
    time_control  = workload['test'][branch]['time_control']

    # Searching for Nodes or Depth time controls ("N=", "D=")
    pattern = r'(?P<mode>((N))|(D))=(?P<value>(\d+))'
    results = re.search(pattern, time_control.upper())

    # No scaling is needed for fixed nodes or fixed depth games
    if results:
        mode, value = results.group('mode', 'value')
        return 'tc=inf %s=%s' % ({'N' : 'nodes', 'D' : 'depth'}[mode], value)

    # Searching for MoveTime or Fixed Time Controls ("MT=")
    pattern = r'(?P<mode>(MT))=(?P<value>(\d+))'
    results = re.search(pattern, time_control.upper())

    # Scale the time based on this machine's NPS. Add a time Margin to avoid time losses.
    if results:
        mode, value = results.group('mode', 'value')
        return 'st=%.2f timemargin=250' % ((float(value) * scale_factor / 1000))

    # Searching for "X/Y+Z" time controls
    pattern = r'(?P<moves>(\d+/)?)(?P<base>\d*(\.\d+)?)(?P<inc>\+(\d+\.)?\d+)?'
    results = re.search(pattern, time_control)
    moves, base, inc = results.group('moves', 'base', 'inc')

    # Strip the trailing and leading symbols
    moves = None if moves == '' else moves.rstrip('/')
    inc   = 0.0  if inc   is None else inc.lstrip('+')

    # Scale the time based on this machine's NPS
    base = float(base) * scale_factor
    inc  = float(inc ) * scale_factor

    # Format the time control for cutechess
    if moves is None:
        return 'tc=%.2f+%.2f timemargin=250' % (base, inc)
    return 'tc=%d/%.2f+%.2f timemargin=250' % (int(moves), base, inc)

def find_pgn_error(reason, command):

    pgn_file = command.split('-pgnout ')[1].split()[0]
    with open(pgn_file, 'r') as fin:
        data = fin.readlines()

    reason = reason.split('{')[1]
    for ii in range(len(data) - 1, -1, -1):
        if reason in data[ii]:
            break

    pgn = ""
    while "[Event " not in data[ii]:
        pgn = data[ii] + pgn
        ii = ii - 1
    return data[ii] + pgn


## Functions interacting with the OpenBench server that establish the initial
## connection and then make simple requests to retrieve Workloads as json objects

def server_configure_worker(config):

    # Server tells us how to build or obtain binaries
    target = url_join(config.server, 'clientGetBuildInfo')
    data   = requests.get(target, timeout=TIMEOUT_HTTP).json()

    config.scan_for_compilers(data)      # Public engine build tools
    config.scan_for_private_tokens(data) # Private engine access tokens
    config.scan_for_cpu_flags(data)      # For executing binaries
    config.scan_for_machine_id()         # None, or the content of machine.txt

    system_info = {
        'compilers'      : config.compilers,      # Key: Engine, Value: (Compiler, Version)
        'tokens'         : config.git_tokens,     # Key: Engine, Value: True, for tokens we have
        'cpu_flags'      : config.cpu_flags,      # List of CPU flags found in the Client or Server
        'cpu_name'       : config.cpu_name,       # Raw CPU name as per py-cpuinfo
        'os_name'        : config.os_name,        # Should be Windows, Linux, or Darwin
        'os_ver'         : config.os_ver,         # Release version of the OS
        'python_ver'     : config.python_ver,     # Python version running the Client
        'mac_address'    : config.mac_address,    # Used to softly verify the Machine IDs
        'logical_cores'  : config.logical_cores,  # Logical cores, to differentiate hyperthreads
        'physical_cores' : config.physical_cores, # Physical cores, to differentiate hyperthreads
        'ram_total_mb'   : config.ram_total_mb,   # Total RAM on the system, to avoid over assigning
        'machine_id'     : config.machine_id,     # Assigned value, or None. Will be replaced if wrong
        'machine_name'   : config.identity,       # Optional pseudonym for the machine, otherwise None
        'concurrency'    : config.threads,        # Threads to use to play games
        'sockets'        : config.sockets,        # Cutechess copies, usually equal to Socket count
        'syzygy_max'     : config.syzygy_max,     # Legacy standard-Syzygy capability
        'tablebases'     : {
            'standard' : config.syzygy_max,
            'atomic'   : {
                'max'             : config.atomic_syzygy_max,
                'manifest_sha256' : config.atomic_syzygy_manifest_sha256,
            },
        },
        'focus'          : config.focus,          # List of engines we have a preference to help
        'client_ver'     : CLIENT_VERSION,        # Version of the Client, which the server may reject
    }

    payload = {
        'username'    : config.username,
        'password'    : config.password,
        'system_info' : json.dumps(system_info),
    }

    # Send all of this to the server, and get a Machine Id + Secret Token
    target   = url_join(config.server, 'clientWorkerInfo')
    response = requests.post(target, data=payload, timeout=TIMEOUT_HTTP).json()

    # Delete the machine.txt if we have saved an invalid machine number
    if response.get('error', '').lower() == "bad machine id":
        config.machine_id = 'None'
        if os.path.isfile('machine.txt'):
            os.remove('machine.txt')

    # Throw all the way back to the client.py
    if 'Bad Client Version' in response.get('error', ''):
        raise BadVersionException();

    # The 'error' header is included if there was an issue
    if 'error' in response:
        raise Exception('[Error] %s' % (response['error']))

    # Save the machine id, to avoid re-registering every time
    with open('machine.txt', 'w') as fout:
        fout.write(str(response['machine_id']))

    # Store machine_id, and the secret for this session
    config.machine_id   = response['machine_id']
    config.secret_token = response['secret']

def server_request_workload(config):

    print('\nRequesting Workload from Server...')

    payload  = { 'machine_id' : config.machine_id, 'secret' : config.secret_token, 'blacklist' : config.blacklist }
    target   = url_join(config.server, 'clientGetWorkload')
    response = requests.post(target, data=payload, timeout=TIMEOUT_HTTP)

    # Server errors produce garbage back, which we should not alarm a user with
    try: response = response.json()
    except json.decoder.JSONDecodeError:
        raise OpenBenchBadServerResponseException() from None

    # Throw all the way back to the client.py
    if 'Bad Client Version' in response.get('error', ''):
        raise BadVersionException();

    # The 'error' header is included if there was an issue
    if 'error' in response:
        raise Exception('[Error] %s' % (response['error']))

    # Log the start of a new Workload
    if 'workload' in response:
        test = response['workload']['test']
        dev_engine = test['dev']['engine']
        dev_name = test['dev']['name']
        datagen = test.get('datagen')
        if test['type'] == 'DATAGEN' and datagen:
            total_chunks = (
                datagen['total_count'] + datagen['positions_per_chunk'] - 1
            ) // datagen['positions_per_chunk']
            print(
                'Workload DATAGEN [%s] %s - chunk %d/%d (test #%d)\n'
                % (
                    dev_engine,
                    dev_name,
                    datagen['chunk_idx'] + 1,
                    total_chunks,
                    test['id'],
                )
            )
        else:
            base_engine = test['base']['engine']
            base_name = test['base']['name']
            print(
                'Workload [%s] %s vs [%s] %s\n'
                % (dev_engine, dev_name, base_engine, base_name)
            )

    config.workload = response.get('workload', None)


class DatagenStopped(Exception):
    pass


class DatagenTransientError(RuntimeError):
    """Retryable packaging or server-transport failure for one DATAGEN chunk."""


class DatagenConfigurationError(RuntimeError):
    """Non-retryable generic DATAGEN configuration rejected by the client."""


DATAGEN_DETERMINISTIC_SETUP_ERRORS = (
    DatagenConfigurationError,
)


class DatagenHeartbeat:

    def __init__(self, config):
        self.config = config
        self.shutdown = threading.Event()
        self.stop_requested = threading.Event()
        self.thread = None

    def __enter__(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback_value):
        self.shutdown.set()
        self.thread.join(timeout=1)

    def _run(self):
        while not self.shutdown.wait(REPORT_INTERVAL):
            try:
                response = ServerReporter.report_heartbeat(self.config).json()
                if 'stop' in response:
                    self.stop_requested.set()
                    return
            except BadVersionException:
                self.stop_requested.set()
                return
            except Exception:
                traceback.print_exc()
                print('[Note] Failed to send DATAGEN heartbeat; lease retry continues')


def terminate_datagen_process(process):
    """Terminate only the DATAGEN process tree started by this worker."""

    if process.poll() is not None:
        return

    targets = []
    try:
        parent = psutil.Process(process.pid)
        targets = parent.children(recursive=True) + [parent]
        for target in targets:
            try:
                target.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(targets, timeout=5)
        for target in alive:
            try:
                target.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        try:
            process.terminate()
        except OSError:
            pass

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass


def _open_datagen_regular(path):
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise OSError('DATAGEN artifact is not a regular file')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0)
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino)
            != (after.st_dev, after.st_ino)
        ):
            raise OSError('DATAGEN artifact changed while opening')
        return os.fdopen(descriptor, 'rb'), after
    except Exception:
        os.close(descriptor)
        raise


def datagen_file_sha256(path):
    data, before = _open_datagen_regular(path)

    digest = hashlib.sha256()
    byte_count = 0
    with data:
        while block := data.read(1024 * 1024):
            digest.update(block)
            byte_count += len(block)
        after = os.fstat(data.fileno())

    identity = lambda stat: (
        stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
    )
    if identity(before) != identity(after) or byte_count != after.st_size:
        raise OSError('DATAGEN artifact changed while hashing')
    return digest.hexdigest(), byte_count


def snapshot_datagen_producer(source_path, snapshot_path):
    """Copy and hash one private executable snapshot from a stable descriptor."""

    source, before = _open_datagen_regular(source_path)
    target_flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    )
    target_fd = None
    digest = hashlib.sha256()
    byte_count = 0
    try:
        target_fd = os.open(snapshot_path, target_flags, 0o700)
        with source, os.fdopen(target_fd, 'wb') as target:
            target_fd = None
            while block := source.read(1024 * 1024):
                digest.update(block)
                byte_count += len(block)
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
            after = os.fstat(source.fileno())
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
        )
        if identity(before) != identity(after) or byte_count != after.st_size:
            raise OSError('DATAGEN producer changed while snapshotting')
        os.chmod(snapshot_path, 0o700)
        return digest.hexdigest(), byte_count
    except Exception:
        if target_fd is not None:
            os.close(target_fd)
        try:
            if os.path.lexists(snapshot_path):
                os.remove(snapshot_path)
        except OSError:
            pass
        raise


def compress_datagen_output(source_path, target_path, heartbeat):
    """Compress one output with bounded retries, replacing partial archives."""

    for attempt in range(DATAGEN_TRANSFER_RETRIES):
        try:
            if os.path.isfile(target_path):
                os.remove(target_path)
            with open(source_path, 'rb') as source:
                with bz2.open(target_path, 'wb', compresslevel=9) as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
            return
        except Exception as error:
            cleanup_error = None
            try:
                if os.path.isfile(target_path):
                    os.remove(target_path)
            except Exception as removal_error:
                # Antivirus scanners and delayed Windows handles can briefly
                # keep the partial archive open. Treat cleanup as part of the
                # same transient packaging failure instead of escaping into
                # the deterministic generator/blacklist path.
                cleanup_error = removal_error
            if attempt + 1 == DATAGEN_TRANSFER_RETRIES:
                detail = str(error)
                if cleanup_error is not None:
                    detail += '; partial archive cleanup failed: %s' % cleanup_error
                raise DatagenTransientError(
                    'DATAGEN bzip2 compression failed after %d attempts: %s'
                    % (DATAGEN_TRANSFER_RETRIES, detail)
                ) from error
            traceback.print_exc()
            if heartbeat.stop_requested.wait(DATAGEN_TRANSFER_RETRY_DELAY):
                raise DatagenStopped()


def upload_datagen_producer(
    config, path, sha256, byte_count, commit, heartbeat
):
    """Bind a cached producer, uploading its bytes only when the CAS needs them."""

    for attempt in range(DATAGEN_TRANSFER_RETRIES):
        try:
            response = ServerReporter.report_datagen_producer(
                config,
                None,
                sha256,
                byte_count,
                commit,
                metadata_only=True,
            )
            if response.get('upload_required') is True:
                response = ServerReporter.report_datagen_producer(
                    config, path, sha256, byte_count, commit
                )
            expected = {
                'sha256': sha256,
                'bytes': byte_count,
                'commit': commit,
            }
            for field, value in expected.items():
                if response.get(field) != value:
                    raise RuntimeError(
                        'DATAGEN producer response mismatched %s' % field
                    )
            return expected
        except BadVersionException:
            raise
        except Exception as error:
            if attempt + 1 == DATAGEN_TRANSFER_RETRIES:
                raise DatagenTransientError(
                    'DATAGEN producer upload failed after %d attempts: %s'
                    % (DATAGEN_TRANSFER_RETRIES, error)
                ) from error
            traceback.print_exc()
            if heartbeat.stop_requested.wait(DATAGEN_TRANSFER_RETRY_DELAY):
                raise DatagenStopped()


def upload_datagen_output(
    config, path, sha256, byte_count, heartbeat, producer=None, tablebase=None
):
    """Upload one immutable archive with bounded transport retries."""

    for attempt in range(DATAGEN_TRANSFER_RETRIES):
        try:
            if tablebase is None:
                if producer is None:
                    response = ServerReporter.report_datagen(
                        config, path, sha256, byte_count
                    )
                else:
                    response = ServerReporter.report_datagen(
                        config, path, sha256, byte_count, producer
                    )
            else:
                response = ServerReporter.report_datagen(
                    config,
                    path,
                    sha256,
                    byte_count,
                    producer=producer,
                    tablebase=tablebase,
                )
            for field in ('completed_chunks', 'total_chunks'):
                if field not in response:
                    raise RuntimeError(
                        'DATAGEN upload response omitted %s' % field
                    )
            if tablebase is not None and not re.fullmatch(
                r'[0-9a-f]{64}',
                response.get('environment_receipt_sha256', ''),
            ):
                raise RuntimeError(
                    'DATAGEN upload response omitted tablebase receipt'
                )
            return response
        except BadVersionException:
            raise
        except Exception as error:
            if attempt + 1 == DATAGEN_TRANSFER_RETRIES:
                raise DatagenTransientError(
                    'DATAGEN upload failed after %d attempts: %s'
                    % (DATAGEN_TRANSFER_RETRIES, error)
                ) from error
            traceback.print_exc()
            if heartbeat.stop_requested.wait(DATAGEN_TRANSFER_RETRY_DELAY):
                raise DatagenStopped()


def report_datagen_transient_failure(config, summary, logs):
    """Ask the server to requeue the lease, retrying only the report itself.

    If every report attempt fails, the heartbeat lease remains the final
    safety net and the server will make the chunk assignable after expiry.
    """

    for attempt in range(DATAGEN_TRANSFER_RETRIES):
        try:
            response = ServerReporter.report_engine_error(config, summary, logs)
            response.raise_for_status()
            return True
        except BadVersionException:
            raise
        except Exception:
            traceback.print_exc()
            if attempt + 1 < DATAGEN_TRANSFER_RETRIES:
                time.sleep(DATAGEN_TRANSFER_RETRY_DELAY)
    return False


def handle_datagen_transient_failure(config, chunk, log_path, error):
    logs = datagen_log_tail(log_path)
    summary = 'DATAGEN chunk %d transient failure: %s' % (
        chunk['chunk_idx'], error
    )
    if not report_datagen_transient_failure(config, summary, logs):
        print(
            '[Note] Failed to report transient DATAGEN failure; '
            'the server lease will safely expire'
        )


def datagen_log_tail(path, limit=65536):
    if not os.path.exists(path):
        return ''
    with open(path, 'rb') as log:
        log.seek(0, os.SEEK_END)
        size = log.tell()
        log.seek(max(0, size - limit), os.SEEK_SET)
        return log.read().decode('utf-8', errors='replace')


def clean_datagen_workspace(output_path):
    """Best-effort removal of files belonging to one DATAGEN chunk attempt."""

    errors = []
    for path in [output_path] + glob.glob(output_path + '.*'):
        if os.path.isfile(path) or os.path.islink(path):
            try:
                os.remove(path)
            except OSError as error:
                errors.append('%s: %s' % (path, error))
    return errors


def datagen_publication_contract(config):
    data = config.workload['test']['datagen']
    protocol = data.get('publication_protocol', 0)
    if protocol in (None, 0):
        return None
    if protocol != 41:
        raise DatagenConfigurationError(
            'DATAGEN publication protocol is malformed or unsupported'
        )
    document = data.get('publication_contract')
    digest = str(data.get('publication_contract_sha256', '')).lower()
    if not isinstance(document, dict) or not re.fullmatch(
        r'[0-9a-f]{64}', digest
    ):
        raise DatagenConfigurationError(
            'DATAGEN publication contract is missing'
        )
    try:
        encoded = json.dumps(
            document,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError):
        raise DatagenConfigurationError(
            'DATAGEN publication contract is not canonical JSON'
        ) from None
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise DatagenConfigurationError(
            'DATAGEN publication contract hash is invalid'
        )

    test = config.workload['test']
    dev = test['dev']
    book = test['book']
    try:
        generation = document['generation']
        engine = document['engine']
        network = document['network']
        frozen_book = document['book']
        producer = document['producer']
        syzygy = document['syzygy']
        teacher = document['teacher']
        command = data['command']
        publication_ids_valid = all(
            isinstance(document.get(field), str)
            and re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,127}', document[field])
            for field in ('campaign_id', 'external_workload_id', 'role', 'cohort')
        )
        network_valid = (
            set(network) == {'name', 'openbench_id', 'sha256', 'bytes'}
            and isinstance(network['name'], str)
            and bool(network['name'])
            and isinstance(network['openbench_id'], str)
            and re.fullmatch(r'[0-9A-F]{8}', network['openbench_id'])
            and isinstance(network['sha256'], str)
            and re.fullmatch(r'[0-9a-f]{64}', network['sha256'])
            and network['sha256'].upper().startswith(network['openbench_id'])
            and type(network['bytes']) is int
            and network['bytes'] > 0
        )
        if frozen_book.get('name') == 'NONE':
            book_valid = frozen_book == {
                'kind': 'builtin-startpos',
                'name': 'NONE',
                'source': None,
                'text_sha256': None,
                'raw_sha256': None,
            }
        else:
            book_valid = (
                set(frozen_book) == {
                    'kind', 'name', 'source', 'text_sha256', 'raw_sha256',
                }
                and frozen_book.get('kind') == 'file'
                and isinstance(frozen_book.get('name'), str)
                and bool(frozen_book.get('name'))
                and isinstance(frozen_book.get('source'), str)
                and bool(frozen_book.get('source').strip())
                and len(frozen_book.get('source')) <= 2048
                and re.fullmatch(
                    r'[0-9a-f]{64}', frozen_book.get('text_sha256', '')
                )
                and re.fullmatch(
                    r'[0-9a-f]{64}', frozen_book.get('raw_sha256', '')
                )
            )
        generation_valid = (
            type(generation.get('total_count')) is int
            and 0 < generation['total_count'] <= 2**63 - 1
            and type(generation.get('positions_per_chunk')) is int
            and 0 < generation['positions_per_chunk'] <= 2**63 - 1
            and type(generation.get('base_seed')) is int
            and 0 <= generation['base_seed'] <= 2**63 - 1
        )
        if generation_valid:
            total_chunks = (
                generation['total_count']
                + generation['positions_per_chunk'] - 1
            ) // generation['positions_per_chunk']
            generation_valid = (
                generation['base_seed'] + total_chunks - 1 <= 2**63 - 1
            )
        producer_valid = (
            type(producer.get('required')) is bool
            and re.fullmatch(
                r'[0-9a-f]{64}', producer.get('contract_sha256', '')
            )
        )
        syzygy_valid = (
            type(syzygy.get('required')) is bool
            and re.fullmatch(
                r'[0-9a-f]{64}',
                syzygy.get('environment_contract_sha256', ''),
            )
        )
        if syzygy_valid and syzygy['required']:
            syzygy_valid = (
                syzygy.get('family') == 'atomic'
                and type(syzygy.get('max')) is int
                and syzygy['max'] in range(3, 7)
                and re.fullmatch(
                    r'[0-9a-f]{64}', syzygy.get('manifest_sha256', '')
                )
                and teacher == {'mode': teacher.get('mode')}
                and teacher.get('mode') in {'pure', 'true'}
            )
        elif syzygy_valid:
            syzygy_valid = (
                syzygy.get('family') is None
                and syzygy.get('max') == 0
                and syzygy.get('manifest_sha256') is None
                and teacher == {'mode': None}
            )
        engine_valid = (
            isinstance(engine.get('name'), str)
            and bool(engine['name'])
            and isinstance(engine.get('repo'), str)
            and bool(engine['repo'])
            and isinstance(engine.get('source'), str)
            and bool(engine['source'])
            and isinstance(engine.get('requested_ref'), str)
            and bool(engine['requested_ref'])
            and re.fullmatch(r'[0-9a-f]{40}', engine.get('commit', ''))
            and type(engine.get('bench')) is int
            and engine['bench'] > 0
            and isinstance(engine.get('options'), str)
        )
        expected = (
            document.get('schema') == 'openbench-datagen-publication-contract-v41'
            and document.get('protocol') == 41
            and publication_ids_valid
            and network_valid
            and book_valid
            and generation_valid
            and producer_valid
            and syzygy_valid
            and engine_valid
            and engine['name'] == dev['engine']
            and engine['repo'] == dev['repo']
            and engine['source'] == dev['source']
            and engine['requested_ref'] == dev['requested_ref']
            and engine['commit'] == str(dev['sha']).lower()
            and engine['bench'] == dev['bench']
            and engine['options'] == dev['options']
            and network['openbench_id'] == str(dev['network']).upper()
            and network['name'] == dev['netname']
            and frozen_book['name'] == book['name']
            and frozen_book['source'] == book['source']
            and frozen_book['text_sha256'] == book['sha']
            and frozen_book['raw_sha256'] == book.get('raw_sha')
            and generation['command'] == command
            and generation['command_sha256']
                == hashlib.sha256(command.encode('utf-8')).hexdigest()
            and generation['total_count'] == data['total_count']
            and generation['positions_per_chunk'] == data['positions_per_chunk']
            and generation['base_seed'] == data['base_seed']
            and generation['seed_method'] == 'base-plus-chunk-index-v1'
            and producer['required'] == data['producer_artifact_required']
            and producer['contract_sha256'] == data['producer_contract_sha256']
            and syzygy['required'] == data['tablebase_required']
            and syzygy['family'] == (data['tablebase_family'] or None)
            and syzygy['max'] == data['tablebase_max']
            and syzygy['manifest_sha256']
                == (data['tablebase_manifest_sha256'] or None)
            and syzygy['environment_contract_sha256']
                == data['environment_contract_sha256']
            and teacher['mode'] == (data['teacher_mode'] or None)
        )
    except (KeyError, TypeError, AttributeError):
        expected = False
    if not expected:
        raise DatagenConfigurationError(
            'DATAGEN publication contract does not match the assigned workload'
        )
    return document


def datagen_tablebase_attestation(config):
    data = config.workload['test']['datagen']
    publication = datagen_publication_contract(config)
    if not data.get('tablebase_required') and publication is None:
        return None

    lease = data.get('environment_lease')
    lease_sha256 = str(data.get('environment_lease_sha256', '')).lower()
    if not isinstance(lease, dict):
        raise DatagenConfigurationError(
            'DATAGEN tablebase lease is missing'
        )
    encoded_lease = json.dumps(
        lease, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    if hashlib.sha256(encoded_lease).hexdigest() != lease_sha256:
        raise DatagenConfigurationError(
            'DATAGEN tablebase lease hash is invalid'
        )
    lease_tablebase = lease.get('tablebase', {})
    family = lease_tablebase.get('family')
    required_max = int(lease_tablebase.get('required_max', 0))
    leased_worker_max = int(lease_tablebase.get('worker_max', 0))
    manifest_value = lease_tablebase.get('manifest_sha256')
    expected_manifest = (
        manifest_value.lower() if isinstance(manifest_value, str) else None
    )
    expected_contract = str(
        lease.get('environment_contract_sha256', '')
    ).lower()
    teacher_mode = lease.get('teacher_mode', '')
    test = config.workload['test']

    if publication is not None:
        schema = 'openbench-datagen-publication-lease-v41'
        protocol = 41
    else:
        schema = 'openbench-datagen-tablebase-lease-v40'
        protocol = 40
    if (
        lease.get('schema') != schema
        or lease.get('protocol') != protocol
        or lease.get('test_id') != test.get('id')
        or lease.get('chunk_idx') != data.get('chunk_idx')
        or lease.get('attempt') != data.get('attempt')
        or str(lease.get('machine_id')) != str(config.machine_id)
        or (
            publication is not None
            and lease.get('publication_contract_sha256')
                != data.get('publication_contract_sha256')
        )
        or test.get('syzygy_adj') != 'DISABLED'
        or (data.get('tablebase_family') or None) != family
        or int(data.get('tablebase_max', 0)) != required_max
        or (data.get('tablebase_manifest_sha256') or None) != expected_manifest
        or str(data.get('environment_contract_sha256', '')).lower()
           != expected_contract
        or (data.get('teacher_mode') or None) != teacher_mode
        or not re.fullmatch(r'[0-9a-f]{64}', expected_contract)
    ):
        raise DatagenConfigurationError(
            'DATAGEN tablebase contract is malformed or unsupported'
        )

    if data.get('tablebase_required'):
        if (
            lease_tablebase.get('required', True) is not True
            or family != 'atomic'
            or required_max not in range(
                ATOMIC_DATAGEN_TABLEBASE_MIN,
                ATOMIC_DATAGEN_TABLEBASE_MAX + 1,
            )
            or test.get('syzygy_wdl') != '%d-MAN' % required_max
            or test['dev'].get('tablebase_family') != family
            or not re.fullmatch(r'[0-9a-f]{64}', expected_manifest)
            or teacher_mode not in ['pure', 'true']
        ):
            raise DatagenConfigurationError(
                'DATAGEN tablebase contract is malformed or unsupported'
            )
        path, worker_max = tablebase_capability(config, family)
        worker_manifest = tablebase_manifest(config, family)
    else:
        if (
            publication is None
            or lease_tablebase != {
                'required': False,
                'family': None,
                'required_max': 0,
                'worker_max': 0,
                'manifest_sha256': None,
            }
            or teacher_mode is not None
        ):
            raise DatagenConfigurationError(
                'DATAGEN publication environment is malformed or unsupported'
            )
        path, worker_max, worker_manifest = None, 0, None
    worker_manifest = (
        worker_manifest.lower() if isinstance(worker_manifest, str) else None
    )
    if data.get('tablebase_required') and (
        not path or worker_max < required_max
    ):
        raise DatagenConfigurationError(
            'DATAGEN requires %d-man %s tablebases; worker has %d-man'
            % (required_max, family, worker_max)
        )
    if worker_max != leased_worker_max:
        raise DatagenConfigurationError(
            'DATAGEN worker capability changed after lease assignment'
        )
    if data.get('tablebase_required') and worker_manifest != expected_manifest:
        raise DatagenConfigurationError(
            'DATAGEN tablebase inventory does not match the pinned manifest'
        )

    return {
        'path': path,
        'tablebase_required': bool(data.get('tablebase_required')),
        'family': family or '',
        'required_max': required_max,
        'worker_max': worker_max,
        'manifest_sha256': worker_manifest,
        'environment_contract_sha256': expected_contract,
        'environment_lease_sha256': lease_sha256,
        'teacher_mode': teacher_mode,
        'publication_contract_sha256': (
            data.get('publication_contract_sha256')
            if publication is not None else None
        ),
    }


def revalidate_datagen_tablebase_inventory(config, tablebase):
    """Re-authenticate the worker-local corpus immediately before launch."""

    if tablebase is None or not tablebase.get('tablebase_required'):
        return
    family = tablebase.get('family')
    if family != 'atomic':
        raise DatagenConfigurationError(
            'DATAGEN tablebase revalidation requires the Atomic family'
        )

    path, worker_max = tablebase_capability(config, family)
    manifest_path = getattr(config, 'atomic_syzygy_manifest', None)
    expected_manifest = tablebase.get('manifest_sha256')
    leased_worker_max = tablebase.get('worker_max')
    required_max = tablebase.get('required_max')
    if (
        not path
        or path != tablebase.get('path')
        or not manifest_path
        or worker_max != leased_worker_max
        or not isinstance(required_max, int)
        or worker_max < required_max
    ):
        raise DatagenConfigurationError(
            'DATAGEN tablebase capability changed before generator launch'
        )

    try:
        observed_manifest = validate_tablebase_inventory(
            path, manifest_path, ('.atbw', '.atbz')
        )
        complete = validate_syzygy_exists(
            path, worker_max, ('.atbw', '.atbz')
        )
    except (OSError, TypeError, ValueError):
        raise DatagenConfigurationError(
            'DATAGEN tablebase inventory failed pre-launch revalidation'
        ) from None
    if observed_manifest != expected_manifest or not complete:
        raise DatagenConfigurationError(
            'DATAGEN tablebase inventory changed before generator launch'
        )


def render_datagen_command(
    config, output_path, network_path=None, producer=None, tablebase=None
):
    data = config.workload['test']['datagen']
    book = config.workload['test']['book']
    book_name = book['name']
    book_path = 'NONE' if book_name.upper() == 'NONE' else os.path.join('Books', book_name)
    book_raw_sha = book.get('raw_sha') or book.get('sha')
    publication = datagen_publication_contract(config)
    if (
        data.get('tablebase_required') or publication is not None
    ) and tablebase is None:
        tablebase = datagen_tablebase_attestation(config)
    network_sha256 = None
    if publication is not None:
        network_sha256 = publication['network']['sha256']
        if (
            not network_path
            or datagen_file_sha256(network_path) != (
                network_sha256,
                publication['network']['bytes'],
            )
        ):
            raise DatagenConfigurationError(
                'DATAGEN network SHA-256/bytes do not match the publication contract'
            )
    authenticated_tablebase = (
        tablebase is not None and tablebase.get('tablebase_required')
    )
    values = {
        'SEED': str(data['seed']),
        'COUNT': str(data['chunk_count']),
        'OUT': output_path.replace('\\', '/'),
        'THREADS': str(config.threads),
        'BOOK': book_path.replace('\\', '/'),
        'BOOK_SHA256': (
            'NONE' if not book_raw_sha else str(book_raw_sha).upper()
        ),
        'NETWORK': (
            'NONE' if not network_path else network_path.replace('\\', '/')
        ),
        'NETWORK_SHA256': network_sha256 or 'NONE',
        'PRODUCER_SHA256': (
            'NONE' if producer is None else producer['sha256'].lower()
        ),
        'SYZYGY': (
            'NONE' if not authenticated_tablebase
            else tablebase['path'].replace('\\', '/')
        ),
        'SYZYGY_MANIFEST_SHA256': (
            'NONE' if not authenticated_tablebase else tablebase['manifest_sha256']
        ),
        'SYZYGY_MAX': (
            '0' if not authenticated_tablebase else str(tablebase['required_max'])
        ),
        'TEACHER_MODE': data.get('teacher_mode', '') or 'NONE',
    }
    if data.get('producer_artifact_required') and producer is None:
        raise DatagenConfigurationError(
            'DATAGEN command requires an authenticated producer artifact'
        )
    return data['command'].format_map(values)


def run_datagen_command(
    config, engine, output_path, log_path, heartbeat, network_path=None,
    producer=None, tablebase=None,
):
    if tablebase is not None:
        revalidate_datagen_tablebase_inventory(config, tablebase)
    command = render_datagen_command(
        config, output_path, network_path, producer, tablebase
    )
    command_sha256 = hashlib.sha256(command.encode('utf-8')).hexdigest()
    print(
        'DATAGEN command prepared: sha256=%s tablebase=%s'
        % (
            command_sha256,
            'authenticated'
            if tablebase is not None and tablebase.get('tablebase_required')
            else 'none',
        )
    )

    with open(log_path, 'wb') as log:
        process = Popen(
            [engine],
            stdin=PIPE,
            stdout=log,
            stderr=STDOUT,
            text=True,
        )
        try:
            process.stdin.write(command + '\nquit\n')
            process.stdin.flush()
            process.stdin.close()

            while process.poll() is None:
                if heartbeat.stop_requested.is_set() or os.path.isfile('openbench.exit'):
                    terminate_datagen_process(process)
                    raise DatagenStopped()
                time.sleep(1)
        except Exception:
            terminate_datagen_process(process)
            raise

    if process.returncode != 0:
        raise RuntimeError('DATAGEN engine exited with code %d' % process.returncode)
    if not os.path.isfile(output_path):
        raise RuntimeError('DATAGEN command completed without creating {OUT}')


def complete_datagen_workload(config):
    test = config.workload['test']
    chunk = test['datagen']
    stem = 'test_%d_chunk_%d' % (test['id'], chunk['chunk_idx'])
    output_path = os.path.join('Datagen', stem + '.bin')
    compressed_path = output_path + '.bz2'
    log_path = os.path.join('Datagen', stem + '.log')
    setup_complete = False
    producer = None
    producer_snapshot_path = None
    tablebase = None

    try:
        tablebase = datagen_tablebase_attestation(config)
        cleanup_errors = clean_datagen_workspace(output_path)
        try:
            if os.path.isfile(log_path):
                os.remove(log_path)
        except OSError as error:
            cleanup_errors.append('%s: %s' % (log_path, error))
        if cleanup_errors:
            raise DatagenTransientError(
                'DATAGEN workspace cleanup failed: %s'
                % '; '.join(cleanup_errors)
            )

        with DatagenHeartbeat(config) as heartbeat:
            download_opening_book_compatible(
                test['book']['sha'],
                test['book']['source'],
                test['book']['name'],
                test['book'].get('raw_sha'),
            )
            dev_network = safe_download_network_weights(config, 'dev')
            dev_name = safe_download_engine(config, 'dev', dev_network)
            setup_complete = True
            # DATAGEN needs one deterministic compatibility check. Its NPS is
            # informational only and is never used to scale generation work.
            dev_nps = safe_run_benchmarks(
                config, 'dev', dev_name, dev_network, bench_threads=1
            )
            try:
                ServerReporter.report_nps(config, dev_nps, dev_nps).raise_for_status()
            except BadVersionException:
                raise
            except Exception as error:
                raise DatagenTransientError(
                    'DATAGEN NPS report failed: %s' % error
                ) from error

            if heartbeat.stop_requested.is_set():
                raise DatagenStopped()

            if chunk.get('producer_artifact_required'):
                producer_path = os.path.join('Engines', dev_name)
                # Preserve .exe on Windows (and any explicit executable
                # suffix elsewhere) while keeping the snapshot private to this
                # exact chunk attempt.
                producer_snapshot_path = (
                    output_path + '.producer' + os.path.splitext(producer_path)[1]
                )
                try:
                    producer_sha256, producer_bytes = snapshot_datagen_producer(
                        producer_path, producer_snapshot_path
                    )
                    snapshot_identity = datagen_file_sha256(
                        producer_snapshot_path
                    )
                    if snapshot_identity != (producer_sha256, producer_bytes):
                        raise OSError(
                            'DATAGEN producer snapshot changed after copy'
                        )
                except Exception as error:
                    raise DatagenTransientError(
                        'DATAGEN producer snapshot failed: %s' % error
                    ) from error
                producer_commit = test['dev']['sha'].lower()
                producer = upload_datagen_producer(
                    config,
                    producer_snapshot_path,
                    producer_sha256,
                    producer_bytes,
                    producer_commit,
                    heartbeat,
                )
                # Upload can be long on a slow worker. Authenticate the private
                # snapshot once more immediately before it becomes the Popen
                # image, rather than trusting the earlier copy-time identity.
                if datagen_file_sha256(producer_snapshot_path) != (
                    producer_sha256, producer_bytes
                ):
                    raise DatagenTransientError(
                        'DATAGEN producer snapshot changed before launch'
                    )

            generator_args = (
                config,
                (
                    producer_snapshot_path
                    if producer_snapshot_path is not None
                    else os.path.join('Engines', dev_name)
                ),
                output_path,
                log_path,
                heartbeat,
                dev_network,
            )
            # Preserve the legacy call shape for ordinary DATAGEN and existing
            # test/extension hooks.  The extra keyword exists only on the new
            # tablebase-backed path.
            if tablebase is None:
                if producer is None:
                    run_datagen_command(*generator_args)
                else:
                    run_datagen_command(*generator_args, producer)
            else:
                run_datagen_command(
                    *generator_args,
                    producer=producer,
                    tablebase=tablebase,
                )

            compress_datagen_output(output_path, compressed_path, heartbeat)
            try:
                sha256, byte_count = datagen_file_sha256(compressed_path)
            except Exception as error:
                raise DatagenTransientError(
                    'DATAGEN archive hashing failed: %s' % error
                ) from error
            response = upload_datagen_output(
                config,
                compressed_path,
                sha256,
                byte_count,
                heartbeat,
                producer,
                tablebase,
            )

            print(
                'Uploaded DATAGEN chunk %d: %d bytes, sha256 %s (%d/%d complete)'
                % (
                    chunk['chunk_idx'],
                    byte_count,
                    sha256,
                    response['completed_chunks'],
                    response['total_chunks'],
                )
            )

    except DatagenStopped:
        print('DATAGEN chunk stopped by server; local process tree was terminated')
        return

    except BadVersionException:
        raise

    except (OpenBenchBuildFailedException,
            OpenBenchMissingArtifactException,
            OpenBenchBadBenchException):
        if test['id'] not in config.blacklist:
            config.blacklist.append(test['id'])
        raise

    except DatagenTransientError as error:
        handle_datagen_transient_failure(config, chunk, log_path, error)
        # Do not blacklist the workload: another worker (or this worker on a
        # later scheduler pass) may safely retry the now-requeued chunk.
        raise

    except Exception as error:
        if (
            not setup_complete
            and not isinstance(error, DATAGEN_DETERMINISTIC_SETUP_ERRORS)
        ):
            transient_error = DatagenTransientError(
                'DATAGEN setup failed before generator launch: %s' % error
            )
            handle_datagen_transient_failure(
                config, chunk, log_path, transient_error
            )
            raise transient_error from error

        logs = datagen_log_tail(log_path)
        summary = 'DATAGEN chunk %d failed: %s' % (chunk['chunk_idx'], error)
        try:
            ServerReporter.report_engine_error(config, summary, logs)
        except Exception:
            traceback.print_exc()
        if test['id'] not in config.blacklist:
            config.blacklist.append(test['id'])
        raise

    finally:
        for cleanup_error in clean_datagen_workspace(output_path):
            print('[Note] DATAGEN cleanup deferred: %s' % cleanup_error)
        try:
            if os.path.isfile(log_path):
                os.remove(log_path)
        except OSError as error:
            print('[Note] DATAGEN log cleanup deferred: %s' % error)


def complete_workload(config):

    if config.workload['test']['type'] == 'DATAGEN' and config.workload['test'].get('datagen'):
        return complete_datagen_workload(config)

    # Download the opening book, throws an exception on corruption
    download_opening_book_compatible(
        config.workload['test']['book']['sha'   ],
        config.workload['test']['book']['source'],
        config.workload['test']['book']['name'  ],
        config.workload['test']['book'].get('raw_sha'),
    )

    # Download each NNUE file, throws an exception on corruption
    dev_network  = safe_download_network_weights(config, 'dev' )
    base_network = safe_download_network_weights(config, 'base')

    # Build or download each engine, or exit if an error occured
    dev_name  = safe_download_engine(config, 'dev' , dev_network )
    base_name = safe_download_engine(config, 'base', base_network)

    # Datagen creates a book on-the-fly
    if config.workload['test']['type'] == 'DATAGEN':
        safe_create_genfens_opening_book(config, dev_name, dev_network)

    # Run the benchmarks and compute the scaling NPS value
    dev_nps  = safe_run_benchmarks(config, 'dev' , dev_name , dev_network )
    base_nps = safe_run_benchmarks(config, 'base', base_name, base_network)
    ServerReporter.report_nps(config, dev_nps, base_nps)

    # Scale the engines together, using their NPS relative to expected
    dev_factor  = config.workload['test']['dev' ]['nps'] / dev_nps
    base_factor = config.workload['test']['base']['nps'] / base_nps
    avg_factor  = (dev_factor + base_factor) / 2

    print () # Record this information
    print ('Scale Factor Dev  : %.4f' % (dev_factor ))
    print ('Scale Factor Base : %.4f' % (base_factor))
    print ('Scale Factor Avg  : %.4f' % (avg_factor ))

    # Server knows how many copies of Cutechess we should run
    cutechess_cnt   = config.workload['distribution']['cutechess-count']
    concurrency_per = config.workload['distribution']['concurrency-per']
    games_per       = config.workload['distribution']['games-per-cutechess']

    print () # Record this information
    print ('%d cutechess copies' % (cutechess_cnt))
    print ('%d concurrent games per copy' % (concurrency_per))
    print ('%d total games per cutechess copy\n' % (games_per))

    # Scale using the base factor only, in the event of a cross-engine test
    dev_engine    = config.workload['test']['dev' ]['engine']
    base_engine   = config.workload['test']['base']['engine']
    scale_factor  = base_factor if dev_engine != base_engine else avg_factor

    # Launch and manage all of the Cutechess workers
    with ThreadPoolExecutor(max_workers=cutechess_cnt) as executor:

        timestamp  = time.time()
        results    = multiprocessing.Queue()
        abort_flag = threading.Event()

        tasks = [] # Create each of the Cutechess workers
        for x in range(cutechess_cnt):
            cmd = build_cutechess_command(config, dev_name, base_name, scale_factor, timestamp, x)
            tasks.append(executor.submit(run_and_parse_cutechess, config, cmd, x, results, abort_flag))

        # Process the Queue until we exit, finish, or are told to stop by the server
        try:
            rr = ResultsReporter(config, tasks, results, abort_flag)
            rr.process_until_finished()

            # A stop response can return from process_until_finished() while
            # runner threads are alive. Terminate them before inspecting
            # futures or PGNs; wait is bounded and send_runner_errors() is
            # non-blocking even if a stubborn process survives.
            if abort_flag.is_set():
                Cutechess.kill_everything(dev_name, base_name)
                wait(tasks, timeout=10)

            runner_failures = rr.send_runner_errors()
            rr.send_errors(timestamp, cutechess_cnt)
            Cutechess.kill_everything(dev_name, base_name)

        # Kill everything during an Exception, but print it
        except (Exception, KeyboardInterrupt):
            traceback.print_exc()
            abort_flag.set()
            Cutechess.kill_everything(dev_name, base_name)
            raise

        if runner_failures:
            raise RuntimeError(
                '%d game runner copies failed; diagnostics were sent to OpenBench'
                % runner_failures
            )

        # Upload the PGN if requested
        if config.workload['test']['upload_pgns'] != 'FALSE':
            compact    = config.workload['test']['upload_pgns'] == 'COMPACT'
            pgn_files  = [Cutechess.pgn_name(config, timestamp, x) for x in range(cutechess_cnt)]
            ServerReporter.report_pgn(config, compress_list_of_pgns(pgn_files, scale_factor, compact))

def safe_download_network_weights(config, branch):

    # Wraps utils.py:download_network()
    # May raise OpenBenchCorruptedNetworkException

    engine   = config.workload['test'][branch]['engine' ]
    net_name = config.workload['test'][branch]['netname']
    net_sha  = config.workload['test'][branch]['network']
    net_path = os.path.join('Networks', net_sha)

    # Not all engines use Network files
    if not net_sha or net_sha == 'None':
        return None

    credentials = (config.server, config.username, config.password)
    download_network(*credentials, engine, net_name, net_sha, net_path)

    return net_path

def safe_download_engine(config, branch, net_path):

    # Wraps utils.py:download_public_engine() and utils.py:download_private_engine()

    engine      = config.workload['test'][branch]['engine']
    branch_name = config.workload['test'][branch]['name']
    commit_sha  = config.workload['test'][branch]['sha']
    source      = config.workload['test'][branch]['source']
    private     = config.workload['test'][branch]['private']

    # Historical PGN DATAGEN uses the regular playing engine. Only the generic
    # chunk protocol has an in-engine generator and therefore a separate role.
    generic_datagen = (
        config.workload['test']['type'] == 'DATAGEN'
        and bool(config.workload['test'].get('datagen'))
    )
    build_role = 'datagen' if generic_datagen else 'play'

    if generic_datagen and private:
        raise DatagenConfigurationError(
            'Generic DATAGEN does not support private engine artifacts: '
            'their artifact metadata has no play/data-generator role'
        )

    bin_name = engine_binary_name(
        engine, commit_sha, net_path, private, build_role
    )
    out_path = os.path.join('Engines', bin_name)

    if private:

        try:
            return download_private_engine(
                engine, branch_name, source, out_path, config.cpu_name, config.cpu_flags)

        except OpenBenchMissingArtifactException as error:
            ServerReporter.report_missing_artifact(config, branch, error.name, error.logs)
            raise

    else:

        make_path = config.workload['test'][branch]['build']['path']
        compiler  = config.compilers[engine][0]

        try:
            return download_public_engine(
                engine,
                net_path,
                branch_name,
                source,
                make_path,
                out_path,
                compiler,
                commit_sha,
                build_role,
            )

        except OpenBenchBuildFailedException as error:

            print ('Failed to build %s-%s...\n\nCompiler Output:' % (engine, branch_name))
            for line in error.logs.split('\n'):
                print ('> %s' % (line))
            print ()

            config.blacklist.append(config.workload['test']['id'])
            ServerReporter.report_build_fail(config, branch, error.logs)
            raise

def safe_create_genfens_opening_book(config, dev_name, dev_network):

    try: create_genfens_opening_book(config, dev_name, dev_network)

    except OpenBenchFailedGenfensException as error:
        ServerReporter.report_engine_error(config, error.message)
        raise

def safe_run_benchmarks(
    config, branch, engine, network, bench_threads=None
):

    name     = config.workload['test'][branch]['name']
    private  = config.workload['test'][branch]['private']
    expected = int(config.workload['test'][branch]['bench'])
    binary   = os.path.join('Engines', engine)
    threads  = config.threads if bench_threads is None else bench_threads

    try:
        print('\nRunning %dx Benchmarks for %s' % (threads, name))
        speed, nodes = bench.run_benchmark(
            binary, network, private, threads, 1, expected)

    except OpenBenchBadBenchException as error:
        ServerReporter.report_bad_bench(config, error.message)
        raise

    print('Bench for %s is %d' % (name, nodes))
    print('Speed for %s is %d' % (name, speed))
    return speed


def build_cutechess_command(config, dev_cmd, base_cmd, scale_factor, timestamp, cutechess_idx):

    # Identical flag construction for every runner: the uci-pair-runner
    # accepts the same command line surface as cutechess-ob, and emits
    # cutechess-compatible output, so nothing downstream changes
    flags  = ' ' + Cutechess.basic_settings(config)
    flags += ' ' + Cutechess.concurrency_settings(config)
    flags += ' ' + Cutechess.adjudication_settings(config)
    flags += ' ' + Cutechess.engine_settings(config, dev_cmd, 'dev', scale_factor, cutechess_idx)
    flags += ' ' + Cutechess.engine_settings(config, base_cmd, 'base', scale_factor, cutechess_idx)
    flags += ' ' + Cutechess.book_settings(config, cutechess_idx)
    flags += ' ' + Cutechess.pgnout_settings(config, timestamp, cutechess_idx)

    # Dispatch on the VARIANTS routing table: cutechess-ob, or uci_pair_runner.py
    return runner_base_command(config) + flags

def cutechess_command_argv(command):

    # The command is kept as a string while it is assembled for compatibility
    # with the existing diagnostics and PGN error reporting. Serialize it to
    # argv here without splitting quoted UCI option names on whitespace.
    tokens = re.findall(r'"[^"]*"|\S+', command)
    return [
        token[1:-1]
        if len(token) >= 2 and token[0] == token[-1] == '"'
        else token
        for token in tokens
    ]

def run_and_parse_cutechess(config, command, cutechess_idx, results_queue, abort_flag):

    launch_delay = Cutechess.launch_stagger_seconds(config, cutechess_idx)
    if launch_delay:
        print('[#%d] Delaying game runner launch by %.3f seconds'
              % (cutechess_idx, launch_delay))
        if abort_flag is not None:
            if abort_flag.wait(launch_delay):
                return None
        else:
            time.sleep(launch_delay)

    print('\n[#%d] Launching Cutechess...\n%s\n' % (cutechess_idx, command))
    cutechess = Popen(
        cutechess_command_argv(command), stdout=PIPE, stderr=STDOUT
    )

    results = {

        'trinomial'   : [0, 0, 0],       # LDW
        'pentanomial' : [0, 0, 0, 0, 0], # LL DL DD DW WW
        'games'       : {},              # game_id : result_str

        'crashes'     : 0,               # " disconnect" or "connection stalls"
        'timelosses'  : 0,               # " loses on time "
        'illegals'    : 0,               # " illegal move "
    }

    output_tail = deque(maxlen=80)
    started_games = 0
    finished_games = 0
    workload = getattr(config, 'workload', {})
    expected_games = workload.get('distribution', {}).get(
        'games-per-cutechess'
    )

    while True:

        # Read each line of output until the pipe closes and we get "" back
        line = cutechess.stdout.readline().strip().decode(
            'utf-8', errors='replace'
        )
        if not line:
            break

        output_tail.append(line)

        if abort_flag is not None and abort_flag.is_set():
            break

        if 'Started game' in line:
            started_games += 1

        if 'Started game' not in line and 'Score of' not in line:
            print('[#%d] %s' % (cutechess_idx, line))

        if 'Finished game' in line:
            finished_games += 1
            Cutechess.update_results(results, line)

        # Add to the results queue every time we have a game-pair finished
        if any(results['pentanomial']):

            # Place the results into the Queue, and be sure to copy the lists
            if results_queue is not None:
                results_queue.put({
                    'trinomial'     : list(results['trinomial']),
                    'pentanomial'   : list(results['pentanomial']),
                    'crashes'       : results['crashes'],
                    'timelosses'    : results['timelosses'],
                    'illegals'      : results['illegals'],
                    'cutechess_idx' : cutechess_idx,
                })

            # Clear out all the results, so we can start collecting a new set
            results['trinomial'  ] = [0, 0, 0]
            results['pentanomial'] = [0, 0, 0, 0, 0]
            results['crashes'    ] = 0
            results['timelosses' ] = 0
            results['illegals'   ] = 0

    # Do not classify an intentional server/user abort as a runner failure;
    # kill_everything() owns terminating the remaining subprocesses.
    if abort_flag is not None and abort_flag.is_set():
        return None

    returncode = cutechess.wait()
    completed_assignment = (
        finished_games > 0
        and (expected_games is None or finished_games >= expected_games)
    )
    if returncode == 0 and completed_assignment:
        return None

    if returncode:
        message = 'game runner exited with code %d' % returncode
    elif finished_games:
        if expected_games is None:
            message = 'game runner exited after completing %d games' % (
                finished_games
            )
        else:
            message = 'game runner exited after completing only %d/%d games' % (
                finished_games, expected_games
            )
    else:
        message = 'game runner exited without completing a game'

    if not finished_games:
        message += ' before the first completed game'

    return {
        'cutechess_idx': cutechess_idx,
        'returncode': returncode,
        'started_games': started_games,
        'finished_games': finished_games,
        'expected_games': expected_games,
        'message': message,
        'logs': '\n'.join(output_tail),
    }

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                           #
#                                                                           #
#                                                                           #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def parse_arguments(client_args):

    # Pretty formatting
    p = argparse.ArgumentParser(
        formatter_class=lambda prog:
            argparse.ArgumentDefaultsHelpFormatter(prog, max_help_position=10)
    )

    # Arguments specific to worker.py
    p.add_argument('-T', '--threads' , help='Total Threads'           , required=True      )
    p.add_argument('-N', '--nsockets', help='Number of Sockets'       , required=True      )
    p.add_argument('-I', '--identity', help='Machine pseudonym'       , required=False     )
    p.add_argument(      '--syzygy'  , help='Syzygy WDL'              , required=False     )
    p.add_argument(      '--atomic-syzygy', help='Atomic Syzygy WDL'  , required=False     )
    p.add_argument(      '--atomic-syzygy-manifest',
                        help='Atomic Syzygy inventory JSON', required=False)
    p.add_argument(      '--fleet'   , help='Fleet Mode'              , action='store_true')
    p.add_argument(      '--focus'   , help='Prefer certain engine(s)', nargs='+'          )

    # Ignore unknown arguments ( from client )
    worker_args, unknown = p.parse_known_args()

    # Add the client args (Username, Password, and Server) to the worker args
    return argparse.Namespace(**{ **vars(client_args), **vars(worker_args) })

def run_openbench_worker(client_args):

    args   = parse_arguments(client_args) # Merge client.py and worker.py args
    config = Configuration(args)          # Holds System info, args, and Workload info

    setup_error      = '[Note] Unable to establish initial connection with the Server!'
    connection_error = '[Note] Unable to reach the server to request a workload!'

    try_forever(server_configure_worker, [config], setup_error)

    if IS_LINUX:
        set_cutechess_permissions()

    while True:
        try:
            # Cleanup on each workload request
            cleanup_client()

            # Keep asking for a workload until we get a response
            try_forever(server_request_workload, [config], connection_error)

            # Complete the workload if there was work to be done
            if config.workload: complete_workload(config)

            # Otherwise --fleet workers will exit when there is no work
            elif config.fleet: time.sleep(TIMEOUT_ERROR); sys.exit()

            # In either case, wait before requesting again
            else: time.sleep(TIMEOUT_WORKLOAD)

            # Check for exit signal via openbench.exit
            if os.path.isfile('openbench.exit'):
                print('Exited via openbench.exit')
                sys.exit()

        except BadVersionException:
            raise BadVersionException()

        except Exception:
            traceback.print_exc()
            time.sleep(TIMEOUT_ERROR)
