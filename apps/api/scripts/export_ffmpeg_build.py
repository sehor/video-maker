import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.media import inspect_ffmpeg_build


def main() -> None:
    print(json.dumps(inspect_ffmpeg_build().to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
