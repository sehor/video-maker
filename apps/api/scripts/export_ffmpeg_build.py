import json
import re
import subprocess


def main() -> None:
    # Manual inventory for GPU worker releases, never part of control-plane validation.
    def run(*args: str) -> str:
        return subprocess.run(
            ["ffmpeg", *args], check=True, capture_output=True, text=True, timeout=10,
        ).stdout

    version = run("-version")
    configuration = next(
        line.removeprefix("configuration:").split()
        for line in version.splitlines() if line.startswith("configuration:")
    )

    def codecs(option: str) -> list[str]:
        return sorted({match.group(1) for line in run("-hide_banner", option).splitlines()
                       if (match := re.match(r"^\s*[VAS.][A-Z.]{5}\s+(\S+)", line))
                       and (match.group(1) == "h264" or "(codec h264)" in line)})

    facts = {
        "version": version.splitlines()[0].removeprefix("ffmpeg version ").split(" Copyright")[0],
        "configuration": configuration,
        "license_status": "NONFREE" if "--enable-nonfree" in configuration else (
            "GPL" if "--enable-gpl" in configuration else "LGPL"
        ),
        "h264_decoders": codecs("-decoders"),
        "h264_encoders": codecs("-encoders"),
    }
    print(json.dumps(facts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
