import json, os, sys, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aba_roots as A

def test_detect_columns_cn_en():
    assert A.detect_columns(["搜索频率排名", "搜索词", "x"]) == (1, 0)
    assert A.detect_columns(["Search Frequency Rank", "Search Term"]) == (1, 0)

def test_clean():
    assert A.clean_term("  Ceramic,  Mug!! ") == "ceramic mug"
    assert A.clean_term("kid's mug - blue") == "kid's mug blue"

def test_read_filter_dedup(tmp_path):
    p = tmp_path / "a.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["meta"]); w.writerow(["Search Term", "Search Frequency Rank"])
        w.writerows([["Mug", "5"], ["mug!", "3"], ["cup", "60000"], ["box", "1,200"]])
    items, st = A.read_aba(p, 50000)
    assert items == [("mug", 3), ("box", 1200)] and st["dup"] == 1 and st["rank_out"] == 1

def test_singular():
    assert [A.singular(w) for w in ["mugs", "boxes", "jars", "glass", "water bottles", "glasses"]] == ["mug", "box", "jar", "glass", "water bottle", "glasses"]

def test_parse_ok_and_filters():
    terms = ["ceramic storage boxes", "asdf"]
    out = A.parse_result(json.dumps({"items": [
        {"i": 0, "roots": [{"word": "box", "type": "general"}, {"word": "ceramic box", "type": "specific"}, {"word": "made up", "type": "specific"}], "modifiers": ["storage", "ceramic"]},
        {"i": 1, "roots": [], "modifiers": []}]}), terms)
    assert out["ceramic storage boxes"]["roots"] == [("box", "general", False)]  # 非连续/编造词被丢
    assert out["ceramic storage boxes"]["modifiers"] == ["storage", "ceramic"]

def test_parse_missing_raises():
    import pytest
    with pytest.raises(ValueError): A.parse_result('{"items":[{"i":0,"roots":[],"modifiers":[]}]}', ["a", "b"])

def test_split_on_failure(monkeypatch):
    calls = []
    def fake(terms, key, timeout=120):
        calls.append(len(terms))
        if len(terms) > 2: return "not json", {}
        return json.dumps({"items": [{"i": i, "roots": [{"word": t, "type": "specific"}], "modifiers": []} for i, t in enumerate(terms)]}), {"prompt_tokens": 1}
    monkeypatch.setattr(A, "call_ark", fake)
    res = A.extract_batch(["mug", "cup", "jar", "bowl"], "k", A.Usage())
    assert set(res) == {"mug", "cup", "jar", "bowl"} and calls[:2] == [4, 4]

def test_postprocess_rules():
    r, m = A.postprocess("body wash", [("body wash", "specific", False), ("wash", "specific", False)], ["body"])
    assert r == [("body wash", "bundle", False)] and m == []
    r, m = A.postprocess("amazon gift card", [("gift card", "specific", False), ("card", "specific", False)], ["amazon"])
    assert r == [("card", "specific", False)] and m == ["gift"]
    r, _ = A.postprocess("kindle", [("kindle", "specific", True)], [])
    assert r == []
    r, _ = A.postprocess("south park", [("south park", "specific", True)], [])
    assert r == [("south park", "specific", True)]
    r, m = A.postprocess("ceramic storage box", [("box", "specific", False), ("storage", "general", False)], ["ceramic"])
    assert r == [("box", "general", False)] and set(m) == {"ceramic", "storage"}

def test_v3_rules():
    r, _ = A.postprocess("protein powder", [("protein powder", "bundle", False)], [])
    assert r == [("protein powder", "bundle", False), ("powder", "specific", False)]
    r, _ = A.postprocess("fake blood", [("fake blood", "bundle", False)], [])
    assert r == [("fake blood", "bundle", False)]
    r, _ = A.postprocess("pokemon cards", [("pokemon card", "bundle", True), ("card", "specific", False)], [])
    assert r == [("pokemon", "specific", True), ("card", "specific", False)]
    r, _ = A.postprocess("crystal lake", [("crystal lake", "bundle", True)], [])
    assert r == [("crystal lake", "specific", True)]
    assert A.postprocess("squishy", [], [])[0] == [("squishy", "specific", False)]
    assert A.postprocess("kindle", [], [])[0] == [] and A.postprocess("xxx-videos", [], [])[0] == []
    r, m = A.postprocess("iphone 18 pro max case", [("case", "specific", False)], ["iphone", "pro max"])
    assert r == [("case", "specific", False)] and m == ["pro max"]

def test_v4_rules():
    assert A.singular("house of leaves", True) == "house of leaves"
    assert A.singular("knives") == "knives" and A.singular("cargo pants") == "cargo pants"
    assert A.singular("cookies") == "cookie" and A.singular("mugs") == "mug"
    r, _ = A.postprocess("morphe brushes", [("morphe brush", "bundle", False)], [])
    assert r[0] == ("brush", "specific", False)
    r, _ = A.postprocess("swiffer wet pads", [("swiffer wet pad", "bundle", False)], [])
    assert r[0] == ("wet pad", "bundle", False)
    assert A.postprocess("pol", [], [])[0] == [] and A.postprocess("squishy", [], [])[0] == [("squishy", "specific", False)]
    r, _ = A.postprocess("chucky doll", [("chucky", "specific", True), ("doll", "specific", False)], [])
    assert r == [("chucky", "specific", True), ("doll", "specific", False)]

def test_parse_zh():
    out = A.parse_zh('```json {"zh":{"backpack":"背包","x":"多余","mug":""}} ```', ["backpack", "mug"])
    assert out == {"backpack": "背包"}
