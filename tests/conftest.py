import sys
from pathlib import Path

# Ensure the repository root (parent directory of this file) is on the import path.
# This allows test modules to do `import app...` even when pytest is executed from
# a sub-directory or when the working directory is not the project root.
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR)) 