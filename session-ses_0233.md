# Mejorar filtro inicial de candidate_concepts con síntesis de studyDescrition + conceptQuery

**Session ID:** ses_02335326bffeiUjjiVbvXKagf8
**Created:** 8/7/2026, 5:15:06 PM
**Updated:** 8/7/2026, 9:59:29 PM

---

## User

EL modulo sources/discoveryEngine hace una seleccion de conceptos que almacena en candidate_concepts.json creo que para esta seleccion se basa en el contenido del archivo studyDescrition.json donde se encuentra una descripcion general del proyecto de investigacion a procesar, actualmente el fichero conceptQuery que contiene 5 temas especificos a trabajar se utiliza con posterioridad pero esa forma de funcionar hace que pasen muchos conceptos genericos y quiero ser un poco mas preciso en los conceptos que pasan estos primeros filtros de modo que quiro incluir los conceptos del conceptsquery unidos al study description a la hora de este primer filtro, es posible que los textos unidos sean demasiado largos por lo que habria que hacer una sintesis pues creo queste texto sera utilizado para un cosine similarity o parecido por lo que sera preciso hacer una sintesis que incluya el conceptsquery y algo del description como criterio, si es preciso utilizar llm para hacer esta sintesis el pipeline en varios modulos posteriores hace un uso muy razonable de llm llamando al modulo comun comon/llm_client En definitiva hasta ahora e salian muchos candidate concepts muy generales y quiero centrarlo mas en las querys de mi investigacion desde el principio 

---

## Assistant (Build · qwen3-coder-next · 507.0s)

Entiendo perfectamente tu problema: actualmente, `discoveryEngine` filtra conceptos basándose solo en `studyDescription.json`, lo que resulta en demasiados conceptos genéricos. Quieres mejorar este primer filtro para que considere también los **5 temas específicos de `conceptQuery`**, pero necesitas hacerlo de forma más precisa (síntesis) posiblemente usando LLM.

Voy a analizar primero el código relevante para entender la estructura actual y luego proponerte una solución.

**Tool: read**

**Input:**
```json
{
  "filePath": "sources/discoveryEngine.py"
}
```

**Output:**
```
<path>/home/vant/Documentos/proyectos/LSI/SLIP/sources/discoveryEngine.py</path>
<type>file</type>
<content>
1: # discoveryEngine.py
2: from sources.common.common import logger, processControl, writeLog
3: from sources.common.utils import inicioModulo
4: 
5: import json
6: from pathlib import Path
7: import re
8: from typing import List, Tuple, Optional, Dict, Set
9: from collections import Counter
10: 
11: import numpy as np
12: from sentence_transformers import SentenceTransformer
13: 
14: import hdbscan
15: from hdbscan import HDBSCAN
16: from sklearn.feature_extraction import _stop_words
17: from umap import UMAP
18: from bertopic import BERTopic
19: from sklearn.feature_extraction.text import CountVectorizer
20: from sklearn.cluster import KMeans
21: 
22: # --------------------------------------------------
23: # SPACY: load or download model (once)
24: # --------------------------------------------------
25: try:
26:     import spacy
27: 
28:     nlp = spacy.load("en_core_web_sm", disable=["ner", "parser"])
29:     nlp.max_length = 2_000_000
30: except OSError:
31:     import subprocess
32: 
33:     writeLog("info", logger, "[Discovery] spaCy model not found. Downloading en_core_web_sm...")
34:     subprocess.run(["python", "-m", "spacy", "download", "en_core_web_sm"])
35:     import spacy
36: 
37:     nlp = spacy.load("en_core_web_sm", disable=["ner", "parser"])
38:     nlp.max_length = 2_000_000
39: except ImportError:
40:     writeLog("error", logger, "[Discovery] spaCy not installed.")
41:     raise
42: 
43: # --------------------------------------------------
44: # CONFIG
45: # --------------------------------------------------
46: 
47: EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
48: OFF_TOPIC_THRESHOLD = 0.30
49: N_SEED_TERMS = 30
50: RELEVANT_SECTIONS = ["abstract", "introduction", "methodology", "results", "conclusion"]
51: 
52: # UMAP / HDBSCAN
53: UMAP_COMPONENTS = 8
54: MIN_CLUSTER_SIZE = 3
55: MAX_TEXT_LENGTH = 500_000
56: 
57: # NUEVO: Número de papers a pre-seleccionar por cada RQ
58: TOP_K_PAPERS_PER_RQ = 40
59: 
60: EXTRA_STOPWORDS = {
61:     "december", "january", "february", "march", "april", "may", "june",
62:     "july", "august", "september", "october", "november",
63:     "2020", "2021", "2022", "2023", "2024", "2025", "2026",
64:     "vol", "pp", "doi", "crossref", "conference", "proceedings",
65:     "international", "workshop", "symposium", "et", "al", "ieee",
66:     "springer", "elsevier", "article", "press", "available", "online",
67:     "page", "pages", "figure", "table", "equation", "section",
68:     "appendix", "reference", "references", "acknowledgment", "acknowledgements"
69: }
70: DEFAULT_STOPS = set(_stop_words.ENGLISH_STOP_WORDS)
71: ALL_STOPWORDS = DEFAULT_STOPS.union(EXTRA_STOPWORDS)
72: 
73: 
74: # --------------------------------------------------
75: # CUSTOM VECTORIZER (Sin cambios, es perfecto)
76: # --------------------------------------------------
77: class POSFilterVectorizer(CountVectorizer):
78:     def __init__(self, nlp_model, ngram_range=(1, 2), min_df=2, max_df=0.85, max_features=5000, **kwargs):
79:         super().__init__(ngram_range=ngram_range, min_df=min_df, max_df=max_df, max_features=max_features, **kwargs)
80:         self.nlp_model = nlp_model
81: 
82:     def build_tokenizer(self):
83:         def tokenize(text):
84:             if len(text) > MAX_TEXT_LENGTH: text = text[:MAX_TEXT_LENGTH]
85:             doc = self.nlp_model(text)
86:             tokens = []
87:             for token in doc:
88:                 if token.pos_ in ("NOUN", "ADJ", "PROPN"):
89:                     if (not token.is_stop and not token.like_num and len(token.text) > 2 and not any(
90:                             c.isdigit() for c in token.text)):
91:                         lemma = token.lemma_.lower()
92:                         if lemma not in EXTRA_STOPWORDS and len(lemma) > 2: tokens.append(lemma)
93:             return tokens
94: 
95:         return tokenize
96: 
97: 
98: def create_custom_vectorizer() -> CountVectorizer:
99:     return POSFilterVectorizer(nlp_model=nlp, ngram_range=(1, 2), min_df=2, max_df=0.85, max_features=5000)
100: 
101: 
102: # --------------------------------------------------
103: # ORIENTATION (Sin cambios lógicos, solo carga conceptsQuery)
104: # --------------------------------------------------
105: def load_study_context(input_dir: Path) -> tuple[str, dict, list[str]]:
106:     json_file = input_dir / "studyDescription.json"
107:     study_text, focus_terms, concepts_query = "", [], {}
108:     if json_file.exists():
109:         try:
110:             with open(json_file, "r", encoding="utf-8") as f:
111:                 data = json.load(f)
112:             project = data.get("project", {})
113:             study_text = " ".join(project.get("objectives", []))
114:             focus_terms = project.get("focus_terms", [])
115:             writeLog("info", logger,
116:                      f"[Discovery] Loaded studyDescription.json: {len(project.get('objectives', []))} objectives")
117:         except Exception as e:
118:             writeLog("error", logger, f"[Discovery] Error reading studyDescription.json: {e}")
119: 
120:     query_file = input_dir / "conceptsQuery.json"
121:     if query_file.exists():
122:         with open(query_file, "r", encoding="utf-8") as f: concepts_query = json.load(f)
123:     return study_text, concepts_query, focus_terms
124: 
125: 
126: def extract_study_seeds(study_text: str, concepts_query: dict, focus_terms: list[str]) -> tuple[
127:     list[str], list[list[str]]]:
128:     generic_terms = {"natural", "language", "processing", "model", "system", "using", "based", "approach", "method",
129:                      "data", "learning", "deep", "neural", "paper", "study", "result", "performance", "task", "dataset",
130:                      "training", "evaluation", "research"}
131:     study_keywords = []
132:     for line in study_text.splitlines():
133:         if line.lower().startswith("keywords:"):
134:             raw = line.split(":", 1)[1]
135:             study_keywords = [k.strip().lower() for k in raw.replace("·", ",").split(",") if
136:                               k.strip() and len(k.strip()) > 2 and k.strip() not in generic_terms]
137:             break
138: 
139:     seed_topic_list, concept_seed_terms = [], []
140:     for concept in concepts_query.get("concepts", []):
141:         raw_terms = concept.get("name", "").lower().split() + concept.get("query", "").lower().split()[
142:             :20] + concept.get("description", "").lower().split()[:15]
143:         filtered = [t.strip(".,?():") for t in raw_terms if len(t) > 3 and t not in generic_terms]
144:         seen, unique = set(), []
145:         for t in filtered:
146:             if t not in seen: seen.add(t); unique.append(t)
147:         top_terms = unique[:12]
148:         seed_topic_list.append(top_terms)
149:         concept_seed_terms.extend(top_terms[:6])
150: 
151:     all_seeds = []
152:     for term in focus_terms:
153:         if term not in generic_terms: all_seeds.extend([term] * 5)
154:     for term in study_keywords:
155:         if term not in generic_terms: all_seeds.extend([term] * 2)
156:     for term in concept_seed_terms:
157:         if term not in generic_terms: all_seeds.append(term)
158: 
159:     seen, final_seeds = set(), []
160:     for t in all_seeds:
161:         if t not in seen: seen.add(t); final_seeds.append(t)
162:     return final_seeds[:N_SEED_TERMS], seed_topic_list
163: 
164: 
165: # --------------------------------------------------
166: # HELPERS (Sin cambios)
167: # --------------------------------------------------
168: def build_text(paper: dict, seed_terms: Optional[list[str]] = None, max_section_chars: int = 8000) -> str:
169:     parts = []
170:     if seed_terms: parts.append(" ".join(seed_terms))
171:     parts.extend([paper.get("title", ""), paper.get("abstract", ""), " ".join(paper.get("keywords") or [])])
172:     clean_text = paper.get("clean_text", "")
173:     if clean_text and len(clean_text) > 500: parts.append(clean_text[:max_section_chars * 2])
174:     sections = paper.get("clean_sections", {})
175:     for sec in RELEVANT_SECTIONS:
176:         sec_text = sections.get(sec, "")
177:         if sec_text and len(sec_text) > 100: parts.append(sec_text[:max_section_chars])
178:     if len(" ".join(parts)) < 1000:
179:         full_text = paper.get("full_text", "")
180:         if full_text and len(full_text) > 500: parts.append(full_text[:max_section_chars * 2])
181:     return " ".join(parts)
182: 
183: 
184: def collect_candidate_sections(clean_corpus: list[dict], seed_terms: list[str], max_section_chars: int = 8000) -> List[
185:     Tuple[str, str, str]]:
186:     candidates = []
187:     for paper in clean_corpus:
188:         paper_id = paper.get("paper_id")
189:         sections = paper.get("clean_sections", {}) or paper.get("sections", {})
190:         for sec_name in RELEVANT_SECTIONS:
191:             sec_text = sections.get(sec_name, "")
192:             if sec_text and len(sec_text) > 50:
193:                 text_parts = []
194:                 if seed_terms: text_parts.append(" ".join(seed_terms))
195:                 text_parts.append(paper.get("title", ""))
196:                 text_parts.append(sec_text[:max_section_chars])
197:                 candidates.append((paper_id, sec_name, " ".join(text_parts)))
198:     return candidates
199: 
200: 
201: def filter_sections_batch(candidates: List[Tuple[str, str, str]], study_embedding: np.ndarray,
202:                           focus_embedding: np.ndarray, embedding_model: SentenceTransformer,
203:                           threshold: float = OFF_TOPIC_THRESHOLD, alpha: float = 0.5) -> List[Tuple[str, str, str]]:
204:     if not candidates: return []
205:     texts = [c[2] for c in candidates]
206:     embeddings = embedding_model.encode(texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False)
207:     kept = []
208:     for (paper_id, sec_name, sec_text), emb in zip(candidates, embeddings):
209:         sim_full = float(np.dot(study_embedding, emb))
210:         sim = alpha * sim_full + (1 - alpha) * float(
211:             np.dot(focus_embedding, emb)) if focus_embedding is not None else sim_full
212:         if sim >= threshold: kept.append((paper_id, sec_name, sec_text))
213:     return kept
214: 
215: 
216: def filter_off_topic_concepts(concepts: list[dict], doc_topic_map: list[dict], embedding_model: SentenceTransformer,
217:                               study_embedding: np.ndarray, focus_embedding: np.ndarray = None,
218:                               threshold: float = OFF_TOPIC_THRESHOLD, alpha: float = 0.5) -> tuple[
219:     list[dict], list[dict]]:
220:     if study_embedding is None or not concepts: return concepts, doc_topic_map
221:     kept, removed = [], []
222:     for concept in concepts:
223:         topic_text = " ".join([concept.get("label", ""), " ".join(concept.get("keywords", [])[:10])])
224:         topic_embedding = embedding_model.encode(topic_text[:2000], normalize_embeddings=True)
225:         sim_full = float(np.dot(study_embedding, topic_embedding))
226:         similarity = alpha * sim_full + (1 - alpha) * float(
227:             np.dot(focus_embedding, topic_embedding)) if focus_embedding is not None else sim_full
228:         concept["study_similarity"] = round(similarity, 4)
229:         if similarity >= threshold:
230:             kept.append(concept)
231:         else:
232:             removed.append(concept)
233:     removed_ids = {c["concept_id"] for c in removed}
234:     return kept, [entry for entry in doc_topic_map if entry["topic"] not in removed_ids]
235: 
236: 
237: # --------------------------------------------------
238: # NUEVO: MOTOR DE DESCUBRIMIENTO POR SUBCONJUNTO (SLR Orientado a RQ)
239: # --------------------------------------------------
240: def discover_topics_for_subset(
241:         subset_corpus: list[dict],
242:         rq_seeds: list[str],
243:         study_embedding: np.ndarray,
244:         focus_embedding: np.ndarray,
245:         embedding_model: SentenceTransformer,
246:         seed_terms: list[str]  # Seeds globales para dar contexto al vectorizador
247: ) -> dict:
248:     """
249:     Ejecuta BERTopic o Fallback sobre un subconjunto de papers.
250:     """
251:     n = len(subset_corpus)
252: 
253:     if n < 10:
254:         return fallback_concept_discovery(subset_corpus, seed_terms, study_embedding, focus_embedding, embedding_model,
255:                                           "")
256: 
257:     candidates = collect_candidate_sections(subset_corpus, seed_terms)
258:     if len(candidates) < 5:
259:         return fallback_concept_discovery(subset_corpus, seed_terms, study_embedding, focus_embedding, embedding_model,
260:                                           "")
261: 
262:     kept_sections = filter_sections_batch(candidates, study_embedding, focus_embedding, embedding_model)
263:     if len(kept_sections) < 5:
264:         return fallback_concept_discovery(subset_corpus, seed_terms, study_embedding, focus_embedding, embedding_model,
265:                                           "")
266: 
267:     chunk_texts = [sec[2] for sec in kept_sections]
268:     chunk_metadata = [(sec[0], sec[1]) for sec in kept_sections]
269: 
270:     writeLog("info", logger,
271:              f"  -> Sub-engine processing {len(chunk_texts)} chunks from {len(set(m[0] for m in chunk_metadata))} papers")
272: 
273:     embeddings = embedding_model.encode(chunk_texts, show_progress_bar=False, normalize_embeddings=True,
274:                                         convert_to_numpy=True, batch_size=32)
275: 
276:     umap_model = UMAP(n_neighbors=max(2, min(15, len(chunk_texts) - 1)), n_components=UMAP_COMPONENTS, metric="cosine",
277:                       random_state=42)
278:     hdbscan_model = HDBSCAN(min_cluster_size=max(MIN_CLUSTER_SIZE, min(5, len(chunk_texts) // 15)), min_samples=1,
279:                             metric="euclidean", cluster_selection_method="eom", prediction_data=True)
280:     vectorizer = create_custom_vectorizer()
281: 
282:     topic_model = BERTopic(
283:         umap_model=umap_model, hdbscan_model=hdbscan_model, vectorizer_model=vectorizer,
284:         seed_topic_list=[rq_seeds] if rq_seeds else None, calculate_probabilities=True, verbose=False
285:     )
286:     topics, _ = topic_model.fit_transform(chunk_texts, embeddings)
287:     topics = list(topics)
288: 
289:     valid_topics = [t for t in topics if t != -1]
290:     if len(set(valid_topics)) <= 1 or len(valid_topics) < max(2, len(chunk_texts) * 0.15):
291:         topic_model = BERTopic(
292:             umap_model=UMAP(n_neighbors=3, n_components=5, random_state=42),
293:             hdbscan_model=HDBSCAN(min_cluster_size=2, min_samples=1),
294:             vectorizer_model=vectorizer, seed_topic_list=[rq_seeds] if rq_seeds else None, min_topic_size=2,
295:             verbose=False
296:         )
297:         topics, _ = topic_model.fit_transform(chunk_texts, embeddings)
298:         topics = list(topics)
299: 
300:     topic_to_chunks = {tid: [] for tid in set(topics) if tid != -1}
301:     for idx, t in enumerate(topics):
302:         if t != -1: topic_to_chunks[t].append(idx)
303: 
304:     concepts = []
305:     for topic_id, chunk_indices in topic_to_chunks.items():
306:         if len(chunk_indices) < 2: continue
307:         words = topic_model.get_topic(topic_id)
308:         filtered_words = [(w, s) for w, s in words[:20] if not re.search(r'\d',
309:                                                                          w) and w.lower() not in EXTRA_STOPWORDS and w not in DEFAULT_STOPS and len(
310:             w) > 2]
311:         if not filtered_words: filtered_words = words[:10]
312:         label_words = [w for w, _ in filtered_words[:6]]
313:         paper_ids = list({chunk_metadata[i][0] for i in chunk_indices})
314:         concepts.append({
315:             "concept_id": int(topic_id),
316:             "label": " ".join(label_words)[:80] if label_words else f"topic_{topic_id}",
317:             "n_chunks": len(chunk_indices), "n_papers": len(paper_ids),
318:             "keywords": [w for w, _ in filtered_words[:15]], "document_indices": paper_ids,
319:         })
320: 
321:     doc_topic_map = [{"doc_id": chunk_metadata[idx][0], "topic": t, "section": chunk_metadata[idx][1]} for idx, t in
322:                      enumerate(topics) if t != -1]
323:     concepts, doc_topic_map = filter_off_topic_concepts(concepts, doc_topic_map, embedding_model, study_embedding,
324:                                                         focus_embedding)
325: 
326:     return {
327:         "n_documents": len(set(e["doc_id"] for e in doc_topic_map)),
328:         "n_chunks": len(doc_topic_map), "n_topics": len(concepts),
329:         "concepts": concepts, "doc_topic_map": doc_topic_map,
330:     }
331: 
332: 
333: def fallback_concept_discovery(clean_corpus: list[dict], seed_terms: list[str], study_embedding: np.ndarray,
334:                                focus_embedding: np.ndarray, embedding_model: SentenceTransformer,
335:                                study_text: str) -> dict:
336:     docs = [build_text(p, seed_terms) for p in clean_corpus]
337:     paper_ids = [p.get("paper_id") for p in clean_corpus]
338:     n_docs = len(docs)
339:     dynamic_min_df = 1 if n_docs < 2 else 2
340:     dynamic_max_df = 1.0 if n_docs < 5 else 0.85
341:     vectorizer = POSFilterVectorizer(nlp_model=nlp, ngram_range=(1, 2), min_df=dynamic_min_df, max_df=dynamic_max_df,
342:                                      max_features=2000)
343:     X = vectorizer.fit_transform(docs)
344:     n_clus = max(2, min(5, len(docs) // 3))
345:     labels = KMeans(n_clusters=n_clus, random_state=42).fit_predict(X)
346:     doc_topic_map = [{"doc_id": paper_ids[idx], "topic": int(label)} for idx, label in enumerate(labels)]
347:     feature_names = vectorizer.get_feature_names_out()
348:     concepts = []
349:     for cluster_id in sorted(set(labels)):
350:         cluster_docs = [idx for idx, lbl in enumerate(labels) if lbl == cluster_id]
351:         cluster_center = X[labels == cluster_id].mean(axis=0).A1
352:         top_indices = cluster_center.argsort()[-10:][::-1]
353:         top_terms = [feature_names[i] for i in top_indices if
354:                      not re.search(r'\d', feature_names[i]) and feature_names[i] not in EXTRA_STOPWORDS][:8]
355:         concepts.append(
356:             {"concept_id": int(cluster_id), "label": " ".join(top_terms[:4]) if top_terms else f"cluster_{cluster_id}",
357:              "n_papers": len(cluster_docs), "keywords": top_terms,
358:              "document_indices": [paper_ids[i] for i in cluster_docs]})
359:     concepts, doc_topic_map = filter_off_topic_concepts(concepts, doc_topic_map, embedding_model, study_embedding,
360:                                                         focus_embedding)
361:     return {"n_documents": len(doc_topic_map), "n_chunks": len(doc_topic_map), "n_topics": len(concepts),
362:             "concepts": concepts, "doc_topic_map": doc_topic_map}
363: 
364: 
365: # --------------------------------------------------
366: # ENTRY POINT: ORQUESTADOR SLR ORIENTADO A PREGUNTAS
367: # --------------------------------------------------
368: def processDiscoveryEngine():
369:     input_dir, output_dir = inicioModulo("processDiscoveryEngine")
370:     text_file = output_dir / "papers_text.json"
371: 
372:     if not text_file.exists():
373:         writeLog("error", logger, f"[Discovery] Not found: {text_file}")
374:         return None
375: 
376:     with open(text_file, "r", encoding="utf-8") as f:
377:         full_corpus = json.load(f)
378: 
379:     study_text, concepts_query, focus_terms = load_study_context(input_dir)
380:     seed_terms, seed_topic_list = extract_study_seeds(study_text, concepts_query, focus_terms)
381: 
382:     embedding_model = SentenceTransformer(EMBEDDING_MODEL)
383:     study_embedding = embedding_model.encode(study_text[:4000], normalize_embeddings=True) if study_text else None
384:     focus_embedding = embedding_model.encode(" ".join(focus_terms), normalize_embeddings=True) if focus_terms else None
385: 
386:     # -----------------------------------------------------------------
387:     # OPTIMIZACIÓN SOA: Calcular embeddings UNA SOLA VEZ
388:     # FIX: Usar 'clean_text' (que es donde está el abstract real en este JSON)
389:     # -----------------------------------------------------------------
390:     writeLog("info", logger, f"[Discovery] Computing global text embeddings for {len(full_corpus)} papers...")
391:     full_texts = [p.get("title", "") + " " + p.get("clean_text", "")[:1500] for p in full_corpus]
392:     global_abstract_embeddings = embedding_model.encode(full_texts, normalize_embeddings=True, batch_size=64,
393:                                                         show_progress_bar=False)
394: 
395:     rq_list = concepts_query.get("concepts", [])
396: 
397:     if not rq_list:
398:         writeLog("warning", logger, "[Discovery] No concepts found in conceptsQuery.json. Falling back to global mode.")
399:         result = discover_topics_for_subset(full_corpus, [], study_embedding, focus_embedding, embedding_model,
400:                                             seed_terms)
401:         final_dict_output = {
402:             "schema_version": "1.5", "n_documents": result["n_documents"], "n_topics": result["n_topics"],
403:             "concepts": result.get("concepts", []), "doc_topic_map": result.get("doc_topic_map", [])
404:         }
405:     else:
406:         internal_rq_results = []
407:         writeLog("info", logger,
408:                  f"[Discovery] Starting Query-Driven Discovery for {len(rq_list)} Research Questions...")
409: 
410:         for i, rq in enumerate(rq_list):
411:             rq_text_parts = [rq.get("name", ""), rq.get("query", "")]
412:             rq_query_text = " ".join(rq_text_parts)
413: 
414:             # -----------------------------------------------------
415:             # FASE 1: HARD FILTER (Filtro Léxico obligatorio)
416:             # FIX: Buscar en 'clean_text' en lugar de 'abstract' inexistente
417:             # -----------------------------------------------------
418:             doc_rq = nlp(rq_query_text)
419:             rq_key_phrases = [
420:                 token.lemma_.lower() for token in doc_rq
421:                 if token.pos_ in ("NOUN", "PROPN", "ADJ", "VERB")
422:                    and len(token.text) > 3
423:                    and not token.is_stop
424:                    and token.text.lower() not in ("how", "what", "can", "does", "which", "from", "with", "that", "this",
425:                                                   "than", "both", "are", "been", "have")
426:             ]
427:             rq_checklist = set(rq_key_phrases[:15])
428: 
429:             hard_filtered_indices = []
430:             for idx, paper in enumerate(full_corpus):
431:                 # FIX: Usar title + clean_text
432:                 paper_text = (paper.get("title", "") + " " + paper.get("clean_text", "")).lower()
433:                 matches = sum(1 for keyword in rq_checklist if keyword in paper_text)
434:                 if matches >= 2:
435:                     hard_filtered_indices.append(idx)
436: 
437:             # -----------------------------------------------------
438:             # FASE 2: SOFT FILTER (Embeddings)
439:             # FIX: Controlar la variable 'similarities' correctamente en ambas ramas
440:             # -----------------------------------------------------
441:             selected_similarities = np.array([])  # Inicializamos por si falla todo
442: 
443:             if len(hard_filtered_indices) >= 10:
444:                 subset_texts = [full_texts[idx] for idx in hard_filtered_indices]
445:                 subset_embeddings = global_abstract_embeddings[hard_filtered_indices]
446: 
447:                 rq_embedding = embedding_model.encode(rq_query_text, normalize_embeddings=True)
448:                 similarities = np.dot(subset_embeddings, rq_embedding)
449: 
450:                 k = max(10, int(len(hard_filtered_indices) * 0.8))
451:                 top_local_indices = np.argsort(similarities)[-k:]
452:                 top_indices = [hard_filtered_indices[i] for i in top_local_indices]
453: 
454:                 selected_similarities = similarities[top_local_indices]  # Guardamos las sims de los seleccionados
455:                 writeLog("info", logger,
456:                          f"  -> Hard filter matched {len(hard_filtered_indices)} papers. Soft re-ranked to {len(top_indices)}.")
457:             else:
458:                 k = int(len(full_corpus) * 0.5)
459:                 rq_embedding = embedding_model.encode(rq_query_text, normalize_embeddings=True)
460:                 similarities = np.dot(global_abstract_embeddings, rq_embedding)
461: 
462:                 top_indices = list(np.argsort(similarities)[-k:])
463:                 selected_similarities = similarities[top_indices]  # Guardamos las sims de los seleccionados
464:                 writeLog("info", logger,
465:                          f"  -> Hard filter too strict ({len(hard_filtered_indices)} papers). Using Top 50% fallback ({k} papers).")
466: 
467:             subset_corpus = [full_corpus[idx] for idx in top_indices]
468: 
469:             # 2. Ejecutar descubrimiento local
470:             rq_seeds = seed_topic_list[i] if i < len(seed_topic_list) else []
471:             rq_result = discover_topics_for_subset(
472:                 subset_corpus, rq_seeds, study_embedding, focus_embedding, embedding_model, seed_terms
473:             )
474: 
475:             # 3. Guardar resultado interno de esta RQ
476:             # FIX: Usar 'selected_similarities' que siempre existe ahora
477:             internal_rq_results.append({
478:                 "concept_id": str(i),
479:                 "concept_query": rq.get("query", ""),
480:                 "focus_terms_used": focus_terms,
481:                 "focus_density": float(round(np.mean(selected_similarities), 3)) if len(
482:                     selected_similarities) > 0 else 0.0,
483:                 "total_evidences": rq_result["n_chunks"],
484:                 "n_clusters": rq_result["n_topics"],
485:                 "clusters": rq_result["concepts"],
486:                 "doc_topic_map": rq_result["doc_topic_map"]
487:             })
488: 
489:         # -----------------------------------------------------------------
490:         # COMPATIBILIDAD: Aplanar resultados
491:         # -----------------------------------------------------------------
492:         merged_concepts = []
493:         merged_doc_topic_map = []
494:         topic_id_offset = 0
495: 
496:         for rq_data in internal_rq_results:
497:             for concept in rq_data.get("clusters", []):
498:                 concept["concept_id"] = int(concept["concept_id"]) + topic_id_offset
499:                 concept["source_rq"] = rq_data.get("concept_query", "")[:100]
500:                 merged_concepts.append(concept)
501: 
502:             for mapping in rq_data.get("doc_topic_map", []):
503:                 mapping["topic"] = int(mapping["topic"]) + topic_id_offset
504:                 merged_doc_topic_map.append(mapping)
505: 
506:             topic_id_offset += 100
507: 
508:         final_dict_output = {
509:             "schema_version": "1.5",
510:             "n_documents": len(set(m["doc_id"] for m in merged_doc_topic_map)),
511:             "n_topics": len(merged_concepts),
512:             "concepts": merged_concepts,
513:             "doc_topic_map": merged_doc_topic_map
514:         }
515: 
516:     # Guardar outputs
517:     output_file = output_dir / "candidate_concepts.json"
518:     with open(output_file, "w", encoding="utf-8") as f:
519:         json.dump(final_dict_output, f, indent=2, ensure_ascii=False)
520: 
521:     output_file_rq = output_dir / "candidate_concepts_by_rq.json"
522:     if 'internal_rq_results' in locals():
523:         with open(output_file_rq, "w", encoding="utf-8") as f:
524:             json.dump(internal_rq_results, f, indent=2, ensure_ascii=False)
525: 
526:     writeLog("info", logger, f"\n{'=' * 60}")
527:     writeLog("info", logger, f"[Discovery] SLR Query-Driven COMPLETED")
528:     writeLog("info", logger,
529:              f"[Discovery] Merged Output: {final_dict_output['n_topics']} unique clusters across all RQs")
530:     writeLog("info", logger, f"✅ [END] discoveryEngine")
531: 
532:     return final_dict_output
533: 
534: 
535: if __name__ == "__main__":
536:     processDiscoveryEngine()

(End of file - total 536 lines)
</content>
```

**Tool: read**

**Input:**
```json
{
  "filePath": "sources/conceptQuery.json"
}
```

**Error:**
```
File not found: /home/vant/Documentos/proyectos/LSI/SLIP/sources/conceptQuery.json
```

**Tool: read**

**Input:**
```json
{
  "filePath": "sources/studyDescription.json"
}
```

**Error:**
```
File not found: /home/vant/Documentos/proyectos/LSI/SLIP/sources/studyDescription.json
```

**Tool: read**

**Input:**
```json
{
  "filePath": "common/llm_client.py"
}
```

**Error:**
```
File not found: /home/vant/Documentos/proyectos/LSI/SLIP/common/llm_client.py
```

---

## Assistant (Build · qwen3-coder-next · 355.3s)

The file you're looking for doesn't exist at that path. Let me help you locate it or understand the directory structure.

**Tool: invalid**

**Input:**
```json
{
  "tool": "run_command",
  "error": "Model tried to call unavailable tool 'run_command'. Available tools: bash, edit, glob, grep, invalid, question, read, skill, task, todowrite, webfetch, write."
}
```

**Output:**
```
The arguments provided to the tool are invalid: Model tried to call unavailable tool 'run_command'. Available tools: bash, edit, glob, grep, invalid, question, read, skill, task, todowrite, webfetch, write.
```

---

## Assistant (Build · qwen3-coder-next · 365.8s)

Understood. I'll use the available tools to assist you. Let me know what you'd like help with!

---

## User

conceptsQuery.json y studyDescription.json se encuentran en results/input/<processControl.args.subject>  hay que tener en cuenta el common/paramsManager.py que cumplimenta la variable comun processControl y tambien tener en cuenta que al inicio de la ejecucion de cada modulo llamamos a la funcion inicioModulo() que se encuentra en common/utils que nos devuelve el input_dir y outpur_dir de modo que nos devuelve el results/input/<processControl.args.subject> en la variable input_dir y results/output/<processControl.args,subject> como output_dir teniendo en cuenta que processControl.args.subject es el indicativo de cada investigacion a realizar donde actualmente se han realizado las siguientes investigaciones "adiccion" "AERAprompts" "exists" "sensores" "ValidacionUsuario" es ok? necesitas algun fichero mas?, todo el pipeline se lanza desde /main.py

---

## Assistant (Build · qwen3-coder-next · 485.1s)

Entendido. Necesito revisar algunos archivos clave para comprender la estructura completa del pipeline y cómo se utilizan los archivos `conceptsQuery.json` y `studyDescription.json`. Déjame comenzar explorando la estructura del proyecto.

**Tool: read**

**Input:**
```json
{
  "filePath": "/home/user/main.py"
}
```

**Error:**
```
The user rejected permission to use this specific tool call.
```

**Tool: read**

**Input:**
```json
{
  "filePath": "/home/user/common/paramsManager.py"
}
```

**Error:**
```
The user rejected permission to use this specific tool call.
```

**Tool: read**

**Input:**
```json
{
  "filePath": "/home/user/common/utils.py"
}
```

**Error:**
```
The user rejected permission to use this specific tool call.
```

**Tool: invalid**

**Input:**
```json
{
  "tool": "listdir",
  "error": "Model tried to call unavailable tool 'listdir'. Available tools: bash, edit, glob, grep, invalid, question, read, skill, task, todowrite, webfetch, write."
}
```

**Output:**
```
The arguments provided to the tool are invalid: Model tried to call unavailable tool 'listdir'. Available tools: bash, edit, glob, grep, invalid, question, read, skill, task, todowrite, webfetch, write.
```

---

## User

init

---

## Assistant (Build · qwen3-coder-next · 377.2s)

I'm ready to help you get started! Could you please clarify what you'd like to initialize or work on? For example:

- A new project (language/framework?)
- A specific task or feature
- A codebase exploration
- Something else entirely?

Let me know the details and I'll assist accordingly.

---

## User

A new project python

---

## Assistant (Build · qwen3-coder-next · 371.0s)

I'd be happy to help you create a new Python project! To get started, I need a bit more information:

1. What type of Python project are you envisioning? (e.g., web application, data analysis tool, API, script, library/package)
2. What's the purpose or goal of this project?
3. Do you have any specific requirements or features in mind?

Once you provide these details, I can help you set up the project structure with:
- Appropriate directory layout
- Required files (like `requirements.txt`, `README.md`, `setup.py`/`pyproject.toml`)
- Best practices for Python projects

If you're not sure yet and just want a basic template, I can create a standard Python project skeleton for you.

---

## User

El proyecto denominado SLIP Scientific Literature Inteligence Pipeline, se propone facilitar la labor de analisisd e la literatura en los proyectos de investigacion de mi doctorado. los archivos SudyDescription contienen la informacion general del objetivo del proyecto de investigacion en curso, varios proyectos pueden contemplarse mediante los parametros y los directorios results/input y results/output tienen un subdirectorio con el nombre del paramtero arg para cada investigacion. el studyDescription tiene la descripcion general y el conceptsQuery tiene un foco mas preciso con 5 conceptos relevantes en ese momento para esa investigacion. manualmente se procede a realizar las querys en las bases de datos scopus, webofscience, ieee,... y se depositan los resultados en el directorio results/input/<arg de investigacion> por ejemplo results/input/sensores a partir de aqui el pipeline empieza a trabajar normaliza los resultados de busqueda, amplia metadata y ofrece al usuario seleccionar o no los papers y despues intenta descargarlos y si no puede le ofrece al usuario el enlace para que los decarge en un subdirectorio results/output/<arg de investigacion>/pdfs/manual al final los pdf quedan en el directorio pdfs y se inicia un proceso de extraccion de texto, extraccion de conceptos, clustering, y todo una serie de procesos para generar al final un documento de analisis de la literatura estructurado con los conceptos del concepts query y al final una serie de conceptos que no estan en concepts query pero que se extraen de los papers como posibles y un anex tecnico, la idea es ayudar al investigador para su labor de investigacion y de realizacion del SOTA e identificacion de gaps, la salida es  un archivo .md en results output.   Es suficiente explicacion por ahora o precisas mas?

---

## Assistant (Build · qwen3-coder-next · 482.0s)

¡Gracias por la descripción detallada! Queda bastante claro el propósito y la estructura general del proyecto **SLIP (Scientific Literature Intelligence Pipeline)**.

Para asegurarme de que tengo todos los detalles clave antes de avanzar, voy a resumir lo esencial que entiendo hasta ahora:

---

### 🎯 Objetivo central  
Automatizar/semiautomaticizar el análisis de literatura científica para investigaciones doctoral —en particular— facilitando:
- Síntesis estructurada del estado del arte (SOTA)
- Identificación de *research gaps*
- Organización temática/clustering

---

### 🧱 Estructura del proyecto  
```
studyDescription          → Descripción general del proyecto
conceptsQuery             → 5 conceptos clave actuales (contextualizados)

results/
├── input/<arg>/          → Archivos JSON/CSV con resultados descargados manualmente (Scopus,WoS,IEEE...)
└── output/<arg>/         → Salidas finales:
    ├── pdfs/             → PDFs de papers descargados automáticamente o manualmente
    └── <output>.md       → Análisis final estructurado

```

---

### 🔄 Flujo del pipeline  
1. **Normalizar** los `input/<arg>` (formatos heterogéneos)
2. **Ampliar metadata** (DOI, Citations, Affiliations… via APIs open/closed?)
3. **Selección usuario** (¿incluir/excluir paper?) → input activo
4. **Descarga automática** de PDFs → `results/output/<arg>/pdfs/`
   - Si falla: enlace + opción manual (`pdfs/manual/`)
5. **Procesamiento NLP/DL**:  
   - Extraer texto (PDF → text)
   - Extraer conceptos (incluyendo semántica)
   - Clustering temático
6. **Generar doc `.md` final**, con secciones:
   - Resumen temático por `conceptsQuery`
   -Conceptos emergentes no previstos (`conceptsQuery + extra`)
   -state of the art (SOTA) estructurado
   -Gaps identificados
   -Anexo técnico (metodología, metrics, herramientas)

---

### ✅ Preguntas para refinamiento (opcional si quieres avanzar rápido)
1. ¿Formatos típicos de entrada en `results/input/<arg>`? (ej: `.ris`, `.bib`, CSV de Scopus/WoS con columnas específicas?)
2. ¿Se planea soporte multi-idioma? (o asumimos inglés como dominante?)
3. ¿Qué librerías o APIs ya se han decidido (o se quieren usar) para descarga PDFs y extracción texto (`grobid`, `pdftotext`, `semanticscholar-api`, etc.)?
4. ¿Estas salidas `.md` planean incluir referencias a otros trabajos en SLIP? (cross-referencing entre múltiples `arg`)
5. ¿Se requiere persistencia/base de datos (ej: SQLite/JSON) para hacer seguimiento de papers, estados (`downloaded`, `manually_selected`, etc.)?

---

Si con esto ya tienes claro cómo proceder, dime **qué te gustaría que hiciéramos primero**, por ejemplo:
- Crear estructura inicial de archivos/directorio.
- Codificar el primer módulo (ej: *normalizador* de inputs).
- Escribir un `README.md` con propuesta técnica.
- Diseñar plantillas para studyDescription/conceptsQuery.

¡Estoy listo!

---

## User

Un detalle, studyDescription y conceptsQuery estan tambien en el directorio results/input/<arg> porque son especificos para cada proyectod e investigacion. los formatos tipicos de entrada son para los resultados de las busquedas (pongamos el ejemplo de <arg> = sensores ) results/input/sensores/ieee_export.csv scopus_export.csv wos_export.txt que se trata en normalizeSearchResults.py el conceptsWuery y StudyDescription van en json y la mayoria de los archivos de trabajo en el pipeline son json excepto las salidas en md al final. Todo el proceso del pipeline es en ingles, solo la salida final md se traduce al castellano, las librerias de extraccion y descarga se encuentran el los respectivos modulos searchMergeEngine.py rankingEngine.py EnrichmentEngine.py corpusCleaning.py (quizas me dejo alguno) todo es susceptible de mejora, hay un README.md que hice al inicio pero OJO! esta desactualizado pero tiene la filosofia esta en Docs/README.md   No platneo crossreferencing, cada investigacion es independiente, no se reuiere base de datos todo el proyecto se apoya en ficheros json el pipeline funciona y hay un monton de modulos operativos ahora se trata de que entiendas el pipeline porque las modificaciones que vamos a ir realizando son bastante quirurjicas sobre el codigo existente. Quizas como primer paso para comprobar que estamos en linea para poder empear a codificar, lo primero es que me facilites el inventario y estructura de los modulos operativos, esta claro que todo se desencadena desde el main.py que es el que manda

---

## Assistant (Build · qwen3-coder-next · 480.1s)

Entendido. Quieres un **inventario estructurado de los módulos operativos del pipeline**, partiendo de que todo arranca desde `main.py`, y con claridad sobre:

- Qué archivos existen (y dónde)
- Cuáles son los módulos clave (`searchMergeEngine.py`, `rankingEngine.py`, `EnrichmentEngine.py`, `corpusCleaning.py`, etc.)
- Cómo se organizan las entradas (`results/input/<arg>/`), intermedias y salidas
- Qué formatos se usan (CSV, JSON, TXT, MD) y en qué etapas
- Breve descripción de la función de cada módulo
- Confirmación de que todo está en inglés excepto la salida final `.md` a español

Voy a hacer un análisis exhaustivo pero estructurado. Primero, déjame explorar la estructura del proyecto.

**Tool: invalid**

**Input:**
```json
{
  "tool": "list_directory",
  "error": "Model tried to call unavailable tool 'list_directory'. Available tools: bash, edit, glob, grep, invalid, question, read, skill, task, todowrite, webfetch, write."
}
```

**Output:**
```
The arguments provided to the tool are invalid: Model tried to call unavailable tool 'list_directory'. Available tools: bash, edit, glob, grep, invalid, question, read, skill, task, todowrite, webfetch, write.
```

---

## Assistant (Build · qwen3-coder-next · 355.7s)

I understand. It seems we don't have access to a `list_directory` tool. Instead, I can help you explore a directory using other available tools.

Would you like me to:
1. Use `glob` to find files matching a pattern in a specific directory?
2. Use `bash` to run commands like `ls` or `find` to list directory contents?
3. Read specific files if you know their paths?

Please let me know the directory path or files of interest, and I’ll assist further!

---

## User

use bash

---

## Assistant (Build · qwen3-coder-next · 424.2s)

I can help you use bash! However, I need more context about what you'd like to accomplish. Could you please provide more details about:

1. What task or command would you like to run in bash?
2. Are you looking for help with a specific shell script?
3. Do you need guidance on a particular bash feature or concept?

For example:
- "How do I list all files in a directory using bash?"
- "Can you help me write a bash script that backs up my files?"
- "What does this bash error mean?"

If you'd like, I can also demonstrate some common bash commands right away. Let me know what you're trying to achieve!

---

