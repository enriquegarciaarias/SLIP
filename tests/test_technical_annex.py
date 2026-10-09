import unittest

from sources.technicalAnnex import (
    AnnexConfig,
    ExtractionField,
    TechnicalProfileExtractor,
    canonicalize_value,
    classify_echo,
)


class TestExtractionField(unittest.TestCase):
    def test_from_dict_parses_examples_list(self):
        field = ExtractionField.from_dict(
            {"name": "f", "description": "d", "type": "list",
             "examples": ["a", "b"]}
        )
        self.assertEqual(field.examples, ["a", "b"])

    def test_from_dict_wraps_scalar_examples(self):
        field = ExtractionField.from_dict(
            {"name": "f", "description": "d", "examples": "only"}
        )
        self.assertEqual(field.examples, ["only"])


def _extractor(fields):
    return TechnicalProfileExtractor(
        llm_client=None, config=AnnexConfig(extraction_fields=fields)
    )


_STRING_FIELD = ExtractionField(
    name="methodological_approach",
    description="Enfoque metodológico del estudio: diseño, muestra y análisis.",
    type="string",
)
_LIST_FIELD = ExtractionField(
    name="educational_levels",
    description="Niveles educativos considerados.",
    type="list",
    examples=["primaria", "secundaria", "educación superior"],
)


class TestPromptConstruction(unittest.TestCase):
    def test_schema_uses_neutral_placeholders_not_description(self):
        extractor = _extractor([_STRING_FIELD, _LIST_FIELD])
        prompt = extractor._build_prompt("T", "bg", "meth", "res")
        self.assertIn('"methodological_approach": "<string or null>"', prompt)
        self.assertIn('"educational_levels": ["<string>", ...]', prompt)
        # La descripción nunca se presenta como valor a devolver.
        self.assertNotIn('"string describing', prompt)

    def test_examples_are_not_injected_in_prompt(self):
        # Capa 2: el vocabulario canónico NO debe aparecer en el prompt
        # (evita que el modelo lo copie como valor).
        extractor = _extractor([_STRING_FIELD, _LIST_FIELD])
        prompt = extractor._build_prompt("T", "bg", "meth", "res")
        self.assertIn("DO NOT copy this text", prompt)
        self.assertNotIn("Possible values", prompt)
        self.assertNotIn("educación superior", prompt)
        self.assertNotIn("secundaria", prompt)


class TestEchoDetection(unittest.TestCase):
    def test_resets_string_equal_to_description(self):
        extractor = _extractor([_STRING_FIELD])
        profile = {"methodological_approach": _STRING_FIELD.description}
        extractor._drop_echoed_fields(profile)
        self.assertIsNone(profile["methodological_approach"])

    def test_resets_list_equal_to_example_vocabulary(self):
        extractor = _extractor([_LIST_FIELD])
        profile = {"educational_levels": list(_LIST_FIELD.examples)}
        extractor._drop_echoed_fields(profile)
        self.assertEqual(profile["educational_levels"], [])

    def test_keeps_legitimate_values(self):
        extractor = _extractor([_STRING_FIELD, _LIST_FIELD])
        profile = {
            "methodological_approach": "Estudio cuantitativo con 120 estudiantes.",
            "educational_levels": ["secundaria"],
        }
        extractor._drop_echoed_fields(profile)
        self.assertEqual(
            profile["methodological_approach"],
            "Estudio cuantitativo con 120 estudiantes.",
        )
        self.assertEqual(profile["educational_levels"], ["secundaria"])

    def test_resets_subset_echo(self):
        field = ExtractionField(
            name="f",
            description="d",
            type="list",
            examples=["uno de prueba", "dos de prueba",
                      "tres de prueba", "cuatro de prueba"],
        )
        extractor = _extractor([field])
        profile = {"f": ["uno de prueba", "dos de prueba", "tres de prueba"]}
        extractor._drop_echoed_fields(profile)
        self.assertEqual(profile["f"], [])

    def test_resets_collapsed_single_string(self):
        field = ExtractionField(
            name="f",
            description="d",
            type="list",
            examples=["uno de prueba", "dos de prueba", "tres de prueba"],
        )
        extractor = _extractor([field])
        profile = {"f": ["uno de prueba, dos de prueba, tres de prueba"]}
        extractor._drop_echoed_fields(profile)
        self.assertEqual(profile["f"], [])

    def test_prunes_mixed_echo_keeps_specifics(self):
        field = ExtractionField(
            name="f",
            description="d",
            type="list",
            examples=["uno de prueba", "dos de prueba", "tres de prueba"],
        )
        extractor = _extractor([field])
        profile = {"f": ["uno de prueba", "dos de prueba", "valor propio del paper"]}
        extractor._drop_echoed_fields(profile)
        self.assertEqual(profile["f"], ["valor propio del paper"])

    def test_keeps_sentinel_only(self):
        field = ExtractionField(
            name="f",
            description="d",
            type="list",
            examples=["a", "b", "no evaluada"],
        )
        extractor = _extractor([field])
        profile = {"f": ["no evaluada"]}
        extractor._drop_echoed_fields(profile)
        self.assertEqual(profile["f"], ["no evaluada"])

    def test_neutral_examples_do_not_trigger_echo(self):
        # Tokens neutros (EEG/AUC/WESAD) pueden ser valores reales.
        field = ExtractionField(
            name="f",
            description="d",
            type="list",
            examples=["EEG", "ECG", "AUC", "WESAD"],
        )
        kind, cleaned = classify_echo(["EEG", "AUC"], field)
        self.assertEqual(kind, "none")
        self.assertEqual(cleaned, ["EEG", "AUC"])

    def test_single_discriminative_category_is_kept(self):
        # Una única categoría legítima ("corpus propio") no debe resetearse.
        field = ExtractionField(
            name="f",
            description="d",
            type="list",
            examples=["corpus propio", "WESAD", "DEAP"],
        )
        kind, cleaned = classify_echo(["corpus propio"], field)
        self.assertEqual(kind, "none")
        self.assertEqual(cleaned, ["corpus propio"])

    def test_partial_echo_keeps_neutral_and_specifics(self):
        field = ExtractionField(
            name="f",
            description="d",
            type="list",
            examples=["EEG", "frecuencia cardíaca", "respiración"],
        )
        kind, cleaned = classify_echo(
            ["EEG", "frecuencia cardíaca", "respiración", "speech"], field
        )
        self.assertEqual(kind, "partial")
        self.assertEqual(cleaned, ["EEG", "speech"])


class TestCanonicalization(unittest.TestCase):
    def test_maps_alias_to_canonical_label(self):
        field = ExtractionField(
            name="f",
            description="d",
            type="list",
            examples=["inyección de ruido", "artefactos de movimiento"],
            aliases={"noise injection": "inyección de ruido",
                     "motion artifacts": "artefactos de movimiento"},
        )
        self.assertEqual(
            canonicalize_value(["noise injection", "other thing"], field),
            ["inyección de ruido", "other thing"],
        )

    def test_examples_act_as_canonical_labels(self):
        field = ExtractionField(
            name="f",
            description="d",
            type="list",
            examples=["EEG", "ECG"],
        )
        self.assertEqual(canonicalize_value(["eeg"], field), ["EEG"])

    def test_dedupes_after_mapping(self):
        field = ExtractionField(
            name="f",
            description="d",
            type="list",
            examples=["inyección de ruido"],
            aliases={"noise": "inyección de ruido"},
        )
        self.assertEqual(
            canonicalize_value(["noise", "inyección de ruido"], field),
            ["inyección de ruido"],
        )


if __name__ == "__main__":
    unittest.main()
