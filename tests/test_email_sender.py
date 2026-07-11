import os
import unittest
from unittest.mock import patch

from mining_qa.config import Settings
from mining_qa.email_sender import agentmail_proxy


class AgentMailProxyTests(unittest.TestCase):
    def test_dedicated_proxy_has_priority_over_process_proxy(self) -> None:
        settings = Settings(AGENTMAIL_PROXY_URL="socks5://127.0.0.1:19090")

        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890"}):
            self.assertEqual(agentmail_proxy(settings), "socks5://127.0.0.1:19090")

    def test_legacy_https_proxy_remains_a_fallback(self) -> None:
        settings = Settings(AGENTMAIL_PROXY_URL="")

        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890"}, clear=False):
            self.assertEqual(agentmail_proxy(settings), "http://127.0.0.1:7890")


if __name__ == "__main__":
    unittest.main()
