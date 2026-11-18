# Audiobooksync on Debian with an Intel GPU

This guide deploys the OpenVINO-enabled Audiobooksync fork in Docker, generates `.smil` sidecars, and optionally watches a Calibre library for matching EPUBs. It was tested on Debian with an Intel Alder Lake-P integrated GPU. Other Intel GPUs may work if supported by OpenVINO.

Audiobooksync is an experimental project. It currently supports English EPUBs paired with a single audiobook file; split-track audiobooks are not supported by the aligner.

## Requirements

- Debian Linux, Docker Engine, and the Docker Compose plugin.
- An Intel GPU supported by OpenVINO, with `/dev/dri/renderD128` available on the host.
- A render-device group ID for the container:

  ```sh
  stat -c '%g' /dev/dri/renderD128
  ```

- An Audiobookshelf library folder and, optionally, a Calibre library folder.
- A compatible modified Audiobookshelf reader. The Android client is an experimental debug APK and must be sideloaded.

The example container installs Debian's `intel-opencl-icd` package and receives `/dev/dri` plus the host render group. VAAPI alone is not the OpenCL compute runtime required by OpenVINO.

## Clone the fork

Use the branch containing the OpenVINO changes:

```sh
git clone --branch feature/openvino-intel-gpu \
  https://github.com/iamstelios/audiobooksync.git
cd audiobooksync
```

The aligner prefers the OpenVINO Intel GPU provider, then falls back to CUDA and CPU. The dependency lockfile pins OpenVINO Plugin EP 1.8.1 and a compatible ONNX Runtime.

## Configure the Docker watcher

The example Compose service runs the folder watcher as a non-root user. It mounts the audiobook library read/write, Calibre read-only, the repository read-only, and a persistent cache for dependencies, the model, and OpenVINO.

```sh
cd examples/debian-docker
mkdir -p cache
cp .env.example .env
```

Edit `.env` and set `AUDIOBOOKS_DIR` and `CALIBRE_LIBRARY_DIR` to the host paths. Set `PUID` and `PGID` to the user/group that own the media folders; set `RENDER_GID` to the number printed by `stat` above. For example:

```dotenv
AUDIOBOOKS_DIR=/media/audiobooks
CALIBRE_LIBRARY_DIR=/media/books
PUID=1000
PGID=1000
RENDER_GID=105
DEBOUNCE_SECONDS=90
```

Ensure that this UID/GID can read the audiobook files, write to the audiobook folders, and read the Calibre library. The Calibre mount is read-only so the watcher will not change Calibre's files.

Build and start the watcher:

```sh
docker compose --env-file .env -f compose.yaml up -d --build
docker compose --env-file .env -f compose.yaml logs -f
```

Look for `Watching /audiobooks and /calibre-books` in the logs. The watcher waits for file activity to settle, tries to copy a uniquely matched EPUB from Calibre into its audiobook folder, then aligns new pairs. The `.smil` file is written beside the EPUB.

## Arrange the books

For automatic Calibre matching, the watcher uses author and book folder names. A typical layout is:

```text
/media/books/
  Frank Herbert/
    Dune (123)/
      Dune - Frank Herbert.epub

/media/audiobooks/
  Frank Herbert/
    Dune (Narrator)/
      Dune.m4b
```

It strips a trailing parenthetical such as Calibre's book ID or an audiobook narrator suffix, normalizes punctuation/case, and requires a unique reciprocal title/author match. Existing EPUBs are never overwritten. If titles differ substantially (for example, an audiobook folder has a subtitle not present in the other library), copy the correct EPUB into that audiobook's folder yourself.

Automatic pairing only works when the EPUB and audio are together in a book folder and there is a single audio file for that book. Ambiguous matches and multi-file audiobooks are skipped and logged.

## Generate a sidecar manually

The watcher normally does this automatically. To explicitly pair one EPUB and one audio file:

```sh
docker compose --env-file .env -f compose.yaml exec audiobooksync-watcher \
  uvx --from /audiobooksync abs-sync pair \
  "/audiobooks/Frank Herbert/Dune (Narrator)/Dune.epub" \
  "/audiobooks/Frank Herbert/Dune (Narrator)/Dune.m4b"
```

To scan all eligible pairs recursively instead:

```sh
docker compose --env-file .env -f compose.yaml run --rm audiobooksync-watcher \
  uvx --from /audiobooksync abs-sync folder /audiobooks
```

The first alignment downloads the approximately 614 MB quantized Parakeet model from Hugging Face. The model and Python packages are cached in `examples/debian-docker/cache`. Allow time for alignment; on the tested machine, `The Daily Laws` (about 13 hours of audio) took roughly 53 minutes on the Intel GPU. Actual time varies by GPU and book.

## Verify GPU selection and read the book

Check the watcher logs for:

```text
model: encoding on OpenVINOExecutionProvider (GPU)
```

If it reports CPU, verify that `/dev/dri/renderD128` exists, the container has the correct `RENDER_GID`, and the OpenCL runtime is installed in the image. The aligner falls back if OpenVINO cannot discover or initialize the Intel GPU.

After generation, scan the Audiobookshelf library if the `.smil` file is not listed under the audiobook's **Library Files**. Install the modified Android client from the [Audiobooksync v0.3.0 release](https://github.com/Nodja/audiobooksync/releases/tag/v0.3.0), connect it to the same Audiobookshelf server, and open the book online once so the EPUB and sidecar can be cached for offline use.

## Troubleshooting

- **Watcher cannot copy an EPUB or write a sidecar:** check ownership and write permissions on that specific audiobook folder. The Calibre library is intentionally read-only.
- **No EPUB match:** verify author/book folder names; if the title or subtitle differs, manually copy the correct EPUB into the audiobook folder.
- **Split-track book skipped:** merge tracks to one supported file (such as M4B) before aligning.
- **CPU provider selected:** inspect the rendered device and `RENDER_GID`; check the watcher logs for the OpenVINO discovery or initialization reason.
- **Resume an interrupted run:** run the same `pair` or `folder` command again. Existing `.smil` files are skipped unless the CLI's overwrite/update options are used.
