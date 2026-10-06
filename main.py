"""
@Purpose: Main script for initializing environment settings and start processing the Scientific Literature Intelligence Pipeline  project, handling main modes:
@Usage: Run `python mainProcess.py`.
"""
from sources.common.common import processControl, logger, writeLog
from sources.common.paramsManager import getConfigs


def mainProcess():
    """
    """
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


    #from sources.fullTextExtractionEngine import processFullTextExtraction
    # selected_papers.json -> papers_metadata.json  papers_text.json
    #processFullTextExtraction()

    from sources.corpusCleaning import processCorpusCleaning
    # papers_metadata.json  papers_text.json -> papers_metadata.json  papers_text.json
    processCorpusCleaning()


    from sources.discoveryEngine import processDiscoveryEngine
    # papers_text.json -> candidate_concepts.json
    # Solo identifica los papers
    processDiscoveryEngine()

    from sources.conceptMiningEngine import processConceptMiningEngine
    # candidate_concepts.json + clean_corpus.json -> concept_candidates.json"
    # identifica keywords y extrae el texto relevante de cada paper indicando la seccion de donde proviene
    processConceptMiningEngine()

    from sources.conceptAlignment import processConceptAlignment
    # concept_candidates.json + conceptsQuery.json + papers_text.json + papers_metadata.json-> aligned_concepts.json
    processConceptAlignment()

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

    #concept_findings.json ->
    from sources.trainingMaterials import processTrainingMaterials
    processTrainingMaterials()

    from sources.relatedWork import processRelatedWork
    processRelatedWork()
    return


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
    if processControl.args.proc == "SLIP":
        mainProcess()
    else:
        customProcess()

    writeLog("info", logger, "********** PROCESS COMPLETED **********")
