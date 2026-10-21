"""
@Purpose: Main script for initializing environment settings and start processing the Scientific Literature Intelligence Pipeline  project, handling main modes:
@Usage: Run `python mainProcess.py`.
"""
from sources.common.common import processControl, logger, writeLog
from sources.common.paramsManager import getConfigs


def mainProcess():
    fine_tuning_generated = False
    server_gpu = getattr(processControl.args, "serverGPU", None)

    # ------------------------------------------------------------------
    # PARTE LOCAL INTERACTIVA (pasos 1-7): se ejecuta en local y requiere
    # intervención del investigador (selección de papers, resolución de
    # descargas de PDF, etc.).
    # ------------------------------------------------------------------
    if server_gpu in (None, "start"):
        from sources.normalizeSearchResults import processNormalizeSearchResults
        # -> wos_search.json + scopus_search.json
        processNormalizeSearchResults()

        from sources.searchMergeEngine import processSearchMergeEngine
        # [bbdd]_search.json .. -> canonical.json
        processSearchMergeEngine()

        from sources.rankingEngine import processRankingEngine
        # canonical.json + studyDescription.json -> ranked_papers.json
        processRankingEngine()

        from sources.enrichmentEngine import processEnrichmentEngine
        #  ranked_papers.json -> enriched_papers.json + candidate_review.json + selected_papers.json + review_original.pdf + review_es.pdf
        processEnrichmentEngine()

        from sources.manualPapersScanner import processManualPapersScanner
        # manual_papers.json (auto-detección de PDFs en input/{subject}/manual_papers/)
        processManualPapersScanner()

        from sources.manualIngestion import processManualIngestion
        processManualIngestion()

        from sources.adquisitionEngine import processAcquisitionEngine
        # selected_papers.json -> pdf's + pdf_download_log.json + manual_recovery_log.json + missing_pdfs.json
        processAcquisitionEngine()

        # Selección completa de papers y generación del informe PRISMA (prisma.md)
        from sources.completeSelection import processCompleteSelection
        processCompleteSelection()


        #from sources.fullTextExtractionEngine import processFullTextExtraction
        # selected_papers.json -> papers_metadata.json  papers_text.json
        #processFullTextExtraction()

        from sources.corpusCleaning import processCorpusCleaning
        # papers_metadata.json  papers_text.json -> papers_metadata.json  papers_text.json
        processCorpusCleaning()

    # ------------------------------------------------------------------
    # Si modo "start", detenerse aquí. El investigador sube los archivos
    # generados al servidor GPU y ejecuta con --serverGPU server.
    # ------------------------------------------------------------------
    if server_gpu == "start":
        writeLog("info", logger,
                 "🛑 [serverGPU:start] Parte local completada. Deteniendo ejecución. "
                 "Suba los archivos de results/ al servidor GPU y ejecute con --serverGPU server.")
        return {"fine_tuning_generated": False, "server_gpu_phase": "start"}

    # ------------------------------------------------------------------
    # PARTE SERVIDOR GPU (pasos 8-21): no interactiva, se ejecuta en el
    # servidor con GPU tras subir los resultados de la parte local.
    # ------------------------------------------------------------------
    if server_gpu in (None, "server"):
        from sources.argumentativeZoning import processArgumentativeZoning
        # papers_text.json -> papers_zoned.json (clasificación retórica de párrafos)
        processArgumentativeZoning()

        from sources.structureExtraction import processStructureExtraction
        # papers_text.json -> papers_structured.json (extracción LLM de metodología, resultados, contribuciones)
        processStructureExtraction()

        from sources.discoveryEngine import processDiscoveryEngine
        # papers_text.json -> candidate_concepts.json
        # Usa el modelo de embeddings (base o fine-tuneado específico si existe)
        processDiscoveryEngine()

        from sources.conceptMiningEngine import processConceptMiningEngine
        # candidate_concepts.json + clean_corpus.json -> concept_candidates.json"
        # identifica keywords y extrae el texto relevante de cada paper indicando la seccion de donde proviene
        processConceptMiningEngine()

        from sources.conceptAlignment import processConceptAlignment
        # concept_candidates.json + conceptsQuery.json + papers_text.json + papers_metadata.json-> aligned_concepts.json
        processConceptAlignment()

        from sources.fineTuningEngine import processFineTuning
        # Fine-tuning automático del modelo de embeddings específico por investigación.
        # Se ejecuta tras conceptAlignment para disponer de aligned_concepts.json.
        # Auto-detección: si ya existe modelo fine-tuneado y el corpus no cambió, se salta.
        # Genera modelo en results/input/<subject>/models/slip-<subject>/
        ft_result = processFineTuning()
        fine_tuning_generated = ft_result.get("fine_tuning_generated", False) if ft_result else False

        from sources.conceptEvidence import processConceptEvidence
        # extrae evidencias (parrafos) de los papers seleccionados
        # aligned_concepts -> concept_evidence.json
        processConceptEvidence()

        from sources.clusterEvidences import processClusterEvidences
        # concept_evidence.json -> clustered_evidences.json
        processClusterEvidences()

        # clustered_evidences.json -> enriched_evidences.json
        from sources.enrich_evidences import processEnrichEvidences
        processEnrichEvidences()


        from sources.sythesizeFindings import processSynthesizeFindings
        # enriched_evidences.json -> concept_findings.json
        processSynthesizeFindings()

        from sources.technicalAnnex import processTechnicalAnnex
        processTechnicalAnnex()

        from sources.trainingMaterials import processTrainingMaterials
        processTrainingMaterials()

        from sources.relatedWork import processRelatedWork
        processRelatedWork()

        # Exportación para Gemini Notebook (fuentes para cuadernos)
        from sources.geminiNotebookExport import processGeminiNotebookExport
        processGeminiNotebookExport()

    return {"fine_tuning_generated": fine_tuning_generated, "server_gpu_phase": server_gpu or "full"}


def customProcess():
    """
    from sources.complementos.ExistsOverview import processExistsOverview
    processExistsOverview()

    from sources.complementos.existOverviewCheck import processExistsOverviewCheck
    processExistsOverviewCheck()
    """
    from sources.complementos.migracionInventory import processMigracionInventory
    processMigracionInventory()


if __name__ == '__main__':
    writeLog("info", logger, "********** STARTING Scientific Literature Intelligence Pipeline (SLIP) Process **********")
    getConfigs()

    max_restarts = 1  # Máximo 1 reinicio automático tras generar fine-tuning
    restart_count = 0

    while restart_count <= max_restarts:
        if processControl.args.proc == "SLIP":
            result = mainProcess()
            fine_tuning_generated = result.get("fine_tuning_generated", False) if result else False
            server_phase = result.get("server_gpu_phase", "full") if result else "full"

            if fine_tuning_generated and restart_count < max_restarts and server_phase != "start":
                restart_count += 1
                writeLog("info", logger,
                         f"🔄 [AUTO-RESTART] Fine-tuning generado en esta ejecución. "
                         f"Reiniciando pipeline (paso {restart_count}/{max_restarts}) "
                         f"para que discoveryEngine use el modelo fine-tuneado...")
                getConfigs()  # Recargar configs para que discoveryEngine vea el modelo nuevo
                continue
        else:
            customProcess()
            break
        break

    writeLog("info", logger, "********** PROCESS COMPLETED **********")
