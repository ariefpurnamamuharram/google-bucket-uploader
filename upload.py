#!/usr/bin/env python3
"""Upload local file(s) or folder(s) to Google Cloud Storage."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import storage
from google.cloud.exceptions import GoogleCloudError
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parent


def parse_target(target: str) -> tuple[str, str]:
    """Split target into bucket name and object prefix."""
    target = target.strip().replace("\\", "/")
    if target.startswith("gs://"):
        target = target[5:]

    target = target.strip("/")
    if not target:
        raise ValueError("target must include a bucket name (e.g. gs://my-bucket/path)")

    if "/" in target:
        bucket, prefix = target.split("/", 1)
        return bucket, prefix.strip("/")

    return target, ""


def load_env() -> None:
    """Load variables from .env in the project root (falls back to cwd)."""
    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        load_dotenv(env_file)
    else:
        load_dotenv()


def validate_credentials() -> None:
    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not credentials_path:
        return

    path = Path(credentials_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f"GOOGLE_APPLICATION_CREDENTIALS points to a missing file: {path}"
        )

    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path.resolve())


def resolve_target(target: str) -> tuple[str, str]:
    """
    Resolve bucket and object prefix.

    When GCS_BUCKET is set in .env, --target is the object prefix only and uploads
    are restricted to that bucket.
    """
    allowed_bucket = os.getenv("GCS_BUCKET", "").strip()
    target = target.strip().replace("\\", "/")

    if allowed_bucket:
        if target.startswith("gs://"):
            bucket, prefix = parse_target(target)
            if bucket != allowed_bucket:
                raise ValueError(
                    f"uploads are restricted to bucket {allowed_bucket!r} (GCS_BUCKET); "
                    f"got {bucket!r} in --target"
                )
            return allowed_bucket, prefix

        return allowed_bucket, target.strip("/")

    return parse_target(target)


def join_blob_prefix(prefix: str, *parts: str) -> str:
    segments = [prefix] if prefix else []
    segments.extend(part.strip("/").replace("\\", "/") for part in parts if part)
    return "/".join(segments)


def collect_uploads(source: Path, target_prefix: str) -> list[tuple[Path, str]]:
    """Map a local file or directory to (local_path, blob_name) pairs."""
    source = source.resolve()

    if not source.exists():
        raise FileNotFoundError(f"source does not exist: {source}")

    if source.is_file():
        blob_name = join_blob_prefix(target_prefix, source.name)
        return [(source, blob_name)]

    if source.is_dir():
        uploads: list[tuple[Path, str]] = []
        base_prefix = join_blob_prefix(target_prefix, source.name)
        for path in sorted(source.rglob("*")):
            if path.is_file():
                relative = path.relative_to(source).as_posix()
                blob_name = join_blob_prefix(base_prefix, relative)
                uploads.append((path, blob_name))
        if not uploads:
            raise ValueError(f"source directory is empty: {source}")
        return uploads

    raise ValueError(f"source is not a file or directory: {source}")


def upload_file(
    client: storage.Client,
    bucket_name: str,
    local_path: Path,
    blob_name: str,
    *,
    show_progress: bool = True,
) -> None:
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    file_size = local_path.stat().st_size

    if not show_progress:
        blob.upload_from_filename(str(local_path))
        return

    with open(local_path, "rb") as raw_file:
        with tqdm.wrapattr(
            raw_file,
            "read",
            total=file_size,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=local_path.name,
            file=sys.stderr,
            leave=True,
        ) as wrapped_file:
            blob.upload_from_file(wrapped_file, size=file_size)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload file(s) or folder(s) to a Google Cloud Storage bucket.",
    )
    parser.add_argument(
        "--source",
        nargs="+",
        required=True,
        metavar="PATH",
        help="Local file or folder path(s) to upload. Folder structure is preserved.",
    )
    parser.add_argument(
        "--target",
        required=True,
        metavar="GCS_PATH",
        help=(
            "Destination path in the bucket. If GCS_BUCKET is set in .env, use a prefix only "
            "(e.g. backups/2024). Otherwise use gs://BUCKET/PREFIX or BUCKET/PREFIX."
        ),
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable the upload progress bar.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env()

    try:
        validate_credentials()
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        bucket_name, target_prefix = resolve_target(args.target)
    except ValueError as exc:
        parser.error(str(exc))

    uploads: list[tuple[Path, str]] = []
    for source_str in args.source:
        try:
            uploads.extend(collect_uploads(Path(source_str), target_prefix))
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    try:
        client = storage.Client()
    except GoogleCloudError as exc:
        print(f"error: failed to create GCS client: {exc}", file=sys.stderr)
        return 1

    show_progress = not args.no_progress and sys.stderr.isatty()

    for local_path, blob_name in uploads:
        try:
            upload_file(
                client,
                bucket_name,
                local_path,
                blob_name,
                show_progress=show_progress,
            )
            tqdm.write(f"uploaded {local_path} -> gs://{bucket_name}/{blob_name}")
        except GoogleCloudError as exc:
            print(f"error: failed to upload {local_path}: {exc}", file=sys.stderr)
            return 1

    tqdm.write(f"done: {len(uploads)} file(s) uploaded to gs://{bucket_name}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
