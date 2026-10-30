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
