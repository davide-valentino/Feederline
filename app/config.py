import os
import json
from dataclasses import dataclass

import boto3
from dotenv import load_dotenv


load_dotenv()


DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://feederline:development@127.0.0.1:55432/feederline"
)


def database_url_from_environment() -> str:
    direct_url = os.environ.get("DB_URI") or os.environ.get("DATABASE_URL")
    if direct_url:
        return direct_url

    secret_arn = os.environ.get("DB_SECRET_ARN")
    if not secret_arn:
        return DEFAULT_DATABASE_URL

    secret_value = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)[
        "SecretString"
    ]
    try:
        secret = json.loads(secret_value)
    except json.JSONDecodeError:
        return secret_value
    if isinstance(secret, dict):
        database_url = secret.get("DB_URI") or secret.get("database_url")
        if database_url:
            return database_url
    raise ValueError("Database secret must be a URL string or contain DB_URI")


@dataclass(frozen=True)
class Settings:
    database_url: str = database_url_from_environment()
    event_publisher: str = os.environ.get("EVENT_PUBLISHER", "log")
    event_bus_name: str = os.environ.get("EVENT_BUS_NAME", "feederline-dev-events")
    workflow_state_machine_arn: str = os.environ.get("WORKFLOW_STATE_MACHINE_ARN", "")
    aws_region: str = os.environ.get("AWS_REGION", "eu-west-1")
    outbox_poll_seconds: float = float(os.environ.get("OUTBOX_POLL_SECONDS", "2"))


settings = Settings()
