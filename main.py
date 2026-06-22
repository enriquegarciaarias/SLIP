"""
@Purpose: Main script for initializing environment settings and start processing the Scientific Literature Intelligence Pipeline  project, handling main modes:
@Usage: Run `python mainProcess.py`.
"""
from sources.common.common import processControl, logger, writeLog
from sources.common.paramsManager import getConfigs

















from huggingface_hub import HfApi
import sys

def mainProcess():
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

    from sources.adquisitionEngine import processAcquisitionEngine
    # selected_papers.json -> pdf's + pdf_download_log.json + manual_recovery_log.json + missing_pdfs.json
    processAcquisitionEngine()
    """

    from sources.fullTextExtractionEngine import processFullTextExtraction
    # selected_papers.json -> papers_metadata.json  papers_text.json
    processFullTextExtraction()

    from sources.corpusCleaning import processCorpusCleaning
    # papers_metadata.json  papers_text.json -> papers_metadata.json  papers_text.json
    processCorpusCleaning()

    from sources.discoveryEngine import processDiscoveryEngine
    # papers_text.json -> candidate_concepts.json
    processDiscoveryEngine()

    from sources.conceptMiningEngine import processConceptMiningEngine
    # candidate_concepts.json + clean_corpus.json -> concept_candidates.json"
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

    from sources.sythesizeFindings import processSynthesizeFindings
    # clustered_evidences.json -> concept_findings.json
    processSynthesizeFindings()

    from sources.technicalAnnex import processTechnicalAnnex
    processTechnicalAnnex()

    from sources.trainingMaterials import processTrainingMaterials
    processTrainingMaterials()

    from sources.relatedWork import processRelatedWork
    processRelatedWork()
    return


if __name__ == '__main__':
    writeLog("info", logger, "********** STARTING Scientific Literature Intelligence Pipeline (SLIP) Process **********")
    getConfigs()
    mainProcess()
    writeLog("info", logger, "********** PROCESS COMPLETED **********")
