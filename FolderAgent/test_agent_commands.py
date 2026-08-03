import unittest

from agent_commands import CODEX_FILE_CREDENTIAL_CONFIG, build_agent_command


class BuildAgentCommandTests(unittest.TestCase):
    def test_codex_builds_noninteractive_command_without_model_by_default(self):
        command = build_agent_command(
            "codex",
            "Use the files to answer.",
            "What is this folder about?",
            {},
        )

        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("--ephemeral", command)
        self.assertIn(CODEX_FILE_CREDENTIAL_CONFIG, command)
        self.assertNotIn("--model", command)
        self.assertEqual(
            command[-1],
            "Use the files to answer.\n\nUser Question: What is this folder about?",
        )

    def test_codex_appends_configured_model(self):
        command = build_agent_command(
            "codex",
            "System prompt",
            "Question",
            {"CODEX_MODEL": "gpt-5.6-terra"},
        )

        model_index = command.index("--model")
        self.assertEqual(command[model_index + 1], "gpt-5.6-terra")
        self.assertEqual(command[-1], "System prompt\n\nUser Question: Question")

    def test_existing_qwen_command_remains_the_default(self):
        command = build_agent_command("unknown", "System", "Question", {})

        self.assertEqual(
            command,
            ["qwen", "-y", "--system-prompt", "System", "-p", "Question"],
        )

    def test_existing_gemini_command_keeps_optional_model(self):
        command = build_agent_command(
            "gemini",
            "System",
            "Question",
            {"GEMINI_MODEL": "gemini-model"},
        )

        self.assertEqual(
            command,
            [
                "gemini",
                "-p",
                "System\n\nUser Question: Question",
                "-y",
                "-m",
                "gemini-model",
            ],
        )

    def test_existing_claude_command_keeps_optional_model(self):
        command = build_agent_command(
            "claude",
            "System",
            "Question",
            {"CLAUDE_MODEL": "claude-model"},
        )

        self.assertEqual(
            command,
            [
                "claude",
                "--dangerously-skip-permissions",
                "--system-prompt",
                "System",
                "-p",
                "Question",
                "--model",
                "claude-model",
            ],
        )


if __name__ == "__main__":
    unittest.main()
