"""Regenerate the in-repo fixtures deterministically."""
from pathlib import Path

from .builder import FRAME_SUMMARY_CSV, build_baseline

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def main() -> None:
    FIXTURE_DIR.mkdir(exist_ok=True)
    media = build_baseline()
    (FIXTURE_DIR / "baseline.mkv").write_bytes(media)
    (FIXTURE_DIR / "frame_summary.csv").write_text(FRAME_SUMMARY_CSV, encoding="utf-8")
    print(f"wrote {FIXTURE_DIR}/baseline.mkv ({len(media)} bytes) and frame_summary.csv")


if __name__ == "__main__":
    main()
