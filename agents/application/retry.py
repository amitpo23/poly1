import time
from typing import Any, Callable


RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
    "upgrade your plan",
    "limit to reset",
)


def is_rate_limit_error(error: Exception) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in RATE_LIMIT_MARKERS)


def run_with_retries(
    operation: Callable[[], Any],
    *,
    operation_name: str,
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
) -> Any:
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as error:
            if is_rate_limit_error(error):
                raise RuntimeError(
                    f"{operation_name} stopped after a rate-limit error: {error}"
                ) from error

            if attempt == max_attempts:
                raise RuntimeError(
                    f"{operation_name} failed after {max_attempts} attempts: {error}"
                ) from error

            print(f"Error {error} \n \n Retrying ({attempt}/{max_attempts - 1})")
            time.sleep(retry_delay_seconds)
