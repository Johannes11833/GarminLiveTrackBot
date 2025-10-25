import logging
from rich.logging import RichHandler


def configure_logs():
    FORMAT = "[%(module)s] %(message)s"
    logging.basicConfig(
        level="INFO",
        format=FORMAT,
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_time=False)],
    )


def get_logger(name: str):
    return logging.getLogger(name)
