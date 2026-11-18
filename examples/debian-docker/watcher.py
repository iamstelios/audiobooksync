from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/audiobooks"))
CALIBRE_ROOT = Path(os.environ.get("CALIBRE_ROOT", "/calibre-books"))
DEBOUNCE_SECONDS = max(10.0, float(os.environ.get("DEBOUNCE_SECONDS", "90")))
SOURCE = os.environ.get("AUDIOSYNC_SOURCE", "/audiobooksync")
AUDIO_EXTENSIONS = {
    ".m4b", ".m4a", ".mp3", ".mp4", ".aac", ".ogg",
    ".oga", ".opus", ".flac", ".wav", ".wma", ".mka",
}
INPUT_EXTENSIONS = AUDIO_EXTENSIONS | {".epub"}
CHANGE_EVENTS = {"created", "modified", "moved", "closed", "deleted"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

changed = threading.Event()
stopping = threading.Event()
state_lock = threading.Lock()
last_change = 0.0


def mark_changed() -> None:
    global last_change
    with state_lock:
        last_change = time.monotonic()
    changed.set()


def normalized_name(value: str) -> str:
    value = re.sub(r"\s*\([^()]*\)\s*$", "", value)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", value.casefold())
    if len(words) > 1 and words[0] in {"the", "a", "an"}:
        words = words[1:]
    return " ".join(words)


def book_identity(path: Path, root: Path) -> tuple[str, set[str]]:
    folders = path.relative_to(root).parent.parts
    title = normalized_name(folders[-1]) if folders else ""
    author = set(normalized_name(folders[-2]).split()) if len(folders) > 1 else set()
    return title, author


def authors_match(left: set[str], right: set[str]) -> bool:
    shared = left & right
    return left == right or (len(shared) >= 2 and (shared == left or shared == right))


def discover_calibre_matches() -> list[tuple[Path, Path]]:
    if not CALIBRE_ROOT.is_dir():
        return []

    epubs = sorted(
        path
        for path in CALIBRE_ROOT.rglob("*")
        if path.is_file() and path.suffix.lower() == ".epub"
    )
    audio = sorted(
        path
        for path in MEDIA_ROOT.rglob("*")
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
    )
    audio_by_folder: dict[Path, list[Path]] = {}
    for path in audio:
        audio_by_folder.setdefault(path.parent, []).append(path)

    ebook_identities = {path: book_identity(path, CALIBRE_ROOT) for path in epubs}
    audio_identities = {path: book_identity(path, MEDIA_ROOT) for path in audio}
    ebook_matches = {
        epub: [
            audio_path
            for audio_path in audio
            if ebook_identities[epub][0]
            and ebook_identities[epub][0] == audio_identities[audio_path][0]
            and authors_match(ebook_identities[epub][1], audio_identities[audio_path][1])
        ]
        for epub in epubs
    }
    audio_matches = {
        audio_path: [
            epub
            for epub in epubs
            if audio_identities[audio_path][0]
            and audio_identities[audio_path][0] == ebook_identities[epub][0]
            and authors_match(audio_identities[audio_path][1], ebook_identities[epub][1])
        ]
        for audio_path in audio
    }

    matches = []
    for epub, candidates in ebook_matches.items():
        if len(candidates) != 1:
            continue
        audio_path = candidates[0]
        if audio_matches[audio_path] != [epub]:
            continue
        if len(audio_by_folder[audio_path.parent]) != 1:
            continue
        if any(path.suffix.lower() == ".epub" for path in audio_path.parent.iterdir() if path.is_file()):
            continue
        matches.append((epub, audio_path))
    return matches


def copy_calibre_matches() -> None:
    for epub, audio_path in discover_calibre_matches():
        destination = audio_path.with_suffix(".epub")
        if destination.exists():
            continue
        try:
            shutil.copy2(epub, destination)
        except OSError:
            logging.exception("Could not copy Calibre EPUB %s", epub)
            continue
        logging.info("Copied matched Calibre EPUB to %s", destination)


class MediaEventHandler(FileSystemEventHandler):
    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type not in CHANGE_EVENTS:
            return
        if event.is_directory:
            if event.event_type in {"created", "moved"}:
                mark_changed()
            return

        paths = [event.src_path]
        destination = getattr(event, "dest_path", None)
        if destination:
            paths.append(destination)
        if any(Path(path).suffix.lower() in INPUT_EXTENSIONS for path in paths):
            mark_changed()


def discover_pairs() -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    folders = [MEDIA_ROOT]
    folders.extend(path for path in sorted(MEDIA_ROOT.rglob("*")) if path.is_dir())

    for folder in folders:
        try:
            files = [path for path in folder.iterdir() if path.is_file()]
        except OSError as error:
            logging.warning("Cannot inspect %s: %s", folder, error)
            continue

        epubs = sorted(
            (path for path in files if path.suffix.lower() == ".epub"),
            key=lambda path: path.name.lower(),
        )
        audio = sorted(
            (path for path in files if path.suffix.lower() in AUDIO_EXTENSIONS),
            key=lambda path: path.name.lower(),
        )
        folder_pairs: list[tuple[Path, Path]] = []
        if len(epubs) == 1 and len(audio) == 1:
            folder_pairs = [(epubs[0], audio[0])]
        elif epubs and len(epubs) == len(audio):
            audio_by_stem: dict[str, list[Path]] = {}
            for audio_path in audio:
                audio_by_stem.setdefault(audio_path.stem.casefold(), []).append(audio_path)
            matched_audio = [audio_by_stem.get(epub.stem.casefold(), []) for epub in epubs]
            if (
                all(len(matches) == 1 for matches in matched_audio)
                and len({matches[0] for matches in matched_audio}) == len(audio)
            ):
                folder_pairs = [
                    (epub, matches[0])
                    for epub, matches in zip(epubs, matched_audio)
                ]

        if epubs and audio and not folder_pairs:
            logging.warning(
                "Skipping %s: use one audio file per EPUB; split-track books are unsupported",
                folder,
            )
            continue

        pairs.extend(
            (epub, audio_path)
            for epub, audio_path in folder_pairs
            if not epub.with_suffix(".smil").exists()
        )
    return pairs


def align_new_pairs() -> None:
    copy_calibre_matches()
    pairs = discover_pairs()
    if not pairs:
        logging.info("No unaligned EPUB/audio pairs found; waiting for files")
        return

    for epub, audio_path in pairs:
        sidecar = epub.with_suffix(".smil")
        if sidecar.exists():
            continue

        command = [
            "uvx", "--from", SOURCE, "abs-sync", "pair",
            str(epub), str(audio_path),
        ]
        logging.info("Aligning %s with %s", epub, audio_path)
        try:
            result = subprocess.run(command, check=False)
        except OSError:
            logging.exception("Could not start Audiobooksync for %s", epub)
            continue

        if result.returncode:
            logging.error("Audiobooksync failed for %s (exit %s)", epub, result.returncode)
        elif sidecar.exists():
            logging.info("Created %s", sidecar)
        else:
            logging.warning("Audiobooksync exited without creating %s", sidecar)


def request_stop(_signum: int, _frame: object) -> None:
    stopping.set()
    changed.set()


def main() -> None:
    if not MEDIA_ROOT.is_dir():
        raise SystemExit(f"Media directory does not exist: {MEDIA_ROOT}")
    if not CALIBRE_ROOT.is_dir():
        logging.warning("Calibre library directory does not exist: %s", CALIBRE_ROOT)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    observer = Observer()
    event_handler = MediaEventHandler()
    observer.schedule(event_handler, str(MEDIA_ROOT), recursive=True)
    if CALIBRE_ROOT.is_dir():
        observer.schedule(event_handler, str(CALIBRE_ROOT), recursive=True)
    observer.start()
    logging.info(
        "Watching %s and %s recursively; waiting %.0f seconds after file changes",
        MEDIA_ROOT,
        CALIBRE_ROOT,
        DEBOUNCE_SECONDS,
    )

    if discover_pairs() or discover_calibre_matches():
        mark_changed()

    try:
        while not stopping.is_set():
            changed.wait()
            while not stopping.is_set():
                changed.clear()
                with state_lock:
                    quiet_for = time.monotonic() - last_change
                remaining = DEBOUNCE_SECONDS - quiet_for
                if remaining > 0:
                    changed.wait(remaining)
                    continue
                with state_lock:
                    if time.monotonic() - last_change < DEBOUNCE_SECONDS:
                        continue
                break

            if not stopping.is_set():
                align_new_pairs()
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
