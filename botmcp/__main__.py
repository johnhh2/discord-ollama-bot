"""`python -m botmcp` — serve over Streamable HTTP; `--stdio` for a local client."""
import logging.config
import sys

from botmcp import server


def main(argv: list) -> int:
    logging.config.dictConfig(server.log_config())
    if "--stdio" in argv:
        # No token, no Host check: stdio's boundary is the process that
        # launched it. Needs the DB and BOT_STATE_URL reachable from here.
        server.build_mcp().run(transport="stdio")
        return 0
    try:
        settings = server.Settings.from_env()
    except server.ConfigError as e:
        print(f"botmcp: {e}", file=sys.stderr)
        return 2
    import uvicorn
    uvicorn.run(server.build_app(settings), host=settings.bind, port=settings.port,
                log_config=server.log_config(), proxy_headers=False, server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
