# **Identificación de oportunidades de investigación para integrar señales fisiológicas en NLP y Human-Centric AI**.


> La literatura demuestra que las señales fisiológicas son eficaces para modelar estados cognitivos y emocionales humanos, pero su integración en NLP sigue siendo marginal. La oportunidad no parece estar en desarrollar nuevos sensores, sino en diseñar métodos, recursos y métricas que incorporen información fisiológica y metadatos del anotador en sistemas NLP y Human-Centric AI.

---

El foco de los trabajos no está en NLP, sino en cognición y emoción, la mayor parte utilizan sensores para:

* Medir carga cognitiva.
* Detectar estrés.
* Reconocer emociones.
* Monitorizar atención.
* Evaluar estados mentales humanos.

Los dominios predominantes son:

* Conducción.
* Educación.
* Salud.
* Interacción humano-robot.
* Affective Computing.
* Human-Machine Interaction.

Las modalidades más utilizadas son EEG, ECG, EDA, eye tracking y respiración.  

Existe por tanto una gran cantidad de conocimiento sobre cómo medir estados humanos, pero muy poco sobre cómo incorporar esa información en pipelines NLP modernos.

---

# Eye Tracking el sensor más prometedor

De todas las modalidades revisadas, el eye tracking aparece repetidamente asociado a:

* Atención.
* Carga cognitiva.
* Comprensión.
* Procesamiento de información.

Además, varios trabajos muestran correlaciones fuertes entre:

* Duración de fijaciones.
* Revisitas.
* Dilatación pupilar.
* Indicadores cognitivos medidos mediante EEG. 


Priorización de sensores

1. Eye tracking.
2. ECG/HRV.
3. EDA.

EEG ofrece mucha información, pero es considerablemente más invasivo y complejo.

---

# Fusión multimodal

Los mejores resultados no provienen de una única señal fisiológica, la tendencia es combinar:

* ECG
* EDA
* Eye tracking
* EEG

mediante arquitecturas de Deep Learning y Transformers multimodales.  


# Escasez de datasets enriquecidos

Muchos trabajos destacan la necesidad de:

* Nuevos datasets multimodales.
* Anotaciones fisiológicas.
* Datos sincronizados con tareas cognitivas.
* Información contextual del usuario.



> Construir recursos NLP enriquecidos con información fisiológica y metadatos del anotador.


---

# Human-Centric AI carece de métricas centradas en humanos

La mayoría de trabajos de XAI afirman ser explicables pero muy pocos validan sus explicaciones con usuarios reales.

Un estudio revisado, CHI EA 2025 (Extended Abstracts of the CHI Conference on Human Factors in Computing Systems) (wos_45). concluye que solo el 0,7% de la literatura XAI incluye validación humana. 

Es por tanto interesante:

> Utilizar sensores fisiológicos para evaluar sistemas NLP desde una perspectiva humana.

Por ejemplo:

* esfuerzo cognitivo,
* confianza,
* estrés,
* sobrecarga,
* atención,
* fatiga.

---

# Reducir alucinaciones y mejorar decisiones

Aunque la evidencia todavía es preliminar, varios trabajos sugieren que los estados fisiológicos pueden utilizarse para:

* detectar incertidumbre,
* detectar sobrecarga cognitiva,
* evaluar confianza del usuario,
* adaptar respuestas de IA.


Puede ser interesante:

> utilizar señales fisiológicas y metainformación del anotador para mejorar la alineación y fiabilidad de sistemas generativos.

---


# Líneas de desarrollo

1. **Datasets NLP enriquecidos con sensores y metadatos de anotadores.**
2. **Modelos NLP multimodales que incorporen señales fisiológicas.**
3. **Métricas Human-Centric para evaluar LLMs mediante sensores.**
4. **Uso de sensores para estimar confianza, incertidumbre y riesgo de alucinación.**
5. **Eye tracking como modalidad principal para modelar procesos de lectura y comprensión.**

De las cinco, la que veo más alineada con los objetivos originales de ANNOTATE y con mayor novedad científica es la combinación de **datasets enriquecidos + métricas Human-Centric para evaluación de sistemas NLP/LLM**, porque conecta directamente sensores, anotadores y evaluación centrada en el ser humano.

## datasets

| Dataset | Sensores Principales | Dominio | Variables Humanas Modeladas | Relevancia para ANNOTATE |
|----------|----------|----------|----------|----------|
| ADABase | ECG, PPG, EDA, EMG, Temperatura, Respiración, Eye Tracking | Carga cognitiva | Atención, esfuerzo mental, carga cognitiva | Muy Alta |
| Multimodal VR Dataset | EEG, fNIRS, Pupillometría, EDA, Respiración, Plethysmography | Realidad Virtual | Carga cognitiva, distracción, urgencia, mind wandering | Muy Alta |
| MultiPhysio-HRC | EEG, ECG, EDA, RESP, EMG | Colaboración Humano-Robot | Estrés, carga cognitiva, estado fisiológico | Alta |
| AMIGOS | EEG, ECG, GSR/EDA, Vídeo | Affective Computing | Emoción, arousal, valence | Alta |
| DREAMER | EEG, ECG | Reconocimiento Emocional | Emoción, arousal, valence, estrés | Alta |
| MAHNOB-HCI | EEG, ECG, Eye Tracking, Vídeo | Human-Computer Interaction | Emoción, atención, respuesta fisiológica | Alta |
| K-EmoCon | ECG, EDA, Conversación, Sensores Wearables | Conversación y Emoción | Emoción continua, interacción social | Muy Alta |
| DeepSAGA | Eye Tracking | Análisis de Mirada | Patrones de atención visual y gaze annotation | Media |



| Dataset | Eye Tracking | ECG | EDA | EEG | Conversación/Texto | Potencial NLP |
|----------|----------|----------|----------|----------|----------|----------|
| ADABase | ✓ | ✓ | ✓ | ✗ | ✗ | Medio |
| Multimodal VR Dataset | ✓ (Pupillometría) | ✗ | ✓ | ✓ | ✗ | Medio |
| MultiPhysio-HRC | ✗ | ✓ | ✓ | ✓ | ✗ | Medio |
| AMIGOS | ✗ | ✓ | ✓ | ✓ | ✗ | Medio |
| DREAMER | ✗ | ✓ | ✗ | ✓ | ✗ | Bajo |
| MAHNOB-HCI | ✓ | ✓ | ✗ | ✓ | ✗ | Medio |
| K-EmoCon | ✗ | ✓ | ✓ | ✗ | ✓ | Alto |
| DeepSAGA | ✓ | ✗ | ✗ | ✗ | ✗ | Alto |

## Anotaciones posibles
Sensores como métricas Human-Centric para evaluar IA
Sensores para detectar incertidumbre del usuario
Sensores para detectar alucinaciones indirectamente (el efecto de la alucinacion en el usuario)

medir:
esfuerzo mental requerido para comprender la respuesta,
confianza que genera,
carga cognitiva,
frustración,
claridad percibida.


Texto
Etiqueta
Demografía
Eye Tracking
EDA
ECG
Feedback del anotador
Tiempo de decisión
Nivel de confianza
