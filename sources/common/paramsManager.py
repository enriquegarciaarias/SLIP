"""
@Purpose: Handles project-wide parameters
@Usage: Functions called by the main process
"""
from sources.common.common import processControl, logger, writeLog
from sources.common.utils import configLoader, dbTimestamp

import argparse
import json
import os
import sys
import socket


from huggingface_hub import login

# Constants for parameter files
JSON_PARMS = "config.json"

def manageArgs():
    """
    @Desc: Parse command-line arguments to configure the process.
    @Result: Returns parsed arguments as a Namespace object.
    """
    parser = argparse.ArgumentParser(description="Main process for Scientific Literature Intelligence Pipeline (SLIP) handling.")
    parser.add_argument('--subject', type=str, help="Subject of investigation: sensores, AERAprompt", default="SensoresACL")
    parser.add_argument('--proc', type=str, help="Process type: proc", default="SLIP")
    parser.add_argument('--manuscript', type=int, choices=[0, 1], default=None,
                        help="1 = integra el manuscrito del investigador (manual_papers/manuscript/) "
                             "y añade la sección de evidencias en training_materials. 0 = desactivado.")
    parser.add_argument('--emergente', type=int, choices=[0, 1], default=None,
                        help="0 = no desarrolla los conceptos emergentes en training_materials "
                             "(se mantienen los conteos). 1 = se desarrollan (por defecto).")
    parser.add_argument('--serverGPU', type=str, choices=['start', 'server', 'serverFull'], default=None,
                        help="Ejecución dividida GPU-server. 'start': ejecutar solo la parte "
                             "interactiva local (pasos 1-7: normalización hasta limpieza de corpus). "
                             "'server': ejecutar solo la parte no interactiva en servidor GPU "
                             "(pasos 8-21: zoning hasta exportación Gemini Notebook). "
                             "'serverFull': ejecutar el pipeline completo (pasos 1-21) en el "
                             "servidor sin prompts: selecciona todos los papers y omite la "
                             "recuperación manual de PDFs. Si no se indica, se ejecuta el "
                             "pipeline completo en local de forma interactiva.")
    parser.add_argument('--step', type=str, default=None,
                        help="Nombre del paso desde el cual iniciar el pipeline (ej. TechnicalAnnex, "
                             "DiscoveryEngine, StructureExtraction). Si se indica, se salta directamente "
                             "a ese paso y se continúa hasta el final. Si no se indica, se ejecuta "
                             "normalmente (o según --serverGPU).")

    args = parser.parse_args()
    return args


def check_gpu(min_memory_gb=8.0):
    """
    Checks available CUDA devices and filters those with total memory >= min_memory_gb.
    Logs details and sets processControl.hiper['device'] accordingly.
    Also sets CUDA_VISIBLE_DEVICES based on suitable GPUs.
    """
    import torch
    suitable_gpus = []
    if torch.cuda.is_available():
        num_gpus = torch.cuda.device_count()
        writeLog("info", logger, f'{num_gpus} CUDA devices available')

        for i in range(num_gpus):
            props = torch.cuda.get_device_properties(i)
            memory_gb = props.total_memory / (1024 ** 3)
            if memory_gb >= min_memory_gb:
                suitable_gpus.append(i)
                writeLog("info", logger, f"GPU {i} suitable: {props.name} ({memory_gb:.1f} GB)")
            else:
                writeLog("info", logger, f"GPU {i} skipped: {props.name} ({memory_gb:.1f} GB < {min_memory_gb} GB)")
    else:
        writeLog("info", logger, "No CUDA devices available")

    if suitable_gpus:
        os.environ['CUDA_VISIBLE_DEVICES'] = ','.join(map(str, suitable_gpus))
        processControl.defaults['device'] = 'cuda'
        writeLog("info", logger, f"Selected GPUs: {os.environ['CUDA_VISIBLE_DEVICES']}")
    else:
        os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
        processControl.defaults['device'] = 'cpu'
        writeLog("info", logger, "No suitable GPUs found; falling back to CPU")


def huggingface_login():
    try:
        # Add your Hugging Face token here, or retrieve it from environment variables
        token = processControl.defaults['huggingFaceToken'] if 'huggingFaceToken' in processControl.defaults else ['', '']
        login(token)
        writeLog("info", logger, "Successfully logged in to Hugging Face.")
    except Exception as e:
        writeLog("error", logger, f"Error logging into Hugging Face {str(e)}")
        raise


def setEnvironment():
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    cache = os.environ.get('.pycache', os.path.expanduser('~/.cache'))
    os.environ['PYTHONPYCACHEPREFIX'] = cache
    os.makedirs(cache, exist_ok=True)
    sys.pycache_prefix = cache

    min_memory = getattr(processControl.defaults, 'min_gpu_memory_gb', 6.0)  # Default 8GB; override in config



    if processControl.env['systemName'] == "PULSAR-PRO":
        os.environ["RANK"] = "0"
        os.environ["WORLD_SIZE"] = "1"
        os.environ["MASTER_ADDR"] = "localhost"
        os.environ["MASTER_PORT"] = "12345"
        os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

    import torch
    torch.cuda.set_per_process_memory_fraction(0.98, device=0)
    torch.backends.cuda.max_split_size_mb = 64
    check_gpu(min_memory_gb=min_memory)


def manageEnv():
    """
    @Desc: Defines environment paths and variables.
    @Result: Returns a dictionary containing environment paths.
    """
    config = configLoader()
    environment = config.get_environment()

    real_path = environment.get("realPath", "")
    results_root_raw = environment.get("results_root", "")

    # Resolver results_root: si está vacío -> None (comportamiento actual: results/ bajo realPath).
    # Si es absoluto, usarlo tal cual. Si es relativo, unir con real_path.
    results_root_abs = None
    if results_root_raw:
        if os.path.isabs(results_root_raw):
            results_root_abs = results_root_raw
        else:
            results_root_abs = os.path.join(real_path, results_root_raw)

    env_data = {}
    for key, value in environment.items():
        if key == "realPath":
            env_data[key] = value
        elif key == "results_root":
            env_data[key] = results_root_abs if results_root_abs else ""
        elif key in ("input", "output"):
            # Si hay results_root, input/output se resuelven respecto a él.
            # Si no, comportamiento actual respecto a realPath.
            if results_root_abs:
                if os.path.isabs(value):
                    env_data[key] = value
                else:
                    env_data[key] = os.path.join(results_root_abs, value)
            else:
                env_data[key] = os.path.join(real_path, value)
        else:
            env_data[key] = os.path.join(real_path, value)

    os.makedirs(env_data['cache'], exist_ok=True)
    os.environ['PYTHONPYCACHEPREFIX'] = env_data['cache']
    sys.pycache_prefix = env_data['cache']
    env_data['systemName'] = socket.getfqdn()
    return env_data


def manageDefaults():
    """
    @Desc: Carga los defaults del pipeline. Hace merge entre el config.json global
           y el config.json específico de la investigación (results/input/<subject>/config.json).
           El config específico sobrescribe al global para las claves que defina.
    @Result: Retorna el diccionario de defaults fusionado.
    """
    config = configLoader()
    environment = config.get_defaults()

    # Cargar config específico de la investigación si existe
    subject = getattr(processControl, "args", None)
    subject_name = subject.subject if subject and hasattr(subject, "subject") else None

    if subject_name:
        real_path = getattr(processControl, "env", {}).get("realPath", "")
        subject_config_path = os.path.join(
            real_path, "results", "input", subject_name, "config.json"
        )
        if os.path.exists(subject_config_path):
            try:
                with open(subject_config_path, "r", encoding="utf-8") as f:
                    subject_config = json.load(f)
                environment = _deep_merge(environment, subject_config)
                writeLog("info", logger,
                         f"[Params] Config específico cargado para investigación: {subject_name}")
            except Exception as exc:
                writeLog("warning", logger,
                         f"[Params] Error cargando config específico de {subject_name}: {exc}")

    return environment


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Hace merge profundo de dos diccionarios. Los valores de `override` sobrescriben
    a los de `base` para las mismas claves. Los diccionarios anidados se fusionan
    recursivamente en lugar de reemplazarse completamente.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result

def manageDatasetVars():
    config = configLoader()
    datasetVars = config.get_datasetVars()
    datasetVars['timestamp'] = dbTimestamp()
    return datasetVars


def getConfigs():
    """
    @Desc: Load environment settings, arguments, and hyperparameters.
    @Result: Stores configurations in processControl variables.
    """
    processControl.env = manageEnv()
    processControl.args = manageArgs()

    processControl.defaults = manageDefaults()
    processControl.datasetVars = manageDatasetVars()

    setEnvironment()
    writeLog("info", logger, "Configuration loaded.")
