#!/usr/bin/env python3
"""
Test script to run conceptMiningEngine standalone with proper processControl setup.
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sources.common.common import processControl, logger, writeLog
from sources.common.paramsManager import getConfigs
from sources.common.utils import configLoader

config = configLoader()
environment = config.get_environment()
env = {
    key: (value if "realPath" in key else os.path.join(environment["realPath"], value))
    for key, value in environment.items()
}

import argparse
args = argparse.Namespace(
    subject="SensoresACL",
    manuscript=0,
    emergente=1
)

processControl.args = args
processControl.defaults = config.get_defaults()
processControl.env = env

from sources.conceptMiningEngine import processConceptMiningEngine

writeLog("info", logger, "🚀 [TEST] Running conceptMiningEngine WITH structured data integration")
result = processConceptMiningEngine()

if result:
    writeLog("info", logger, f"✅ [TEST] Completed: {result.get('n_concepts', 0)} concepts")
else:
    writeLog("error", logger, "❌ [TEST] conceptMiningEngine returned None")
    sys.exit(1)