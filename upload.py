#!/usr/bin/env python3
"""Upload local file(s) or folder(s) to Google Cloud Storage, or download with --reverse."""

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


def resolve_gcs_path(path: str, *, arg_name: str = "--target") -> tuple[str, str]:
    """
    Resolve bucket and object prefix.

    When GCS_BUCKET is set in .env, the path is the object prefix only and transfers
    are restricted to that bucket.
    """
    allowed_bucket = os.getenv("GCS_BUCKET", "").strip()
    path = path.strip().replace("\\", "/")

    if allowed_bucket:
        if path.startswith("gs://"):
            bucket, prefix = parse_target(path)
            if bucket != allowed_bucket:
                raise ValueError(
                    f"transfers are restricted to bucket {allowed_bucket!r} (GCS_BUCKET); "
                    f"got {bucket!r} in {arg_name}"
                )
            return allowed_bucket, prefix

        return allowed_bucket, path.strip("/")

    return parse_target(path)


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


def collect_downloads(
    client: storage.Client,
    bucket_name: str,
    source_prefix: str,
    local_target: Path,
) -> list[tuple[str, str, Path]]:
    """Map a GCS object or prefix to (bucket_name, blob_name, local_path) triples."""
    bucket = client.bucket(bucket_name)
    source_prefix = source_prefix.strip("/")

    if source_prefix:
        exact = bucket.blob(source_prefix)
        if exact.exists():
            return [(bucket_name, source_prefix, local_target / Path(source_prefix).name)]

    list_prefix = f"{source_prefix}/" if source_prefix else ""
    blobs = sorted(
        (blob for blob in client.list_blobs(bucket_name, prefix=list_prefix) if not blob.name.endswith("/")),
        key=lambda blob: blob.name,
    )
    if not blobs:
        location = f"gs://{bucket_name}/{source_prefix}" if source_prefix else f"gs://{bucket_name}/"
        raise FileNotFoundError(f"no objects found at {location}")

    base_name = Path(source_prefix).name if source_prefix else ""
    downloads: list[tuple[str, str, Path]] = []
    for blob in blobs:
        relative = blob.name[len(list_prefix) :] if list_prefix else blob.name
        if not relative:
            continue
        local_path = local_target / base_name / relative if base_name else local_target / relative
        downloads.append((bucket_name, blob.name, local_path))

    if not downloads:
        location = f"gs://{bucket_name}/{source_prefix}" if source_prefix else f"gs://{bucket_name}/"
        raise FileNotFoundError(f"no objects found at {location}")

    return downloads


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


def download_file(
    client: storage.Client,
    bucket_name: str,
    blob_name: str,
    local_path: Path,
    *,
    show_progress: bool = True,
) -> None:
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.reload()
    local_path.parent.mkdir(parents=True, exist_ok=True)

    if not show_progress:
        blob.download_to_filename(str(local_path))
        return

    file_size = blob.size or 0
    with open(local_path, "wb") as raw_file:
        with tqdm.wrapattr(
            raw_file,
            "write",
            total=file_size,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=local_path.name,
            file=sys.stderr,
            leave=True,
        ) as wrapped_file:
            blob.download_to_file(wrapped_file)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Upload file(s) or folder(s) to a Google Cloud Storage bucket, "
            "or download from GCS with --reverse."
        ),
    )
    parser.add_argument(
        "--source",
        nargs="+",
        required=True,
        metavar="PATH",
        help=(
            "Local file or folder path(s) to upload. With --reverse, GCS object or "
            "prefix path(s) to download. Folder structure is preserved."
        ),
    )
    parser.add_argument(
        "--target",
        required=True,
        metavar="PATH",
        help=(
            "Destination path in the bucket. If GCS_BUCKET is set in .env, use a prefix only "
            "(e.g. backups/2024). Otherwise use gs://BUCKET/PREFIX or BUCKET/PREFIX. "
            "With --reverse, a local directory to download into."
        ),
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Download from GCS to local: --source is GCS path(s), --target is a local directory.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable the transfer progress bar.",
    )
    return parser


def run_upload(args: argparse.Namespace, client: storage.Client, show_progress: bool) -> int:
    try:
        bucket_name, target_prefix = resolve_gcs_path(args.target, arg_name="--target")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    uploads: list[tuple[Path, str]] = []
    for source_str in args.source:
        try:
            uploads.extend(collect_uploads(Path(source_str), target_prefix))
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

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


def run_download(args: argparse.Namespace, client: storage.Client, show_progress: bool) -> int:
    local_target = Path(args.target).expanduser().resolve()
    if local_target.exists() and not local_target.is_dir():
        print(f"error: --target must be a directory: {local_target}", file=sys.stderr)
        return 1
    local_target.mkdir(parents=True, exist_ok=True)

    downloads: list[tuple[str, str, Path]] = []
    for source_str in args.source:
        try:
            bucket_name, source_prefix = resolve_gcs_path(source_str, arg_name="--source")
            downloads.extend(
                collect_downloads(client, bucket_name, source_prefix, local_target)
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    for bucket_name, blob_name, local_path in downloads:
        try:
            download_file(
                client,
                bucket_name,
                blob_name,
                local_path,
                show_progress=show_progress,
            )
            tqdm.write(f"downloaded gs://{bucket_name}/{blob_name} -> {local_path}")
        except GoogleCloudError as exc:
            print(f"error: failed to download gs://{bucket_name}/{blob_name}: {exc}", file=sys.stderr)
            return 1

    tqdm.write(f"done: {len(downloads)} file(s) downloaded to {local_target}/")
    return 0


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
        client = storage.Client()
    except GoogleCloudError as exc:
        print(f"error: failed to create GCS client: {exc}", file=sys.stderr)
        return 1

    show_progress = not args.no_progress and sys.stderr.isatty()

    if args.reverse:
        return run_download(args, client, show_progress)
    return run_upload(args, client, show_progress)


if __name__ == "__main__":
    raise SystemExit(main())
