"""
@Purpose: Main script for initializing environment settings and start processing the Scientific Literature Intelligence Pipeline  project, handling main modes:
@Usage: Run `python mainProcess.py`.
"""
from sources.common.common import processControl, logger, writeLog
from sources.common.paramsManager import getConfigs
from sources.normalizeSearchResults import processNormalizeSearchResults
from sources.searchMergeEngine import processSearchMergeEngine
from sources.rankingEngine import processRankingEngine
from sources.enrichmentEngine import processEnrichmentEngine
from sources.adquisitionEngine import processAcquisitionEngine
from sources.fullTextExtractionEngine import processFullTextExtraction
from sources.corpusCleaning import processCorpusCleaning
from sources.discoveryEngine import processDiscoveryEngine
from sources.conceptMiningEngine import processConceptMiningEngine
from sources.conceptAlignment import processConceptAlignment
from sources.conceptEvidence import processConceptEvidence
from sources.clusterEvidences import processClusterEvidences
from sources.sythesizeFindings import processSynthesizeFindings
from sources.technicalAnnex import processTechnicalAnnex
from sources.trainingMaterials import processTrainingMaterials
from sources.relatedWork import processRelatedWork

from huggingface_hub import HfApi
import sys

def mainProcess():
    """

    # -> wos_search.json + scopus_search.json
    processNormalizeSearchResults()
    # [bbdd]_search.json .. -> canonical.json
    processSearchMergeEngine()
    # canonical.json + studyDescription.txt -> ranked_papers.json
    processRankingEngine()

    #  ranked_papers.json -> enriched_papers.json + candidate_review.json + selected_papers.json + review_original.pdf + review_es.pdf
    processEnrichmentEngine()

    # selected_papers.json -> pdf's + pdf_download_log.json + manual_recovery_log.json + missing_pdfs.json
    processAcquisitionEngine()

    # selected_papers.json -> papers_metadata.json  papers_text.json
    processFullTextExtraction()


    # papers_metadata.json  papers_text.json -> papers_metadata.json  papers_text.json
    processCorpusCleaning()
    """
    # papers_text.json -> candidate_concepts.json
    processDiscoveryEngine()

    # candidate_concepts.json + clean_corpus.json -> concept_candidates.json"
    processConceptMiningEngine()

    # concept_candidates.json + conceptsQuery.json + papers_text.json + papers_metadata.json-> aligned_concepts.json
    processConceptAlignment()

    # extrae evidencias (parrafos) de los papers seleccionados
    # aligned_concepts -> concept_evidence.json
    processConceptEvidence()

    # concept_evidence.json -> clustered_evidences.json
    processClusterEvidences()

    # clustered_evidences.json -> concept_findings.json
    processSynthesizeFindings()

    processTechnicalAnnex()

    processTrainingMaterials()

    processRelatedWork()
    return


if __name__ == '__main__':
    writeLog("info", logger, "********** STARTING Scientific Literature Intelligence Pipeline (SLIP) Process **********")
    getConfigs()
    mainProcess()
    writeLog("info", logger, "********** PROCESS COMPLETED **********")
