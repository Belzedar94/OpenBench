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
import string

from django.db.models import CharField, IntegerField, BigIntegerField, BooleanField, FloatField
from django.db.models import JSONField, ForeignKey, DateTimeField, OneToOneField, TextField
from django.db.models import (
    CASCADE, PROTECT, SET_NULL, CheckConstraint, Model, Q, UniqueConstraint,
)
from django.contrib.auth.models import User

class Engine(Model):

    name     = CharField(max_length=128)
    source   = CharField(max_length=1024)
    sha      = CharField(max_length=64)
    bench    = IntegerField(default=0)

    def __str__(self):
        return '{0} ({1})'.format(self.name, self.bench)

class Profile(Model):

    user     = ForeignKey(User, PROTECT, related_name='user')
    games    = IntegerField(default=0)
    tests    = IntegerField(default=0)
    repos    = JSONField(default=dict, blank=True, null=True)
    engine   = CharField(max_length=128, blank=True)
    enabled  = BooleanField(default=False)
    approver = BooleanField(default=False)
    updated  = DateTimeField(auto_now=True)

    def __str__(self):
        return self.user.__str__()

class Machine(Model):

    user      = ForeignKey(User, PROTECT, related_name='owner')
    mnps      = FloatField(default=0.00)
    dev_mnps  = FloatField(default=0.00)
    base_mnps = FloatField(default=0.00)
    updated   = DateTimeField(auto_now=True)
    secret    = CharField(max_length=64, default='None')
    info      = JSONField()
    workload  = IntegerField(default=0)

    def __str__(self):
        return '[%d] %s' % (self.id, self.user.username)

class Result(Model):

    test     = ForeignKey('Test', PROTECT, related_name='test')
    machine  = ForeignKey('Machine', PROTECT, related_name='machine')
    updated  = DateTimeField(auto_now=True)

    # Trinomial Distributions
    losses = IntegerField(default=0)
    draws  = IntegerField(default=0)
    wins   = IntegerField(default=0)

    # Pentanomial Distributions
    LL = IntegerField(default=0)
    LD = IntegerField(default=0)
    DD = IntegerField(default=0)
    DW = IntegerField(default=0)
    WW = IntegerField(default=0)

    # Overall collection of Results
    games    = IntegerField(default=0)
    crashes  = IntegerField(default=0)
    timeloss = IntegerField(default=0)

    def __str__(self):
        return '{0} {1}'.format(self.test.dev.name, self.machine.__str__())

class Test(Model):

    # Misc information
    author      = CharField(max_length=64)
    upload_pgns = CharField(max_length=16, default='FALSE')

    # Opening book settings
    book_name  = CharField(max_length=32)
    book_index = IntegerField(default=1)

    # Dev Engine, and all of its settings
    dev              = ForeignKey('Engine', PROTECT, related_name='dev')
    dev_repo         = CharField(max_length=1024)
    dev_engine       = CharField(max_length=64)
    dev_options      = CharField(max_length=256)
    dev_network      = CharField(max_length=256, blank=True)
    dev_netname      = CharField(max_length=256, blank=True)
    dev_time_control = CharField(max_length=32)

    # Base Engine, and all of its settings
    base              = ForeignKey('Engine', PROTECT, related_name='base')
    base_repo         = CharField(max_length=1024)
    base_engine       = CharField(max_length=64)
    base_options      = CharField(max_length=256)
    base_network      = CharField(max_length=256, blank=True)
    base_netname      = CharField(max_length=256, blank=True)
    base_time_control = CharField(max_length=32)

    # Changable Test Parameters
    workload_size = IntegerField(default=32)
    priority      = IntegerField(default=0)
    throughput    = IntegerField(default=0)

    # Tablebases and Cutechess adjudicatoins
    syzygy_wdl  = CharField(max_length=16, default='OPTIONAL')
    syzygy_adj  = CharField(max_length=16, default='OPTIONAL')
    win_adj     = CharField(max_length=64, default='movecount=3 score=400')
    draw_adj    = CharField(max_length=64, default='movenumber=40 movecount=8 score=10')

    # Test Mode specific values, either SPRT, GAMES, SPSA, or DATAGEN
    test_mode     = CharField(max_length=16, default='SPRT')
    elolower      = FloatField(default=0.0) # SPRT
    eloupper      = FloatField(default=0.0) # SPRT
    alpha         = FloatField(default=0.0) # SPRT
    beta          = FloatField(default=0.0) # SPRT
    lowerllr      = FloatField(default=0.0) # SPRT
    currentllr    = FloatField(default=0.0) # SPRT
    upperllr      = FloatField(default=0.0) # SPRT
    max_games     = IntegerField(default=0) # GAMES or legacy DATAGEN summary
    spsa          = JSONField(default=dict, blank=True, null=True) # SPSA
    genfens_args  = CharField(max_length=256, default='', blank=True) # DATAGEN
    play_reverses = BooleanField(default=False) # DATAGEN

    # Generic DATAGEN settings. Empty command means a historical PGN datagen.
    datagen_command             = TextField(default='', blank=True)
    datagen_total_count         = BigIntegerField(default=0)
    datagen_positions_per_chunk = BigIntegerField(default=0)
    datagen_base_seed           = BigIntegerField(default=0)
    datagen_completed_chunks    = IntegerField(default=0)
    # Frozen when the chunk map is initialized.  Runtime authorization must
    # never be derived from the mutable command text after workers have begun.
    datagen_producer_required        = BooleanField(default=False)
    datagen_producer_contract_sha256 = CharField(
        max_length=64, default='', blank=True,
    )
    # Durable reservation counters make producer admission independent of the
    # number of chunks and prevent upload -> requeue quota bypasses.
    datagen_producer_build_count = IntegerField(default=0)
    datagen_producer_build_bytes = BigIntegerField(default=0)

    # Collection of all individual Result() objects
    games  = BigIntegerField(default=0) # Overall / generic DATAGEN positions
    losses = IntegerField(default=0) # Trinomial
    draws  = IntegerField(default=0) # Trinomial
    wins   = IntegerField(default=0) # Trinomial
    LL     = IntegerField(default=0) # Pentanomial
    LD     = IntegerField(default=0) # Pentanomial
    DD     = IntegerField(default=0) # Pentanomial
    DW     = IntegerField(default=0) # Pentanomial
    WW     = IntegerField(default=0) # Pentanomial

    # Switching all future tests to Pentanomial
    use_tri   = BooleanField(default=False)
    use_penta = BooleanField(default=True)

    # All status flags associated with the test
    passed      = BooleanField(default=False)
    failed      = BooleanField(default=False)
    finished    = BooleanField(default=False)
    deleted     = BooleanField(default=False)
    approved    = BooleanField(default=False)
    awaiting    = BooleanField(default=False)
    error       = BooleanField(default=False)

    # Datetime house keeping for meta data
    creation    = DateTimeField(auto_now_add=True)
    updated     = DateTimeField(auto_now=True)

    def __str__(self):
        return '{0} vs {1} @ {2}'.format(self.dev.name, self.base.name, self.dev_time_control)

    def results(self):
        return self.as_tri() if self.use_tri else self.as_penta()

    def as_tri(self):
        return (self.losses, self.draws, self.wins)

    def as_penta(self):
        return (self.LL, self.LD, self.DD, self.DW, self.WW)

    def as_nwld(self):
        return (self.games, self.wins, self.losses, self.draws)

    def is_generic_datagen(self):
        return self.test_mode == 'DATAGEN' and bool(self.datagen_command)

    def datagen_requires_producer_artifact(self):
        return self.is_generic_datagen() and self.datagen_producer_required

    @staticmethod
    def producer_requirement_from_command(command):
        try:
            return any(
                name == 'PRODUCER_SHA256'
                for _literal, name, _format_spec, _conversion
                in string.Formatter().parse(command)
            )
        except ValueError:
            return False

    @classmethod
    def producer_contract_for_command(cls, command):
        required = cls.producer_requirement_from_command(command)
        contract = json.dumps({
            'command': command,
            'producer_artifact_required': required,
            'protocol': 39,
            'schema': 'openbench-datagen-producer-v39',
        }, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return required, hashlib.sha256(contract).hexdigest()

    def freeze_datagen_producer_contract(self):
        required, contract_sha256 = self.producer_contract_for_command(
            self.datagen_command
        )
        self.datagen_producer_required = required
        self.datagen_producer_contract_sha256 = contract_sha256

    def datagen_producer_contract_is_current(self):
        if not self.is_generic_datagen():
            return True
        required, contract_sha256 = self.producer_contract_for_command(
            self.datagen_command
        )
        return (
            required == self.datagen_producer_required
            and contract_sha256 == self.datagen_producer_contract_sha256
        )

    def datagen_total_chunks(self):
        if not self.datagen_positions_per_chunk:
            return 0
        return (
            self.datagen_total_count + self.datagen_positions_per_chunk - 1
        ) // self.datagen_positions_per_chunk

class DatagenChunk(Model):

    PENDING   = 'PENDING'
    RUNNING   = 'RUNNING'
    COMPLETED = 'COMPLETED'

    test = ForeignKey('Test', CASCADE, related_name='datagen_chunks')
    idx  = IntegerField()
    position_count = BigIntegerField()

    status      = CharField(max_length=16, default=PENDING)
    sha256      = CharField(max_length=64, default='', blank=True)
    bytes       = BigIntegerField(default=0)
    producer_sha256 = CharField(max_length=64, default='', blank=True)
    producer_bytes  = BigIntegerField(default=0)
    producer_commit = CharField(max_length=40, default='', blank=True)
    producer_build = ForeignKey(
        'DatagenProducerBuild', SET_NULL,
        related_name='chunks', null=True, blank=True,
    )
    machine     = ForeignKey(
        'Machine', SET_NULL, related_name='datagen_chunks', null=True, blank=True)
    attempts    = IntegerField(default=0)
    last_error  = TextField(default='', blank=True)

    created      = DateTimeField(auto_now_add=True)
    assigned     = DateTimeField(null=True, blank=True)
    completed    = DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['idx']
        constraints = [
            UniqueConstraint(fields=['test', 'idx'], name='unique_datagen_test_chunk'),
        ]

    def __str__(self):
        return 'Datagen #%d chunk %d (%s)' % (self.test_id, self.idx, self.status)

    def filename(self):
        return 'datagen/%d/chunk_%d.bz2' % (self.test_id, self.idx)

    def seed(self):
        return self.test.datagen_base_seed + self.idx


class DatagenProducerArtifact(Model):

    # The executable itself is immutable content-addressed evidence. Campaign
    # build rows bind commit/contract; chunk strings remain wire-compatible
    # denormalized evidence backed by the producer_build FK.
    STAGING   = 'STAGING'
    AVAILABLE = 'AVAILABLE'
    CORRUPT   = 'CORRUPT'
    UNVERIFIED = 'UNVERIFIED'

    sha256 = CharField(max_length=64, unique=True)
    bytes  = BigIntegerField()
    state = CharField(max_length=16, default=STAGING)
    staging_name = CharField(max_length=512, default='', blank=True)
    reference_count = BigIntegerField(default=0)
    created = DateTimeField(auto_now_add=True)
    updated = DateTimeField(auto_now=True)
    last_verified = DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            CheckConstraint(
                check=Q(bytes__gt=0), name='datagen_producer_bytes_positive',
            ),
            CheckConstraint(
                check=Q(reference_count__gte=0),
                name='datagen_producer_refcount_nonnegative',
            ),
            CheckConstraint(
                check=Q(state__in=[
                    'STAGING', 'AVAILABLE', 'CORRUPT', 'UNVERIFIED',
                ]),
                name='datagen_producer_state_valid',
            ),
        ]

    def __str__(self):
        return 'DATAGEN producer %s' % self.sha256

    def filename(self):
        return 'datagen-producers/sha256/%s/%s' % (
            self.sha256[:2], self.sha256,
        )


class DatagenProducerBuild(Model):

    test = ForeignKey('Test', CASCADE, related_name='datagen_producer_builds')
    artifact = ForeignKey(
        'DatagenProducerArtifact', PROTECT, related_name='campaign_builds',
    )
    owner = ForeignKey(
        User, PROTECT, related_name='datagen_producer_builds',
    )
    commit = CharField(max_length=40)
    contract_sha256 = CharField(max_length=64)
    created = DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=['test', 'artifact'],
                name='unique_datagen_campaign_producer',
            ),
        ]

    def __str__(self):
        return 'DATAGEN #%d producer %s' % (self.test_id, self.artifact.sha256)


class DatagenProducerQuota(Model):

    # A single locked row serializes physical CAS reservations on every
    # database backend, including PostgreSQL where aggregate scans alone race.
    key = CharField(max_length=16, primary_key=True, default='global')
    artifact_count = BigIntegerField(default=0)
    reserved_bytes = BigIntegerField(default=0)
    updated = DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            CheckConstraint(
                check=Q(key='global'), name='datagen_global_quota_singleton',
            ),
            CheckConstraint(
                check=Q(artifact_count__gte=0),
                name='datagen_global_artifacts_nonnegative',
            ),
            CheckConstraint(
                check=Q(reserved_bytes__gte=0),
                name='datagen_global_bytes_nonnegative',
            ),
        ]


class DatagenProducerOwnerQuota(Model):

    owner = OneToOneField(
        User, CASCADE, primary_key=True, related_name='datagen_producer_quota',
    )
    build_count = BigIntegerField(default=0)
    reserved_bytes = BigIntegerField(default=0)
    updated = DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            CheckConstraint(
                check=Q(build_count__gte=0),
                name='datagen_owner_builds_nonnegative',
            ),
            CheckConstraint(
                check=Q(reserved_bytes__gte=0),
                name='datagen_owner_bytes_nonnegative',
            ),
        ]

class LogEvent(Model):

    author     = CharField(max_length=128) # Username for the OpenBench Profile
    summary    = CharField(max_length=128) # Quick summary of the Event or Error
    log_file   = CharField(max_length=128) # .log file stored in /Media/

    machine_id = IntegerField(default=0)   # Only set for Client based Log Events
    test_id    = IntegerField(default=0)   # Should always be set

    created    = DateTimeField(auto_now_add=True)

    def __str__(self):
        return "{0} {1} {2}".format(self.author, str(self.test_id), self.summary)

class Network(Model):

    default     = BooleanField(default=False)
    was_default = BooleanField(default=False)
    sha256      = CharField(max_length=8)
    name        = CharField(max_length=64)
    engine      = CharField(max_length=64)
    author      = CharField(max_length=64)
    created     = DateTimeField(auto_now_add=True)

    def __str__(self):
        return '[{}] {} ({})'.format(self.engine, self.name, self.sha256)

class PGN(Model):

    test_id    = IntegerField(default=0)
    result_id  = IntegerField(default=0)
    book_index = IntegerField(default=0)
    processed  = BooleanField(default=False)

    def __str__(self):
        return self.filename()

    def filename(self):
        return '%s.%s.%s.pgn.bz2' % (self.test_id, self.result_id, self.book_index)
