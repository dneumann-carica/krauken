from __future__ import annotations

import uvicorn

from krauken.api.app import create_app
from krauken.config import Config

app = create_app()


def main() -> None:
    config = Config.from_env()
    uvicorn.run("krauken.api.__main__:app", host=config.api_host, port=config.api_port, reload=False)


if __name__ == "__main__":
    main()
