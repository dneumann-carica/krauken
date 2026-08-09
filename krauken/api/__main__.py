from __future__ import annotations

import uvicorn

from krauken.api.app import create_app

app = create_app()


def main() -> None:
    uvicorn.run("krauken.api.__main__:app", host="127.0.0.1", port=8080, reload=False)


if __name__ == "__main__":
    main()
