"""
Media Archive Lambda handler.
"""
import logging
import os
import tempfile
import zipfile
from urllib.parse import unquote_plus

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3 = boto3.client("s3")

ARCHIVE_PREFIX = os.environ.get("ARCHIVE_PREFIX", "archive/")

def handler(event, context):
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        try:
            archive_key = process_object(bucket, key)
            results.append({"source": key, "archive": archive_key, "status": "ok"})
        except Exception:
            logger.exception("Failed to archive s3://%s/%s", bucket, key)
            raise
    return {"processed": results}


def process_object(bucket: str, key: str) -> str:
    filename = os.path.basename(key)
    if not filename:
        raise ValueError(f"Refusing to process key with no filename: {key}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        local_source = os.path.join(tmp_dir, filename)
        local_zip = os.path.join(tmp_dir, f"{filename}.zip")

        logger.info("Downloading s3://%s/%s", bucket, key)
        s3.download_file(bucket, key, local_source)

        logger.info("Compressing %s -> %s", local_source, local_zip)
        with zipfile.ZipFile(local_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(local_source, arcname=filename)

        archive_key = f"{ARCHIVE_PREFIX}{filename}.zip"
        logger.info("Uploading archive to s3://%s/%s", bucket, archive_key)
        s3.upload_file(local_zip, bucket, archive_key)

        # Only delete the original once the archive is confirmed in S3.
        logger.info("Deleting original s3://%s/%s", bucket, key)
        s3.delete_object(Bucket=bucket, Key=key)

    return archive_key
