import argparse
import os
from pathlib import Path

import uvicorn

from .app import DEFAULT_DATA, create_app
from .vault import Vault


def main():
    parser = argparse.ArgumentParser(description="Standalone local Douyin Cookie service")
    parser.add_argument("command", choices=["serve", "token"], default="serve", nargs="?")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0", "::1"], default="127.0.0.1")
    args = parser.parse_args()
    if args.command == "token":
        print(Vault(Path(os.environ.get("DY_COOKIE_DATA_DIR", DEFAULT_DATA))).api_token)
        return
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            access_log=False,
            proxy_headers=False,
        )
    )
    app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
