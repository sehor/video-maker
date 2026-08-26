import json

from app.media import inspect_ffmpeg_build


def main() -> None:
    print(json.dumps(inspect_ffmpeg_build().to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
