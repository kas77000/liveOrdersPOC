"""Parsing of target.fixmsg and the participation levels it carries.

fixmsg is `tag=value` items separated by `;`. Tag 9012 holds a nested list of
`subtag=value` items separated by `^`, e.g.

    9001=LINE;9012=5=4^6=12^15=1

Participation levels (percent):
    9012 sub-tag 5 -> min participation
    9012 sub-tag 6 -> max participation
    9012 sub-tag 7 -> target participation
When the sub-tags are absent, the engine falls back to the generic beta/delta
params, depending on the algo. For PART, min = max = target.
"""
from __future__ import annotations

import math


def parse_fixmsg(fixmsg) -> dict[str, str]:
    out: dict[str, str] = {}
    if not fixmsg or not isinstance(fixmsg, str):
        return out
    for item in fixmsg.split(";"):
        if "=" in item:
            tag, val = item.split("=", 1)
            out[tag.strip()] = val
    return out


def parse_9012(value) -> dict[str, str]:
    out: dict[str, str] = {}
    if not value:
        return out
    for item in value.split("^"):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# Which generic param feeds which level when 9012 does not set it explicitly.
_MAX_FROM_DELTA = {"LINE", "PCAP", "SMKT", "IIS", "SMALL", "PASS", "GNR"}
_MAX_FROM_BETA = {"WOW", "TMX", "OPETMX", "TMX2", "VWAP", "OPEVWAP", "OPEVWAP1",
                  "OPEVWAP2", "OPEVWAP3", "OPEVWAP4", "OPEVWAP5", "OPEVWAP6", "VWAP1",
                  "VWAP2", "VWAP3", "VWAPB", "PART", "FALL", "CASH"}
_MIN_FROM_BETA = {"LINE", "PART"}
_MIN_FROM_DELTA = {"SHADOW", "SHADOWMET1", "SHADOWMET2", "OPESHADOW1", "OPESHADOW2",
                   "OPESHADOW3", "SHADOW1", "SHADOW2", "SHADOW3", "SHADOWMIN", "SHADOWNC"}


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or v <= 0:
        return None          # 0 / null means "not set" in the engine
    return v


def participation(algo, tags: dict, beta=None, delta=None) -> dict:
    """Return {'min': float|None, 'max': ..., 'target': ...} in percent."""
    algo = (algo or "").upper()
    sub = parse_9012(tags.get("9012"))
    lo, hi, tgt = _num(sub.get("5")), _num(sub.get("6")), _num(sub.get("7"))

    if hi is None:
        if algo in _MAX_FROM_DELTA:
            hi = _num(delta)
        elif algo in _MAX_FROM_BETA:
            hi = _num(beta)
    if lo is None:
        if algo in _MIN_FROM_BETA:
            lo = _num(beta)
        elif algo in _MIN_FROM_DELTA:
            lo = _num(delta)
    if tgt is None and algo == "PART":
        tgt = _num(beta)
    if algo == "PART" and tgt is not None:
        lo = hi = tgt
    return {"min": lo, "max": hi, "target": tgt}
