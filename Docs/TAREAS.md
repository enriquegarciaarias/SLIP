Una mejora que te recomendaría para la siguiente iteración es no depender del nombre del PDF para asociarlo con el paper. Durante el proceso de descarga guarda un índice:

{
  "paper_id": "...",
  "pdf_file": "xxxx.pdf"
}

en pdf_download_log.json. Luego el extractor usa ese índice directamente. Es mucho más robusto que intentar reconstruir la correspondencia a partir del título.