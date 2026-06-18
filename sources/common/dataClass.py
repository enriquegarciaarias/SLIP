from dataclasses import dataclass
from typing import List, Dict, Any

@dataclass
class RepresentativeDoc:
    doc_id: int
    title: str
    text: str


@dataclass
class Concept:
    concept_id: int
    label: str
    size: int
    keywords: List[str]
    representative_docs: List[RepresentativeDoc]
    document_indices: List[int]