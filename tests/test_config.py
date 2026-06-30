import unittest

from oa_agent_connector.config import build_config


class ConfigTest(unittest.TestCase):
    def test_build_config_uses_state_dir(self):
        config = build_config("https://example.com/oa/", state_dir="/tmp/oa-agent-state")
        env = config["mcpServers"]["oa"]["env"]
        self.assertEqual(env["OA_BASE_URL"], "https://example.com/oa/")
        self.assertEqual(env["OA_AGENT_STATE_DIR"], "/tmp/oa-agent-state")


if __name__ == "__main__":
    unittest.main()
