"""Тест подгрузки .env: переменные берутся из файла и из окружения,
причём окружение приоритетнее (load_dotenv без override)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from tests.util import TmpFiles

VAR = "DNS_SYNC_TEST_VAR"


class DotenvPriorityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TmpFiles("env")
        os.environ.pop(VAR, None)

    def tearDown(self):
        os.environ.pop(VAR, None)
        self.tmp.cleanup()

    def _write_envfile(self, value: str) -> str:
        path = self.tmp.path(".env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{VAR}={value}\n")
        return path

    def test_env_takes_priority_over_dotenv_file(self):
        envfile = self._write_envfile("from-file")
        os.environ[VAR] = "from-env"
        load_dotenv(envfile)
        self.assertEqual(os.environ[VAR], "from-env")

    def test_dotenv_file_used_when_env_not_set(self):
        envfile = self._write_envfile("from-file")
        load_dotenv(envfile)
        self.assertEqual(os.environ[VAR], "from-file")


if __name__ == "__main__":
    unittest.main()
