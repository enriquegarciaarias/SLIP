from sources.common.common import logger, processControl, writeLog

import json
# ==========================================================
# JSON writer
# ==========================================================

def save_json(data, filepath):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)