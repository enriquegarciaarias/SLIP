from sources.common import global_vars
import logging
from logging.handlers import RotatingFileHandler


class controlProcess():
    def __init__(self,  datasetVars={}, args={}, defaults={}, parms={}):
        self.datasetVars = datasetVars
        self.args = args
        self.defaults = defaults
        self.parms = parms
    def to_dict(self):
        return {
            "datasetVars": self.datasetVars,
            "args": self.args,
            "defaults": self.defaults,
            "parms": self.parms,
        }
global_vars.procCtrl = controlProcess()
processControl = global_vars.procCtrl

# ANSI escape sequences for colors
COLORS = {
    "info": "\033[92m",        # Light Green - readable on both dark/light
    "warning": "\033[93m",     # Light Yellow - softer and visible everywhere
    "error": "\033[91m",       # Light Red - less aggressive than pure red
    "debug": "\033[36m",       # Cyan - better contrast than blue on dark bg
    "exception": "\033[95m",   # Light Magenta - softer and visible on both
    "reset": "\033[0m"         # Reset to default
}

# Custom formatter to add color to the console output
class ColoredFormatter(logging.Formatter):
    def format(self, record):
        color = COLORS.get(record.levelname.lower(), COLORS["reset"])
        message = super().format(record)
        return f"{color}{message}{COLORS['reset']}"

def configureLogger(type="log", loggerName="deepMountainLog"):
    if type == "log":
        logFilePath = "./ProcessLog.txt"
    elif type == "proc":
        logFilePath = "./Process.txt"
    logger = logging.getLogger(loggerName)

    if not logger.hasHandlers():
        logger.setLevel(logging.DEBUG)

        # Rotating file handler
        fileHandler = RotatingFileHandler(logFilePath, maxBytes=5 * 1024 * 1024, backupCount=5)
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
        fileHandler.setFormatter(formatter)
        logger.addHandler(fileHandler)

        if type == "log":
            # Console handler with colored output
            console_handler = logging.StreamHandler()
            color_formatter = ColoredFormatter('%(asctime)s [%(levelname)s] %(message)s')
            console_handler.setFormatter(color_formatter)
            logger.addHandler(console_handler)

    return logger

def writeLog(logType, logger, msg):
    if logType == "info":
        logger.info(msg)
    elif logType == "warning":
        logger.warning(msg)
    elif logType == "debug":
        logger.debug(msg)
    elif logType == "error":
        logger.error(msg)
    elif logType == "exception":
        logger.exception(msg)
    else:
        logger.error("Invalid log type specified: %s", logType)

logger = configureLogger("log", "deepMountainLog")
logProc = configureLogger("proc", "deepMountainProc")
