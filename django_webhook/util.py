# pylint: disable=redefined-outer-name
import functools
from datetime import datetime, timedelta


def cache(ttl=timedelta(minutes=1)):
    """
    https://stackoverflow.com/a/50866968/2966951
    """

    def wrap(func):
        store = {}  # type: ignore

        @functools.wraps(func)
        def wrapped(*args, **kw):
            now = datetime.now()
            # see lru_cache for fancier alternatives
            key = tuple(args), frozenset(kw.items())
            if key not in store or now - store[key][0] > ttl:
                value = func(*args, **kw)
                store[key] = (now, value)
            return store[key][1]

        # Exposed so callers (and tests) can invalidate the cache explicitly.
        wrapped.cache = store  # type: ignore[attr-defined]
        wrapped.cache_clear = store.clear  # type: ignore[attr-defined]
        return wrapped

    return wrap
