from sources.common.common import logger, processControl, writeLog
import json

import time
import os
from os.path import isdir

import unicodedata
import re
from huggingface_hub import login
import hashlib
from pathlib import Path


def huggingface_login():
    try:
        # Add your Hugging Face token here, or retrieve it from environment variables
        token = processControl.defaults['huggingFaceToken'] if 'huggingFaceToken' in processControl.defaults else ['', '']
        login(token)
        writeLog("info", logger, "Successfully logged in to Hugging Face.")
    except Exception as e:
        writeLog("error", logger, f"Error logging into Hugging Face {str(e)}")
        raise

def sha1(text: str) -> str:
    """
    Generate SHA1 hash from a string (used for stable paper_id fallback).
    """
    return hashlib.sha1(text.encode("utf-8")).hexdigest()

def mkdir(dir_path):
    """
    @Desc: Creates directory if it doesn't exist.
    @Usage: Ensures a directory exists before proceeding with file operations.
    """
    if not isdir(dir_path):
        os.makedirs(dir_path)

def safe_filename(name: str) -> str:
    # Normaliza caracteres (elimina tildes y acentos)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    # Reemplaza espacios por guiones bajos
    name = name.replace(" ", "_")
    # Elimina cualquier carácter no alfanumérico, guión o subrayado
    name = re.sub(r"[^A-Za-z0-9_\-]", "", name)
    # Convierte a minúsculas
    return name.lower()


def dbTimestamp():
    """
    @Desc: Generates a timestamp formatted as "YYYYMMDDHHMMSS".
    @Result: Formatted timestamp string.
    """
    timestamp = int(time.time())
    formatted_timestamp = str(time.strftime("%Y%m%d%H%M%S", time.gmtime(timestamp)))
    return formatted_timestamp

class configLoader:
    """
    @Desc: Loads and provides access to JSON configuration data.
    @Usage: Instantiates with path to config JSON file.
    """
    def __init__(self, config_path='config.json'):
        self.base_path = os.path.realpath(os.getcwd())
        realConfigPath = os.path.join(self.base_path, config_path)
        self.config = self.load_config(realConfigPath)

    def load_config(self, realConfigPath):
        with open(realConfigPath, 'r') as config_file:
            return json.load(config_file)

    def get_environment(self):
        environment =  self.config.get("environment", None)
        environment["realPath"] = self.base_path
        return environment

    def get_defaults(self):
        return self.config.get("defaults", {})

    def get_search(self):
        return self.config.get("search", {})

    def get_datasetVars(self):
        return self.config.get("datasetVars", {})
    def get_params(self):
        return self.config.get("params", {})

def image_parser(args):
    out = args.image_file.split(args.sep)
    return out

def safe_int(value):
    """
    Convierte un valor a entero.
    Devuelve None si no es posible.
    """

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    try:
        return int(value)
    except (ValueError, TypeError):
        return None

def normalized_title(text: str) -> str:
    if not text:
        return ""

    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text

def normalize_text(text):
    if not text:
        return ""

    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_doi(doi):
    if not doi:
        return ""

    return doi.strip().lower()

def inicioModulo(modulo):
    writeLog("info", logger, "-" * 60)
    writeLog("info", logger, f"🚀 [START] Processing {modulo}")
    base_input_dir = Path(processControl.env.get("input", ""))
    base_output_dir = Path(processControl.env.get("output", ""))
    subject = processControl.args.subject
    return base_input_dir / subject, base_output_dir / subject
