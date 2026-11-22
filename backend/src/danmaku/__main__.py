from argparse import ArgumentParser
from pathlib import Path

import uvicorn

from danmaku.main import create_app


def main() -> None:
    parser = ArgumentParser(description="Run the local danmaku workbench")
    parser.add_argument("--ui-dir", type=Path, default=Path("frontend/dist"))
    parser.add_argument("--data-dir", type=Path)
    options = parser.parse_args()
    if not (options.ui_dir / "index.html").is_file():
        parser.error(f"Built UI missing from {options.ui_dir}. Run npm run build in frontend.")
    uvicorn.run(
        create_app(data_dir=options.data_dir, static_dir=options.ui_dir.resolve()),
        host="127.0.0.1", port=8000, workers=1, proxy_headers=False, access_log=False,
    )


if __name__ == "__main__":
    main()
