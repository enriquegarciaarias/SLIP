"""
@Purpose: Main script for initializing environment settings and start processing the Scientific Literature Intelligence Pipeline  project, handling main modes:
@Usage: Run `python mainProcess.py`.
"""
from importlib import import_module
from sources.common.common import processControl, logger, writeLog
from sources.common.paramsManager import getConfigs


# ---------------------------------------------------------------------------
# Mapeo de pasos del pipeline: nombre -> (módulo, función importable)
# Los nombres se normalizan a minúsculas para comparación case-insensitive.
# ---------------------------------------------------------------------------
_STEP_REGISTRY: list[tuple[str, str, str]] = [
    ("normalizeSearchResults",   "sources.normalizeSearchResults",   "processNormalizeSearchResults"),
    ("searchMergeEngine",        "sources.searchMergeEngine",        "processSearchMergeEngine"),
    ("rankingEngine",            "sources.rankingEngine",            "processRankingEngine"),
    ("enrichmentEngine",         "sources.enrichmentEngine",         "processEnrichmentEngine"),
    ("manualPapersScanner",      "sources.manualPapersScanner",      "processManualPapersScanner"),
    ("manualIngestion",          "sources.manualIngestion",          "processManualIngestion"),
    ("adquisitionEngine",        "sources.adquisitionEngine",        "processAcquisitionEngine"),
    ("completeSelection",        "sources.completeSelection",        "processCompleteSelection"),
    ("corpusCleaning",           "sources.corpusCleaning",           "processCorpusCleaning"),
    ("argumentativeZoning",      "sources.argumentativeZoning",      "processArgumentativeZoning"),
    ("structureExtraction",      "sources.structureExtraction",      "processStructureExtraction"),
    ("discoveryEngine",          "sources.discoveryEngine",          "processDiscoveryEngine"),
    ("conceptMiningEngine",      "sources.conceptMiningEngine",      "processConceptMiningEngine"),
    ("conceptAlignment",         "sources.conceptAlignment",         "processConceptAlignment"),
    ("fineTuningEngine",         "sources.fineTuningEngine",         "processFineTuning"),
    ("conceptEvidence",          "sources.conceptEvidence",          "processConceptEvidence"),
    ("clusterEvidences",         "sources.clusterEvidences",         "processClusterEvidences"),
    ("enrichEvidences",          "sources.enrich_evidences",         "processEnrichEvidences"),
    ("sythesizeFindings",        "sources.sythesizeFindings",        "processSynthesizeFindings"),
    ("technicalAnnex",           "sources.technicalAnnex",           "processTechnicalAnnex"),
    ("trainingMaterials",        "sources.trainingMaterials",        "processTrainingMaterials"),
    ("relatedWork",              "sources.relatedWork",              "processRelatedWork"),
    ("geminiNotebookExport",     "sources.geminiNotebookExport",     "processGeminiNotebookExport"),
]

# Índice de búsqueda por nombre (case-insensitive)
_STEP_INDEX: dict[str, int] = {
    nombre.lower(): idx for idx, (nombre, _, _) in enumerate(_STEP_REGISTRY)
}


def _resolve_step(start_index: int) -> dict:
    """
    Ejecuta los pasos del pipeline desde `start_index` hasta el final.

    Retorna un diccionario con el resultado (fine_tuning_generated, server_gpu_phase).
    """
    fine_tuning_generated = False

    for idx in range(start_index, len(_STEP_REGISTRY)):
        nombre, modulo, funcion = _STEP_REGISTRY[idx]

        # Importar y ejecutar el paso de forma dinámica
        module = import_module(modulo)
        fn = getattr(module, funcion)

        writeLog("info", logger,
                 f"▶️ [STEP {idx + 1}/{len(_STEP_REGISTRY)}] Ejecutando {nombre} ({modulo}.{funcion})")

        try:
            result = fn()
        except Exception as exc:
            writeLog("error", logger,
                     f"❌ [STEP {idx + 1}] Error en {nombre}: {exc}")
            raise

        # Capturar fine_tuning_generated de fineTuningEngine
        if nombre == "fineTuningEngine" and isinstance(result, dict):
            fine_tuning_generated = result.get("fine_tuning_generated", False)

    return {"fine_tuning_generated": fine_tuning_generated,
            "server_gpu_phase": f"step:{_STEP_REGISTRY[start_index][0]}"}


def mainProcess():
    fine_tuning_generated = False
    server_gpu = getattr(processControl.args, "serverGPU", None)
    step = getattr(processControl.args, "step", None)

    # ------------------------------------------------------------------
    # Si se indica --step, saltar directamente a ese paso
    # ------------------------------------------------------------------
    if step is not None:
        step_lower = step.strip().lower()
        if step_lower not in _STEP_INDEX:
            writeLog("error", logger,
                     f"❌ Paso '{step}' no reconocido. Pasos disponibles: "
                     f"{', '.join(n for n, _, _ in _STEP_REGISTRY)}")
            raise ValueError(f"Paso '{step}' no válido para --step")

        start_idx = _STEP_INDEX[step_lower]
        nombre, modulo, funcion = _STEP_REGISTRY[start_idx]
        writeLog("info", logger,
                 f"🚀 [STEP] Iniciando ejecución desde el paso {start_idx + 1}: {nombre}")

        result = _resolve_step(start_idx)
        fine_tuning_generated = result.get("fine_tuning_generated", False)
        return result

    # ------------------------------------------------------------------
    # PARTE LOCAL INTERACTIVA (pasos 1-7): se ejecuta en local y requiere
    # intervención del investigador (selección de papers, resolución de
    # descargas de PDF, etc.).
    # ------------------------------------------------------------------
    if server_gpu in (None, "start"):
        from sources.normalizeSearchResults import processNormalizeSearchResults
        processNormalizeSearchResults()

        from sources.searchMergeEngine import processSearchMergeEngine
        processSearchMergeEngine()

        from sources.rankingEngine import processRankingEngine
        processRankingEngine()

        from sources.enrichmentEngine import processEnrichmentEngine
        processEnrichmentEngine()

        from sources.manualPapersScanner import processManualPapersScanner
        processManualPapersScanner()

        from sources.manualIngestion import processManualIngestion
        processManualIngestion()

        from sources.adquisitionEngine import processAcquisitionEngine
        processAcquisitionEngine()

        from sources.completeSelection import processCompleteSelection
        processCompleteSelection()

        from sources.corpusCleaning import processCorpusCleaning
        processCorpusCleaning()

    # ------------------------------------------------------------------
    # Si modo "start", detenerse aquí.
    # ------------------------------------------------------------------
    if server_gpu == "start":
        writeLog("info", logger,
                 "🛑 [serverGPU:start] Parte local completada. Deteniendo ejecución. "
                 "Suba los archivos de results/ al servidor GPU y ejecute con --serverGPU server.")
        return {"fine_tuning_generated": False, "server_gpu_phase": "start"}

    # ------------------------------------------------------------------
    # PARTE SERVIDOR GPU (pasos 8-21): no interactiva
    # ------------------------------------------------------------------
    if server_gpu in (None, "server"):
        from sources.argumentativeZoning import processArgumentativeZoning
        processArgumentativeZoning()

        from sources.structureExtraction import processStructureExtraction
        processStructureExtraction()

        from sources.discoveryEngine import processDiscoveryEngine
        processDiscoveryEngine()

        from sources.conceptMiningEngine import processConceptMiningEngine
        processConceptMiningEngine()

        from sources.conceptAlignment import processConceptAlignment
        processConceptAlignment()

        from sources.fineTuningEngine import processFineTuning
        ft_result = processFineTuning()
        fine_tuning_generated = ft_result.get("fine_tuning_generated", False) if ft_result else False

        from sources.conceptEvidence import processConceptEvidence
        processConceptEvidence()

        from sources.clusterEvidences import processClusterEvidences
        processClusterEvidences()

        from sources.enrich_evidences import processEnrichEvidences
        processEnrichEvidences()

        from sources.sythesizeFindings import processSynthesizeFindings
        processSynthesizeFindings()

        from sources.technicalAnnex import processTechnicalAnnex
        processTechnicalAnnex()

        from sources.trainingMaterials import processTrainingMaterials
        processTrainingMaterials()

        from sources.relatedWork import processRelatedWork
        processRelatedWork()

        from sources.geminiNotebookExport import processGeminiNotebookExport
        processGeminiNotebookExport()

    return {"fine_tuning_generated": fine_tuning_generated,
            "server_gpu_phase": server_gpu or "full"}


def customProcess():
    from sources.complementos.migracionInventory import processMigracionInventory
    processMigracionInventory()


if __name__ == '__main__':
    writeLog("info", logger, "********** STARTING Scientific Literature Intelligence Pipeline (SLIP) Process **********")
    getConfigs()

    if processControl.args.proc == "SLIP":
        result = mainProcess()
        fine_tuning_generated = result.get("fine_tuning_generated", False) if result else False
    else:
        customProcess()
        fine_tuning_generated = False

    # Aviso al final si se regeneró fine-tuning en esta ejecución.
    # El operador relanzará el pipeline cuando considere oportuno para aprovechar el nuevo modelo.
    if fine_tuning_generated:
        writeLog("info", logger,
                 "=" * 70)
        writeLog("info", logger,
                 "📢 [FINE-TUNING] Se ha regenerado el modelo fine-tuneado en esta ejecución.")
        writeLog("info", logger,
                 "   Este modelo podrá ser aprovechado por discoveryEngine en una pasada posterior.")
        writeLog("info", logger,
                 "   Relance el pipeline cuando considere oportuno para usar el nuevo modelo.")
        writeLog("info", logger,
                 "=" * 70)

    writeLog("info", logger, "********** PROCESS COMPLETED **********")
