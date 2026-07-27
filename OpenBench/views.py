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

import os, base64, hashlib, datetime, json, secrets, stat, sys, re, time

import django.http
import django.shortcuts
import django.contrib.auth

import OpenBench.config
import OpenBench.datagen
import OpenBench.datagen_publication
import OpenBench.index_metrics
import OpenBench.stats
import OpenBench.utils

from OpenBench.workloads.create_workload import create_workload
from OpenBench.workloads.get_workload import get_workload
from OpenBench.workloads.modify_workload import modify_workload
from OpenBench.workloads.verify_workload import verify_workload
from OpenBench.workloads.view_workload import view_workload

from OpenBench.config import OPENBENCH_CONFIG, OPENBENCH_STATIC_VERSION
from OpenSite.settings import PROJECT_PATH

from OpenBench.models import *
from django.contrib.auth.models import User
from OpenSite.settings import MEDIA_ROOT

from django.db import OperationalError, transaction
from django.db.models import Count, F, Q
from django.http import HttpResponse, JsonResponse, FileResponse
from django.views.decorators.csrf import csrf_exempt
from django.core.files.storage import FileSystemStorage
from django.core.files.base import ContentFile
from django.utils import timezone

from wsgiref.util import FileWrapper

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                              GENERAL UTILITIES                              #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

ERROR_MESSAGES = {
    'disabled'            : 'Account has not been enabled. Contact an Administrator',
    'fakeuser'            : 'This is not a real OpenBench User. Create an OpenBench account',
    'requires_login'      : 'All pages require a user login to access',
    'manual_registration' : 'Registration can only be done via an Administrator',
}

class UnableToAuthenticate(Exception):
    pass

def render(request, template, content={}, always_allow=False, error=None, warning=None, status=None):

    data = content.copy()
    data.update({ 'config' : OPENBENCH_CONFIG })
    data.update({ 'static_version' : OPENBENCH_STATIC_VERSION })

    if OPENBENCH_CONFIG['require_login_to_view']:
        if not request.user.is_authenticated and not always_allow:
            return redirect(request, '/login/',  error=ERROR_MESSAGES['requires_login'])

    if request.user.is_authenticated:

        profile = Profile.objects.filter(user=request.user)
        data.update({'profile' : profile.first()})

        if profile.first() and not profile.first().enabled:
            request.session['error_message'] = ERROR_MESSAGES['disabled']

        elif request.user.is_authenticated and not profile.first():
            request.session['error_message'] = ERROR_MESSAGES['fakeuser']

    if error:
        request.session['error_message'] = error

    if warning:
        request.session['warning_message'] = error

    if status:
        request.session['status_message'] = status

    response = django.shortcuts.render(request, 'OpenBench/{0}'.format(template), data)

    for key in ['status_message', 'warning_message', 'error_message']:
        if key in request.session: del request.session[key]

    return response

def redirect(request, destination, error=None, warning=None, status=None):

    if error:
        request.session['error_message'] = error

    if warning:
        request.session['warning_message'] = warning

    if status:
        request.session['status_message'] = status

    return django.http.HttpResponseRedirect(destination)

def authenticate(request, requireEnabled=False):

    try:
        user = django.contrib.auth.authenticate(
            username = request.POST['username'],
            password = request.POST['password'])

        if requireEnabled:
            profile = OpenBench.models.Profile.objects.get(user=user)
            if not profile.enabled: raise UnableToAuthenticate()

    except Exception:
        raise UnableToAuthenticate()

    if user is None:
        raise UnableToAuthenticate()

    return user

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                            ADMINISTRATIVE VIEWS                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def register(request):

    if request.method == 'GET':
        if not OPENBENCH_CONFIG['require_manual_registration']:
            return render(request, 'register.html', always_allow=True)
        return redirect(request, '/login/', error=ERROR_MESSAGES['manual_registration'])

    if request.POST['password1'] != request.POST['password2']:
        return redirect(request, '/register/', error='Passwords do not match')

    if not request.POST['username'].isalnum():
        return redirect(request, '/register/', error='Alpha-numeric usernames Only')

    if User.objects.filter(username=request.POST['username']):
        return redirect(request, '/register/', error='That username is already taken')

    email    = request.POST['email']
    username = request.POST['username']
    password = request.POST['password1']

    user = User.objects.create_user(username, email, password)
    django.contrib.auth.login(request, user)
    Profile.objects.create(user=user)

    return redirect(request, '/index/')

def login(request):

    if request.method == 'GET':
        return render(request, 'login.html', always_allow=True)

    try:
        django.contrib.auth.login(request, authenticate(request))
        return redirect(request, '/index/')

    except UnableToAuthenticate:
        return redirect(request, '/login/', error='Unable to authenticate user')

def logout(request):

    django.contrib.auth.logout(request)
    return redirect(request, '/index/', status='Logged out')

def profile(request):

    if not request.user.is_authenticated:
        return redirect(request, '/login/')

    if not Profile.objects.filter(user=request.user).first():
        return redirect(request, '/index/')

    if request.method == 'GET':
        return render(request, 'profile.html')

    changes_message = ''
    if request.user.email != request.POST['email']:
        changes_message += 'Updated email address to %s' % (request.POST['email'])
        request.user.email = request.POST['email']
        request.user.save()

    if request.POST['password1'] != request.POST['password2']:
        return redirect(request, '/profile/', status=changes_message, error='Passwords do not match')

    if request.POST['password1']:
        request.user.set_password(request.POST['password1'])
        request.user.save()
        django.contrib.auth.login(request, request.user)
        changes_message += '\nUpdated password'

    return redirect(request, '/profile/', status=changes_message.removeprefix('\n'))

def profile_config(request):

    if not request.user.is_authenticated:
        return redirect(request, '/login/')

    if not (profile := Profile.objects.filter(user=request.user).first()):
        return redirect(request, 'index')

    if request.method == 'GET':
        return render(request, 'profile.html')

    changes = ''

    if (engine := request.POST.get('default-status', profile.engine)) != profile.engine:
        changes += 'Set %s as the default, replacing %s\n' % (engine, profile.engine)
        profile.engine = engine

    for engine in json.loads(request.POST.get('deleted-repos', '[]')):
        profile.repos.pop(engine, False)
        changes += 'Deleted Engine: %s\n' % (engine)

    for (engine, current_repo) in profile.repos.items():
        repo_name = request.POST.get('engine-repo-%s' % (engine), '').removesuffix('/')
        repo = 'https://github.com/%s' % (repo_name)

        if repo != current_repo and repo_name:
            changes += 'Updated Engine: %s to use %s\n' % (engine, repo)
            profile.repos[engine] = repo

    if changes:
        profile.save()

    engine_name = request.POST.get('new-engine-name', 'None')
    engine_repo = request.POST.get('new-engine-repo', '').removesuffix('/')

    if engine_name != 'None' and engine_repo:

        if not engine_repo.startswith('https://github.com/'):
            return redirect(request, '/profile/', error='Repositories must be on Github')

        if not profile.engine:
            profile.engine = engine_name

        changes += 'Added Engine: %s at %s' % (engine_name, engine_repo)
        profile.repos[engine_name] = engine_repo
        profile.save()

    return redirect(request, '/profile/', status=changes)

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                               TEST LIST VIEWS                               #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def index(request, page=1):

    pending   = OpenBench.utils.get_pending_tests()
    active    = OpenBench.utils.get_active_tests()
    completed = OpenBench.utils.get_completed_tests()
    awaiting  = OpenBench.utils.get_awaiting_tests()

    start, end, paging = OpenBench.utils.getPaging(completed, page, 'index')

    data = {
        'pending'   : pending,
        'active'    : active,
        'completed' : completed[start:end],
        'awaiting'  : awaiting,
        'paging'    : paging,
        'status'    : OpenBench.utils.getMachineStatus(),
        'dashboard_metrics': OpenBench.index_metrics.get_index_metrics(),
    }

    return render(request, 'index.html', data)

def user(request, username, page=1):

    pending   = OpenBench.utils.get_pending_tests().filter(author=username)
    active    = OpenBench.utils.get_active_tests().filter(author=username)
    completed = OpenBench.utils.get_completed_tests().filter(author=username)
    awaiting  = OpenBench.utils.get_awaiting_tests().filter(author=username)

    start, end, paging = OpenBench.utils.getPaging(completed, page, 'user/%s' % (username))

    data = {
        'pending'   : pending,
        'active'    : active,
        'completed' : completed[start:end],
        'awaiting'  : awaiting,
        'paging'    : paging,
        'status'    : OpenBench.utils.getMachineStatus(username),
    }

    return render(request, 'index.html', data)

def greens(request, page=1):

    completed = OpenBench.utils.get_completed_tests().filter(passed=True)
    start, end, paging = OpenBench.utils.getPaging(completed, page, 'greens')

    data = { 'completed' : completed[start:end], 'paging' : paging }
    return render(request, 'index.html', data)

def _regression_tests():
    return Test.objects.filter(
        finished=True,
        deleted=False,
        test_mode='GAMES',
        dev__name__startswith='regression-',
    )

def regression_index(request):

    engines = _regression_tests().values('dev_engine').annotate(
        test_count=Count('id'),
    ).order_by('dev_engine')

    return render(request, 'regression_index.html', { 'engines' : engines })

def regression_engine(request, engine):

    tests = list(
        _regression_tests()
        .filter(dev_engine=engine)
        .select_related('dev', 'base')
        .order_by('-creation', '-id')
    )

    if not tests:
        raise django.http.Http404('No regression measurements for this engine')

    for test in tests:
        results = test.results()
        lower, elo, upper = OpenBench.stats.Elo(results)
        test.regression_elo = elo
        test.regression_error = max(upper - elo, elo - lower)
        test.regression_los = 100.0 * OpenBench.stats.LOS(results)

    return render(request, 'regression_engine.html', {
        'engine' : engine,
        'tests'  : tests,
    })

def search(request):

    if request.method == 'GET':
        return render(request, 'search.html', {})

    tests = Test.objects.all()

    # Optional Selection box filters

    if request.POST['author']:
        tests = tests.filter(author=request.POST['author'])

    if request.POST['engine']:
        tests = tests.filter(Q(base_engine=request.POST['engine']) | Q(dev_engine=request.POST['engine']))

    if request.POST['opening-book']:
        tests = tests.filter(book_name=request.POST['opening-book'])

    if request.POST['test-mode']:
        tests = tests.filter(test_mode=request.POST['test-mode'])

    if request.POST['syzygy-wdl']:
        tests = tests.filter(syzygy_wdl=request.POST['syzygy-wdl'])

    # Checkboxes for Test statuses

    if 'show-greens' not in request.POST:
        tests = tests.annotate(x=F('elolower') + F('eloupper')).exclude(x__gte=0, passed=True)

    if 'show-yellows' not in request.POST:
        tests = tests.exclude(failed=True, wins__gte=F('losses'))

    if 'show-reds' not in request.POST:
        tests = tests.exclude(failed=True, wins__lt=F('losses'))

    if 'show-blues' not in request.POST:
        tests = tests.annotate(x=F('elolower') + F('eloupper')).exclude(x__lt=0, passed=True)

    if 'show-stopped' not in request.POST:
        tests = tests.exclude(passed=False, failed=False)

    if 'show-deleted' not in request.POST:
        tests = tests.exclude(deleted=True)

    # Remaining filtering is hard to do with standard Django queries

    filtered = []
    keywords = request.POST['keywords'].upper().split()

    tc_type   = request.POST['tc-type']
    tc_value  = request.POST['tc-value-input']
    tc_select = request.POST['tc-value-select']

    # Attempt to parse the time control

    try:
        if tc_value:
            tc_value = OpenBench.utils.TimeControl.parse(tc_value)
    except:
        return redirect(request, '/search/', error='Invalid Time Control')

    # Filter out tests

    for test in tests:

        # None of the keywords appear in the dev branch name
        if keywords and not any(x in test.dev.name.upper() for x in keywords):
            continue

        # Generic DATAGEN does not have a fixed engine Threads option or a
        # chess time control: {THREADS} is supplied by the assigned worker.
        # Keep it searchable when no time-control filter was requested and do
        # not force its intentionally empty options through int(None).
        if test.is_generic_datagen():
            if tc_type or tc_value:
                continue
            filtered.append(test)
            continue

        # Determine the max number of threads that either engine used
        dev_threads  = OpenBench.utils.extract_option(test.dev_options, 'Threads')
        base_threads = OpenBench.utils.extract_option(test.base_options, 'Threads')
        max_threads  = max(int(dev_threads), int(base_threads))

        # Extract requsted configuration
        select_value = request.POST['threads-select']
        input_value  = int(request.POST['threads-input'])

        # Requested Threads value did not match observed value
        if select_value == '='  and max_threads != input_value: continue
        if select_value == '>=' and max_threads  < input_value: continue
        if select_value == '<=' and max_threads  > input_value: continue

        # Filter our undesired time control types
        if tc_type and tc_type != OpenBench.utils.TimeControl.control_type(test.dev_time_control):
            continue

        # Filter tests of the same time control type, but outside our range
        if tc_value:

            search_base = OpenBench.utils.TimeControl.control_base(tc_value)
            test_base   = OpenBench.utils.TimeControl.control_base(test.dev_time_control)

            if tc_select == '='  and search_base != test_base: continue
            if tc_select == '>=' and search_base  > test_base: continue
            if tc_select == '<=' and search_base  < test_base: continue

        filtered.append(test)

    error = 'No matching tests found' if not len(filtered) else None
    return render(request, 'search.html', { 'tests' : reversed(filtered) }, error=error)

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                           GENERAL DATA TABLE VIEWS                          #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def users(request):

    data = { 'profiles' : Profile.objects.order_by('-games', '-tests') }
    return render(request, 'users.html', data)

def event(request, id):

    try:
        with open(os.path.join(MEDIA_ROOT, LogEvent.objects.get(id=id).log_file)) as fin:
            return render(request, 'event.html', { 'content' : fin.read() })
    except:
        return redirect(request, '/index/', error='No logs for event exist')

def events_actions(request, page=1):

    events = LogEvent.objects.all().filter(machine_id=0).order_by('-id')
    start, end, paging = OpenBench.utils.getPaging(events, page, 'events')

    data = { 'events' : events[start:end], 'paging' : paging };
    return render(request, 'events.html', data)

def events_errors(request, page=1):

    events = LogEvent.objects.all().exclude(machine_id=0).order_by('-id')
    start, end, paging = OpenBench.utils.getPaging(events, page, 'errors')

    data = { 'events' : events[start:end], 'paging' : paging };
    return render(request, 'errors.html', data)

def machines(request, machineid=None):

    if machineid == None:
        import datetime as _dt
        from django.utils import timezone as _tz
        from atomicdb.models import WorkerPing
        cutoff = _tz.now() - _dt.timedelta(minutes=5)
        data = {
            'machines'       : OpenBench.utils.getRecentMachines(),
            'atomic_workers' : WorkerPing.objects.filter(last_seen__gte=cutoff),
        }
        return render(request, 'machines.html', data)

    try:
        data = { 'machine' : OpenBench.models.Machine.objects.get(id=machineid) }
        return render(request, 'machine.html', data)

    except:
        return redirect(request, '/machines/', error='Machine does not exist')


# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                            TEST MANAGEMENT VIEWS                            #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def test(request, id, action=None):

    # Request is to modify or interact with the Test
    if action != None:
        return modify_workload(request, id, action)

    # Verify that the Test id exists
    if not (test := Test.objects.filter(id=id).first()):
        return redirect(request, '/index/', error='No such Test exists')

    # Verify that it is indeed a Test and not a Tune
    if test.test_mode == 'TUNE':
        return redirect(request, '/tune/%d' % (id))

    # Verify that it is indeed a Test and not Datagen
    if test.test_mode == 'DATAGEN':
        return redirect(request, '/datagen/%d' % (id))

    return view_workload(request, test, 'TEST')

def tune(request, id, action=None):

    # Request is to modify or interact with the Tune
    if action != None:
        return modify_workload(request, id, action)

    # Verify that the Tune id exists
    if not (tune := Test.objects.filter(id=id).first()):
        return redirect(request, '/index/', error='No such Tune exists')

    # Verify that it is indeed a Tune and not a Test
    if tune.test_mode == 'SPRT' or tune.test_mode == 'GAMES':
        return redirect(request, '/test/%d' % (id))

    # Verify that it is indeed a Tune and not Datagen
    if tune.test_mode == 'DATAGEN':
        return redirect(request, '/datagen/%d' % (id))

    return view_workload(request, tune, 'TUNE')

def datagen(request, id, action=None):

    # Request is to modify or interact with the Datagen
    if action != None:
        return modify_workload(request, id, action)

    # Verify that the Datagen id exists
    if not (datagen := Test.objects.filter(id=id).first()):
        return redirect(request, '/index/', error='No such Datagen exists')

    # Verify that it is indeed a Datagen and not a Tune
    if datagen.test_mode == 'TUNE':
        return redirect(request, '/tune/%d' % (id))

    # Verify that it is indeed a Datagen and not a Test
    if datagen.test_mode == 'SPRT' or datagen.test_mode == 'GAMES':
        return redirect(request, '/test/%d' % (id))

    return view_workload(request, datagen, 'DATAGEN')

def create_test(request):
    return create_workload(request, 'TEST')

def create_tune(request):
    return create_workload(request, 'TUNE')

def create_datagen(request):
    return create_workload(request, 'DATAGEN')

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                          NETWORK MANAGEMENT VIEWS                           #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def networks(request, engine=None, action=None, name=None, client=False):

    # Without an identifier and a valid action, all we can do is view the list
    if not name or action.upper() not in ['UPLOAD', 'DEFAULT', 'DELETE', 'DOWNLOAD', 'EDIT']:
        networks = Network.objects.all()
        if engine and engine in OPENBENCH_CONFIG['engines'].keys():
            networks = networks.filter(engine=engine)
        is_approver = request.user.is_authenticated \
            and Profile.objects.filter(user=request.user, approver=True).exists()
        return render(request, 'networks.html', {
            'networks' : list(networks.order_by('-id').values()),
            'is_approver' : is_approver })

    # Require logins. Clients will be artifically logged in
    if not request.user.is_authenticated:
        return django.http.HttpResponseRedirect('/login/')

    # Require approver credentials, unless downloading as a client
    if not client and not Profile.objects.get(user=request.user).approver:
        return django.http.HttpResponseRedirect('/index/')

    # Split out Uploads, since there is no logic to disambiguate the name
    if action.upper() == 'UPLOAD':
        return OpenBench.utils.network_upload(request, engine, name)

    # Push off all the actual effort to OpenBench.utils for all actions
    actions = {
        'DEFAULT'  : OpenBench.utils.network_default,
        'DELETE'   : OpenBench.utils.network_delete,
        'DOWNLOAD' : OpenBench.utils.network_download,
        'EDIT'     : OpenBench.utils.network_edit,
    }

    # Update the Network, if we can find one for the given name/sha256
    if (network := OpenBench.utils.network_disambiguate(engine, name)):
        return actions[action.upper()](request, engine, network)

    # Otherwise we could not find the Network, and cannot do anything
    return redirect(request, '/networks/', error='No network found with matching Sha')

def network_form(request):

    # Require logins. Clients will be artifically logged in
    if not request.user.is_authenticated:
        return django.http.HttpResponseRedirect('/login/')

    # Require approver credentials, unless downloading as a client
    if not Profile.objects.get(user=request.user).approver:
        return django.http.HttpResponseRedirect('/index/')

    # Get requests should not be reaching this point
    if request.method == 'GET':
        return render(request, 'uploadnet.html', {})

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                             OPENBENCH SCRIPTING                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

@csrf_exempt
def scripts(request):

    login(request) # All requests are attached to a User

    if request.POST['action'] == 'UPLOAD_NETWORK':
        engine = request.POST['engine']
        name   = request.POST['name']
        return networks(request, engine, 'upload', name)

    if request.POST['action'] == 'CREATE_TEST':
        return create_test(request)

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                              CLIENT HOOK VIEWS                              #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def verify_worker(function):

    def wrapped_verify_worker(*args, **kwargs):

        # Reject producer bodies from Content-Length before Django parses a
        # multipart upload into memory/temporary storage.  The reverse proxy
        # must enforce the same ceiling for chunked transfer encoding.
        if function.__name__ == 'client_submit_datagen_producer':
            raw_length = args[0].META.get('CONTENT_LENGTH')
            try:
                content_length = int(raw_length)
            except (TypeError, ValueError):
                content_length = -1
            if (
                content_length < 0
                or content_length
                > OpenBench.datagen.MAX_DATAGEN_PRODUCER_REQUEST_BYTES
            ):
                return JsonResponse(
                    {'error': 'DATAGEN producer request exceeds size limit'},
                    status=413,
                )

        # Get the machine, assuming it exists
        try: machine = Machine.objects.get(id=int(args[0].POST['machine_id']))
        except: return JsonResponse({ 'error' : 'Bad Machine Id' })

        # Ensure the Client is using the same version as the Server
        if machine.info['client_ver'] != OPENBENCH_CONFIG['client_version']:
            expected_ver = OPENBENCH_CONFIG['client_version']
            return JsonResponse({ 'error' : 'Bad Client Version: Expected %d' % (expected_ver)})

        # Use the secret token as our soft verification
        if machine.secret != args[0].POST['secret']:
            return JsonResponse({ 'error' : 'Invalid Secret Token' })

        # Disabling a worker account must revoke already-issued sessions as
        # well as prevent fresh credential authentication.  Otherwise a
        # connected worker can continue claiming and requeueing chunks until
        # its process is manually stopped.
        if not Profile.objects.filter(user=machine.user, enabled=True).exists():
            return JsonResponse({
                'error' : 'Worker Account Disabled',
                'stop'  : True,
            })

        # Otherwise, carry on, and pass along the machine
        return function(*args, machine)

    return wrapped_verify_worker

@csrf_exempt
def client_version_ref(request):

    # Verify the User's credentials
    try: user = authenticate(request, True)
    except UnableToAuthenticate:
        return JsonResponse({ 'error' : 'Bad Credentials' })

    # Enough information to download the right Client
    return JsonResponse({
        'client_version'  : OPENBENCH_CONFIG['client_version' ],
        'client_repo_url' : OPENBENCH_CONFIG['client_repo_url'],
        'client_repo_ref' : OPENBENCH_CONFIG['client_repo_ref'],
    })

@csrf_exempt
def client_get_build_info(request):

    ## Information pulled from the config about how to build each engine.
    ## Toss in a private flag as well to indicate the need for Github Tokens.

    data = {}
    for engine, config in OPENBENCH_CONFIG['engines'].items():
        data[engine] = config['build'].copy()
        data[engine]['private'] = config['private']
    return JsonResponse(data)

@csrf_exempt
def client_worker_info(request):

    # Verify the User's credentials
    try: user = authenticate(request, True)
    except UnableToAuthenticate:
        return JsonResponse({ 'error' : 'Bad Credentials' })

    # Attempt to fetch the Machine, or create a new one
    info    = json.loads(request.POST['system_info'])
    machine = OpenBench.utils.get_machine(info['machine_id'], user, info)

    # Indicate invalid request
    if not machine:
        return JsonResponse({ 'error' : 'Bad Machine Id' })

    # Save the machine's latest information and Secret Token for this session
    machine.info   = info
    machine.secret = secrets.token_hex(32)

    # Tag engines that the Machine can build and/or run with binaries
    machine.info['supported'] = []
    for engine, data in OPENBENCH_CONFIG['engines'].items():

        # Must have all CPU flags, for both Public and Private engines
        if any([flag not in machine.info['cpu_flags'] for flag in data['build']['cpuflags']]):
            continue

        # Private engines must have, or think they have, a Git Token
        if data['private'] and engine not in machine.info['tokens'].keys():
            continue

        # Public engines must have a compiler of a sufficient version
        if not data['private'] and engine not in machine.info['compilers'].keys():
            continue

        # Must match the Operating Systems supported by the engine
        if machine.info['os_name'] not in data['build']['systems']:
            continue

        # All requirements are met, and this Machine can play with the given engine
        machine.info['supported'].append(engine)

    # Finish up
    machine.save()

    # Pass back the Machine Id, and Secret Token for this session
    return JsonResponse({ 'machine_id' : machine.id, 'secret' : machine.secret })

@csrf_exempt
def client_get_network(request, engine, name):

    # Verify the User's credentials
    try: django.contrib.auth.login(request, authenticate(request, True))
    except UnableToAuthenticate: return HttpResponse('Bad Credentials')

    # Return the requested Neural Network file for the Client
    return networks(request, engine, 'DOWNLOAD', name, client=True)

@csrf_exempt
@verify_worker
def client_get_workload(request, machine):
    return JsonResponse(get_workload(request, machine))

@csrf_exempt
@verify_worker
def client_bench_error(request, machine):

    # Find and stop the test with the bad bench
    test = Test.objects.get(id=int(request.POST['test_id']))
    test.finished = True; test.save()

    # Log the error into the Events table
    LogEvent.objects.create(
        author     = machine.user.username,
        summary    = request.POST['error'],
        log_file   = '',
        machine_id = int(request.POST['machine_id']),
        test_id    = int(request.POST['test_id']))

    return JsonResponse({})

@csrf_exempt
@verify_worker
def client_submit_nps(request, machine):

    # Update the NPS counters for the GUI views
    machine.mnps      = float(request.POST['nps'     ]) / 1e6;
    machine.dev_mnps  = float(request.POST['dev_nps' ]) / 1e6;
    machine.base_mnps = float(request.POST['base_nps']) / 1e6;
    machine.save()

    # Pass back an empty JSON response
    return JsonResponse({})

@csrf_exempt
@verify_worker
def client_submit_error(request, machine):

    ## Report an error when working on test. This could be one three kinds.
    ## 1. Error building the engine. Does not compile, for whatever reason.
    ## 2. Error getting the artifacts. Does not exist, lacks credentials.
    ## 3. Error during actual gameplay. Timeloss, Disconnect, Crash, etc.

    released_datagen = False
    if request.POST.get('chunk_idx') is not None:
        try:
            test = Test.objects.get(id=int(request.POST['test_id']))
            if test.is_generic_datagen():
                released_datagen = OpenBench.datagen.requeue_chunk(
                    test.id,
                    int(request.POST['chunk_idx']),
                    machine,
                    int(request.POST['attempt']),
                    request.POST.get('error', ''),
                )
                if not released_datagen:
                    # Authenticate the attempt before creating a LogEvent or a
                    # Media file. Late ABA reports are untrusted input from an
                    # attempt that no longer owns the lease.
                    return JsonResponse(
                        {'error': 'DATAGEN chunk lease is stale'}, status=409
                    )
        except (KeyError, Test.DoesNotExist, ValueError):
            return JsonResponse(
                {'error': 'Malformed DATAGEN error report'}, status=400
            )

    # Log the Error into the Events table only after DATAGEN lease CAS.
    event = LogEvent.objects.create(
        author     = machine.user.username,
        summary    = request.POST['error'],
        log_file   = '',
        machine_id = int(request.POST['machine_id']),
        test_id    = int(request.POST['test_id']))

    # Save the Logs to /Media/ to be viewed later
    logfile = ContentFile(request.POST['logs'])
    FileSystemStorage().save('event%d.log' % (event.id), logfile)
    event.log_file = 'event%d.log' % (event.id); event.save()

    if released_datagen:
        machine.workload = 0
        machine.save(update_fields=['workload'])

    return JsonResponse({})

@csrf_exempt
@verify_worker
def client_submit_results(request, machine):

    # Returns {}, or { 'stop' : True }
    return JsonResponse(OpenBench.utils.update_test(request, machine))

@csrf_exempt
@verify_worker
def client_heartbeat(request, machine):

    test = Test.objects.get(id=int(request.POST['test_id']))

    # Generic DATAGEN heartbeats also renew the chunk lease. A client holding
    # a stale or reassigned lease is explicitly stopped.
    if test.is_generic_datagen():
        try:
            active = OpenBench.datagen.renew_chunk(
                test.id,
                int(request.POST['chunk_idx']),
                machine,
                int(request.POST['attempt']),
            )
        except (KeyError, ValueError):
            active = False
        machine.save()
        return JsonResponse(
            {} if active and not test.finished else {'stop': True}
        )

    # Force a refresh of the updated timestamp for gameplay workloads.
    machine.save()
    return JsonResponse([{}, { 'stop' : True }][test.finished])

@csrf_exempt
@verify_worker
def client_submit_pgn(request, machine):

    with transaction.atomic():

        # Format: test.result.book-index.pgn.bz2
        pgn            = PGN()
        pgn.test_id    = int(request.POST['test_id']   )
        pgn.result_id  = int(request.POST['result_id'] )
        pgn.book_index = int(request.POST['book_index'])
        pgn.save()

        # Save the .pgn.bz2 to /Media/
        FileSystemStorage().save(pgn.filename(), ContentFile(request.FILES['file'].read()))

    return JsonResponse({})


def _datagen_uploaded_digest(upload, max_bytes=None):
    digest = hashlib.sha256()
    byte_count = 0
    for block in upload.chunks():
        digest.update(block)
        byte_count += len(block)
        if max_bytes is not None and byte_count > max_bytes:
            raise ValueError('uploaded artifact exceeds its size limit')
    upload.seek(0)
    return digest.hexdigest(), byte_count


def _hash_regular_file(path):
    if os.path.islink(path) or not os.path.isfile(path):
        raise OSError('content-addressed artifact is not a regular file')

    digest = hashlib.sha256()
    byte_count = 0
    with open(path, 'rb') as data:
        before = os.fstat(data.fileno())
        while True:
            block = data.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            byte_count += len(block)
        after = os.fstat(data.fileno())

    identity = lambda stat: (
        stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
    )
    if identity(before) != identity(after) or byte_count != after.st_size:
        raise OSError('content-addressed artifact changed while hashing')
    return digest.hexdigest(), byte_count


def _open_regular_descriptor(path, expected_bytes=None):
    """Open one non-symlink regular-file snapshot and return that descriptor."""

    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise OSError('content-addressed artifact is not a regular file')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0)
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode):
            raise OSError('content-addressed artifact is not a regular file')
        if (
            (before.st_dev, before.st_ino)
            != (after.st_dev, after.st_ino)
            or (
                expected_bytes is not None
                and after.st_size != expected_bytes
            )
        ):
            raise OSError('content-addressed artifact changed while opening')
        return os.fdopen(descriptor, 'rb')
    except Exception:
        os.close(descriptor)
        raise


def _open_verified_producer_descriptor(artifact, storage=None):
    """Hash one immutable producer snapshot and rewind it for optional use."""

    if artifact is None or artifact.state != DatagenProducerArtifact.AVAILABLE:
        raise OSError('DATAGEN producer artifact is not available')
    storage = storage or FileSystemStorage()
    data = _open_regular_descriptor(
        storage.path(artifact.filename()), artifact.bytes
    )
    try:
        before = os.fstat(data.fileno())
        digest = hashlib.sha256()
        byte_count = 0
        while True:
            block = data.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            byte_count += len(block)
        after = os.fstat(data.fileno())
        identity = lambda item: (
            item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
        )
        if (
            identity(before) != identity(after)
            or byte_count != artifact.bytes
            or digest.hexdigest() != artifact.sha256
        ):
            raise OSError('DATAGEN producer artifact failed CAS verification')
        data.seek(0)
        return data
    except Exception:
        data.close()
        raise


def _cached_producer_available(artifact, storage=None):
    try:
        data = _open_verified_producer_descriptor(artifact, storage)
        data.close()
        return True
    except OSError:
        return False


def _fsync_promoted_file(path):
    # Windows requires a writable CRT descriptor for fsync(). Validate first,
    # then open only for durability; content identity was already hashed.
    with _open_regular_descriptor(path):
        pass
    with open(path, 'rb+' if os.name == 'nt' else 'rb') as data:
        os.fsync(data.fileno())
    if os.name != 'nt':
        directory = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _datagen_database_retry(action):
    """Retry only SQLite BUSY reads/CAS statements with a known no-commit result."""

    for attempt in range(OpenBench.datagen.DATAGEN_CLAIM_RETRIES):
        try:
            return action()
        except OperationalError as error:
            if not OpenBench.datagen._is_sqlite_lock_contention(error):
                raise
            if attempt + 1 == OpenBench.datagen.DATAGEN_CLAIM_RETRIES:
                raise
            time.sleep(min(
                OpenBench.datagen.DATAGEN_CLAIM_BACKOFF * (attempt + 1),
                0.05,
            ))



@csrf_exempt
@verify_worker
def client_submit_datagen_producer(request, machine):
    """Reserve, bind and publish one immutable producer under protocol v39."""

    try:
        test_id = int(request.POST['test_id'])
        chunk_idx = int(request.POST['chunk_idx'])
        lease_attempt = int(request.POST['attempt'])
        expected_sha = request.POST['sha256'].lower()
        expected_bytes = int(request.POST['bytes'])
        producer_commit = request.POST['commit'].lower()
        metadata_text = request.POST.get('metadata_only', '0')
        assert metadata_text in ('0', '1')
        metadata_only = metadata_text == '1'
        upload = None if metadata_only else request.FILES['file']
        assert lease_attempt > 0
        assert re.fullmatch(r'[0-9a-f]{64}', expected_sha)
        assert re.fullmatch(r'[0-9a-f]{40}', producer_commit)
        assert 0 < expected_bytes <= OpenBench.datagen.MAX_DATAGEN_PRODUCER_BYTES
    except (KeyError, ValueError, AssertionError):
        return JsonResponse(
            {'error': 'Malformed DATAGEN producer upload'}, status=400
        )

    try:
        test, chunk = _datagen_database_retry(lambda: (
            Test.objects.select_related('dev').filter(pk=test_id).first(),
            DatagenChunk.objects.filter(
                test_id=test_id, idx=chunk_idx
            ).first(),
        ))
    except OperationalError:
        return JsonResponse(
            {'error': 'DATAGEN database is temporarily busy'}, status=503
        )
    if test is None or chunk is None or not test.is_generic_datagen():
        return JsonResponse({'error': 'Unknown DATAGEN chunk'}, status=404)
    if (
        not test.datagen_producer_contract_is_current()
        or not test.datagen_environment_contract_is_current()
        or not test.datagen_publication_contract_is_current()
    ):
        return JsonResponse(
            {'error': 'DATAGEN workload contract is stale'}, status=409
        )
    if not test.datagen_requires_producer_artifact():
        return JsonResponse(
            {'error': 'DATAGEN workload does not request producer evidence'},
            status=409,
        )
    if not test.datagen_producer_contract_is_current():
        return JsonResponse(
            {'error': 'DATAGEN producer contract changed'}, status=409
        )
    if test.finished or test.deleted:
        return JsonResponse({'error': 'DATAGEN test is not active'}, status=409)
    if (
        chunk.status != DatagenChunk.RUNNING
        or chunk.machine_id != machine.id
        or chunk.attempts != lease_attempt
    ):
        return JsonResponse(
            {'error': 'DATAGEN chunk lease is not owned by this attempt'},
            status=409,
        )
    if producer_commit != test.dev.sha.lower():
        return JsonResponse(
            {'error': 'DATAGEN producer commit does not match workload'},
            status=409,
        )

    storage = FileSystemStorage()
    try:
        existing = _datagen_database_retry(lambda: (
            DatagenProducerArtifact.objects.filter(
                sha256=expected_sha, bytes=expected_bytes
            ).first()
        ))
    except OperationalError:
        return JsonResponse(
            {'error': 'DATAGEN database is temporarily busy'}, status=503
        )
    cached_descriptor = None
    if metadata_only and existing is not None:
        try:
            cached_descriptor = _open_verified_producer_descriptor(
                existing, storage
            )
        except OSError:
            pass
    cached_before = cached_descriptor is not None

    def close_cached_descriptor():
        nonlocal cached_descriptor
        if cached_descriptor is not None:
            cached_descriptor.close()
            cached_descriptor = None

    if metadata_only and not cached_before:
        return JsonResponse({
            'sha256': expected_sha,
            'bytes': expected_bytes,
            'commit': producer_commit,
            'already_registered': False,
            'upload_required': True,
        })

    if not metadata_only:
        try:
            actual_sha, actual_bytes = _datagen_uploaded_digest(
                upload, OpenBench.datagen.MAX_DATAGEN_PRODUCER_BYTES
            )
        except ValueError:
            return JsonResponse(
                {'error': 'DATAGEN producer exceeds size limit'}, status=400
            )
        if (actual_sha, actual_bytes) != (expected_sha, expected_bytes):
            return JsonResponse({
                'error': 'DATAGEN producer sha256 or byte count mismatch',
                'sha256': actual_sha,
                'bytes': actual_bytes,
            }, status=400)

    staging_name = None

    def cleanup_staging(name):
        if not name:
            return
        try:
            if storage.exists(name):
                storage.delete(name)
        except OSError:
            pass

    try:
        if not metadata_only:
            staging_name = 'datagen-producers/.staging/%d-%d-%d-%s' % (
                test_id, chunk_idx, lease_attempt, secrets.token_hex(16),
            )
            try:
                saved_name = storage.save(staging_name, upload)
                if saved_name != staging_name:
                    cleanup_staging(saved_name)
                    raise OSError('storage changed the staging name')
                if _hash_regular_file(storage.path(staging_name)) != (
                    expected_sha, expected_bytes
                ):
                    raise OSError('staged producer failed verification')
                _fsync_promoted_file(storage.path(staging_name))
            except OSError:
                return JsonResponse(
                    {'error': 'Unable to stage DATAGEN producer artifact'},
                    status=500,
                )

            # Hash the existing CAS object only after staging the submitted
            # bytes and before entering the write transaction. Keep that exact
            # descriptor open through reservation. This avoids repeating a
            # potentially large hash while SQLite holds its global writer lock.
            if existing is not None:
                try:
                    cached_descriptor = _open_verified_producer_descriptor(
                        existing, storage
                    )
                except OSError:
                    pass

        def reserve_and_bind():
            with transaction.atomic():
                # Completion locks the chunk CAS before updating Test progress.
                # Use the same chunk -> campaign order here to avoid a
                # PostgreSQL deadlock between a producer retry and completion.
                current = (
                    DatagenChunk.objects.select_for_update()
                    .filter(pk=chunk.pk, test_id=test_id)
                    .first()
                )
                campaign = (
                    Test.objects.select_for_update().select_related('dev')
                    .filter(pk=test_id, finished=False, deleted=False)
                    .first()
                )
                if campaign is None or current is None:
                    return JsonResponse(
                        {'error': 'DATAGEN campaign is no longer active'},
                        status=409,
                    ), None, False
                if (
                    current.status != DatagenChunk.RUNNING
                    or current.machine_id != machine.id
                    or current.attempts != lease_attempt
                ):
                    return JsonResponse(
                        {'error': 'DATAGEN chunk lease changed'}, status=409
                    ), None, False
                if (
                    not campaign.datagen_producer_required
                    or not campaign.datagen_producer_contract_sha256
                    or not campaign.datagen_producer_contract_is_current()
                    or not campaign.datagen_environment_contract_is_current()
                    or not campaign.datagen_publication_contract_is_current()
                    or producer_commit != campaign.dev.sha.lower()
                ):
                    return JsonResponse(
                        {'error': 'DATAGEN producer contract changed'}, status=409
                    ), None, False

                owner = User.objects.filter(username=campaign.author).first()
                if owner is None:
                    return JsonResponse(
                        {'error': 'DATAGEN campaign owner is unavailable'},
                        status=500,
                    ), None, False

                # The singleton and owner rows are explicit serialization
                # points. Aggregate quota checks without these locks race on
                # PostgreSQL when two distinct hashes arrive concurrently.
                DatagenProducerQuota.objects.get_or_create(key='global')
                global_quota = DatagenProducerQuota.objects.select_for_update().get(
                    key='global'
                )
                DatagenProducerOwnerQuota.objects.get_or_create(owner=owner)
                owner_quota = DatagenProducerOwnerQuota.objects.select_for_update().get(
                    owner=owner
                )

                artifact = (
                    DatagenProducerArtifact.objects.select_for_update()
                    .filter(sha256=expected_sha)
                    .first()
                )
                new_artifact = artifact is None
                if new_artifact:
                    if staging_name is None:
                        return JsonResponse(
                            {'error': 'DATAGEN producer CAS requires bytes'},
                            status=409,
                        ), None, False
                    if (
                        global_quota.artifact_count + 1
                        > OpenBench.datagen.MAX_DATAGEN_PRODUCERS_GLOBAL
                        or global_quota.reserved_bytes + expected_bytes
                        > OpenBench.datagen.MAX_DATAGEN_PRODUCER_BYTES_GLOBAL
                    ):
                        return JsonResponse(
                            {'error': 'DATAGEN global producer quota exceeded'},
                            status=409,
                        ), None, False
                    artifact = DatagenProducerArtifact.objects.create(
                        sha256=expected_sha,
                        bytes=expected_bytes,
                        state=DatagenProducerArtifact.STAGING,
                        staging_name=staging_name,
                    )
                    global_quota.artifact_count += 1
                    global_quota.reserved_bytes += expected_bytes
                    global_quota.save(update_fields=[
                        'artifact_count', 'reserved_bytes', 'updated',
                    ])
                elif artifact.bytes != expected_bytes:
                    return JsonResponse(
                        {'error': 'DATAGEN producer identity conflict'}, status=500
                    ), None, False

                available = (
                    artifact.state == DatagenProducerArtifact.AVAILABLE
                    and existing is not None
                    and artifact.pk == existing.pk
                    and cached_descriptor is not None
                )
                if not available:
                    if staging_name is None:
                        return JsonResponse(
                            {'error': 'DATAGEN producer CAS requires bytes'},
                            status=409,
                        ), None, False
                    artifact.state = DatagenProducerArtifact.STAGING
                    artifact.staging_name = staging_name
                    artifact.save(update_fields=[
                        'state', 'staging_name', 'updated',
                    ])

                build = (
                    DatagenProducerBuild.objects.select_for_update()
                    .filter(test=campaign, artifact=artifact)
                    .first()
                )
                if build is None:
                    if (
                        campaign.datagen_producer_build_count + 1
                        > OpenBench.datagen.MAX_DATAGEN_PRODUCERS_PER_CAMPAIGN
                        or campaign.datagen_producer_build_bytes + expected_bytes
                        > OpenBench.datagen.MAX_DATAGEN_PRODUCER_BYTES_PER_CAMPAIGN
                    ):
                        transaction.set_rollback(True)
                        return JsonResponse(
                            {'error': 'DATAGEN producer build-set quota exceeded'},
                            status=409,
                        ), None, False
                    if (
                        owner_quota.build_count + 1
                        > OpenBench.datagen.MAX_DATAGEN_PRODUCERS_PER_OWNER
                        or owner_quota.reserved_bytes + expected_bytes
                        > OpenBench.datagen.MAX_DATAGEN_PRODUCER_BYTES_PER_OWNER
                    ):
                        transaction.set_rollback(True)
                        return JsonResponse(
                            {'error': 'DATAGEN owner producer quota exceeded'},
                            status=409,
                        ), None, False
                    build = DatagenProducerBuild.objects.create(
                        test=campaign,
                        artifact=artifact,
                        owner=owner,
                        commit=producer_commit,
                        contract_sha256=(
                            campaign.datagen_producer_contract_sha256
                        ),
                    )
                    campaign.datagen_producer_build_count += 1
                    campaign.datagen_producer_build_bytes += expected_bytes
                    campaign.save(update_fields=[
                        'datagen_producer_build_count',
                        'datagen_producer_build_bytes',
                        'updated',
                    ])
                    owner_quota.build_count += 1
                    owner_quota.reserved_bytes += expected_bytes
                    owner_quota.save(update_fields=[
                        'build_count', 'reserved_bytes', 'updated',
                    ])
                    artifact.reference_count += 1
                    artifact.save(update_fields=['reference_count', 'updated'])
                elif (
                    build.owner_id != owner.id
                    or build.commit != producer_commit
                    or build.contract_sha256
                    != campaign.datagen_producer_contract_sha256
                ):
                    transaction.set_rollback(True)
                    return JsonResponse(
                        {'error': 'DATAGEN producer build binding conflict'},
                        status=500,
                    ), None, False

                if current.producer_build_id not in (None, build.id):
                    transaction.set_rollback(True)
                    return JsonResponse(
                        {'error': 'DATAGEN chunk producer binding changed'},
                        status=409,
                    ), None, False
                current.producer_build = build
                current.producer_sha256 = expected_sha
                current.producer_bytes = expected_bytes
                current.producer_commit = producer_commit
                current.save(update_fields=[
                    'producer_build', 'producer_sha256', 'producer_bytes',
                    'producer_commit',
                ])
                return None, artifact.id, available

        try:
            response, artifact_id, already_available = _datagen_database_retry(
                reserve_and_bind
            )
        except OperationalError:
            return JsonResponse(
                {'error': 'DATAGEN database is temporarily busy'}, status=503
            )
        if response is not None:
            return response

        try:
            artifact = _datagen_database_retry(lambda: (
                DatagenProducerArtifact.objects.get(pk=artifact_id)
            ))
        except OperationalError:
            return JsonResponse(
                {'error': 'DATAGEN database is temporarily busy'}, status=503
            )
        canonical_path = storage.path(artifact.filename())
        if not already_available:
            try:
                os.makedirs(os.path.dirname(canonical_path), exist_ok=True)
                os.replace(storage.path(staging_name), canonical_path)
                staging_name = None
                _fsync_promoted_file(canonical_path)
            except OSError:
                # Preserve the tracked staging object for the reconciler. The
                # reservation and build remain durable and cannot be used to
                # bypass any quota via requeue.
                staging_name = None
                return JsonResponse(
                    {'error': 'Unable to promote DATAGEN producer artifact'},
                    status=500,
                )
            try:
                _datagen_database_retry(lambda: (
                    DatagenProducerArtifact.objects.filter(
                        pk=artifact_id,
                        sha256=expected_sha,
                        bytes=expected_bytes,
                    ).update(
                        state=DatagenProducerArtifact.AVAILABLE,
                        staging_name='',
                        last_verified=timezone.now(),
                        updated=timezone.now(),
                    )
                ))
            except OperationalError:
                return JsonResponse(
                    {'error': 'DATAGEN database is temporarily busy'}, status=503
                )

        try:
            lease_is_current = _datagen_database_retry(lambda: (
                DatagenChunk.objects.filter(
                    pk=chunk.pk,
                    status=DatagenChunk.RUNNING,
                    machine_id=machine.id,
                    attempts=lease_attempt,
                    producer_build__artifact_id=artifact_id,
                    producer_sha256=expected_sha,
                    producer_bytes=expected_bytes,
                    producer_commit=producer_commit,
                    test__finished=False,
                    test__deleted=False,
                ).exists()
            ))
        except OperationalError:
            return JsonResponse(
                {'error': 'DATAGEN database is temporarily busy'}, status=503
            )
        if not lease_is_current:
            return JsonResponse(
                {'error': 'DATAGEN chunk lease changed after producer binding'},
                status=409,
            )
        return JsonResponse({
            'sha256': expected_sha,
            'bytes': expected_bytes,
            'commit': producer_commit,
            'already_registered': already_available,
            'upload_required': False,
        })
    finally:
        close_cached_descriptor()
        cleanup_staging(staging_name)


def _frozen_datagen_tablebase_attestation(test, chunk, machine):
    """Reconstruct and authenticate every semantic field in one v40 lease."""

    if test.is_publication_datagen():
        return _frozen_datagen_publication_attestation(test, chunk, machine)
    if not test.datagen_tablebase_required:
        return None
    lease = chunk.environment_lease
    if machine is None or not isinstance(lease, dict):
        raise PermissionError(
            'DATAGEN chunk lacks a frozen tablebase lease'
        )
    leased_tablebase = lease.get('tablebase', {})
    if not isinstance(leased_tablebase, dict):
        raise PermissionError('DATAGEN tablebase lease is malformed')
    worker_max = leased_tablebase.get('worker_max')
    if type(worker_max) is not int:
        raise PermissionError('DATAGEN tablebase lease is malformed')
    if (
        not OpenBench.datagen.valid_atomic_datagen_tablebase_contract(test)
        or chunk.machine_id != machine.id
        or worker_max < test.datagen_tablebase_max
        or not isinstance(chunk.environment_lease_sha256, str)
        or not re.fullmatch(r'[0-9a-f]{64}', chunk.environment_lease_sha256)
    ):
        raise PermissionError(
            'DATAGEN tablebase lease does not match campaign or worker'
        )
    expected_lease = {
        'schema': OpenBench.datagen.DATAGEN_TABLEBASE_LEASE_SCHEMA,
        'protocol': 40,
        'test_id': test.id,
        'chunk_idx': chunk.idx,
        'attempt': chunk.attempts,
        'machine_id': machine.id,
        'environment_contract_sha256': (
            test.datagen_environment_contract_sha256
        ),
        'tablebase': {
            'family': test.datagen_tablebase_family,
            'required_max': test.datagen_tablebase_max,
            'worker_max': worker_max,
            'manifest_sha256': test.datagen_tablebase_manifest_sha256,
        },
        'teacher_mode': test.datagen_teacher_mode,
    }
    if (
        lease != expected_lease
        or _canonical_json_sha256(lease) != chunk.environment_lease_sha256
        or _canonical_json_sha256(expected_lease)
           != chunk.environment_lease_sha256
    ):
        raise PermissionError(
            'DATAGEN tablebase lease does not match campaign or worker'
        )
    return {
        'protocol': 40,
        'tablebase_required': True,
        'publication_contract_sha256': None,
        'environment_contract_sha256': (
            test.datagen_environment_contract_sha256
        ),
        'environment_lease_sha256': chunk.environment_lease_sha256,
        'family': test.datagen_tablebase_family,
        'required_max': test.datagen_tablebase_max,
        'worker_max': worker_max,
        'manifest_sha256': (
            test.datagen_tablebase_manifest_sha256
        ),
        'teacher_mode': test.datagen_teacher_mode,
    }


def _frozen_datagen_publication_attestation(test, chunk, machine):
    """Reconstruct the complete protocol-v41 publication lease."""

    lease = chunk.environment_lease
    if (
        machine is None
        or not isinstance(lease, dict)
        or not test.datagen_publication_contract_is_current()
        or not isinstance(chunk.environment_lease_sha256, str)
        or not re.fullmatch(r'[0-9a-f]{64}', chunk.environment_lease_sha256)
    ):
        raise PermissionError('DATAGEN publication lease is missing or stale')
    leased_tablebase = lease.get('tablebase')
    if not isinstance(leased_tablebase, dict):
        raise PermissionError('DATAGEN publication lease is malformed')
    worker_max = leased_tablebase.get('worker_max')
    if type(worker_max) is not int:
        raise PermissionError('DATAGEN publication lease is malformed')

    if test.datagen_tablebase_required:
        if (
            not OpenBench.datagen.valid_atomic_datagen_tablebase_contract(test)
            or worker_max < test.datagen_tablebase_max
        ):
            raise PermissionError(
                'DATAGEN publication lease does not match tablebase contract'
            )
        expected_tablebase = {
            'required': True,
            'family': test.datagen_tablebase_family,
            'required_max': test.datagen_tablebase_max,
            'worker_max': worker_max,
            'manifest_sha256': test.datagen_tablebase_manifest_sha256,
        }
        teacher_mode = test.datagen_teacher_mode
    else:
        expected_tablebase = {
            'required': False,
            'family': None,
            'required_max': 0,
            'worker_max': 0,
            'manifest_sha256': None,
        }
        teacher_mode = None

    expected_lease = {
        'schema': OpenBench.datagen_publication.DATAGEN_PUBLICATION_LEASE_SCHEMA,
        'protocol': 41,
        'test_id': test.id,
        'chunk_idx': chunk.idx,
        'attempt': chunk.attempts,
        'machine_id': machine.id,
        'publication_contract_sha256': (
            test.datagen_publication_contract_sha256
        ),
        'environment_contract_sha256': (
            test.datagen_environment_contract_sha256
        ),
        'tablebase': expected_tablebase,
        'teacher_mode': teacher_mode,
    }
    if (
        chunk.machine_id != machine.id
        or lease != expected_lease
        or OpenBench.datagen_publication.canonical_json_sha256(lease)
           != chunk.environment_lease_sha256
    ):
        raise PermissionError(
            'DATAGEN publication lease does not match campaign or worker'
        )
    return {
        'protocol': 41,
        'tablebase_required': test.datagen_tablebase_required,
        'publication_contract_sha256': (
            test.datagen_publication_contract_sha256
        ),
        'environment_contract_sha256': (
            test.datagen_environment_contract_sha256
        ),
        'environment_lease_sha256': chunk.environment_lease_sha256,
        'family': test.datagen_tablebase_family,
        'required_max': test.datagen_tablebase_max,
        'worker_max': worker_max,
        'manifest_sha256': (
            test.datagen_tablebase_manifest_sha256
            if test.datagen_tablebase_required else None
        ),
        'teacher_mode': test.datagen_teacher_mode or None,
    }


def _datagen_tablebase_attestation(request, test, chunk, machine):
    """Authenticate request evidence against the complete frozen v40 lease."""

    expected = _frozen_datagen_tablebase_attestation(test, chunk, machine)
    if expected is None:
        return None
    if expected['protocol'] == 41:
        try:
            submitted = {
                'protocol': 41,
                'tablebase_required': expected['tablebase_required'],
                'publication_contract_sha256': request.POST[
                    'publication_contract_sha256'
                ].lower(),
                'environment_contract_sha256': request.POST[
                    'environment_contract_sha256'
                ].lower(),
                'environment_lease_sha256': request.POST[
                    'environment_lease_sha256'
                ].lower(),
                'family': request.POST.get('tablebase_family', ''),
                'required_max': int(request.POST.get('tablebase_max', 0)),
                'worker_max': int(request.POST.get('tablebase_worker_max', 0)),
                'manifest_sha256': (
                    request.POST.get('tablebase_manifest_sha256') or None
                ),
                'teacher_mode': request.POST.get('teacher_mode') or None,
            }
            for field in (
                'publication_contract_sha256',
                'environment_contract_sha256',
                'environment_lease_sha256',
            ):
                assert re.fullmatch(r'[0-9a-f]{64}', submitted[field])
            if submitted['manifest_sha256'] is not None:
                submitted['manifest_sha256'] = submitted[
                    'manifest_sha256'
                ].lower()
                assert re.fullmatch(
                    r'[0-9a-f]{64}', submitted['manifest_sha256']
                )
        except (KeyError, TypeError, ValueError, AssertionError):
            raise ValueError('DATAGEN upload omitted publication attestation')
        if submitted != expected:
            raise PermissionError(
                'DATAGEN publication attestation does not match campaign or worker'
            )
        return submitted
    try:
        submitted = {
            'protocol': 40,
            'tablebase_required': True,
            'publication_contract_sha256': None,
            'environment_contract_sha256': request.POST[
                'environment_contract_sha256'
            ].lower(),
            'environment_lease_sha256': request.POST[
                'environment_lease_sha256'
            ].lower(),
            'family': request.POST['tablebase_family'],
            'required_max': int(request.POST['tablebase_max']),
            'worker_max': int(request.POST['tablebase_worker_max']),
            'manifest_sha256': request.POST[
                'tablebase_manifest_sha256'
            ].lower(),
            'teacher_mode': request.POST.get('teacher_mode', ''),
        }
        assert re.fullmatch(
            r'[0-9a-f]{64}', submitted['environment_contract_sha256']
        )
        assert re.fullmatch(
            r'[0-9a-f]{64}', submitted['environment_lease_sha256']
        )
        assert re.fullmatch(r'[0-9a-f]{64}', submitted['manifest_sha256'])
        assert submitted['required_max'] > 0
        assert submitted['worker_max'] >= submitted['required_max']
    except (KeyError, ValueError, AssertionError):
        raise ValueError('DATAGEN upload omitted tablebase attestation')
    if submitted != expected:
        raise PermissionError(
            'DATAGEN tablebase attestation does not match campaign or worker'
        )
    return submitted


def _datagen_environment_receipt(
    test, chunk, machine, artifact_sha256, artifact_bytes, producer, attestation,
):
    if attestation is None:
        return {}, ''
    if attestation['protocol'] == 41:
        receipt = {
            'schema': (
                OpenBench.datagen_publication.DATAGEN_PUBLICATION_RECEIPT_SCHEMA
            ),
            'protocol': 41,
            'test_id': test.id,
            'chunk_idx': chunk.idx,
            'attempt': chunk.attempts,
            'machine_id': machine.id,
            'publication_contract_sha256': (
                attestation['publication_contract_sha256']
            ),
            'environment_contract_sha256': (
                attestation['environment_contract_sha256']
            ),
            'environment_lease_sha256': (
                attestation['environment_lease_sha256']
            ),
            'tablebase': {
                'required': attestation['tablebase_required'],
                'family': attestation['family'] or None,
                'required_max': attestation['required_max'],
                'worker_max': attestation['worker_max'],
                'manifest_sha256': attestation['manifest_sha256'],
            },
            'teacher_mode': attestation['teacher_mode'],
            'artifact': {
                'sha256': artifact_sha256,
                'bytes': artifact_bytes,
            },
            'producer': producer,
        }
        return receipt, OpenBench.datagen_publication.canonical_json_sha256(
            receipt
        )
    receipt = {
        'schema': DATAGEN_RECEIPT_SCHEMA,
        'protocol': 40,
        'test_id': test.id,
        'chunk_idx': chunk.idx,
        'attempt': chunk.attempts,
        'machine_id': machine.id,
        'environment_contract_sha256': (
            attestation['environment_contract_sha256']
        ),
        'environment_lease_sha256': (
            attestation['environment_lease_sha256']
        ),
        'tablebase': {
            'family': attestation['family'],
            'required_max': attestation['required_max'],
            'worker_max': attestation['worker_max'],
            'manifest_sha256': attestation['manifest_sha256'],
        },
        'teacher_mode': attestation['teacher_mode'],
        'artifact': {
            'sha256': artifact_sha256,
            'bytes': artifact_bytes,
        },
        'producer': producer,
    }
    return receipt, _canonical_json_sha256(receipt)


def _canonical_json_sha256(document):
    encoded = json.dumps(
        document, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


@csrf_exempt
@verify_worker
def client_submit_datagen(request, machine):

    try:
        test_id = int(request.POST['test_id'])
        chunk_idx = int(request.POST['chunk_idx'])
        lease_attempt = int(request.POST['attempt'])
        expected_sha = request.POST['sha256'].lower()
        expected_bytes = int(request.POST['bytes'])
        upload = request.FILES['file']
        assert lease_attempt > 0
        assert re.fullmatch(r'[0-9a-f]{64}', expected_sha)
        assert expected_bytes >= 0
    except (KeyError, ValueError, AssertionError):
        return JsonResponse({'error': 'Malformed DATAGEN upload'}, status=400)

    storage = FileSystemStorage()
    receipt = {}
    receipt_sha256 = ''

    def completed_response(test):
        completed, total, positions = OpenBench.datagen.completed_progress(test)
        return JsonResponse({
            'sha256': actual_sha,
            'bytes': actual_bytes,
            'producer_sha256': chunk.producer_sha256 or None,
            'producer_bytes': chunk.producer_bytes,
            'producer_commit': chunk.producer_commit or None,
            'environment_receipt_sha256': (
                receipt_sha256 or chunk.environment_receipt_sha256 or None
            ),
            'completed_chunks': completed,
            'total_chunks': total,
            'positions': positions,
            'finished': test.finished,
        })

    try:
        test, chunk = _datagen_database_retry(lambda: (
            Test.objects.filter(id=test_id).first(),
            DatagenChunk.objects.filter(test_id=test_id, idx=chunk_idx).first(),
        ))
    except OperationalError:
        return JsonResponse(
            {'error': 'DATAGEN database is temporarily busy'}, status=503
        )
    if test is None or chunk is None or not test.is_generic_datagen():
        return JsonResponse({'error': 'Unknown DATAGEN chunk'}, status=404)
    if (
        not test.datagen_producer_contract_is_current()
        or not test.datagen_environment_contract_is_current()
        or not test.datagen_publication_contract_is_current()
    ):
        return JsonResponse(
            {'error': 'DATAGEN workload contract is stale'}, status=409
        )
    if chunk.attempts != lease_attempt:
        return JsonResponse(
            {'error': 'DATAGEN chunk lease attempt is stale'}, status=409
        )

    try:
        tablebase_attestation = _datagen_tablebase_attestation(
            request, test, chunk, machine
        )
    except ValueError as error:
        return JsonResponse({'error': str(error)}, status=400)
    except PermissionError as error:
        return JsonResponse({'error': str(error)}, status=409)

    submitted_producer = None
    submitted_producer_bytes = 0
    submitted_producer_commit = None
    if test.datagen_requires_producer_artifact():
        try:
            submitted_producer = request.POST['producer_sha256'].lower()
            submitted_producer_bytes = int(request.POST['producer_bytes'])
            submitted_producer_commit = request.POST['producer_commit'].lower()
            assert re.fullmatch(r'[0-9a-f]{64}', submitted_producer)
            assert re.fullmatch(r'[0-9a-f]{40}', submitted_producer_commit)
            assert submitted_producer_bytes > 0
        except (KeyError, ValueError, AssertionError):
            return JsonResponse(
                {'error': 'DATAGEN upload omitted producer evidence'}, status=400
            )
        if (
            chunk.producer_build_id is None
            or not chunk.producer_sha256
            or submitted_producer != chunk.producer_sha256
            or submitted_producer_bytes != chunk.producer_bytes
            or submitted_producer_commit != chunk.producer_commit
        ):
            return JsonResponse(
                {'error': 'DATAGEN upload producer evidence does not match lease'},
                status=409,
            )
        try:
            build = _datagen_database_retry(lambda: (
                DatagenProducerBuild.objects.select_related('artifact').filter(
                    pk=chunk.producer_build_id,
                    test_id=test.id,
                    commit=chunk.producer_commit,
                    contract_sha256=test.datagen_producer_contract_sha256,
                    artifact__sha256=chunk.producer_sha256,
                    artifact__bytes=chunk.producer_bytes,
                ).first()
            ))
        except OperationalError:
            return JsonResponse(
                {'error': 'DATAGEN database is temporarily busy'}, status=503
            )
        if build is None or not _cached_producer_available(build.artifact):
            return JsonResponse(
                {'error': 'DATAGEN producer artifact is unavailable or corrupt'},
                status=409,
            )

    if chunk.status == DatagenChunk.COMPLETED:
        if chunk.machine_id != machine.id:
            return JsonResponse(
                {'error': 'DATAGEN completed chunk belongs to another machine'},
                status=409,
            )
    elif (
        test.finished
        or test.deleted
        or chunk.status != DatagenChunk.RUNNING
        or chunk.machine_id != machine.id
    ):
        return JsonResponse(
            {'error': 'DATAGEN chunk lease is not owned by this attempt'},
            status=409,
        )

    # Authenticate the exact lease and producer before hashing a potentially
    # multi-gigabyte request body. Stale attempts cannot consume upload CPU or
    # create staging files.
    actual_sha, actual_bytes = _datagen_uploaded_digest(upload)
    if actual_sha != expected_sha or actual_bytes != expected_bytes:
        return JsonResponse({
            'error': 'DATAGEN sha256 or byte count mismatch',
            'sha256': actual_sha,
            'bytes': actual_bytes,
        }, status=400)

    producer_receipt = None
    if submitted_producer is not None:
        producer_receipt = {
            'sha256': submitted_producer,
            'bytes': submitted_producer_bytes,
            'commit': submitted_producer_commit,
        }
    receipt, receipt_sha256 = _datagen_environment_receipt(
        test,
        chunk,
        machine,
        actual_sha,
        actual_bytes,
        producer_receipt,
        tablebase_attestation,
    )

    # Lost HTTP responses may cause an identical retry. Keep completed chunks
    # immutable while accepting that retry idempotently without staging again.
    if chunk.status == DatagenChunk.COMPLETED:
        if chunk.sha256 != actual_sha or chunk.bytes != actual_bytes:
            return JsonResponse(
                {'error': 'DATAGEN chunk already completed with different data'},
                status=409,
            )
        if tablebase_attestation is not None and (
            chunk.environment_receipt != receipt
            or chunk.environment_receipt_sha256 != receipt_sha256
        ):
            return JsonResponse(
                {'error': 'DATAGEN completed chunk has different tablebase evidence'},
                status=409,
            )
        return completed_response(test)
    if test.finished or test.deleted:
        return JsonResponse({'error': 'DATAGEN test is not active'}, status=409)
    if (
        chunk.status != DatagenChunk.RUNNING
        or chunk.machine_id != machine.id
        or chunk.attempts != lease_attempt
    ):
        return JsonResponse(
            {'error': 'DATAGEN chunk lease is not owned by this attempt'},
            status=409,
        )

    filename = chunk.filename()
    chunk_pk = chunk.pk
    chunk_position_count = chunk.position_count
    chunk_producer_build_id = chunk.producer_build_id
    staging_name = '%s.staging-%d-%s' % (
        filename,
        machine.id,
        secrets.token_hex(16),
    )

    def cleanup_staging(name):
        if name is None:
            return
        try:
            if storage.exists(name):
                storage.delete(name)
        except OSError:
            pass

    # Materialize the potentially multi-gigabyte upload on the destination
    # filesystem before opening a database write transaction.  Production uses
    # SQLite, so holding its global writer lock during a C: -> F: copy would
    # otherwise stall heartbeats and every other OpenBench workload.
    try:
        saved_name = storage.save(staging_name, upload)
    except Exception:
        cleanup_staging(staging_name)
        return JsonResponse({'error': 'Unable to stage DATAGEN chunk'}, status=500)
    if saved_name != staging_name:
        cleanup_staging(saved_name)
        cleanup_staging(staging_name)
        return JsonResponse(
            {'error': 'Unable to stage DATAGEN chunk at canonical path'},
            status=500,
        )
    try:
        if _hash_regular_file(storage.path(staging_name)) != (
            actual_sha, actual_bytes,
        ):
            raise OSError('staged DATAGEN chunk failed verification')
        _fsync_promoted_file(storage.path(staging_name))
    except OSError:
        cleanup_staging(staging_name)
        return JsonResponse(
            {'error': 'Unable to verify staged DATAGEN chunk'}, status=500,
        )

    def commit_staged_upload():
        nonlocal staging_name
        with transaction.atomic():
            # Ownership validation and completion are one conditional write.
            # Keep it as the first statement in the transaction: SQLite can
            # then serialize writers without a read-to-write upgrade deadlock.
            completion = DatagenChunk.objects.filter(
                pk=chunk_pk,
                status=DatagenChunk.RUNNING,
                machine_id=machine.id,
                attempts=lease_attempt,
            )
            if submitted_producer is not None:
                completion = completion.filter(
                    producer_build_id=chunk_producer_build_id,
                    producer_sha256=submitted_producer,
                    producer_bytes=submitted_producer_bytes,
                    producer_commit=submitted_producer_commit,
                )
            if tablebase_attestation is not None:
                completion = completion.filter(
                    environment_lease_sha256=tablebase_attestation[
                        'environment_lease_sha256'
                    ]
                )
            completed_by_machine = completion.update(
                status=DatagenChunk.COMPLETED,
                sha256=actual_sha,
                bytes=actual_bytes,
                environment_receipt=receipt,
                environment_receipt_sha256=receipt_sha256,
                completed=timezone.now(),
                last_error='',
            )
            if completed_by_machine != 1:
                test = Test.objects.filter(id=test_id).first()
                chunk = DatagenChunk.objects.filter(pk=chunk_pk).first()
                if test is None or chunk is None or not test.is_generic_datagen():
                    return JsonResponse({'error': 'Unknown DATAGEN chunk'}, status=404)
                if (
                    chunk.status == DatagenChunk.COMPLETED
                    and chunk.attempts == lease_attempt
                    and chunk.sha256 == actual_sha
                    and chunk.bytes == actual_bytes
                ):
                    return completed_response(test)
                return JsonResponse(
                    {'error': 'DATAGEN chunk lease is not owned by machine'},
                    status=409,
                )

            # Advance progress only if the campaign is still active.  This is
            # a direct Test-row CAS after the direct chunk-row CAS, avoiding
            # the stale cross-table UPDATE subquery used by Django on
            # PostgreSQL. Any failure rolls the chunk transition back.
            progressed = Test.objects.filter(
                pk=test_id, finished=False, deleted=False,
            )
            if tablebase_attestation is not None and (
                tablebase_attestation['protocol'] == 41
            ):
                progressed = progressed.filter(
                    datagen_publication_contract_sha256=(
                        tablebase_attestation['publication_contract_sha256']
                    )
                )
            progressed = progressed.update(
                games=F('games') + chunk_position_count,
                datagen_completed_chunks=F('datagen_completed_chunks') + 1,
                updated=timezone.now(),
            )
            if progressed != 1:
                transaction.set_rollback(True)
                return JsonResponse(
                    {'error': 'DATAGEN test is not active'}, status=409,
                )
            test = Test.objects.get(id=test_id)

            # Both names live under the same FileSystemStorage root, so this is
            # a short atomic rename rather than a large copy under SQLite's
            # writer lock. Roll back the chunk transition if promotion fails.
            try:
                os.replace(storage.path(staging_name), storage.path(filename))
            except OSError:
                transaction.set_rollback(True)
                return JsonResponse(
                    {'error': 'Unable to promote staged DATAGEN chunk'},
                    status=500,
                )
            staging_name = None

            # F() updates above serialize concurrent chunks without rescanning
            # the complete chunk table or losing a PostgreSQL update.
            completed, total, positions = OpenBench.datagen.completed_progress(test)
            if completed > total or positions > test.datagen_total_count:
                transaction.set_rollback(True)
                return JsonResponse(
                    {'error': 'DATAGEN progress counters exceeded workload totals'},
                    status=500,
                )
            if completed == total or positions == test.datagen_total_count:
                if completed != total or positions != test.datagen_total_count:
                    transaction.set_rollback(True)
                    return JsonResponse(
                        {'error': 'DATAGEN progress counters are inconsistent'},
                        status=500,
                    )
                Test.objects.filter(pk=test.pk).update(
                    passed=True, finished=True, updated=timezone.now()
                )
                test.refresh_from_db()

            Machine.objects.filter(pk=machine.id).update(workload=0)
            return completed_response(test)

    try:
        for attempt in range(OpenBench.datagen.DATAGEN_CLAIM_RETRIES):
            try:
                return commit_staged_upload()
            except OperationalError as error:
                # A SQLite BUSY/locked error on the first CAS has no unknown
                # commit outcome and is safe to retry while staging still
                # exists. Never retry after promotion or arbitrary database
                # errors, where commit state may be ambiguous.
                retryable = (
                    staging_name is not None
                    and OpenBench.datagen._is_sqlite_lock_contention(error)
                )
                if not retryable:
                    raise
                if attempt + 1 == OpenBench.datagen.DATAGEN_CLAIM_RETRIES:
                    return JsonResponse(
                        {'error': 'DATAGEN database is temporarily busy'},
                        status=503,
                    )
                time.sleep(min(
                    OpenBench.datagen.DATAGEN_CLAIM_BACKOFF * (attempt + 1),
                    0.05,
                ))
    finally:
        cleanup_staging(staging_name)

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def api_response(data):
    return HttpResponse(json.dumps(data, indent=4), content_type='application/json')

@csrf_exempt
def api_authenticate(request, require_enabled=False):

    try:

        # Force requiring an enabled user when require_login_to_view is set
        require_enabled = require_enabled or OPENBENCH_CONFIG['require_login_to_view']

        # Don't require a login for Public frameworks
        if not require_enabled:
            return True

        # Request is made from a browser, and is already logged in
        if request.user.is_authenticated:
            return Profile.objects.get(user=request.user).enabled

        # Command-line GET clients use HTTP Basic. Historical POST credentials
        # remain accepted for the rest of the API.
        authorization = request.META.get('HTTP_AUTHORIZATION', '')
        if authorization.startswith('Basic '):
            # Basic credentials are reusable secrets.  Never accept them over
            # cleartext HTTP; deployments behind a trusted TLS terminator must
            # opt in to SECURE_PROXY_SSL_HEADER in settings.
            if not request.is_secure():
                return False
            decoded = base64.b64decode(
                authorization[6:], validate=True
            ).decode('utf-8')
            username, password = decoded.split(':', 1)
        else:
            username = request.POST['username']
            password = request.POST['password']
        user = django.contrib.auth.authenticate(
            username=username, password=password)
        return Profile.objects.get(user=user).enabled

    except Exception:
        return False

@csrf_exempt
def api_configs(request, engine=None):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    if engine == None:
        engines = list(OPENBENCH_CONFIG['engines'].keys())
        books   = OPENBENCH_CONFIG['books']
        return api_response({ 'engines' : engines, 'books' : books })

    if engine in OPENBENCH_CONFIG['engines'].keys():
        return api_response(OPENBENCH_CONFIG['engines'][engine])

    return api_response({ 'error' : 'Engine not found. Check /api/config/ for a full list' })

@csrf_exempt
def api_networks(request, engine):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    if engine in OPENBENCH_CONFIG['engines'].keys():

        if not (network := Network.objects.filter(engine=engine, default=True).first()):
            return api_response({ 'error' : 'Engine does not have a default Network' })

        default = {
            'sha'    : network.sha256, 'name'    : network.name,
            'author' : network.author, 'created' : str(network.created) }

        networks = [
          { 'sha'    : network.sha256, 'name'    : network.name,
            'author' : network.author, 'created' : str(network.created) }
            for network in Network.objects.filter(engine=engine) ]

        return api_response({ 'default' : default, 'networks' : networks })

    else:
        return api_response({ 'error' : 'Engine not found. Check /api/config/ for a full list' })

@csrf_exempt
def api_network_download(request, engine, identifier):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    if not api_authenticate(request, require_enabled=True):
        return api_response({ 'error' : 'API requires authentication for this endpoint' })

    if (network := Network.objects.filter(engine=engine, sha256=identifier).first()):
        return OpenBench.utils.network_download(request, engine, network)

    if (network := Network.objects.filter(engine=engine, name=identifier).first()):
        return OpenBench.utils.network_download(request, engine, network)

    return api_response({ 'error' : 'Engine not found. Check /api/config/ for a full list' })

@csrf_exempt
def api_build_info(request):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    data = {}
    for engine, config in OPENBENCH_CONFIG['engines'].items():
        data[engine] = config

    for network in Network.objects.filter(default=True):

        if network.engine not in data:
            continue

        data[network.engine]['network'] = {
            'sha'     : network.sha256,
            'name'    : network.name,
            'author'  : network.author,
            'created' : str(network.created)
        }

    return api_response(data)

@csrf_exempt
def api_pgns(request, pgn_id):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    # Possible to request a PGN that does not exist
    pgn_path = FileSystemStorage('Media/PGNs').path('%d.pgn.tar' % (pgn_id))
    if not os.path.exists(pgn_path):
        return api_response({ 'error' : 'Unable to find PGN for Workload #%d' % (pgn_id) })

    # Craft the download HTML response
    fwrapper = FileWrapper(open(pgn_path, 'rb'), 8192)
    response = FileResponse(fwrapper, content_type='application/octet-stream')

    # Set all headers and return response
    response['Expires'] = -1
    response['Content-Length'] = os.path.getsize(pgn_path)
    response['Content-Disposition'] = 'attachment; filename=%d.pgn.tar' % (pgn_id)
    return response

@csrf_exempt
def api_datagen_chunk(request, test_id, chunk_idx):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    chunk = DatagenChunk.objects.filter(
        test_id=test_id,
        idx=chunk_idx,
        status=DatagenChunk.COMPLETED,
    ).first()
    if chunk is None:
        return api_response({
            'error': 'Unable to find DATAGEN chunk %d for Workload #%d'
                     % (chunk_idx, test_id)
        })

    path = FileSystemStorage().path(chunk.filename())
    if not os.path.exists(path):
        return api_response({'error': 'DATAGEN chunk metadata exists but file is missing'})

    fwrapper = FileWrapper(open(path, 'rb'), 8192)
    response = FileResponse(fwrapper, content_type='application/x-bzip2')
    response['Expires'] = -1
    response['Content-Length'] = os.path.getsize(path)
    response['Content-Disposition'] = 'attachment; filename=chunk_%d.bz2' % chunk_idx
    return response


@csrf_exempt
def api_datagen_manifest(request, test_id):

    if not api_authenticate(request, require_enabled=True):
        return api_response({ 'error' : 'API requires authentication for this server' })

    test = Test.objects.filter(id=test_id).first()
    if test is None or not test.is_generic_datagen():
        return api_response({
            'error': 'Unable to find generic DATAGEN Workload #%d' % test_id
        })
    if (
        not test.datagen_producer_contract_is_current()
        or not test.datagen_environment_contract_is_current()
        or not test.datagen_publication_contract_is_current()
    ):
        return api_response({
            'error': 'DATAGEN Workload #%d has a stale contract' % test_id
        })

    chunks = list(
        test.datagen_chunks.select_related(
            'producer_build__artifact', 'machine'
        ).order_by('idx')
    )
    if (
        not test.finished
        or test.datagen_completed_chunks != test.datagen_total_chunks()
        or any(chunk.status != DatagenChunk.COMPLETED for chunk in chunks)
    ):
        return api_response({
            'error': 'DATAGEN Workload #%d is not complete' % test_id
        })

    producer_required = test.datagen_requires_producer_artifact()
    tablebase_required = test.datagen_tablebase_required
    environment_evidence_required = (
        tablebase_required or test.is_publication_datagen()
    )
    environment_evidence_label = (
        'publication' if test.is_publication_datagen() else 'tablebase'
    )
    if environment_evidence_required:
        if (
            tablebase_required
            and not OpenBench.datagen.valid_atomic_datagen_tablebase_contract(test)
        ):
            return api_response({
                'error': 'DATAGEN Workload #%d has an invalid environment contract'
                         % test_id
            })
        for chunk in chunks:
            try:
                attestation = _frozen_datagen_tablebase_attestation(
                    test, chunk, chunk.machine
                )
            except PermissionError:
                return api_response({
                    'error': 'DATAGEN Workload #%d has inconsistent %s lease evidence'
                             % (test_id, environment_evidence_label)
                })
            producer = None
            if chunk.producer_sha256:
                producer = {
                    'sha256': chunk.producer_sha256,
                    'bytes': chunk.producer_bytes,
                    'commit': chunk.producer_commit,
                }
            expected_receipt, expected_receipt_sha = (
                _datagen_environment_receipt(
                    test,
                    chunk,
                    chunk.machine,
                    chunk.sha256,
                    chunk.bytes,
                    producer,
                    attestation,
                )
            )
            if (
                chunk.environment_receipt != expected_receipt
                or _canonical_json_sha256(chunk.environment_receipt)
                   != chunk.environment_receipt_sha256
                or chunk.environment_receipt_sha256
                   != expected_receipt_sha
            ):
                return api_response({
                    'error': 'DATAGEN Workload #%d has inconsistent %s receipt evidence'
                             % (test_id, environment_evidence_label)
                })
    producer_builds = []
    if producer_required:
        expected_commit = test.dev.sha.lower()
        if (
            not re.fullmatch(r'[0-9a-f]{40}', expected_commit)
            or not re.fullmatch(
                r'[0-9a-f]{64}', test.datagen_producer_contract_sha256
            )
            or any(
                chunk.producer_build_id is None
                or not re.fullmatch(r'[0-9a-f]{64}', chunk.producer_sha256)
                or chunk.producer_bytes <= 0
                or chunk.producer_commit != expected_commit
                for chunk in chunks
            )
        ):
            return api_response({
                'error': 'DATAGEN Workload #%d has incomplete producer evidence'
                         % test_id
            })

        used_build_ids = set()
        for chunk in chunks:
            build = chunk.producer_build
            if (
                build.test_id != test.id
                or build.commit != expected_commit
                or build.contract_sha256
                != test.datagen_producer_contract_sha256
                or build.artifact.sha256 != chunk.producer_sha256
                or build.artifact.bytes != chunk.producer_bytes
            ):
                return api_response({
                    'error': (
                        'DATAGEN Workload #%d has inconsistent producer evidence'
                        % test_id
                    )
                })
            used_build_ids.add(build.id)

        builds = list(
            DatagenProducerBuild.objects.select_related('artifact')
            .filter(test=test, id__in=used_build_ids)
            .order_by('artifact__sha256')
        )
        for build in builds:
            if not _cached_producer_available(build.artifact):
                return api_response({
                    'error': 'DATAGEN Workload #%d has unavailable producer evidence'
                             % test_id
                })
            producer_builds.append({
                'sha256': build.artifact.sha256,
                'bytes': build.artifact.bytes,
                'commit': expected_commit,
            })

    document = {
        'test_id': test.id,
        'engine': test.dev_engine,
        'producer_commit': test.dev.sha.lower(),
        'producer_builds': producer_builds,
        'producer_artifact_required': producer_required,
        'producer_contract_sha256': (
            test.datagen_producer_contract_sha256
        ),
        'environment': {
            'tablebase_required': tablebase_required,
            'contract_sha256': test.datagen_environment_contract_sha256,
            'tablebase_family': test.datagen_tablebase_family or None,
            'tablebase_max': test.datagen_tablebase_max,
            'tablebase_manifest_sha256': (
                test.datagen_tablebase_manifest_sha256 or None
            ),
            'teacher_mode': test.datagen_teacher_mode or None,
        },
        'total_count': test.datagen_total_count,
        'positions_per_chunk': test.datagen_positions_per_chunk,
        'base_seed': test.datagen_base_seed,
        'chunks': [
            {
                'index': chunk.idx,
                'seed': chunk.seed(),
                'positions': chunk.position_count,
                'artifact_sha256': chunk.sha256,
                'artifact_bytes': chunk.bytes,
                'producer_sha256': chunk.producer_sha256 or None,
                'producer_bytes': chunk.producer_bytes,
                'producer_commit': chunk.producer_commit or None,
                'environment_lease': chunk.environment_lease or None,
                'environment_lease_sha256': (
                    chunk.environment_lease_sha256 or None
                ),
                'environment_receipt': chunk.environment_receipt or None,
                'environment_receipt_sha256': (
                    chunk.environment_receipt_sha256 or None
                ),
            }
            for chunk in chunks
        ],
    }
    if test.is_publication_datagen():
        document.update({
            'schema': (
                OpenBench.datagen_publication.
                DATAGEN_PUBLICATION_MANIFEST_SCHEMA
            ),
            'version': (
                OpenBench.datagen_publication.
                DATAGEN_PUBLICATION_MANIFEST_VERSION
            ),
            'protocol': 41,
            'publication_contract': test.datagen_publication_contract,
            'publication_contract_sha256': (
                test.datagen_publication_contract_sha256
            ),
        })
        document = OpenBench.datagen_publication.add_manifest_hash(document)
    return api_response(document)


@csrf_exempt
def api_datagen_producer(request, sha256):

    if not api_authenticate(request, require_enabled=True):
        return api_response({ 'error' : 'API requires authentication for this server' })

    sha256 = sha256.lower()
    if not re.fullmatch(r'[0-9a-f]{64}', sha256):
        return api_response({'error': 'Malformed DATAGEN producer SHA-256'})

    artifact = DatagenProducerArtifact.objects.filter(sha256=sha256).first()
    if artifact is None:
        return api_response({'error': 'Unable to find DATAGEN producer artifact'})

    if artifact.state != DatagenProducerArtifact.AVAILABLE:
        return api_response({
            'error': 'DATAGEN producer artifact is not available'
        })

    try:
        descriptor = _open_verified_producer_descriptor(artifact)
    except OSError:
        return api_response({
            'error': 'DATAGEN producer metadata exists but CAS is invalid'
        })

    # FileResponse owns the exact descriptor hashed above; there is no path
    # close/reopen TOCTOU and the already-read snapshot is simply rewound.
    response = FileResponse(descriptor, content_type='application/octet-stream')
    response['Expires'] = -1
    response['Content-Length'] = artifact.bytes
    response['ETag'] = '"sha256:%s"' % artifact.sha256
    response['Content-Disposition'] = (
        'attachment; filename=producer-%s.bin' % artifact.sha256
    )
    return response

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                BUSINESS VIEWS                               #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def buyEthereal(request):
    return render(request, 'buyEthereal.html')
