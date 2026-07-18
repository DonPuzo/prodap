from django.db import transaction

from .models import StatusUpdate


def transition_status(*, record, new_status, updated_by, note=''):
    """The only sanctioned way to change a ProcurementRecord's status.

    Writes an audit row and updates the record in the same transaction —
    never allow a direct status field update that bypasses this (see
    PRODAP_AGENT_BUILD_PROMPT_V2.md section 3.6).
    """
    old_status = record.status
    with transaction.atomic():
        record.status = new_status
        record.save(update_fields=['status', 'updated_at'])
        StatusUpdate.objects.create(
            record=record,
            old_status=old_status,
            new_status=new_status,
            note=note,
            updated_by=updated_by,
        )
    return record
