import os
from collections.abc import Mapping


CODEX_FILE_CREDENTIAL_CONFIG = 'cli_auth_credentials_store="file"'


def build_agent_command(
    provider: str,
    system_prompt: str,
    question: str,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Build the CLI command for a single agent request."""
    env = os.environ if environ is None else environ

    if provider == "gemini":
        # Gemini has no dedicated system-prompt flag, so send one combined prompt.
        full_prompt = f"{system_prompt}\n\nUser Question: {question}"
        cmd = ["gemini", "-p", full_prompt, "-y"]
        model = env.get("GEMINI_MODEL")
        if model:
            cmd.extend(["-m", model])
        return cmd

    if provider == "claude":
        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "--system-prompt",
            system_prompt,
            "-p",
            question,
        ]
        model = env.get("CLAUDE_MODEL")
        if model:
            cmd.extend(["--model", model])
        return cmd

    if provider == "codex":
        # Codex has no dedicated system-prompt flag, so send one combined prompt.
        full_prompt = f"{system_prompt}\n\nUser Question: {question}"
        cmd = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "-c",
            CODEX_FILE_CREDENTIAL_CONFIG,
        ]
        model = env.get("CODEX_MODEL")
        if model:
            cmd.extend(["--model", model])
        cmd.append(full_prompt)
        return cmd

    # Default to Qwen for backward compatibility.
    return [
        "qwen",
        "-y",
        "--system-prompt",
        system_prompt,
        "-p",
        question,
    ]
