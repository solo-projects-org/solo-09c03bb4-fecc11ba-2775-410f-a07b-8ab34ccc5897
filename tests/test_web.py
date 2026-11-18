import re
import sqlite3
import threading
import time

import pytest

from carryover import create_app, db, store
from carryover import engine as engine_module
from carryover.ingest import CsvError, parse_csv
from tests.conftest import create, csv_bytes, upload


def page(client, batch_id=1):
    return client.get(f"/batches/{batch_id}").get_data(as_text=True)


def row_values(text, injection_no):
    """Pull the cells of one result row out of the rendered HTML."""
    match = re.search(
        r"<tr class=\"status-[^\"]*\">\s*<td>" + re.escape(injection_no) + r"</td>"
        r"(.*?)</tr>",
        text,
        re.S,
    )
    assert match, f"row {injection_no} not rendered"
    return re.findall(r"<td[^>]*>(.*?)</td>", match.group(0), re.S)


def status_of(text, injection_no):
    cells = row_values(text, injection_no)
    label = re.search(r'class="badge status">(.*?)</span>', cells[4], re.S)
    return label.group(1) if label else cells[4]


def trigger_of(text, injection_no):
    return row_values(text, injection_no)[5].strip()


def test_create_batch_shows_and_downloads_current_results(client):
    response = create(client)
    assert response.status_code == 302
    text = page(client)
    assert status_of(text, "S1") == "高值样品"
    assert status_of(text, "S2") == "残留风险"
    assert status_of(text, "S3") == "未标记"
    assert "现行结果" in text

    download = client.get("/batches/1/results.csv")
    assert download.status_code == 200
    body = download.get_data(as_text=True)
    assert "残留风险,S1" in body
    assert "S1,样品,1200,1,高值样品" in body
    assert download.headers["Content-Disposition"].startswith("attachment;")


def test_early_blank_correction_recomputes_subsequent_verdicts(client):
    create(client)
    # Raise the early blank B1 from 10 to 500: it no longer clears the risk.
    payload = csv_bytes("B1,blank,500,2")
    response = upload(client, "/batches/1/upload", "fix.csv", payload)
    assert response.status_code == 303

    text = page(client)
    assert status_of(text, "B1") == "空白超限"
    assert status_of(text, "S2") == "残留风险"
    assert status_of(text, "S3") == "残留风险"
    assert trigger_of(text, "S3") == "S1"

    download = client.get("/batches/1/results.csv").get_data(as_text=True)
    assert "S3,样品,100,1,残留风险,S1" in download


def test_same_or_lower_revision_is_skipped(client):
    create(client)
    upload(client, "/batches/1/upload", "same.csv", csv_bytes("B1,blank,500,1"))
    text = page(client)
    assert status_of(text, "B1") == "空白未超限"
    assert "已跳过 1 行" in text

    upload(client, "/batches/1/upload", "lower.csv", csv_bytes("B1,blank,900,1"))
    text = page(client)
    assert status_of(text, "B1") == "空白未超限"


def test_higher_revision_accepts_and_marks_new_number(client):
    create(client)
    payload = csv_bytes("S2,sample,2000,2", "S4,sample,50,1")
    upload(client, "/batches/1/upload", "fix.csv", payload)
    text = page(client)
    assert status_of(text, "S2") == "高值样品"
    # B1 now sits after the promoted S2, so it clears that episode; S4, which
    # was appended later in run order, is therefore not flagged.
    assert status_of(text, "B1") == "空白未超限"
    assert status_of(text, "S4") == "未标记"


def test_new_number_between_samples_uses_appended_run_position(client):
    create(client)
    upload(client, "/batches/1/upload", "fix.csv", csv_bytes("S2B,sample,10,1"))
    text = page(client)
    assert status_of(text, "S2B") == "未标记"


def test_conflicting_same_revision_in_one_file_is_rejected(client):
    create(client)
    payload = csv_bytes("B1,blank,500,2", "B1,blank,10,2")
    response = upload(client, "/batches/1/upload", "bad.csv", payload)
    assert response.status_code == 303
    text = page(client)
    assert "存在冲突数据" in text
    # No correction was accepted: old verdict stays current.
    assert status_of(text, "B1") == "空白未超限"


def test_csv_with_higher_revision_in_same_file_wins():
    rows = parse_csv(
        csv_bytes("B1,blank,500,2", "B1,blank,10,1", "S9,sample,1,1")
    )
    by_no = {row.injection_no: row for row in rows}
    assert by_no["B1"].peak_area == 500
    assert by_no["B1"].revision == 2


def test_csv_validation_rejects_missing_columns_and_bad_values(client):
    response = client.post(
        "/batches",
        data={
            "name": "x",
            "high_threshold": "1000",
            "blank_limit": "50",
            "csv_file": (__import__("io").BytesIO(b"a,b\n1,2\n"), "x.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert "缺少必需列" in response.get_data(as_text=True)

    with pytest.raises(CsvError) as exc:
        parse_csv(csv_bytes("S1,sample,,1"))
    assert "样品缺少峰面积" in str(exc.value)

    with pytest.raises(CsvError):
        parse_csv(csv_bytes("S1,sample,10,0"))
    with pytest.raises(CsvError):
        parse_csv(csv_bytes("S1,sample,nan,1"))
    with pytest.raises(CsvError):
        parse_csv(csv_bytes("S1,其他,10,1"))


def test_missing_blank_area_is_allowed_and_does_not_clear(client):
    create(client)
    upload(client, "/batches/1/upload", "fix.csv", csv_bytes("B1,blank,,2"))
    text = page(client)
    assert status_of(text, "B1") == "空白缺失"
    assert status_of(text, "S2") == "残留风险"


def test_failed_recompute_invalidates_old_verdict(client, monkeypatch):
    create(client)
    original = engine_module.compute_results

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟计算失败")

    monkeypatch.setattr(engine_module, "compute_results", boom)
    upload(client, "/batches/1/upload", "fix.csv", csv_bytes("B1,blank,500,2"))
    monkeypatch.setattr(engine_module, "compute_results", original)

    text = page(client)
    results_html = text.split("<h2>整批现行结果</h2>")[1].split("</section>")[0]
    assert "<tbody>" not in results_html
    assert "重算失败" in text
    assert "结果已作废" in text
    response = client.get("/batches/1/results.csv")
    assert response.status_code == 303
    # The correction itself was accepted and retained.
    assert "B1" in text and "500" in text


def test_recompute_recovers_after_failure(client, monkeypatch):
    create(client)

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟计算失败")

    monkeypatch.setattr(engine_module, "compute_results", boom)
    upload(client, "/batches/1/upload", "fix.csv", csv_bytes("B1,blank,500,2"))
    assert "结果已作废" in page(client)

    monkeypatch.undo()
    response = client.post("/batches/1/recompute")
    assert response.status_code == 303
    text = page(client)
    assert "现行结果" in text
    assert status_of(text, "B1") == "空白超限"
    assert status_of(text, "S2") == "残留风险"
    assert status_of(text, "S3") == "残留风险"


def test_restart_invalidates_stale_snapshot(tmp_path):
    path = tmp_path / "restart.sqlite3"
    app = create_app(str(path), testing=True)
    client = app.test_client()
    create(client)
    assert "现行结果" in page(client)

    # Simulate a correction that was accepted but whose recompute never ran.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("UPDATE batches SET data_version = data_version + 1 WHERE id = 1")
    conn.close()

    restarted = create_app(str(path), testing=True)
    text = restarted.test_client().get("/batches/1").get_data(as_text=True)
    assert "结果已作废" in text
    results_html = text.split("<h2>整批现行结果</h2>")[1].split("</section>")[0]
    assert "<tbody>" not in results_html


def test_page_hides_forged_current_rows(client, tmp_path):
    create(client)
    conn = sqlite3.connect(client.application.config["DATABASE"], isolation_level=None)
    conn.execute("UPDATE results SET revision = 99 WHERE injection_no = 'S2'")
    conn.close()
    text = page(client)
    assert "结果已作废" in text


def _assert_no_mixed_revisions(path):
    conn = sqlite3.connect(path)
    state, result_version, data_version = conn.execute(
        "SELECT result_state, result_version, data_version FROM batches WHERE id = 1"
    ).fetchone()
    injections = dict(
        conn.execute(
            "SELECT injection_no, revision FROM injections WHERE batch_id = 1"
        ).fetchall()
    )
    results = dict(
        conn.execute("SELECT injection_no, revision FROM results WHERE batch_id = 1").fetchall()
    )
    conn.close()
    if state == "current":
        assert result_version == data_version
        assert results == injections, "results are a mixture of revisions"
    else:
        assert results == {}, "stale/pending batch must not keep old rows"


def test_recompute_and_correction_cannot_mix_revisions(client, app, monkeypatch):
    create(client)
    original = engine_module.compute_results
    state = {"calls": 0}
    release = threading.Event()

    def slow_compute(injections, high, low):
        state["calls"] += 1
        if state["calls"] == 1:
            release.wait(timeout=10)
        return original(list(injections), high, low)

    monkeypatch.setattr(engine_module, "compute_results", slow_compute)

    errors = []

    def do_recompute():
        try:
            app.test_client().post("/batches/1/recompute")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def do_correction():
        try:
            app.test_client().post(
                "/batches/1/upload",
                data={"csv_file": (__import__("io").BytesIO(csv_bytes("B1,blank,500,2")), "fix.csv")},
                content_type="multipart/form-data",
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    recompute_thread = threading.Thread(target=do_recompute)
    recompute_thread.start()
    deadline = time.time() + 10
    while state["calls"] == 0 and time.time() < deadline:
        time.sleep(0.01)
    assert state["calls"] == 1

    # The recompute has read the old revision snapshot and holds the write
    # lock.  The correction must wait for it and then invalidate what it wrote.
    correction_thread = threading.Thread(target=do_correction)
    correction_thread.start()
    time.sleep(0.3)
    release.set()
    recompute_thread.join(timeout=10)
    correction_thread.join(timeout=10)
    assert not errors

    _assert_no_mixed_revisions(app.config["DATABASE"])
    text = page(client)
    if "现行结果" in text:
        assert status_of(text, "B1") == "空白超限"
        assert status_of(text, "S2") == "残留风险"
        assert status_of(text, "S3") == "残留风险"
    else:
        assert "结果已作废" in text or "待重算" in text


def test_concurrent_corrections_and_recomputes_stay_consistent(app):
    create(app.test_client())
    payload = "进样号,类型,峰面积,修订号\n" + "".join(
        f"S{i},sample,{1500 if i % 3 == 0 else 100},1\n" for i in range(1, 13)
    )
    response = app.test_client().post(
        "/batches",
        data={
            "name": "并发",
            "high_threshold": "1000",
            "blank_limit": "50",
            "csv_file": (__import__("io").BytesIO(payload.encode()), "x.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302

    errors = []

    def worker(seed):
        import random

        rnd = random.Random(seed)
        c = app.test_client()
        for _ in range(5):
            rev = rnd.randint(2, 30)
            rows = [
                f"B{rnd.randint(1, 5)},blank,{rnd.choice([10, 60])},{rev}"
                for _ in range(rnd.randint(1, 2))
            ]
            data = ("进样号,类型,峰面积,修订号\n" + "\n".join(rows) + "\n").encode()
            try:
                c.post(
                    "/batches/1/upload",
                    data={"csv_file": (__import__("io").BytesIO(data), "f.csv")},
                    content_type="multipart/form-data",
                )
                if rnd.random() < 0.5:
                    c.post("/batches/1/recompute")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(seed,)) for seed in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    _assert_no_mixed_revisions(app.config["DATABASE"])
    assert app.test_client().get("/batches/1").status_code == 200


def test_correcting_trigger_sample_downward_clears_successors(client):
    create(client)
    upload(client, "/batches/1/upload", "fix.csv", csv_bytes("S1,sample,100,2"))
    text = page(client)
    assert status_of(text, "S1") == "未标记"
    assert status_of(text, "S2") == "未标记"
    assert status_of(text, "B1") == "空白未超限"
    assert trigger_of(text, "S2") == ""


def test_correction_records_revision_and_can_change_type(client):
    create(client)
    upload(client, "/batches/1/upload", "fix.csv", csv_bytes("B1,blank,20,2", "S3,blank,700,2"))
    text = page(client)
    # S3 became an exceeded blank; it must not clear anything, and it is no
    # longer a sample needing a risk flag.
    assert status_of(text, "S3") == "空白超限"
    assert status_of(text, "S2") == "残留风险"
    assert status_of(text, "B1") == "空白未超限"
    # Every accepted revision is audited: S3 original + S3 correction, B1 x2.
    revisions = text.split("<h2>已受理修订</h2>")[1].split("</section>")[0]
    assert revisions.count("<td>S3</td>") == 2
    assert revisions.count("<td>B1</td>") == 2
    assert "<td>blank</td>" not in revisions


def test_empty_upload_and_missing_file_are_reported(client):
    create(client)
    response = client.post("/batches", data={"name": "x", "high_threshold": "1", "blank_limit": "1"})
    assert response.status_code == 400
    assert "请选择要上传的 CSV 文件" in response.get_data(as_text=True)

    response = upload(client, "/batches/1/upload", "empty.csv", b"")
    assert response.status_code == 303
    assert "CSV 文件为空" in page(client)


def test_thresholds_must_be_finite_and_non_negative(client):
    create(client)
    for high, blank in [("-1", "50"), ("nan", "50"), ("1000", "-5"), ("", "50")]:
        response = client.post(
            "/batches",
            data={
                "name": "x",
                "high_threshold": high,
                "blank_limit": blank,
                "csv_file": (__import__("io").BytesIO(csv_bytes("S1,sample,1,1")), "x.csv"),
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
