import functools
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 2.0,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:  # noqa: BLE001
                    last_exc = e
                    if attempt < max_attempts:
                        time.sleep(base_delay * (2 ** (attempt - 1)))
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator
