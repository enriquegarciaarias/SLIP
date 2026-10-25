# Comandos y prompts de sesión — SLIP

Archivo de referencia rápida para no olvidar los prompts de inicio/cierre y los comandos útiles del proyecto.

---

## 1. Prompt de inicio de sesión (enviar al empezar cada sesión)

> Como inicio de sesión, lee los archivos `AGENTS.md` y `SESSION_STATE.md` para comprender el proyecto SLIP antes de responder.

Este prompt carga el contexto del pipeline (estructura, módulos, configuración, estado actual) sin que tengas que pegar nada manualmente.

---

## 2. Prompt de cierre de sesión (enviar como último mensaje antes de cerrar)

> Como cierre de sesión, actualiza `AGENTS.md` y `SESSION_STATE.md` con lo que se hizo en esta sesión: qué salió bien, qué falló, próximos pasos y cualquier cambio en estructura de directorios, orden de módulos, configuración o flujos.

> **Nota:** este prompt no es automático al cerrar la pestaña. Debes enviarlo tú como último mensaje de la conversación para que el agente pueda editar los archivos antes de terminar.

---

## 3. Comandos útiles del proyecto

### Ejecución del pipeline
```bash
# Activar entorno virtual
source .venv/bin/activate

# Pipeline completo para el subject "sensores"
python main.py --subject sensores --proc SLIP

# Con manuscrito del investigador y sin desarrollar conceptos emergentes
python main.py --subject sensores --proc SLIP --manuscript 1 --emergente 0
```

### Tests
```bash
source .venv/bin/activate
python -m unittest discover -s tests -t .
```

### Configuración
```bash
# Crear config.json a partir del ejemplo (contiene claves privadas, gitignorado)
cp config_example.json config.json
```

### Ollama (dependencia externa)
```bash
ollama serve                    # Levantar servicio (si no está corriendo)
ollama list                     # Ver modelos instalados
ollama pull qwen3:8b            # Modelo primario
ollama pull qwen2.5:7b          # Fallback
ollama pull llama3.1:8b         # Fallback
```

### SpaCy (modelos de idioma)
```bash
python -m spacy download en_core_web_sm
python -m spacy download es_core_news_sm
```

---

## 4. Flujo recomendado por sesión

| Momento | Acción |
|---|---|
| **Inicio** | Enviar el **prompt de inicio** (sección 1) |
| **Trabajo** | Modificar código, ejecutar pipeline, ajustar `config.json`, etc. |
| **Cierre** | Enviar el **prompt de cierre** (sección 2) como último mensaje |
| **Después del cierre** | Revisar que `AGENTS.md` y `SESSION_STATE.md` queden actualizados |

---

## 5. Archivos clave del proyecto

| Archivo | Propósito |
|---|---|
| `AGENTS.md` | Estructura y funciones del pipeline (contexto para el agente) |
| `SESSION_STATE.md` | Estado de la última sesión y próximos pasos |
| `main.py` | Orquestador del pipeline (`mainProcess()`) |
| `config.json` | Configuración global (crear a partir de `config_example.json`) |
| `docs/pipeline_SLIP.md` | Documentación detallada del flujo de datos |
| `docs/architecture.md` | Arquitectura y responsabilidades de módulos |
| `docs/configuration.md` | Referencia completa de `config.json` |
| `sources/` | Todos los módulos del pipeline |
| `tests/` | Tests unitarios |
