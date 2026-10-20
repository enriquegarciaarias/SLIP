# Prompts de configuración de SLIP

Dos prompts encadenados para configurar la entrada de SLIP antes de lanzar una
búsqueda de literatura:

1. **studyDescription.json** — descripción de la investigación (objectives, keywords,
   focus_terms, technical_extraction).
2. **conceptsQuery.json** — descomposición operativa en consultas booleanas y conceptos
   (uno por pregunta de investigación).

Deben usarse en orden: el prompt 2 toma como entrada la salida del prompt 1.

---

## Prompt 1 — studyDescription.json

````text
Actúa como metodólogo de investigación en PLN/IA y experto en recuperación de información científica. Eres el asistente de configuración de mi herramienta SLIP (Scientific Literature Intelligence Pipeline). A partir del brainstorming que te doy al final, redáctame el contenido de `studyDescription.json` para una NUEVA investigación.

## CÓMO SLIP USA ESTOS CAMPOS (esto condiciona la redacción)
- SLIP NO lee el JSON como texto. Construye UNA consulta concatenando:
  `objectives` (×1) + `keywords` (×2) + `focus_terms` (×3).
- Esa consulta se codifica con un modelo de recuperación en inglés (BAAI/bge-base-en-v1.5) y se compara por similitud coseno con `TITLE + ABSTRACT + KEYWORDS` de cada paper candidato. Solo los top‑100 pasan al resto del pipeline.
- Los mismos `objectives` se usan además como ancla semántica en `discoveryEngine` para agrupar y cribar.
- El encoder trunca a ~512 tokens y conserva el inicio: por eso lo más discriminativo debe ir AL PRINCIPIO de los objetivos.
- Consecuencia: `objectives` debe maximizar la separación semántica entre papers relevantes e irrelevantes. Debe contener vocabulario técnico real que un paper relevante usaría en su abstract (nombres de señales, tareas, datasets, métricas, técnicas de representación). PROHIBIDO el relleno genérico ("es un tema importante", "revolucionario", "en la era de la IA"): aplana el ranking y no aporta señal léxica.
- `focus_terms` es el campo de MÁS peso: dame términos de búsqueda discriminativos, no genéricos. Incluye su equivalente inglés (el modelo es inglés) además del español.

## QUÉ DEBES REDACTAR
1. `title`: descriptivo y específico (no genérico).
2. `objectives`: 5–9 párrafos autocontenidos, acumulativos (sin repetir ideas), ~60–120 palabras cada uno, cubriendo en orden:
   (a) problema y brecha concreta; (b) hipótesis de trabajo; (c) objetivos específicos enumerados (a…e) con verbos de acción; (d) marco/enfoque metodológico; (e) contribuciones esperadas.
   - Registro académico; deben LEERSE como descripción de la investigación, no como una simple lista.
   - Integra de forma natural los términos clave (señal, tarea, representación, métrica, dataset) para enriquecer la consulta.
   - Sin citas, sin bibliografía, sin Markdown.
3. `keywords`: 12–20 términos técnicos.
4. `focus_terms`: 60–90 términos, ordenados de más a menos discriminativo, con señales, tareas, datasets/benchmarks, métricas, estrategias de representación e integración (en inglés y español).
5. `technical_extraction.fields`: 15–18 campos para extraer evidencia de cada paper. Cada campo con `name`, `description` NEUTRA (guía, nunca la respuesta), `type` (`list`/`string`) y `examples` con valores ilustrativos.

## CONTEXTO DE LA INVESTIGACIÓN
[PEGA AQUÍ el contenido de docs/dialogo.txt]

## SALIDA
Devuelve SOLO un JSON válido, sin texto extra ni comentarios, con esta forma:
{
  "project": {
    "title": "...",
    "objectives": ["...", "..."],
    "keywords": ["..."],
    "focus_terms": ["..."],
    "technical_extraction": { "fields": [ { "name": "...", "description": "...", "type": "list", "examples": ["..."] } ] }
  }
}
````

---

## Prompt 2 — conceptsQuery.json

````text
Actúa como metodólogo de investigación en PLN/IA y experto en recuperación de información científica. Eres el asistente de configuración de mi herramienta SLIP (Scientific Literature Intelligence Pipeline). Te doy el `studyDescription.json` ya redactado de una investigación (y, opcionalmente, su brainstorming) y debes generar el `conceptsQuery.json`: la descomposición operativa de los objetivos en consultas de búsqueda y conceptos.

## CÓMO SLIP USA conceptsQuery.json (esto condiciona la redacción)
- `query` (obligatorio) es una cadena de búsqueda BOOLEANA que se envía TAL CUAL a las bases de datos (Scopus, Web of Science…). Es la consulta principal de adquisición. Sintaxis real: `AND`, `OR`, `NOT`, comillas para frases y `*` para truncamiento.
- `queries` (opcional): variantes por fuente, p. ej. `{ "scopus": "...", "wos": "..." }`. Prioridad: config.json > `queries.<source>` > `query`. Úsalo solo si necesitas adaptar la sintaxis por base de datos.
- `concepts[]`: descomposición en preguntas de investigación (RQ). El `query` en lenguaje natural de cada concepto se añade a la búsqueda de la fuente para ampliar el recall (una consulta por RQ, no una sola global).
- En `conceptAlignment`, cada concepto se embebe como `name + query + description` (más `focus_terms` del studyDescription, ×2) y se alinea por similitud coseno con los conceptos descubiertos por topic modeling en el corpus. Los conceptos deben estar poco solapados entre sí para que la alineación no se confunda.
- El texto se embebe con un modelo en inglés (BAAI/bge-base-en-v1.5): escribe el contenido técnico en inglés para maximizar la precisión (en `query` y `description`); el `name` puede ir en español.
- `expansion_prompt` se envía a un LLM (queryExpander) para generar sinónimos de recuperación; debe estar en inglés e instruir la producción de una lista de términos.
- Los conceptos se ordenan por `id` y su número fija las RQ del trabajo relacionado. Define entre 4 y 6 conceptos mutuamente excluyentes y colectivamente exhaustivos (MECE) que cubran TODAS las facetas de los objetivos.

## QUÉ DEBES REDACTAR
1. `query`: cadena booleana principal que capture la INTERSECCIÓN de las familias temáticas del proyecto (p. ej. modelos de lenguaje AND señales fisiológicas AND interpretación/fiabilidad). Incluye variantes ortográficas y truncamientos. Ni tan amplia que meta ruido ni tan estrecha que vacíe el recall.
2. `queries`: (opcional) variantes por fuente solo si la sintaxis lo exige.
3. `concepts`: 4–6 objetos con:
   - `id`: entero consecutivo desde 0.
   - `name`: título corto de la RQ (en español).
   - `query`: pregunta de investigación en INGLÉS, autocontenida y específica (una frase).
   - `description`: 2–4 frases que precisen qué buscar, sinónimos técnicos y qué comparar; incluye términos técnicos que aparecerán en los abstracts.
   - `expansion_prompt`: `"Generate synonyms including: term1, term2, ..."` con 10–20 sinónimos en inglés.
   Los conceptos deben cubrir todas las facetas de los objetivos sin solaparse.

## ENTRADA
[PEGA AQUÍ el studyDescription.json ya redactado]

## SALIDA
Devuelve SOLO un JSON válido, sin texto extra ni comentarios, con esta forma:
{
  "query": "...",
  "queries": { "scopus": "...", "wos": "..." },
  "concepts": [
    { "id": 0, "name": "...", "query": "...", "description": "...", "expansion_prompt": "Generate synonyms including: ..." }
  ]
}
````
