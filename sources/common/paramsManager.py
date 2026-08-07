"""
@Purpose: Handles project-wide parameters
@Usage: Functions called by the main process
"""
from sources.common.common import processControl, logger, writeLog
from sources.common.utils import configLoader, dbTimestamp

import argparse
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
    parser.add_argument('--subject', type=str, help="Subject of investigation: sensores, AERAprompt", default="AERAprompt")
    parser.add_argument('--proc', type=str, help="Process type: proc", default="SLIP")

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

    env_data = {}
    for key, value in environment.items():
        if "realPath" in key:
            env_data[key] = value
        else:
            env_data[key] = os.path.join(environment["realPath"], value)

    os.makedirs(env_data['cache'], exist_ok=True)
    os.environ['PYTHONPYCACHEPREFIX'] = env_data['cache']
    sys.pycache_prefix = env_data['cache']
    env_data['systemName'] = socket.getfqdn()
    return env_data


def manageDefaults():
    config = configLoader()
    environment = config.get_defaults()
    return environment

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
