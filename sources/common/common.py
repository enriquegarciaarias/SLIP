from sources.common import global_vars

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from colorama import just_fix_windows_console

# ----------------------------------------------------------------------
# Enable ANSI support (Windows/Linux/macOS)
# ----------------------------------------------------------------------

just_fix_windows_console()

USE_COLORS = (
    sys.stderr.isatty()
    and os.getenv("TERM") not in (None, "dumb")
)

# ----------------------------------------------------------------------
# Global process control
# ----------------------------------------------------------------------

class controlProcess:

    def __init__(self, datasetVars=None, args=None, defaults=None, parms=None):

        self.datasetVars = datasetVars or {}
        self.args = args or {}
        self.defaults = defaults or {}
        self.parms = parms or {}

    def to_dict(self):

        return {
            "datasetVars": self.datasetVars,
            "args": self.args,
            "defaults": self.defaults,
            "parms": self.parms,
        }


global_vars.procCtrl = controlProcess()
processControl = global_vars.procCtrl

# ----------------------------------------------------------------------
# Colors
# ----------------------------------------------------------------------

COLORS = {
    "DEBUG": "\033[36m",       # Cyan
    "INFO": "\033[92m",        # Green
    "WARNING": "\033[93m",     # Yellow
    "ERROR": "\033[91m",       # Red
    "CRITICAL": "\033[95m",    # Magenta
}

RESET = "\033[0m"

# ----------------------------------------------------------------------
# Formatter
# ----------------------------------------------------------------------

class ColoredFormatter(logging.Formatter):

    def format(self, record):

        original_level = record.levelname

        if USE_COLORS and original_level in COLORS:
            record.levelname = f"{COLORS[original_level]}{original_level:<8}{RESET}"
        else:
            record.levelname = f"{original_level:<8}"

        message = super().format(record)

        record.levelname = original_level

        return message


# ----------------------------------------------------------------------
# Logger configuration
# ----------------------------------------------------------------------

def configureLogger(log_type="log", logger_name="deepMountain"):

    log_file = "./ProcessLog.txt" if log_type == "log" else "./Process.txt"

    logger = logging.getLogger(logger_name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # --------------------------------------------------
    # File Handler
    # --------------------------------------------------

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )

    file_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s"
    )

    file_handler.setFormatter(file_formatter)

    logger.addHandler(file_handler)

    # --------------------------------------------------
    # Console Handler
    # --------------------------------------------------

    if log_type == "log":

        console_handler = logging.StreamHandler()

        console_formatter = ColoredFormatter(
            "%(asctime)s [%(levelname)s] %(message)s"
        )

        console_handler.setFormatter(console_formatter)

        logger.addHandler(console_handler)

    return logger


# ----------------------------------------------------------------------
# Logging helper
# ----------------------------------------------------------------------

def writeLog(level: str, logger: logging.Logger, message: str):

    level = level.lower()

    if not hasattr(logger, level):
        logger.error(f"Invalid log level '{level}'")
        return

    getattr(logger, level)(message)


# ----------------------------------------------------------------------
# Global loggers
# ----------------------------------------------------------------------

logger = configureLogger("log", "enriqueLog")
logProc = configureLogger("proc", "enriqueProc")