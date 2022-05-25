import boto3
import botocore
import hashlib
from base64 import b64decode, b64encode

import typing
from dataclasses import dataclass
from django.core.files.uploadedfile import UploadedFile

from django.apps import apps
from django.conf import settings

config = apps.get_app_config("sic")


class Session:
    def __init__(self, *args, **kwargs):
        self.session = boto3.session.Session(
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION_NAME,
        )

    def s3(self) -> boto3.resources.base.ServiceResource:
        return self.session.resource("s3")


@dataclass
class BucketObject:
    """A media object uploaded to an S3 bucket."""

    base64digest: str
    hexdigest: str
    bucket_name: str = config.S3_BUCKET

    @staticmethod
    def from_sha256(digest: str) -> "BucketObject":
        base64digest = b64encode(bytes.fromhex(digest)).decode("utf-8")
        return BucketObject(base64digest=base64digest, hexdigest=digest)

    def url(self) -> str:
        resource = config.aws_session.s3()
        retval = resource.meta.client.generate_presigned_url(
            "get_object",
            ExpiresIn=3660,
            Params={"Bucket": self.bucket_name, "Key": self.hexdigest},
        )
        return retval


def upload_media(f: UploadedFile) -> BucketObject:
    if f.size > 1024 * 1024 * 1024 * 5:
        raise Exception(f"Uploaded file is too big: {f.size} bytes")
    s3 = config.aws_session.s3()
    b = f.read()
    sha256 = hashlib.sha256(b)
    digest = sha256.digest()
    hexdigest = sha256.hexdigest()

    object_ = s3.Object(config.S3_BUCKET, hexdigest)
    response = object_.put(
        Body=b, ChecksumSHA256=b64encode(digest).decode("utf-8"), Key=hexdigest
    )
    # Response is a dict:
    #   {
    #    'Expiration': 'string',
    #    'ETag': 'string',
    #    'ChecksumCRC32': 'string',
    #    'ChecksumCRC32C': 'string',
    #    'ChecksumSHA1': 'string',
    #    'ChecksumSHA256': 'string',
    #    'ServerSideEncryption': 'AES256'|'aws:kms',
    #    'VersionId': 'string',
    #    'SSECustomerAlgorithm': 'string',
    #    'SSECustomerKeyMD5': 'string',
    #    'SSEKMSKeyId': 'string',
    #    'SSEKMSEncryptionContext': 'string',
    #    'BucketKeyEnabled': True|False,
    #    'RequestCharged': 'requester'
    # }
    print(f"s3 put object replied with {response}")
    retval = BucketObject(hexdigest=hexdigest, base64digest=response["ChecksumSHA256"])
    return retval
