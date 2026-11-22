import json
import threading

from app.colors import hex_to_rgb01
from app.jsonio import write_json


def test_write_json_makes_folder_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "a" / "b.json"
    write_json(p, {"글": 1})
    assert json.loads(p.read_text(encoding="utf-8")) == {"글": 1}
    assert "글" in p.read_text(encoding="utf-8")  # ensure_ascii=False
    assert [x.name for x in p.parent.iterdir()] == ["b.json"]


def test_write_json_threads_do_not_mix(tmp_path):
    p = tmp_path / "x.json"
    ts = [threading.Thread(target=write_json, args=(p, {"n": i, "pad": "x" * 5000})) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert json.loads(p.read_text(encoding="utf-8"))["n"] in range(8)
    assert [x.name for x in tmp_path.iterdir()] == ["x.json"]


def test_hex_to_rgb01():
    assert hex_to_rgb01("#ff0080") == (1.0, 0.0, 128 / 255)
    assert hex_to_rgb01("000000") == (0.0, 0.0, 0.0)


def test_write_json_mkdir_false_keeps_missing_folder_an_error(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        write_json(tmp_path / "gone" / "x.json", {}, mkdir=False)
    assert not (tmp_path / "gone").exists()
