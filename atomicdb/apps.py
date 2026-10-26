from django.apps import AppConfig


class AtomicdbConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "atomicdb"

    def ready(self):
        # An approved opening name puts its position on the watch list of
        # the root extremes (roots.watch_approved).  A signal and not a call
        # in the moderation view: the view saves the decision inside its own
        # atomic(), so the receiver runs in that same transaction, and the
        # view itself does not have to change.
        from django.db.models.signals import post_save

        post_save.connect(_name_saved, sender='atomicdb.OpeningNameSuggestion',
                          dispatch_uid='atomicdb.watch-approved-names')


def _name_saved(sender, instance, raw=False, **kwargs):
    if raw or instance.status != 'APPROVED':
        return
    # Imported here: roots brings the move generator with it, and nothing
    # else needs it this early.
    from . import roots

    roots.watch_approved(instance.position_id)
