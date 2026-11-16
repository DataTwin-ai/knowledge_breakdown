"""Full document names, the link found while splitting, the link check (no database, no internet).
Run: python -m pytest -q ref_ss/tests
"""
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import kp_settings  # noqa: E402
from ref_ss import link  # noqa: E402
from ref_ss.detect import detect  # noqa: E402
from ref_ss.fetch import check_link  # noqa: E402

FULL = "The Central Goods and Services Tax (Amendment) Act, 2018 (No. 31 of 2018)"
NOTE = ("Inserted w.e.f 01st July, 2017 by The Central Goods and Services Tax (Amendment) Act, 2018 (No. 31 of 2018) "
        "– Brought into force on 01st February, 2019.")


# ---------------------------------------------------------------- 1. full name
def test_footnote_gives_the_full_name_ikp_read():
    refs = detect("CGSTA17", "text", [{"text": NOTE, "instrument": FULL}])
    assert [(r["ref_id"], r["relation"], r["mention"]) for r in refs] == [("ACT_31_2018", "AMENDED_BY", FULL)]


def test_full_name_from_the_text_of_an_amendment_mkp():
    refs = detect("CGSTA17", "Amendment of CGSTA17_S7_SS1_Cc states: Omitted ―and‖ w.e.f 01st July, 2017 by " + FULL
                  + " – Brought into force on 01st February, 2019.")
    assert refs[0]["ref_id"] == "ACT_31_2018" and refs[0]["mention"] == FULL


def test_notification_keeps_its_date_and_a_short_act_stays_whole():
    refs = {r["ref_id"]: r["mention"] for r in detect(
        "CGSTA17", "exempt under Notification No. 12/2017-Central Tax (Rate), dated 28.06.2017 and Ins. by Act 31 of 2018")}
    assert refs["N_CTR_12_2017"] == "Notification No. 12/2017-Central Tax (Rate) dated 28.06.2017"
    assert refs["ACT_31_2018"] == "Act 31 of 2018"


def test_old_string_footnotes_still_work():
    refs = detect("CGSTA17", "x", [NOTE])
    assert refs[0]["ref_id"] == "ACT_31_2018" and refs[0]["mention"] == FULL


# ---------------------------------------------------------------- 2. link while splitting
def _cfg(internet=True, on=True):
    cfg = copy.deepcopy(kp_settings.load())
    cfg["access"]["internet"] = internet
    cfg["refs"]["link_on_split"] = on
    return cfg


class FakeStore:
    def __init__(self, rows):
        self.rows = {r["ref_id"]: dict(r) for r in rows}
        self.saved = {}

    def link_state(self, ids):
        return [dict(self.rows[i]) for i in sorted(ids) if i in self.rows]

    def set(self, ref_id, **f):
        self.rows[ref_id].update(f)
        self.saved[ref_id] = f


ACT = {"ref_id": "ACT_31_2018", "kind": "AMENDING_ACT", "title": FULL, "number": 31, "year": 2018, "series": None,
       "link": "", "link_status": "", "status": "PENDING", "doc_code": ""}
URL = "https://egazette.gov.in/WriteReadData/2018/188869.pdf"


def _search(url, calls):
    def search(prompt, domains, uses):
        calls.append((prompt, tuple(domains), uses))
        return json.dumps({"url": url, "title": FULL}), [url]
    return search


def test_link_ok_is_saved_with_its_status():
    calls, st = [], FakeStore([ACT])
    n = link.on_split(_cfg(), st, ["ACT_31_2018"], _search(URL, calls), lambda u, t, h: (True, "opens, PDF"))
    assert n == 1 and st.saved["ACT_31_2018"]["link"] == URL
    assert (st.saved["ACT_31_2018"]["link_status"], st.saved["ACT_31_2018"]["status"]) == ("OK", "LINK_FOUND")
    prompt, domains, uses = calls[0]
    assert FULL in prompt and "egazette.gov.in" in domains and uses == kp_settings.load()["refs"]["link_search_max_uses"]


def test_bad_link_goes_to_the_scraper_with_the_reason():
    st = FakeStore([ACT])
    link.on_split(_cfg(), st, ["ACT_31_2018"], _search(URL, []), lambda u, t, h: (False, "HTTP 404"),
                  lambda *a: [])
    got = st.saved["ACT_31_2018"]
    assert (got["link"], got["link_status"], got["status"]) == ("", "BAD", "LINK_BAD") and "HTTP 404" in got["note"]


def test_nothing_found_stays_pending_for_the_scraper():
    st = FakeStore([ACT])
    link.on_split(_cfg(), st, ["ACT_31_2018"], _search("", []), lambda u, t, h: (True, ""), lambda *a: [])
    assert (st.saved["ACT_31_2018"]["link_status"], st.saved["ACT_31_2018"]["status"]) == ("NOT_FOUND", "PENDING")


def test_each_document_is_searched_once():
    calls, st = [], FakeStore([ACT])
    ok = lambda u, t, h: (True, "")
    link.on_split(_cfg(), st, ["ACT_31_2018"], _search(URL, calls), ok)
    link.on_split(_cfg(), st, ["ACT_31_2018"], _search(URL, calls), ok)       # second law citing it
    assert len(calls) == 1


def test_no_search_without_internet_or_switch_or_for_held_documents():
    calls = []
    for cfg in (_cfg(internet=False), _cfg(on=False)):
        assert link.on_split(cfg, FakeStore([ACT]), ["ACT_31_2018"], _search(URL, calls), lambda *a: (True, "")) == 0
    held = dict(ACT, ref_id="CGSTR17", kind="RULES", doc_code="CGSTR17", status="INSERTED")
    assert link.on_split(_cfg(), FakeStore([held]), ["CGSTR17"], _search(URL, calls), lambda *a: (True, "")) == 0
    assert calls == []


def test_a_search_that_fails_leaves_the_row_for_next_time():
    st = FakeStore([ACT])

    def boom(*a):
        raise RuntimeError("Claude is not available - web search skipped")
    assert link.on_split(_cfg(), st, ["ACT_31_2018"], boom, lambda *a: (True, "")) == 0
    assert st.saved == {} and link.needs_link(st.rows["ACT_31_2018"])


def test_a_page_is_followed_to_its_pdf_and_result_pdfs_are_tried():
    page = "https://www.indiacode.nic.in/handle/123/act31"
    pdf = "https://www.indiacode.nic.in/bitstream/123/1/A2018-31.pdf"
    good = lambda u, t, h: (u == pdf, "opens, PDF" if u == pdf else "not a PDF")

    def search(prompt, domains, uses):                   # the model gives only the page
        return json.dumps({"url": "", "page": page}), [page]
    seen = []
    st = FakeStore([ACT])
    link.on_split(_cfg(), st, ["ACT_31_2018"], search, good, lambda p, t, h, w: seen.append((p, tuple(w))) or [pdf])
    assert st.saved["ACT_31_2018"]["link"] == pdf and seen[0] == (page, ("31", "2018"))

    def search2(prompt, domains, uses):                  # no answer, but a PDF among the search results
        return "{}", ["https://egazette.gov.in/a/other.pdf", pdf]
    st = FakeStore([ACT])
    link.on_split(_cfg(), st, ["ACT_31_2018"], search2, good, lambda *a: [])
    assert st.saved["ACT_31_2018"]["link"] == pdf


def test_the_stronger_model_searches_only_when_the_first_finds_nothing():
    asked = []

    def first(prompt, domains, uses):
        asked.append(("haiku", uses))
        return json.dumps({"url": URL}), [URL]

    def second(prompt, domains, uses):
        asked.append(("sonnet", uses, "already tried" in prompt))
        return json.dumps({"url": URL.replace(".pdf", "b.pdf")}), []
    ok_second = lambda u, t, h: (u.endswith("b.pdf"), "opens, PDF" if u.endswith("b.pdf") else "HTTP 404")
    got = link.find(_cfg(), ACT, [("ref_link", first, 3), ("ref_link_retry", second, 6)], ok_second, lambda *a: [])
    assert got["status"] == "LINK_FOUND" and got["note"].startswith("ref_link_retry")
    assert asked == [("haiku", 3), ("sonnet", 6, True)]
    asked.clear()
    link.find(_cfg(), ACT, [("ref_link", first, 3), ("ref_link_retry", second, 6)], lambda u, t, h: (True, ""),
              lambda *a: [])
    assert asked == [("haiku", 3)]                       # found by the cheap model: no retry


def test_link_check_refuses_other_hosts_and_non_links_without_the_network():
    hosts = kp_settings.load()["refs"]["allowed_hosts"]
    assert check_link("https://example.com/a.pdf", 5, hosts)[0] is False
    assert check_link("not a link", 5, hosts)[0] is False


def test_claude_search_reads_the_answer_and_the_result_links():
    pytest.importorskip("anthropic")
    import llm_runtime as L

    class B:
        def __init__(self, **k):
            self.__dict__.update(k)

    class Client:
        class messages:
            @staticmethod
            def create(**k):
                Client.seen = k
                return B(content=[B(type="server_tool_use"),
                                  B(type="web_search_tool_result", content=[B(url=URL), B(url=URL)]),
                                  B(type="text", text='{"url": "' + URL + '"}')], usage=None)
    text, links = L.call_claude_search({"provider": "CLAUDE", "model": "m", "client": Client()}, "find",
                                       ["egazette.gov.in"], 2)
    assert links == [URL] and URL in text
    tool = Client.seen["tools"][0]
    assert tool["type"] == L.WEB_SEARCH_TOOL and tool["allowed_domains"] == ["egazette.gov.in"] and tool["max_uses"] == 2
