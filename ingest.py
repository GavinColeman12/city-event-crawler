#!/usr/bin/env python3
"""CLI to ingest policy documents into the vector store."""

import argparse
import os
import sys
from pathlib import Path

from src.parsers import parse_file, SUPPORTED_EXTENSIONS
from src.chunker import chunk_documents
from src.vectorstore import add_chunks, get_ingested_sources, delete_by_source
from src.config import load_config


def ingest_file(filepath: str, client_id: str = None, force: bool = False) -> int:
    """Ingest a single file. Returns number of chunks added."""
    filepath = str(Path(filepath).resolve())

    if not force:
        existing = get_ingested_sources(client_id)
        if filepath in existing:
            print(f"  Skipping (already ingested): {filepath}")
            return 0

    print(f"  Parsing: {os.path.basename(filepath)}")
    documents = parse_file(filepath)
    if not documents:
        print(f"  Warning: No content extracted from {filepath}")
        return 0

    print(f"  Chunking: {len(documents)} document section(s)")
    chunks = chunk_documents(documents)

    if force:
        deleted = delete_by_source(filepath, client_id)
        if deleted:
            print(f"  Replaced {deleted} existing chunks")

    count = add_chunks(chunks, client_id)
    print(f"  Added {count} chunks")
    return count


def ingest_directory(dirpath: str, client_id: str = None, force: bool = False) -> int:
    """Ingest all supported files in a directory. Returns total chunks added."""
    total = 0
    dirpath = Path(dirpath)

    files = sorted([
        f for f in dirpath.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    ])

    if not files:
        print(f"No supported files found in {dirpath}")
        return 0

    print(f"Found {len(files)} files to ingest:")
    for f in files:
        total += ingest_file(str(f), client_id, force)

    return total


def main():
    parser = argparse.ArgumentParser(description="Ingest HR policy documents into the vector store")
    parser.add_argument("--path", required=True, help="File or directory to ingest")
    parser.add_argument("--force", action="store_true", help="Re-ingest even if already present")
    parser.add_argument("--config", default=None, help="Path to client config YAML")
    args = parser.parse_args()

    config = load_config(args.config)
    client_id = config.get("client", {}).get("id")

    target = Path(args.path)
    if not target.exists():
        print(f"Error: {args.path} does not exist")
        sys.exit(1)

    print(f"Ingesting for client: {config.get('client', {}).get('name', client_id)}")
    print(f"Client collection: client_{client_id}\n")

    if target.is_dir():
        total = ingest_directory(str(target), client_id, args.force)
    else:
        total = ingest_file(str(target), client_id, args.force)

    print(f"\nDone! Total chunks ingested: {total}")


if __name__ == "__main__":
    main()
