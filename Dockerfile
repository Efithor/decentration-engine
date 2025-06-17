# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy dependency declaration files
COPY requirements.in requirements.txt /app/

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

RUN uv pip install --system -r requirements.txt

# Copy the current directory contents into the container at /app
COPY . /app

# -------------------------------
# Dependency sanity & locking
# -------------------------------
# 1. Install deptry to analyse imports vs declared deps
RUN uv pip install --system deptry

# 2. Let deptry analyse the codebase and patch requirements.in:
#    - add missing / transitive packages (DEP001 / DEP003)
#    - drop unused packages (DEP002)
# Afterwards re-compile a fresh, fully-pinned requirements.txt and
# install those exact versions into the image.
RUN deptry . --json-output /tmp/deptry_report.json || true && \
    python - <<'PY' \
import json, re, pathlib, sys, os

req_in = pathlib.Path('requirements.in')
# If the project doesn't ship a requirements.in, abort quietly
if not req_in.exists():
    sys.exit(0)

try:
    with open('/tmp/deptry_report.json', 'r', encoding='utf-8') as fh:
        issues = json.load(fh)
except FileNotFoundError:
    issues = []

missing_or_transitive = {it['module'] for it in issues if it['error']['code'] in ('DEP001', 'DEP003')}
unused = {it['module'] for it in issues if it['error']['code'] == 'DEP002'}

def base_name(line: str) -> str:
    """Return the bare package name, stripping any version / markers."""
    return re.split(r'[<>=~!;]', line, 1)[0].strip().lower()

lines = req_in.read_text().splitlines()
new_lines = []
present_pkgs = set()

for ln in lines:
    stripped = ln.strip()
    if not stripped or stripped.startswith('#'):
        new_lines.append(ln)
        continue
    name = base_name(stripped)
    if name not in unused:
        new_lines.append(ln)
        present_pkgs.add(name)

# Append missing / transitive deps without version spec; uv will resolve pins
for pkg in sorted(missing_or_transitive):
    if pkg.lower() not in present_pkgs:
        new_lines.append(pkg)

req_in.write_text('\n'.join(new_lines) + '\n')
PY && \
    uv pip compile requirements.in -o requirements.txt && \
    uv pip install --system -r requirements.txt

# Execute with gunicorn specifying the app object
ENTRYPOINT ["python", "-m", "main_driver"]
