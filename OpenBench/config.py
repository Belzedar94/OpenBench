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

import hashlib
import json
import os
import re
import sys
import traceback

from OpenSite.settings import PROJECT_PATH

# Assets that base.html links with the cache-busting token below.
OPENBENCH_STATIC_ASSETS = [
    os.path.join(PROJECT_PATH, 'OpenBench', 'static'),
    os.path.join(PROJECT_PATH, 'atomicdb', 'static', 'atomicdb', 'theme.js'),
]

def compute_static_version(base='v7'):

    # base.html appends this token to every stylesheet and script URL. While it
    # was a hand-maintained constant, any edit to style.css that forgot to bump
    # it kept the URL identical, so browsers and proxies happily served the
    # previous file from cache -- which is exactly how the theme switch first
    # shipped with new markup and old CSS. Folding a digest of the assets into
    # the token makes the URL move whenever the assets do. Size and mtime are
    # enough (and cheap); the token only has to change, not be reproducible.

    digest = hashlib.sha256()

    try:
        for asset in OPENBENCH_STATIC_ASSETS:
            paths = [asset]
            if os.path.isdir(asset):
                paths = sorted(
                    os.path.join(root, name)
                    for root, dirs, names in os.walk(asset)
                    for name in names)

            for path in paths:
                stats = os.stat(path)
                digest.update(os.path.basename(path).encode('utf-8'))
                digest.update(str(stats.st_size).encode('utf-8'))
                digest.update(str(int(stats.st_mtime)).encode('utf-8'))

    except OSError:
        return base # Unreadable assets: fall back to the manual token

    return '%s-%s' % (base, digest.hexdigest()[:10])

OPENBENCH_STATIC_VERSION = compute_static_version()

OPENBENCH_CONFIG = None # Initialized by OpenBench/apps.py

def create_openbench_config():

    with open(os.path.join(PROJECT_PATH, 'Config', 'config.json')) as fin:
        config_dict = json.load(fin)
        verify_general_config(config_dict)


    config_dict['books'] = {
        book : load_book_config(book) for book in config_dict['books']
    }

    config_dict['engines'] = {
        engine : load_engine_config(engine) for engine in config_dict['engines']
    }

    return config_dict

def load_book_config(book_name):

    with open(os.path.join(PROJECT_PATH, 'Books', '%s.json' % (book_name))) as fin:
        conf = json.load(fin)

    # Draft manifests may live in-tree before their immutable release receipt
    # exists, but adding one to Config/config.json must fail closed.
    assert type(conf.get('onboarding_ready', True)) == bool
    assert conf.get('onboarding_ready', True)
    assert type(conf.get('datagen_enabled', True)) == bool
    assert type(conf.get('sha')) == str
    assert type(conf.get('source')) == str
    variant_contract = conf.get('variant_contract')
    assert variant_contract is None or (
        type(variant_contract) == str
        and re.fullmatch(r'[A-Z0-9][A-Z0-9_]{0,63}', variant_contract)
    )
    if 'raw_sha' in conf:
        assert type(conf['raw_sha']) == str
        assert re.fullmatch(r'[0-9a-fA-F]{64}', conf['raw_sha'])

    return conf

def load_engine_config(engine_name):

    try:
        with open(os.path.join(PROJECT_PATH, 'Engines', '%s.json' % (engine_name))) as fin:
            conf = json.load(fin)

        # Engine scaffolds remain inert until branches, benches, NPS, artifacts,
        # and any required credentials have concrete receipts.
        assert type(conf.get('onboarding_ready', True)) == bool
        assert conf.get('onboarding_ready', True)
        verify_engine_basics(conf)
        verify_engine_build(engine_name, conf)

        for preset_type in ['test_presets', 'tune_presets', 'datagen_presets']:
            if preset_type not in conf.keys() or 'default' not in conf[preset_type].keys():
                conf[preset_type] = { 'default' : {} }

        assert 'default' in conf['test_presets'].keys()
        assert 'default' in conf['tune_presets'].keys()
        assert 'default' in conf['datagen_presets'].keys()

        for key, test_preset in conf['test_presets'].items():
            verify_engine_test_preset(test_preset)

        for key, tune_preset in conf['tune_presets'].items():
            verify_engine_tune_preset(tune_preset)

        for key, datagen_preset in conf['datagen_presets'].items():
          verify_engine_datagen_preset(datagen_preset)

    except Exception as error:
        traceback.print_exc()
        print ('%s has errors on the configuration json' % (engine_name))
        sys.exit()

    return conf


def verify_general_config(conf):

    assert type(conf.get("client_version"  ) == int)
    assert type(conf.get("client_repo_url" ) == str)
    assert type(conf.get("client_repo_ref" ) == str)

    assert type(conf.get("use_cross_approval"         ) == bool)
    assert type(conf.get("require_login_to_view"      ) == bool)
    assert type(conf.get("require_manual_registration") == bool)
    assert type(conf.get("balance_engine_throughputs" ) == bool)

def verify_engine_basics(conf):

    assert type(conf.get('private')) == bool
    assert type(conf.get('nps')) == int and conf['nps'] > 0
    assert type(conf.get('source')) == str
    assert type(conf.get('build')) == dict
    variant_contract = conf.get('variant_contract')
    assert variant_contract is None or (
        type(variant_contract) == str
        and re.fullmatch(r'[A-Z0-9][A-Z0-9_]{0,63}', variant_contract)
    )
    assert conf.get('tablebase_family', 'standard') in ['standard', 'atomic']
    assert type(conf.get('worker_max_concurrency', 0)) == int
    assert 0 <= conf.get('worker_max_concurrency', 0) <= 1024
    assert type(conf.get('cutechess_max_concurrency', 0)) == int
    assert 0 <= conf.get('cutechess_max_concurrency', 0) <= 1024
    assert type(conf.get('cutechess_launch_stagger_ms', 0)) == int
    assert 0 <= conf.get('cutechess_launch_stagger_ms', 0) <= 60000
    manifest = conf.get('tablebase_manifest_sha256')
    assert manifest is None or re.fullmatch('[0-9a-fA-F]{64}', manifest)

def verify_engine_build(engine_name, conf):

    assert type(conf['build'].get('cpuflags')) == list
    assert all(type(x) == str for x in conf['build']['cpuflags'])

    assert type(conf['build'].get('systems')) == list
    assert all(type(x) == str for x in conf['build']['systems'])

    artifact_roles = conf['build'].get('artifact_roles', ['play'])
    assert type(artifact_roles) == list
    assert artifact_roles
    assert len(artifact_roles) == len(set(artifact_roles))
    assert all(role in ['play', 'datagen'] for role in artifact_roles)
    if conf['private']:
        assert 'play' in artifact_roles

    datagen_provenance = conf['build'].get('datagen_provenance')
    if datagen_provenance is not None:
        assert not conf['private']
        assert 'datagen' in artifact_roles
        assert type(datagen_provenance) == dict
        assert set(datagen_provenance) == {'source_tree', 'src_tree'}
        assert all(
            type(value) == str and re.fullmatch('[0-9a-f]{40}', value)
            for value in datagen_provenance.values()
        )

    if conf['private']: # Private engines require a PAT
        fname = 'credentials.%s' % (engine_name.replace(' ', '').lower())
        assert os.path.exists(os.path.join(PROJECT_PATH, 'Config', fname))

    else: # Public engines require a Makefile path and valid compilers
        assert type(conf['build'].get('path')) == str
        assert type(conf['build'].get('compilers')) == list
        assert all(type(x) == str for x in conf['build']['compilers'])

def verify_engine_test_preset(test_preset):

    valid_keys = [

        'both_branch',
        'both_bench',
        'both_network',
        'both_options',
        'both_time_control',

        'dev_branch',
        'dev_bench',
        'dev_network',
        'dev_options',
        'dev_time_control',

        'base_branch',
        'base_bench',
        'base_network',
        'base_options',
        'base_time_control',

        'test_bounds',
        'test_confidence',
        'test_max_games',

        'book_name',
        'upload_pgns',
        'priority',
        'throughput',
        'workload_size',
        'syzygy_wdl',

        'syzygy_adj',
        'win_adj',
        'draw_adj',
    ]

    for key in test_preset.keys():
        if key not in valid_keys:
            raise Exception('Contains invalid key: %s' % (key))

def verify_engine_tune_preset(tune_preset):

    valid_keys = [

        'dev_branch',
        'dev_bench',
        'dev_network',
        'dev_options',
        'dev_time_control',

        'spsa_reporting_type',
        'spsa_distribution_type',
        'spsa_alpha',
        'spsa_gamma',
        'spsa_A_ratio',
        'spsa_iterations',
        'spsa_pairs_per',

        'book_name',
        'upload_pgns',
        'priority',
        'throughput',
        'syzygy_wdl',

        'syzygy_adj',
        'win_adj',
        'draw_adj',
    ]

    for key in tune_preset.keys():
        if key not in valid_keys:
            raise Exception('Contains invalid key: %s' % (key))

def verify_engine_datagen_preset(datagen_preset):

    valid_keys = [

        'both_branch',
        'both_bench',
        'both_network',
        'both_options',
        'both_time_control',

        'dev_branch',
        'dev_bench',
        'dev_network',
        'dev_options',
        'dev_time_control',

        'base_branch',
        'base_bench',
        'base_network',
        'base_options',
        'base_time_control',

        'book_name',
        'upload_pgns',
        'priority',
        'throughput',
        'workload_size',
        'syzygy_wdl',

        'syzygy_adj',
        'win_adj',
        'draw_adj',

        'datagen_custom_genfens',
        'datagen_play_reverses',
        'datagen_max_games',

        'datagen_command',
        'datagen_total_count',
        'datagen_positions_per_chunk',
        'datagen_base_seed',
        'datagen_teacher_mode',
        'datagen_teacher_id',
        'datagen_publication_protocol',
        'datagen_campaign_id',
        'datagen_external_workload_id',
        'datagen_role',
        'datagen_cohort',
    ]

    for key in datagen_preset.keys():
        if key not in valid_keys:
            raise Exception('Contains invalid key: %s' % (key))
