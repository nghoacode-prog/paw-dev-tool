#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import requests
import math
import unicodedata
from collections import Counter
from statistics import mode

API_URL = "https://wtxmd52.tele68.com/v1/txmd5/sessions"
HU_API_URL = "https://wtx.tele68.com/v1/tx/sessions"
HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://lc79b.bet"}


def fetch_sessions(api_url=None, timeout=8, retries=2):
    """Fetch newest-first sessions with direct access first and CORS-proxy fallbacks.

    Proxy fallback is server-side: the browser should call only PAWNOVA's own API.
    AllOrigins /get wraps the upstream JSON in {contents: "..."}, which is unwrapped.
    """
    import json
    import time
    from urllib.parse import quote, urlsplit, urlunsplit, parse_qsl, urlencode

    source_url = api_url or API_URL
    parts = urlsplit(source_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["_paw_ts"] = str(int(time.time() * 1000))
    fresh_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    encoded = quote(fresh_url, safe="")
    candidates = [
        ("direct", fresh_url, False),
        ("allorigins_raw", "https://api.allorigins.win/raw?url=" + encoded, False),
        ("allorigins_get", "https://api.allorigins.win/get?url=" + encoded, True),
        ("codetabs", "https://api.codetabs.com/v1/proxy?quest=" + encoded, False),
        ("corsproxy", "https://corsproxy.io/?url=" + encoded, False),
    ]
    last_error = None
    for attempt in range(max(1, int(retries))):
        for proxy_name, request_url, wrapped in candidates:
            try:
                response = requests.get(
                    request_url,
                    timeout=timeout,
                    headers={"User-Agent": HEADERS.get("User-Agent", "Mozilla/5.0"),
                             "Accept": "application/json,text/plain,*/*"},
                )
                response.raise_for_status()
                data = response.json()
                if wrapped:
                    contents = data.get("contents") if isinstance(data, dict) else None
                    if not isinstance(contents, str) or not contents.strip():
                        raise ValueError("AllOrigins GET thiếu trường contents")
                    data = json.loads(contents)
                raw = data.get("list", []) if isinstance(data, dict) else data
                if not isinstance(raw, list):
                    raise ValueError("API response has no session list")
                out = []
                seen = set()
                for it in raw:
                    if not isinstance(it, dict):
                        continue
                    try:
                        raw_id = it.get("id", it.get("phien"))
                        if raw_id is None:
                            continue
                        sid = int(str(raw_id).strip())
                        side = next((normalized for raw_side in (
                            it.get("resultTruyenThong"), it.get("result"), it.get("ket_qua")
                        ) if (normalized := normalize_side(raw_side))), None)
                        if side not in ("TAI", "XIU") or sid in seen:
                            continue
                        rec = {"id": sid, "result": side}
                        dice = it.get("dices")
                        if isinstance(dice, (list, tuple)) and len(dice) == 3:
                            try:
                                vals = [int(v) for v in dice]
                                if all(1 <= v <= 6 for v in vals):
                                    rec["dices"] = vals
                                    try:
                                        rec["point"] = int(it.get("point", sum(vals)))
                                    except (TypeError, ValueError):
                                        rec["point"] = sum(vals)
                            except (TypeError, ValueError, OverflowError):
                                pass
                        out.append(rec)
                        seen.add(sid)
                    except (TypeError, ValueError, OverflowError):
                        continue
                out.sort(key=lambda x: x["id"], reverse=True)
                if out:
                    return out[:500]
                raise ValueError("API response contains no valid TAI/XIU sessions")
            except Exception as exc:
                last_error = f"{proxy_name}: {type(exc).__name__}: {str(exc)[:100]}"
        if attempt + 1 < max(1, int(retries)):
            time.sleep(0.5)
    # Keep the last failure available for server logs/diagnostics without exposing secrets.
    global LAST_FETCH_ERROR
    LAST_FETCH_ERROR = last_error or "No upstream candidates succeeded"
    return []

LAST_FETCH_ERROR = None


def normalize_side(s):
    if s is None:
        return None
    # Normalize Vietnamese diacritics as a defensive fallback; source APIs usually use ASCII.
    s = unicodedata.normalize("NFD", str(s).upper().strip())
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.replace("Đ", "D").strip()
    if s in ("T", "TAI"):
        return "TAI"
    if s in ("X", "XIU"):
        return "XIU"
    return None


def get_runs(seq):
    if not seq:
        return []
    runs = []
    cur = seq[0]
    length = 1
    for i in range(1, len(seq)):
        if seq[i] == cur:
            length += 1
        else:
            runs.append((cur, length))
            cur = seq[i]
            length = 1
    runs.append((cur, length))
    return runs


def _opp(side):
    return "XIU" if side == "TAI" else "TAI"


def _runs_to_seq(runs):
    seq = []
    for side, L in runs:
        seq.extend([side] * L)
    return seq


def mod_lifecycle(runs):
    n = len(runs)
    if n < 5:
        return "UNKNOWN", 0, 0
    recent_lens = [r[1] for r in runs[-15:]]
    last_len = runs[-1][1]
    # Adjacent runs necessarily alternate sides, so checking whether the last three
    # run labels are equal can never detect a streak. A run of length >= 3 is the bet.
    is_bet = last_len >= 3 and n >= 3
    is_khuon = False
    if n >= 6:
        l = [r[1] for r in runs[-6:]]
        if l[0] == l[2] == l[4] and l[1] == l[3] == l[5]:
            is_khuon = True
    try:
        typical = mode(recent_lens[:-1]) if len(recent_lens) > 1 else 1
    except Exception:
        typical = 1
    if last_len >= typical * 2 and last_len >= 4:
        return "MATURE", last_len, typical
    if last_len >= typical * 1.5 and last_len >= 3:
        return "MATURE", last_len, typical
    if last_len >= 2 and is_bet and n >= 4:
        return "CONFIRMED", last_len, typical
    if is_khuon:
        return "CONFIRMED", last_len, typical
    if n >= 5:
        return "FORMING", last_len, typical
    return "UNKNOWN", last_len, typical


def mod_break(runs):
    n = len(runs)
    if n < 15:
        return 0.5, 0
    last_side = runs[-1][0]
    last_len = runs[-1][1]
    reached = broke = 0
    for i, (s, L) in enumerate(runs[:-1]):
        if s == last_side and L >= last_len:
            if i + 1 < n:
                reached += 1
                if runs[i + 1][0] != last_side:
                    broke += 1
    if reached < 3:
        return 0.5, reached
    p = (broke + 0.5) / (reached + 1)
    state, _, _ = mod_lifecycle(runs)
    if state == "MATURE":
        p = min(0.85, p + 0.10)
    elif state == "CONFIRMED":
        p = max(0.15, p - 0.05)
    return p, reached


def mod_bias(runs):
    if len(runs) < 20:
        return 0.5, 0

    def pw(n):
        recent = runs[-n:]
        total = sum(r[1] for r in recent)
        if total < 8:
            return None, 0
        tai = sum(r[1] for r in recent if r[0] == "TAI")
        return tai / total, total

    p20, n20 = pw(min(20, len(runs)))
    p40, n40 = pw(min(40, len(runs)))
    p60, n60 = pw(min(60, len(runs)))
    vals, weights = [], []
    if p20 is not None:
        vals.append(p20)
        weights.append(0.50)
    if p40 is not None:
        vals.append(p40)
        weights.append(0.30)
    if p60 is not None:
        vals.append(p60)
        weights.append(0.20)
    if not vals:
        return 0.5, 0
    tw = sum(weights)
    return sum(v * w for v, w in zip(vals, weights)) / tw, n20 + n40 + n60


def mod_markov(runs):
    seq = _runs_to_seq(runs)
    n = len(seq)
    if n < 30:
        return 0.5, 0
    last = seq[-1]
    ct = cx = 0
    for i in range(n - 1):
        if seq[i] == last:
            if seq[i + 1] == "TAI":
                ct += 1
            else:
                cx += 1
    tot = ct + cx
    if tot < 5:
        return 0.5, tot
    p1 = (ct + 0.5) / (tot + 1)
    ct2 = cx2 = 0
    if n >= 3:
        ctx = tuple(seq[-2:])
        for i in range(n - 2):
            if tuple(seq[i:i + 2]) == ctx:
                if seq[i + 2] == "TAI":
                    ct2 += 1
                else:
                    cx2 += 1
    tot2 = ct2 + cx2
    p2 = (ct2 + 0.5) / (tot2 + 1) if tot2 >= 3 else p1
    return p1 * 0.4 + p2 * 0.6, tot + tot2


def mod_ngram(runs):
    seq = _runs_to_seq(runs)
    n = len(seq)
    if n < 40:
        return 0.5, 0
    results = []
    for L in [3, 4, 5]:
        if n < L + 10:
            continue
        ctx = tuple(seq[-L:])
        ct = cx = 0
        for i in range(n - L):
            if tuple(seq[i:i + L]) == ctx:
                if seq[i + L] == "TAI":
                    ct += 1
                else:
                    cx += 1
        tot = ct + cx
        if tot >= 3:
            p = (ct + 1) / (tot + 2)
            w = L * math.sqrt(min(tot, 20))
            results.append((p, w, tot))
    if not results:
        return 0.5, 0
    tw = sum(r[1] for r in results)
    return sum(r[0] * r[1] for r in results) / tw, sum(r[2] for r in results)
def mod_run_ngram(runs):
    n = len(runs)
    if n < 15:
        return 0.5, 0
    lens = [r[1] for r in runs]
    sides = [r[0] for r in runs]
    tt = tx = 0
    for L in [2, 3, 4]:
        if n < L + 3:
            continue
        ctx = tuple(lens[-L:])
        for i in range(n - L):
            if tuple(lens[i:i + L]) == ctx:
                if sides[i + L] == "TAI":
                    tt += 1
                else:
                    tx += 1
    tot = tt + tx
    if tot < 3:
        return 0.5, tot
    return (tt + 0.5) / (tot + 1), tot


def mod_cycle(runs):
    n = len(runs)
    if n < 12:
        return None
    lens = [r[1] for r in runs]
    for period in range(2, 9):
        if n < period * 3:
            continue
        recent = lens[-period * 3:]
        c1 = recent[:period]
        c2 = recent[period:period * 2]
        c3 = recent[period * 2:period * 3]
        if c1 == c2 == c3:
            return {"period": period, "cycle": c1, "next_side": _opp(runs[-1][0]), "confidence": 78}
    return None


def mod_symmetry(runs):
    n = len(runs)
    if n < 7:
        return None
    lens = [r[1] for r in runs]
    for span in [9, 7, 5]:
        if n < span:
            continue
        seg = lens[-span:]
        half = span // 2
        if seg[:half] == seg[half + 1:][::-1]:
            return {"span": span, "prediction": _opp(runs[-1][0]), "confidence": 68}
    return None


def mod_morph(runs):
    n = len(runs)
    if n < 5:
        return None
    recent = [r[1] for r in runs[-5:]]
    last_side = runs[-1][0]
    if recent[0] == 1 and recent[1] == 1 and recent[2] == 2:
        return {"prediction": last_side, "confidence": 62, "type": "1-1-2"}
    if recent[0] == 2 and recent[1] == 2 and recent[2] == 1:
        return {"prediction": last_side, "confidence": 62, "type": "2-2-1"}
    if recent[0] == 3 and recent[1] == 3 and recent[2] == 1:
        return {"prediction": last_side, "confidence": 62, "type": "3-3-1"}
    if recent[0] == 1 and recent[1] == 2 and recent[2] == 3:
        return {"prediction": last_side, "confidence": 60, "type": "1-2-3"}
    if recent[0] == 3 and recent[1] == 2 and recent[2] == 1:
        return {"prediction": last_side, "confidence": 60, "type": "3-2-1"}
    return None


def mod_entropy(runs):
    seq = _runs_to_seq(runs)
    if len(seq) < 60:
        return 0.5, 0

    def ent(s):
        if len(s) < 5:
            return 1.0
        p = s.count("TAI") / len(s)
        if p in (0, 1):
            return 0.0
        return -(p * math.log2(p) + (1 - p) * math.log2(1 - p))

    e20 = ent(seq[-20:])
    e60 = ent(seq[-60:])
    diff = e20 - e60
    last = seq[-1]
    if diff < -0.08:
        return (0.60 if last == "TAI" else 0.40), 60
    if diff > 0.08:
        return (0.45 if last == "TAI" else 0.55), 60
    return 0.5, 60


def mod_forecast(runs, window=3):
    n = len(runs)
    if n < 8:
        return []
    sig = tuple(r[1] for r in runs[-window:])
    matches = []
    for i in range(n - window - 1):
        cand = tuple(runs[i + j][1] for j in range(window))
        if cand == sig:
            idx = i + window
            if idx < n:
                matches.append(runs[idx][0])
    if len(matches) < 2:
        return []
    c = Counter(matches)
    tot = sum(c.values())
    return [{"side": s, "prob": n2 / tot, "count": n2} for s, n2 in c.most_common()]


def mod_1_1(runs):
    n = len(runs)
    if n < 6:
        return None
    lens = [r[1] for r in runs[-8:]]
    if len(lens) >= 6 and all(l == 1 for l in lens[-6:]):
        return {"prediction": _opp(runs[-1][0]), "confidence": 78, "type": "1-1"}
    ones = sum(1 for l in lens[-8:] if l == 1)
    if ones >= 6:
        return {"prediction": _opp(runs[-1][0]), "confidence": 72, "type": "1-1 weak"}
    return None


def mod_2_2(runs):
    n = len(runs)
    if n < 4:
        return None
    lens = [r[1] for r in runs[-6:]]
    if len(lens) >= 6 and all(l == 2 for l in lens[-6:]):
        return {"prediction": _opp(runs[-1][0]), "confidence": 82, "type": "2-2 strong"}
    if len(lens) >= 4 and all(l == 2 for l in lens[-4:]):
        return {"prediction": _opp(runs[-1][0]), "confidence": 78, "type": "2-2"}
    if len(lens) >= 3 and lens[-3] == 2 and lens[-2] == 2 and lens[-1] == 1:
        return {"prediction": runs[-1][0], "confidence": 68, "type": "2-2 start"}
    return None


def mod_3_3(runs):
    n = len(runs)
    if n < 3:
        return None
    lens = [r[1] for r in runs[-6:]]
    if len(lens) >= 4 and lens[-4] == 3 and lens[-3] == 3 and lens[-2] == 3 and lens[-1] == 1:
        return {"prediction": runs[-1][0], "confidence": 72, "type": "3-3 start"}
    if len(lens) >= 3 and all(l == 3 for l in lens[-3:]):
        return {"prediction": _opp(runs[-1][0]), "confidence": 82, "type": "3-3"}
    if len(lens) >= 3 and lens[-3] == 3 and lens[-2] == 3 and lens[-1] == 2:
        return {"prediction": runs[-1][0], "confidence": 70, "type": "3-3 2/3"}
    return None


def mod_4_4(runs):
    n = len(runs)
    if n < 3:
        return None
    lens = [r[1] for r in runs[-5:]]
    if len(lens) >= 3 and all(l == 4 for l in lens[-3:]):
        return {"prediction": _opp(runs[-1][0]), "confidence": 82, "type": "4-4"}
    return None
def mod_1_2_1(runs):
    n = len(runs)
    if n < 5:
        return None
    lens = [r[1] for r in runs[-5:]]
    if lens[-3:] == [1, 2, 1]:
        return {"prediction": runs[-1][0], "confidence": 68, "type": "1-2-1"}
    if lens[-4:] == [1, 2, 1, 2]:
        return {"prediction": runs[-1][0], "confidence": 72, "type": "1-2-1-2"}
    return None


def mod_211_112(runs):
    n = len(runs)
    if n < 4:
        return None
    lens = [r[1] for r in runs[-5:]]
    if lens[-3:] == [2, 1, 1]:
        return {"prediction": runs[-1][0], "confidence": 65, "type": "2-1-1"}
    if lens[-3:] == [1, 1, 2]:
        return {"prediction": runs[-1][0], "confidence": 65, "type": "1-1-2"}
    if lens[-4:] == [2, 1, 1, 2]:
        return {"prediction": runs[-1][0], "confidence": 72, "type": "2-1-1-2"}
    return None


def mod_322_232(runs):
    n = len(runs)
    if n < 4:
        return None
    lens = [r[1] for r in runs[-5:]]
    if lens[-3:] == [3, 2, 2]:
        return {"prediction": runs[-1][0], "confidence": 68, "type": "3-2-2"}
    if lens[-3:] == [2, 3, 2]:
        return {"prediction": runs[-1][0], "confidence": 68, "type": "2-3-2"}
    if lens[-3:] == [2, 2, 3]:
        return {"prediction": runs[-1][0], "confidence": 68, "type": "2-2-3"}
    if lens[-3:] == [3, 3, 2]:
        return {"prediction": runs[-1][0], "confidence": 68, "type": "3-3-2"}
    return None


def mod_nghieng(runs):
    n = len(runs)
    if n < 4:
        return None
    lens = [r[1] for r in runs[-5:]]
    if lens[-4:] == [1, 2, 3, 4]:
        return {"prediction": _opp(runs[-1][0]), "confidence": 70, "type": "1-2-3-4"}
    if lens[-3:] == [1, 2, 3]:
        return {"prediction": runs[-1][0], "confidence": 62, "type": "1-2-3"}
    if lens[-3:] == [2, 3, 4]:
        return {"prediction": runs[-1][0], "confidence": 65, "type": "2-3-4"}
    return None


def mod_giam(runs):
    n = len(runs)
    if n < 4:
        return None
    lens = [r[1] for r in runs[-5:]]
    if lens[-4:] == [4, 3, 2, 1]:
        return {"prediction": _opp(runs[-1][0]), "confidence": 70, "type": "4-3-2-1"}
    if lens[-3:] == [4, 3, 2]:
        return {"prediction": runs[-1][0], "confidence": 65, "type": "4-3-2"}
    if lens[-3:] == [3, 2, 1]:
        return {"prediction": _opp(runs[-1][0]), "confidence": 65, "type": "3-2-1"}
    return None


def mod_2211(runs):
    n = len(runs)
    if n < 5:
        return None
    lens = [r[1] for r in runs[-6:]]
    if lens[-4:] == [2, 2, 1, 1]:
        return {"prediction": _opp(runs[-1][0]), "confidence": 68, "type": "2-2-1-1"}
    if lens[-4:] == [1, 1, 2, 2]:
        return {"prediction": _opp(runs[-1][0]), "confidence": 68, "type": "1-1-2-2"}
    return None


def mod_bat_thuong(runs):
    n = len(runs)
    if n < 5:
        return None
    lens = [r[1] for r in runs[-5:]]
    if lens[-4:] == [5, 1, 5, 1]:
        return {"prediction": runs[-1][0], "confidence": 62, "type": "5-1-5-1"}
    if lens[-4:] == [4, 1, 4, 1]:
        return {"prediction": runs[-1][0], "confidence": 62, "type": "4-1-4-1"}
    return None


_BOT_HISTORY = []


def record_bot_result(correct):
    _BOT_HISTORY.append(1 if correct else 0)
    if len(_BOT_HISTORY) > 100:
        _BOT_HISTORY.pop(0)


def mod_anti_flip():
    if len(_BOT_HISTORY) < 15:
        return 0, 0.0
    recent = _BOT_HISTORY[-20:]
    rate = sum(recent) / len(recent)
    if rate <= 0.35:
        return -1, rate
    if rate >= 0.65:
        return 1, rate
    return 0, rate


def mod_stability(votes):
    if len(votes) < 3:
        return 0.7
    probs = [v[0] for v in votes]
    mean = sum(probs) / len(probs)
    var = sum((p - mean) ** 2 for p in probs) / len(probs)
    std = math.sqrt(var)
    if std > 0.20:
        return 0.5
    if std > 0.12:
        return 0.75
    return 1.0


def _mature_signal(last_side, p_break):
    """Return P(next=TÀI), weight and label with side-symmetric break/follow logic."""
    if p_break >= 0.60:
        # A break means the next side is opposite the current run.
        p_tai = p_break if last_side == "XIU" else (1.0 - p_break)
        return p_tai, 1.8, f"MATURE bẻ {p_break*100:.0f}%"
    if p_break <= 0.40:
        # A low break rate favors continuing the current run.
        p_tai = (1.0 - p_break) if last_side == "TAI" else p_break
        return p_tai, 1.4, f"MATURE theo {p_break*100:.0f}%"
    return None


def _collect_all(runs):
    votes = []
    reasons = []
    state, last_len, typical = mod_lifecycle(runs)
    p_break, br_n = mod_break(runs)

    p_bias, bias_n = mod_bias(runs)
    if bias_n >= 30:
        votes.append((p_bias, 2.0))
        reasons.append(f"bias {p_bias*100:.0f}% n{bias_n}")

    p_mk, mk_n = mod_markov(runs)
    if mk_n >= 10:
        votes.append((p_mk, 1.0))
        reasons.append(f"markov n{mk_n}")

    p_ng, ng_n = mod_ngram(runs)
    if ng_n >= 5:
        votes.append((p_ng, 1.6))
        reasons.append(f"ngram n{ng_n}")

    p_rn, rn_n = mod_run_ngram(runs)
    if rn_n >= 5:
        votes.append((p_rn, 1.3))
        reasons.append(f"run-ngram n{rn_n}")

    p_et, _ = mod_entropy(runs)
    votes.append((p_et, 0.6))

    if state == "MATURE":
        mature_vote = _mature_signal(runs[-1][0], p_break)
        if mature_vote:
            p_tai, weight, label = mature_vote
            votes.append((p_tai, weight))
            reasons.append(label)
    elif state == "CONFIRMED":
        if p_break <= 0.45:
            last = runs[-1][0]
            votes.append((0.62 if last == "TAI" else 0.38, 1.5))
            reasons.append("CONFIRMED theo")

    cyc = mod_cycle(runs)
    if cyc:
        p = 0.68 if cyc["next_side"] == "TAI" else 0.32
        votes.append((p, 1.8))
        reasons.append(f"cycle {cyc['period']}")

    sym = mod_symmetry(runs)
    if sym:
        p = 0.65 if sym["prediction"] == "TAI" else 0.35
        votes.append((p, 1.2))
        reasons.append(f"sym {sym['span']}")

    morph = mod_morph(runs)
    if morph:
        p = 0.60 if morph["prediction"] == "TAI" else 0.40
        votes.append((p, 1.6))
        reasons.append(f"morph {morph['type']}")

    fc = mod_forecast(runs)
    if fc:
        top = fc[0]
        p = top["prob"] if top["side"] == "TAI" else 1 - top["prob"]
        votes.append((p, 0.9))
        reasons.append(f"fc {top['side']} {top['prob']*100:.0f}%")

    for mod_fn, weight in [
        (mod_1_1, 2.2),
        (mod_2_2, 2.0),
        (mod_3_3, 2.0),
        (mod_4_4, 2.0),
        (mod_1_2_1, 1.5),
        (mod_211_112, 1.4),
        (mod_322_232, 1.4),
        (mod_nghieng, 1.3),
        (mod_giam, 1.3),
        (mod_2211, 1.5),
        (mod_bat_thuong, 1.0),
    ]:
        r = mod_fn(runs)
        if r:
            p = 0.60 if r["prediction"] == "TAI" else 0.40
            votes.append((p, weight))
            reasons.append(f"{r['type']}")

    return votes, reasons, state, last_len, p_break
def ensemble_vote(runs):
    n = len(runs)
    if n < 15:
        return None

    votes, reasons, state, last_len, p_break = _collect_all(runs)
    if not votes:
        return None

    stab = mod_stability(votes)
    # Symmetric aggregation: measure signal mass for each side, then shrink toward 50/50.
    # Duplicate pattern detectors can correlate; cap any side's influence by limiting the
    # contribution from all hard-pattern votes as one block.
    total_w = sum(max(0.0, float(v[1])) for v in votes)
    p_tai = sum(float(v[0]) * max(0.0, float(v[1])) for v in votes) / max(total_w, 1e-9)
    # If the ensemble has little disagreement/data, do not manufacture directional certainty.
    vote_margin = abs(p_tai - 0.5)
    if len(votes) < 3:
        stab *= 0.65
    elif vote_margin < 0.06:
        stab *= 0.80

    anti, anti_rate = mod_anti_flip()
    flipped = False
    conf_boost = 0
    # Do not invert the entire ensemble based on a short, noisy win/loss window.
    # That feedback loop can create persistent one-sided bias. Use recent accuracy
    # only to damp confidence; keep the direction supported by current signals.
    if anti == -1:
        reasons.append(f"recent-hit-low {anti_rate*100:.0f}%")
    elif anti == 1:
        conf_boost = 2

    # Shrink extreme probabilities toward neutral and cap the displayed probability.
    p_tai = 0.5 + (p_tai - 0.5) * 0.75
    p_tai = max(0.35, min(0.65, p_tai))

    margin = abs(p_tai - 0.5)
    # Do not turn a numerical tie into an automatic TÀI prediction.
    if margin < 0.005:
        return None
    pred = "TAI" if p_tai > 0.5 else "XIU"
    # This is a heuristic score, not a true win probability. Keep its ceiling conservative.
    conf = 52 + int(margin * 100 * stab) + conf_boost
    if anti == -1:
        conf -= 3
    conf = min(68, max(51, conf))

    return {
        "prediction": pred,
        "confidence": conf,
        "p_tai": p_tai,
        "state": state,
        "p_break": p_break,
        "n_votes": len(votes),
        "reasons": reasons,
        "flipped": flipped,
        "stability": stab,
        "model_version": "PAWNOVA-ENSEMBLE-2.1-BALANCED",
        "last_side": runs[-1][0],
        "last_len": last_len,
    }



# ===== PAWNOVA NEW ALGORITHM INTEGRATION =====
BASE_TAI_RATE = 0.4861

def _mod_alpha(seq, max_order=6, alpha=0.5, min_samples=3):
    """Module α — matching theo context dài nhất."""
    n = len(seq)
    for order in range(min(max_order, n - 1), 0, -1):
        ctx = tuple(seq[-order:])
        ct = cx = 0
        for i in range(n - order):
            if tuple(seq[i:i + order]) == ctx:
                if seq[i + order] == "T":
                    ct += 1
                else:
                    cx += 1
        tot = ct + cx
        if tot >= min_samples:
            return (ct + alpha) / (tot + 2 * alpha), order, tot
    return 0.5, 0, 0


def _mod_beta(seq, window=40, a0=6.0, b0=6.0, decay=0.95):
    """Module β — trung bình có trọng số mũ + prior."""
    recent = seq[-window:]
    a, b = a0, b0
    L = len(recent)
    for i, r in enumerate(recent):
        w = decay ** (L - 1 - i)
        if r == "T":
            a += w
        else:
            b += w
    return a / (a + b)


def _mod_gamma(seq, max_lag=25):
    """Module γ — tìm chu kỳ ẩn."""
    x = [1 if r == "T" else 0 for r in seq]
    n = len(x)
    if n < 10:
        return 0, 0.0
    mean = sum(x) / n
    var = sum((v - mean) ** 2 for v in x) / n
    if var == 0:
        return 0, 0.0
    best_lag, best_corr = 0, 0.0
    for lag in range(2, min(max_lag, n // 2)):
        cov = sum((x[i] - mean) * (x[i + lag] - mean)
                  for i in range(n - lag)) / (n - lag)
        corr = cov / var
        if abs(corr) > abs(best_corr):
            best_corr, best_lag = corr, lag
    return best_lag, best_corr


def _mod_delta(seq):
    """Module δ — phân tích chuỗi (đảo chiều có điều kiện)."""
    last = seq[-1]
    s = 1
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last:
            s += 1
        else:
            break
    p, w = 0.5, 0.0
    if s >= 4:
        depth = min(0.30, 0.06 * (s - 3))
        p = (0.5 - depth) if last == "T" else (0.5 + depth)
        w = 1.5
    elif s == 1:
        p = 0.55 if last == "T" else 0.45
        w = 0.6
    return p, w, s, last


def _mod_epsilon(seq, window=20):
    """Module ε — tần suất cửa sổ."""
    recent = seq[-window:]
    return recent.count("T") / len(recent) if recent else 0.5


def _mod_zeta(seq, max_len=6, min_count=4):
    """Module ζ — khai thác mẫu lặp."""
    n = len(seq)
    best = (0.5, None, 0, 0)
    for length in range(2, min(max_len, n - 1) + 1):
        ctx = tuple(seq[-length:])
        ct = cx = 0
        for i in range(n - length):
            if tuple(seq[i:i + length]) == ctx:
                if seq[i + length] == "T":
                    ct += 1
                else:
                    cx += 1
        tot = ct + cx
        if tot >= min_count and length > best[3]:
            p = (ct + 0.5) / (tot + 1)
            best = (p, "".join(ctx), tot, length)
    return best[0], best[1], best[2]


def _mod_eta(seq, long_w=400, k=80):
    """Module η — base rate thích ứng."""
    w = seq[-long_w:]
    n = len(w)
    if n < 50:
        return BASE_TAI_RATE
    return (w.count("T") + BASE_TAI_RATE * k) / (n + k)


def _mod_theta(seq):
    """Module θ — phát hiện regime shift."""
    if len(seq) < 100:
        return "stable", 0.5, 0.5, 0.0
    recent = seq[-50:]
    old = seq[-200:-50]
    pr = recent.count("T") / len(recent)
    po = old.count("T") / len(old)
    diff = pr - po
    if diff > 0.10:
        return "tai_shift", pr, po, diff
    if diff < -0.10:
        return "xiu_shift", pr, po, diff
    return "stable", pr, po, diff


def _entropy(seq, window=50):
    recent = seq[-window:]
    if not recent:
        return 1.0
    p = recent.count("T") / len(recent)
    if p in (0.0, 1.0):
        return 0.0
    return -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


# ================== META-LEARNING ==================
def _meta_weights(history, names, lookback=50):
    """Tính multiplier cho từng module dựa trên accuracy 50 phiên gần."""
    recent = [h for h in history[-lookback:] if "signals" in h]
    if len(recent) < 10:
        return {n: 1.0 for n in names}

    correct = {n: 0 for n in names}
    total = {n: 0 for n in names}
    for h in recent:
        actual = h["actual"]
        for s in h["signals"]:
            nm = s["name"]
            if nm not in correct:
                continue
            pred = "TAI" if s["p_tai"] >= 50 else "XIU"
            total[nm] += 1
            if pred == actual:
                correct[nm] += 1

    weights = {}
    for n in names:
        t = total.get(n, 0)
        if t < 5:
            weights[n] = 1.0
        else:
            acc = correct[n] / t
            weights[n] = max(0.3, min(2.0, acc / 0.5))
    return weights


# ================== ENSEMBLE ==================
def _analyze_new_algorithm(sessions, history=None):
    """Chạy 8 module, gộp có trọng số, trả dict kết quả."""
    history = history if isinstance(history, list) else []
    clean = []
    for item in sessions:
        if not isinstance(item, dict):
            continue
        side = normalize_side(item.get("resultTruyenThong", item.get("result", item.get("ket_qua"))))
        if side in ("TAI", "XIU"):
            clean.append({"id": item.get("id", item.get("phien", 0)), "resultTruyenThong": side})
    sessions = clean
    results = [s["resultTruyenThong"] for s in sessions]
    n = len(results)
    if n < 30:
        return None

    seq = ["T" if r == "TAI" else "X" for r in results]
    raw = []

    # α
    p_a, ord_a, samp_a = _mod_alpha(seq, max_order=6)
    w_a = 2.8 if samp_a >= 5 else (1.4 if samp_a >= 3 else 0.4)
    raw.append(("α", p_a, w_a, f"o{ord_a}/n{samp_a}"))

    # β
    p_b = _mod_beta(seq, window=40)
    raw.append(("β", p_b, 1.6, ""))

    # γ
    lag, corr = _mod_gamma(seq)
    if lag > 0 and abs(corr) >= 0.10 and n > lag:
        ref = seq[-lag]
        p_g = (1.0 if ref == "T" else 0.0) if corr > 0 else (0.0 if ref == "T" else 1.0)
        w_g = min(2.5, abs(corr) * 6)
    else:
        p_g, w_g = 0.5, 0.0
    raw.append(("γ", p_g, w_g, f"L{lag}/r{corr:+.2f}"))

    # δ
    p_d, w_d, streak, last = _mod_delta(seq)
    raw.append(("δ", p_d, w_d, f"{last}×{streak}"))

    # ε
    p_e = _mod_epsilon(seq, window=20)
    raw.append(("ε", p_e, 0.8, f"{p_e*100:.0f}%"))

    # ζ
    p_z, ctx_z, n_z = _mod_zeta(seq, max_len=6, min_count=4)
    w_z = 2.5 if n_z >= 6 else (1.5 if n_z >= 4 else 0.0)
    raw.append(("ζ", p_z, w_z, f"{ctx_z or '—'}/n{n_z}"))

    # η
    p_h = _mod_eta(seq)
    raw.append(("η", p_h, 0.9, f"{p_h*100:.1f}%"))

    # θ
    regime, pr, po, rdiff = _mod_theta(seq)
    if regime == "tai_shift":
        raw.append(("θ", 0.62, 1.5, f"+{rdiff*100:.0f}%"))
    elif regime == "xiu_shift":
        raw.append(("θ", 0.38, 1.5, f"{rdiff*100:.0f}%"))

    # ---- Meta ----
    names = [r[0] for r in raw]
    meta = _meta_weights(history, names, lookback=50)

    signals = []
    total_w = 0.0
    total_pw = 0.0
    for nm, p, w, note in raw:
        mw = meta.get(nm, 1.0)
        eff_w = w * mw
        total_w += eff_w
        total_pw += p * eff_w
        signals.append({
            "name": nm, "p_tai": p * 100, "w": w,
            "meta": mw, "eff_w": eff_w, "note": note
        })

    p_tai = total_pw / total_w if total_w > 0 else 0.5

    # ---- Entropy gate (đã giảm nhẹ) ----
    H = _entropy(seq, 50)
    if H >= 0.98:
        p_tai = p_tai * 0.55 + 0.5 * 0.45
    elif H >= 0.92:
        p_tai = p_tai * 0.75 + 0.5 * 0.25

    p_tai = max(0.25, min(0.75, p_tai))
    predict = "TAI" if p_tai >= 0.5 else "XIU"
    conf = max(p_tai, 1 - p_tai) * 100
    skip = False   # luôn dự đoán, không skip

    return {
        "predict": predict, "confidence": conf, "skip": skip,
        "p_tai": p_tai * 100, "p_xiu": (1 - p_tai) * 100,
        "streak": streak, "last": last,
        "f10": (seq[-10:].count("T") / 10) * 100,
        "f20": p_e * 100,
        "f50": (seq[-50:].count("T") / min(50, n)) * 100,
        "entropy": H,
        "regime": regime, "regime_diff": rdiff * 100,
        "base_adaptive": p_h * 100,
        "pattern_ctx": ctx_z, "pattern_n": n_z,
        "best_lag": lag, "best_corr": corr,
        "signals": signals,
        "top_id": sessions[-1]["id"],
        "n": n,
    }



def analyze_sessions(sessions):
    """Adapter giữ nguyên contract PAWNOVA cũ, dùng thuật toán ensemble mới."""
    if not isinstance(sessions, list):
        return None
    valid = []
    for item in sessions:
        if not isinstance(item, dict):
            continue
        side = next((normalized for raw_side in (
            item.get("resultTruyenThong"), item.get("result"), item.get("ket_qua")
        ) if (normalized := normalize_side(raw_side))), None)
        if side in ("TAI", "XIU"):
            valid.append({"id": item.get("id", item.get("phien", 0)), "resultTruyenThong": side,
                          "dices": item.get("dices"), "point": item.get("point")})
    if len(valid) < 30:
        return None
    try:
        valid.sort(key=lambda item: int(item.get("id", 0)))
    except (TypeError, ValueError):
        # API input is newest-first; reverse to chronological when IDs are not numeric.
        valid.reverse()
    # New engine expects oldest -> newest. Keep at most 400 valid sessions.
    valid = valid[-400:]
    result = _analyze_new_algorithm(valid, _BOT_HISTORY if isinstance(_BOT_HISTORY, list) else [])
    if not result:
        return None
    confidence = min(75.0, max(50.0, float(result.get("confidence", 50.0))))
    return {
        "prediction": result["predict"],
        "confidence": round(confidence, 1),
        "p_tai": round(float(result["p_tai"]), 1),
        "pattern_type": "PAWNOVA-ENSEMBLE-NEW-" + str(result.get("regime", "stable")).upper(),
        "reason": "Ensemble mới: " + ", ".join(str(x.get("name", "")) for x in result.get("signals", [])[:3]),
        "thinking": "Entropy %.3f; T10 %.1f%%; T20 %.1f%%; regime %s" % (
            float(result.get("entropy", 0.0)), float(result.get("f10", 0.0)),
            float(result.get("f20", 0.0)), str(result.get("regime", "stable"))),
        "last_side": result.get("last", "X"),
        "last_run_len": int(result.get("streak", 1)),
        "runs_last_10": [],
        "total_runs": len(valid),
        "state": result.get("regime", "stable"),
        "p_break": 0.0,
        "n_votes": len(result.get("signals", [])),
        "stability": 1.0 - float(result.get("entropy", 1.0)),
        "model_version": "PAWNOVA-ENSEMBLE-NEW",
        "flipped": False,
        "new_algorithm": result,
    }



def predict_next(api_url=None):
    """Return a deterministic forecast plus the source rows needed for result grading.

    `confidence` is a heuristic ensemble score, not a calibrated win probability.
    """
    try:
        sessions = fetch_sessions(api_url)
        if not sessions:
            return None, "Nguồn API không trả phiên hợp lệ; " + str(LAST_FETCH_ERROR or "không có chi tiết lỗi")
        res = analyze_sessions(sessions)
        if not res:
            return None, "Engine chưa tạo được tín hiệu dự đoán từ dữ liệu phiên hợp lệ (cần ít nhất 20 phiên; tín hiệu có thể hòa)"
        top_id = sessions[0].get("id")
        try:
            next_id = int(top_id) + 1
        except (TypeError, ValueError):
            next_id = None
        return {
            "predict": res["prediction"],
            "confidence": res["confidence"],
            "p_tai": res["p_tai"],
            "version": res["pattern_type"],
            "regime": res["pattern_type"],
            "tier": "STRONG" if res["confidence"] >= 70 else ("OK" if res["confidence"] >= 60 else "WEAK"),
            "top_id": top_id,
            "target_id": next_id,
            "reasons": [res["reason"], res.get("thinking", "")],
            "pattern": res["pattern_type"],
            "last_run_len": res["last_run_len"],
            "last_side": res["last_side"],
            "source_rows": sessions,
        }, None
    except Exception as e:
        return None, f"Lỗi phân tích dữ liệu: {type(e).__name__}"


def save_result(session_id, predict_result, actual, signals=None):
    try:
        record_bot_result(predict_result == actual)
    except Exception:
        pass


def get_stats():
    if not _BOT_HISTORY:
        return {}
    total = len(_BOT_HISTORY)
    wins = sum(_BOT_HISTORY)
    return {"total": total, "wins": wins, "rate": wins / total * 100 if total else 0}


def get_module_stats():
    return get_stats()
