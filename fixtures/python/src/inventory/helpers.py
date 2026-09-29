"""Helper decorators and formatting."""

import functools


def retry(times):
    """Decorator factory: call the wrapped function up to `times` times."""

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for _ in range(times - 1):
                try:
                    return fn(*args, **kwargs)
                except OSError:
                    pass
            return fn(*args, **kwargs)

        return wrapper

    return decorate


def fmt(value):
    return f"<{value}>"
