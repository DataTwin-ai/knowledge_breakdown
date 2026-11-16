"""Abbreviated ids for every knowledge point (notes section 3). Python makes every id; the LLM never does.

    MKP      M_<acronym of the concept name>                       M_SIAFOS
    element  <MKP id>_<TYPE>_<acronym of its own text or name>    M_SIAFOS_A_SGSBS   (TYPE: A G LE PD OR CT TC LK)
    fact     F_<acronym of the fact name>                          F_SCWCC  (no prefix: one fact, one id in every law)

Acronym = the first letter of EVERY word, in order (small words count too: of, the, a, for).
A numeral is kept whole ("Section 16" -> 16, "16A" -> 16A); a Roman numeral written in capitals is kept whole
("Schedule II" -> II); a hyphenated word is one word. At most 10 characters (a longer text needs a short name).
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, Optional, Tuple

MAX_LEN = 10
ELEMENT_TYPES = ("A", "G", "LE", "PD", "OR", "CT", "TC", "LK")

_WORD = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")
_ROMAN = re.compile(r"(?=[MDCLXVI]{2,})M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")
_MKP_RE = re.compile(r"^M_[A-Z0-9]+$")
_FACT_RE = re.compile(r"^F_[A-Z0-9]+$")
_ELEMENT_RE = re.compile(r"^(M_[A-Z0-9]+)_(A|G|LE|PD|OR|CT|TC|LK)_([A-Z0-9]+)$")


class IdError(ValueError):
    """An id cannot be made or does not follow the rule (the Python gate fails)."""


def words(text: str) -> list:
    return _WORD.findall(str(text or ""))


def _piece(token: str) -> str:
    plain = re.sub(r"['’-]", "", token)
    if plain[0].isdigit():
        return plain.upper()                       # 16, 16A, 2019 - kept whole
    if plain.isupper() and _ROMAN.match(plain):
        return plain                               # II, III, IV - only when written in capitals
    return plain[0].upper()


def acronym(text: str) -> str:
    """Acronym of a text (no length check)."""
    toks = words(text)
    if not toks:
        raise IdError(f"no words to make an id from: {text!r}")
    return "".join(_piece(t) for t in toks)


def _checked(acr: str, text: str) -> str:
    if len(acr) > MAX_LEN:
        raise IdError(f"acronym {acr} of {text!r} is longer than {MAX_LEN} - give a shorter name (3-7 words)")
    return acr


def mkp_id(concept: str) -> str:
    return "M_" + _checked(acronym(concept), concept)


def element_id(mkp: str, kind: str, text: str) -> str:
    kind = str(kind or "").upper()
    if kind not in ELEMENT_TYPES:
        raise IdError(f"unknown element type {kind!r}")
    if not _MKP_RE.match(str(mkp or "")):
        raise IdError(f"not an MKP id: {mkp!r}")
    return f"{mkp}_{kind}_{_checked(acronym(text), text)}"


def fact_id(name: str) -> str:
    return "F_" + _checked(acronym(name), name)


# ---------------------------------------------------------------- gate

def kind_of(identifier: str) -> Optional[str]:
    """MKP / F / A / G / LE / PD / OR / CT / TC, or None when the id does not follow the rule."""
    s = str(identifier or "")
    m = _ELEMENT_RE.match(s)
    if m:
        return m.group(2)
    if _FACT_RE.match(s):
        return "F"
    if _MKP_RE.match(s):
        return "MKP"
    return None


def check(identifier: str, kind: Optional[str] = None) -> list:
    """Problems of one id ([] = fine). kind: the expected kind (MKP, F, A, G ...)."""
    got = kind_of(identifier)
    if got is None:
        return [f"{identifier!r} is not an abbreviated id (capitals, digits and _ only, M_ / F_ / <MKP>_<TYPE>_)"]
    if kind and got != kind:
        return [f"{identifier} is a {got} id, expected {kind}"]
    return []


# ---------------------------------------------------------------- uniqueness

def strip_number(identifier: str, base: str) -> bool:
    """True when identifier is base or base followed by a clash number (base2, base3 ...)."""
    return identifier == base or bool(re.fullmatch(re.escape(base) + r"[2-9]|" + re.escape(base) + r"[1-9]\d+",
                                                   identifier))


def with_number(base: str, taken: Iterable[str]) -> str:
    """base, or base2, base3 ... - the first one not in taken."""
    taken = set(taken)
    if base not in taken:
        return base
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


def unique_mkp_id(concept: str, element: str, taken: Dict[str, Tuple[str, str]], own: Optional[str] = None) -> str:
    """The MKP id for one provision, unique across ALL laws.

    taken: mkp_id -> (element, concept) of the ids already saved for other provisions.
    own:   the id this provision already has (a re-run keeps it while its concept gives the same acronym).
    A clash first gets a second letter of the first word that differs from the other concept
    (Supply Of Goods / Sale Of Goods -> M_SUOG), then a number (M_LAATOS2).
    """
    base = mkp_id(concept)
    others = {k: v for k, v in taken.items() if v[0] != element}
    if own and strip_number(own, base) and own not in others:
        return own
    if base not in others:
        return base
    mine, theirs = words(concept), words(others[base][1])
    for i, w in enumerate(mine):
        other = theirs[i] if i < len(theirs) else ""
        if w.lower() != other.lower() and len(w) > 1 and not w[0].isdigit():
            pieces = [_piece(t) for t in mine]
            pieces[i] = pieces[i] + w[1].upper()
            cand = "M_" + "".join(pieces)
            if len(cand) - 2 <= MAX_LEN and cand not in others:
                return cand
            break
    return with_number(base, others)


def unique_ids(ids: Iterable[str]) -> list:
    """Make the ids of one law unique, in order: [X, X, Y] -> [X, X2, Y]."""
    seen, out = set(), []
    for i in ids:
        j = with_number(i, seen)
        seen.add(j)
        out.append(j)
    return out
