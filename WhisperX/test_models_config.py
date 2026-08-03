import unittest

from models_config import BAKED_MODELS, baked_models, ensure_baked_model


class ModelsConfigTests(unittest.TestCase):
    def test_single_model_is_default(self):
        self.assertEqual(baked_models(None), ["large-v3"])
        self.assertEqual(baked_models("true"), ["large-v3"])

    def test_multi_model_build_keeps_curated_whitelist(self):
        self.assertEqual(baked_models("false"), ["small", "medium", "large-v3"])

    def test_baked_models_are_allowed(self):
        for model in BAKED_MODELS:
            ensure_baked_model(model)

    def test_unbaked_model_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not baked into this image"):
            ensure_baked_model("large-v2")


if __name__ == "__main__":
    unittest.main()
