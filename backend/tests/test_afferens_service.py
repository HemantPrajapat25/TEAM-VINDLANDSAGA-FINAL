import unittest

from services.afferens_service import AfferensService


class AfferensServiceFallbackTests(unittest.TestCase):
    def test_simulator_response_is_structured(self):
        service = AfferensService()
        response = service._simulator_response()

        self.assertEqual(response["status"], "simulator")
        self.assertIn("detections", response)
        self.assertIn("suspicious_activity", response)
        self.assertFalse(response["suspicious_activity"]["detected"])


if __name__ == "__main__":
    unittest.main()
