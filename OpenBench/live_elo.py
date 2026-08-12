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

# One reading of a gameplay workload, shared by the two places that need it:
# the initial render of the test page and the polling endpoint behind it. The
# dials would otherwise be drawn from one set of numbers and refreshed from
# another, which is exactly the kind of drift nobody notices until a needle
# disagrees with the stat block underneath it.
#
# The dial design follows Fishtest's live_elo page.

import math

import OpenBench.stats

# Only gameplay workloads have an Elo to report. SPSA tunes and DATAGEN
# sessions reuse the same model, and their counters mean something else.
LIVE_ELO_MODES = ['SPRT', 'GAMES']

def has_live_elo(workload):
    return workload.test_mode in LIVE_ELO_MODES

def live_elo_results(workload):

    # Pentanomial when the workload records it and games have landed. A test
    # that predates the pentanomial switch has empty (LL..WW) counters, and
    # asking Elo() for the Elo of five zeroes answers 0.00 for a test that
    # played thousands of games.

    penta = workload.as_penta()

    if workload.use_penta and sum(penta):
        return penta

    return workload.as_tri()

def elo_axis(lower, upper):

    # The dial has to contain the whole confidence interval and still show
    # which side of zero it sits on, so the axis is at least [-1, +1] and
    # always keeps a unit of air around the interval.

    return (
        float(math.floor(min(float(lower), -1.0) - 1.0)),
        float(math.ceil (max(float(upper),  1.0) + 1.0)),
    )

def percentages(games, wins, losses, draws):
    if not games:
        return (0.0, 0.0, 0.0)
    return tuple(100.0 * x / games for x in (wins, losses, draws))

def summary_line(payload):

    # The one-line reading Fishtest prints above its dials.

    parts = []

    if payload['llr'] is not None:
        parts.append('LLR: %0.2f [%0.2f, %0.2f]' % (
            payload['llr'], payload['llr_lower'], payload['llr_upper']))

    parts.append('Elo: %0.2f [%0.2f, %0.2f] (95%%)' % (
        payload['elo'], payload['elo_lower'], payload['elo_upper']))

    parts.append('LOS: %0.1f%%' % (payload['los']))

    win, loss, draw = percentages(
        payload['games'], payload['wins'],
        payload['losses'], payload['draws'])

    parts.append('Games: %d [W %0.1f%% L %0.1f%% D %0.1f%%]' % (
        payload['games'], win, loss, draw))

    if payload['penta'] is not None:
        parts.append('Pentanomial: [%d, %d, %d, %d, %d]' % tuple(payload['penta']))

    return ' | '.join(parts)

def live_elo_payload(workload):

    results = live_elo_results(workload)

    lower, elo, upper = OpenBench.stats.Elo(results)
    axis_lower, axis_upper = elo_axis(lower, upper)

    games, wins, losses, draws = workload.as_nwld()

    payload = {
        'id'             : workload.id,
        'mode'           : workload.test_mode,

        'llr'            : None,
        'llr_lower'      : None,
        'llr_upper'      : None,

        # scipy answers with numpy scalars; the wire carries plain floats
        'elo'            : round(float(elo  ), 4),
        'elo_lower'      : round(float(lower), 4),
        'elo_upper'      : round(float(upper), 4),
        'elo_axis_lower' : axis_lower,
        'elo_axis_upper' : axis_upper,

        'los'            : round(100.0 * float(OpenBench.stats.LOS(results)), 4),

        'games'          : games,
        'wins'           : wins,
        'losses'         : losses,
        'draws'          : draws,
        'penta'          : list(workload.as_penta()) if workload.use_penta else None,

        'finished'       : workload.finished,
        'passed'         : workload.passed,
        'failed'         : workload.failed,
    }

    if workload.test_mode == 'SPRT':
        payload.update({
            'llr'       : round(workload.currentllr, 4),
            'llr_lower' : round(workload.lowerllr  , 4),
            'llr_upper' : round(workload.upperllr  , 4),
            'elo0'      : workload.elolower,
            'elo1'      : workload.eloupper,
        })

    if workload.test_mode == 'GAMES':
        payload['max_games'] = workload.max_games

    payload['summary'] = summary_line(payload)

    return payload
