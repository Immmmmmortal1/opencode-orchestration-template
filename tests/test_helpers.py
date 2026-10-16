from __future__ import annotations

import json
import os
import unittest

from orchagent.opencode import primary_config_path, sync_agents
from tests.helpers import IsolatedEnv


class IsolatedEnvContractTests(unittest.TestCase):
    """IsolatedEnv 的隔离契约：不仅隔离子进程，也必须隔离**测试进程自身**。

    历史缺陷：IsolatedEnv 只把 OPENCODE_CONFIG 注入子进程环境，未改测试进程的
    os.environ。于是进程内调用（apply_draft(syncOpencode=True) →
    sync_agents_from_registry → primary_config_path）会解析到**用户真实**的
    opencode 配置并覆盖它——每跑一次测试套件就污染一次真实配置。
    """

    def test_process_env_opencode_config_points_inside_isolation(self) -> None:
        env = IsolatedEnv().setUp()
        try:
            self.assertEqual(str(env.opencode_config), os.environ.get("OPENCODE_CONFIG"))
            # 关键断言：进程内解析的宿主配置目标必须落在隔离目录内
            self.assertEqual(str(env.opencode_config), str(primary_config_path()))
        finally:
            env.tearDown()

    def test_teardown_restores_previous_opencode_config(self) -> None:
        original = os.environ.get("OPENCODE_CONFIG")

        env = IsolatedEnv().setUp()
        try:
            self.assertNotEqual(original, os.environ.get("OPENCODE_CONFIG"))
        finally:
            env.tearDown()

        self.assertEqual(original, os.environ.get("OPENCODE_CONFIG"))

    def test_inprocess_sync_targets_isolated_config(self) -> None:
        env = IsolatedEnv().setUp()
        try:
            env.install_home()

            result = sync_agents(env.home)

            self.assertEqual("ok", result["status"])
            # 写入目标必须是隔离配置，绝不能是真实用户配置
            self.assertEqual(str(env.opencode_config), str(result["target"]))
            written = json.loads(env.opencode_config.read_text(encoding="utf-8"))
            self.assertIn("orchagent-role-investigator", written["agent"])
        finally:
            env.tearDown()


if __name__ == "__main__":
    unittest.main()
