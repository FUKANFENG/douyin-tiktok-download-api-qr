"""Generate a fresh Docker configuration without overwriting an existing installation."""

import argparse
import os
import secrets
from pathlib import Path


def initialize(path: Path, *, port: int = 8000, project: str = "dtk-qr") -> bool:
    if not 1024 <= port <= 65535:
        raise ValueError("port must be between 1024 and 65535")
    import re

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise ValueError("project must contain lowercase letters, digits, hyphens or underscores")
    content = {
        "DTK_QR_PROJECT": project,
        "DTK_BIND_HOST": "127.0.0.1",
        "DTK_BIND_PORT": str(port),
        "DTK_SECRET_KEY": secrets.token_hex(32),
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "REDIS_PASSWORD": secrets.token_hex(32),
        "DTK_QR_LOGIN_TOKEN": secrets.token_hex(32),
        "DTK_DOWNLOADER_TOKEN": secrets.token_hex(32),
    }
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
        file.write("# Generated locally. Keep this file private and backed up.\n")
        file.write("\n".join(f"{key}={value}" for key, value in content.items()) + "\n")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".env.qr"))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--project", default="dtk-qr")
    args = parser.parse_args()
    created = initialize(args.output, port=args.port, project=args.project)
    print(
        f"Created {args.output}; random secrets are stored only in this file."
        if created
        else f"Kept existing {args.output}; no settings or secrets were changed."
    )


if __name__ == "__main__":
    main()
