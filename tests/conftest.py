import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from carryover import create_app  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    application = create_app(str(tmp_path / "test.sqlite3"), testing=True)
    application.config["SECRET_KEY"] = "test"
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def csv_bytes(*rows: str) -> bytes:
    header = "进样号,类型,峰面积,修订号\n"
    return (header + "\n".join(rows) + "\n").encode("utf-8")


def upload(client, path, filename, payload):
    return client.post(
        path,
        data={"csv_file": (io.BytesIO(payload), filename)},
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def create(client, name="批次 A", high="1000", blank="50", payload=None):
    return client.post(
        "/batches",
        data={
            "name": name,
            "high_threshold": high,
            "blank_limit": blank,
            "csv_file": (
                io.BytesIO(payload if payload is not None else csv_bytes(
                    "S1,sample,1200,1",
                    "S2,sample,100,1",
                    "B1,blank,10,1",
                    "S3,sample,100,1",
                )),
                "batch.csv",
            ),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
