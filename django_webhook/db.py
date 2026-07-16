"""
Set-based write helpers (spec D3).

These perform a set-based write and emit events for *exactly* the rows the write
affected, capturing them atomically in the write itself (``UPDATE ... RETURNING``)
rather than via a racy read-then-write. The guard stays in the ``WHERE`` clause,
so concurrent writers cannot slip between a select and an update.

For a backend without ``RETURNING`` (e.g. older MySQL) the helper falls back to
a ``SELECT ... FOR UPDATE`` inside a transaction, which is still atomic under a
row lock, and logs that it did so.
"""

import logging

from django.db import connections, transaction

from .dispatch import emit_events

logger = logging.getLogger(__name__)

_RETURNING_VENDORS = {"postgresql", "sqlite"}


def update_and_emit(queryset, *, operation="update", **updates):
    """
    Apply ``**updates`` to every row matched by ``queryset`` and emit a webhook
    event for each affected row. Returns the list of affected instances (as they
    are after the update).

    Example::

        update_and_emit(User.objects.filter(active=False), is_archived=True)
    """
    instances = _update_returning(queryset, updates)
    if instances:
        emit_events(instances, operation)
    return instances


def _update_returning(queryset, updates):  # pylint: disable=too-many-locals
    using = queryset.db
    connection = connections[using]
    model = queryset.model

    if connection.vendor not in _RETURNING_VENDORS:
        return _update_locked_fallback(queryset, updates)

    fields = model._meta.concrete_fields
    returning_cols = ", ".join(connection.ops.quote_name(f.column) for f in fields)

    set_fragments = []
    set_params = []
    for name, value in updates.items():
        field = model._meta.get_field(name)
        set_fragments.append(f"{connection.ops.quote_name(field.column)} = %s")
        set_params.append(field.get_db_prep_save(value, connection))

    compiler = queryset.query.get_compiler(using)
    where_sql, where_params = queryset.query.where.as_sql(compiler, connection)

    sql = f"UPDATE {connection.ops.quote_name(model._meta.db_table)} SET " + ", ".join(
        set_fragments
    )
    params = list(set_params)
    if where_sql:
        sql += f" WHERE {where_sql}"
        params += list(where_params)
    sql += f" RETURNING {returning_cols}"

    attnames = [f.attname for f in fields]
    instances = []
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        for row in cursor.fetchall():
            instances.append(model.from_db(using, attnames, row))
    return instances


def _update_locked_fallback(queryset, updates):
    logger.info(
        "Backend %r does not support UPDATE ... RETURNING; using SELECT FOR "
        "UPDATE fallback in update_and_emit.",
        queryset.db,
    )
    # pylint: disable=protected-access
    with transaction.atomic(using=queryset.db):
        manager = queryset.model._base_manager.using(queryset.db)
        locked = list(queryset.select_for_update())
        pks = [obj.pk for obj in locked]
        manager.filter(pk__in=pks).update(**updates)
        # Re-read the affected rows so emitted payloads reflect the new values.
        return list(manager.filter(pk__in=pks))
