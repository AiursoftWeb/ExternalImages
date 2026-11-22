"""Install the locked CPU-only inference environment at build time."""
import subprocess

subprocess.run(["uv", "sync", "--project", "moire-remove", "--frozen", "--no-dev"], check=True)
subprocess.run(["uv", "pip", "check", "--python", "moire-remove/.venv/bin/python"], check=True)
subprocess.run(["moire-remove/.venv/bin/python", "-c", "import torch; assert torch.__version__ == '2.6.0+cpu'; assert torch.version.cuda is None"], check=True)
