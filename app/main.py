from __future__ import annotations

import argparse
import shutil

from .config import DEFAULT_CONFIG_PATH, load_config, save_config
from .remote_server import main as remote_server_main
from .server import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="meeting-workbench")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-config")
    server = sub.add_parser("server")
    server.add_argument("--host")
    server.add_argument("--port", type=int)
    remote = sub.add_parser("remote-asr")
    remote.add_argument("--host", default="127.0.0.1")
    remote.add_argument("--port", type=int, default=8978)
    overlay = sub.add_parser("overlay")
    overlay.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    args = parser.parse_args(argv)

    if args.cmd == "init-config":
        if not DEFAULT_CONFIG_PATH.exists():
            example = DEFAULT_CONFIG_PATH.with_name("config.example.json")
            shutil.copy2(example, DEFAULT_CONFIG_PATH)
        cfg = load_config(DEFAULT_CONFIG_PATH)
        save_config(cfg, DEFAULT_CONFIG_PATH)
        print(f"Config ready: {DEFAULT_CONFIG_PATH}")
        print(f"Auth token: {cfg.server.auth_token}")
        return

    if args.cmd == "server":
        cfg = load_config(DEFAULT_CONFIG_PATH)
        if args.host:
            cfg.server.host = args.host
            cfg.security.allow_remote = args.host not in {"127.0.0.1", "localhost"}
        if args.port:
            cfg.server.port = args.port
        import uvicorn

        print(f"Meeting Workbench: http://{cfg.server.host}:{cfg.server.port}")
        if cfg.security.require_token:
            print(f"Auth token: {cfg.server.auth_token}")
        uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port, log_level="info")
        return

    if args.cmd == "remote-asr":
        remote_server_main(["--host", args.host, "--port", str(args.port)])
        return

    if args.cmd == "overlay":
        from .overlay import main as overlay_main

        overlay_main(["--url", args.url])


if __name__ == "__main__":
    main()

