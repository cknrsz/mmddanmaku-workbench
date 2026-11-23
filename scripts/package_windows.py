"""Package the built UI and Python source for local Windows installation."""

from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    frontend = root / "frontend" / "dist"
    if not (frontend / "index.html").is_file():
        raise SystemExit("Build the frontend before packaging.")

    files = [root / "README.md", root / "launch-windows.cmd", root / "首次安装.cmd", root / "backend" / "pyproject.toml"]
    files.extend((root / "backend" / "src" / "danmaku").rglob("*.py"))
    files.extend((root / "docs").glob("*.md"))
    files.extend(path for path in frontend.rglob("*") if path.is_file())

    release = root / "release"
    release.mkdir(exist_ok=True)
    archive = release / "danmaku-workbench-windows.zip"
    with NamedTemporaryFile(dir=release, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as package:
            for path in sorted(files):
                package.write(path, path.relative_to(root).as_posix())
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    print(archive)


if __name__ == "__main__":
    main()
