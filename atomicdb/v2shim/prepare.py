"""Inputs an entry needs from v1's other models, and the writes it hands back.

v2's writer gets *prepared* inputs (plan 3.1): whatever lives outside the
graph is read before the batch.  In the shim the same happens per call:
``PREPARE[v1_name](args, kwargs, alias)`` returns a JSON-able dict that the
entry reads as ``ctx.extra.prepared``.  Preparers only read; they must not
write (a v1 helper that leaves a receipt is called back as a follow-up
instead, when the entry decides v1 would have called it).

The ``apply_*`` functions are the follow-up calls entries ask for
(``$calls``) to write models the rules port does not own, exactly as v1
writes them.
"""

from django.utils import timezone


def _quality_arm(args, kwargs, alias):
    """What the ascent's purchases read (BKD-11): the quality arm's hourly
    rate, tasks it created in the window, PENDING tasks, its cap."""
    from .. import ingest
    from ..models import AnalysisTask
    tasks = AnalysisTask.objects.using(alias)
    arm = ingest.QUALITY_ARM
    rate = ingest.arm_rate(arm)
    made = (tasks.filter(arm=arm, created__gte=timezone.now() - ingest.ARM_RATE_WINDOW).count()
            if rate > 0 else 0)
    return {'quality': {'arm': arm, 'rate': rate, 'made': made,
                        'pending': tasks.filter(state='PENDING', arm=arm).count(),
                        'cap': ingest.QUALITY_QUEUE_CAP}}


PREPARE = {
    'ingest.backup_backed_evals': _quality_arm,
}


def apply_purchase(child_key, generation, budget):
    """One purchase of the quality arm, as ``ingest._queue_quality_convergence``
    and ``_queue_cycle_disambiguation`` write it: create the task, or raise a
    PENDING one with a smaller budget (compare-and-set on the old budget)."""
    from .. import ingest
    from ..database import current_alias
    from ..models import AnalysisTask, Position
    alias = current_alias()
    child = Position.objects.using(alias).get(pk=child_key)
    task, created = AnalysisTask.objects.using(alias).get_or_create(
        position=child, generation=generation,
        defaults={'budget_nodes': budget,
                  'multipv': ingest.multipv_for(child.visits, budget),
                  'source': AnalysisTask.Source.FILL,
                  'arm': ingest.QUALITY_ARM})
    if not created and task.state == 'PENDING' and task.budget_nodes < budget:
        AnalysisTask.objects.using(alias).filter(
            id=task.id, state='PENDING', budget_nodes=task.budget_nodes).update(
                budget_nodes=budget, source=AnalysisTask.Source.FILL,
                arm=ingest.QUALITY_ARM)


def arm_rate_receipt(arm, rate, made):
    """The receipt a full arm leaves (``ingest._arm_rate_receipt``)."""
    from .. import ingest
    ingest._arm_rate_receipt(arm, rate, made)
