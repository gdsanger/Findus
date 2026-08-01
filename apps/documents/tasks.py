import logging

logger = logging.getLogger(__name__)


def example_ping_task(message="pong"):
    """Minimal Django-Q2 demo task, queued from the HTMX example page.

    Confirms the redis broker + worker wiring end to end: enqueue here,
    watch the `worker` container logs for the "Findus worker" line below.
    """
    logger.info("Findus worker processed background task: %s", message)
    return message
