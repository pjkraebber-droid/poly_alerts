"""Offline tests for whalewatch. Run with:  python3 -m unittest discover tests"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import whalewatch as ww  # noqa: E402

NOW = 1_793_000_000
WHALE = "0x" + "a" * 40

SPORTS_LIST = [
    {"sport": "nba", "tags": "1,100639,745"},
    {"sport": "lal", "tags": "1,780,100639,100350"},
    {"sport": "epl", "tags": "1,82,100639,100350"},
    {"sport": "kor", "tags": "1,100639,100350,102771"},
]

EVENTS = {
    "texas-senate-election-winner": {
        "slug": "texas-senate-election-winner", "title": "Texas Senate Election Winner",
        "tags": [{"id": "2", "label": "Politics", "slug": "politics"},
                 {"id": "102289", "label": "Midterms", "slug": "midterms"}],
    },
    "ballon-dor-winner-2026": {  # Gamma sometimes returns no tags for this one
        "slug": "ballon-dor-winner-2026", "title": "Ballon d'Or Winner 2026",
        "description": "Resolves per France Football.",
    },
    "kor-dae-vis-2026-10-26": {
        "slug": "kor-dae-vis-2026-10-26", "title": "Daejeon Citizen vs. Vissel Kobe",
        "tags": [{"id": "1", "label": "Sports", "slug": "sports"}],
    },
    "nba-gsw-den-2026-10-26": {
        "slug": "nba-gsw-den-2026-10-26", "title": "Warriors vs. Nuggets",
        "tags": [{"id": "1", "label": "Sports", "slug": "sports"}, {"id": "745", "label": "NBA", "slug": "nba"}],
    },
    "mystery-match": {  # no tags, no soccer words, but soccer resolution language
        "slug": "mystery-match", "title": "Team A vs. Team B",
        "description": "Resolves on the result after 90 minutes plus stoppage time.",
    },
    "fed-decision-in-october": {
        "slug": "fed-decision-in-october", "title": "Fed decision in October",
        "tags": [{"id": "100328", "label": "Economy", "slug": "economy"}],
    },
}


class FakeApi(ww.Polymarket):
    def __init__(self, trades=None, events=None, fail_events=()):
        super().__init__(getter=self._route)
        self.trades = trades or {}
        self.events = events if events is not None else EVENTS
        self.fail_events = set(fail_events)
        self.calls = []

    def _route(self, url, params):
        self.calls.append((url, dict(params)))
        if url.endswith("/sports"):
            return SPORTS_LIST
        if url.endswith("/events"):
            slug = params["slug"]
            if slug in self.fail_events:
                raise ww.ApiError("boom")
            ev = self.events.get(slug)
            return [ev] if ev else []
        if url.endswith("/markets"):
            return [{"slug": params["slug"], "outcomePrices": '["0.74", "0.26"]'}]
        if url.endswith("/v2/activity"):
            rows = [r for r in self.trades.get(params["user"], [])
                    if r["timestamp"] >= params.get("start", 0)]
            rows.sort(key=lambda r: -r["timestamp"])
            return {"data": rows, "pagination": {"has_more": False, "next_cursor": None}}
        raise AssertionError(f"unexpected call {url}")


def trade(ts, usd, event_slug, title, side="BUY", cond="c1", outcome="Yes", idx=0, tx=None, **extra):
    price = 0.5
    row = {"proxy_wallet": WHALE, "timestamp": ts, "condition_id": cond, "type": "TRADE",
           "size": usd / price, "usdc_size": usd, "transaction_hash": tx or f"0x{ts}{cond}{usd}",
           "price": price, "token_id": f"tok-{cond}-{idx}", "side": side, "outcome_index": idx,
           "title": title, "slug": title.lower().replace(" ", "-"), "event_slug": event_slug,
           "outcome": outcome, "name": "whale"}
    row.update(extra)
    return row


ALERTS = dict(ww.DEFAULT_ALERTS)
WHALES = [{"name": "TestWhale", "wallet": WHALE, "note": "", "min_bet_usd": 1000.0}]


class Recorder:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    def __call__(self, title, message, click=None, tags=None, priority=3, group=None):
        self.sent.append({"title": title, "message": message, "click": click, "priority": priority, "group": group})
        return self.ok


def classify(slug, title="x"):
    api = FakeApi()
    index = ww.SportsIndex.from_sports_list(SPORTS_LIST)
    return ww.Classifier(api, {}, index, NOW).classify(trade(NOW, 10, slug, title))


class SoccerFilterTests(unittest.TestCase):
    def test_politics_is_allowed(self):
        self.assertEqual(classify("texas-senate-election-winner", "Will the Democrats win the Texas Senate race?"), "other")

    def test_economics_is_allowed(self):
        self.assertEqual(classify("fed-decision-in-october", "Will there be no change in Fed rates?"), "other")

    def test_ballon_dor_without_tags_is_soccer(self):
        self.assertEqual(classify("ballon-dor-winner-2026", "Will Harry Kane win the 2026 Ballon d'Or?"), "soccer")

    def test_league_code_in_slug_is_soccer(self):
        self.assertEqual(classify("kor-dae-vis-2026-10-26", "Daejeon Citizen vs. Vissel Kobe"), "soccer")

    def test_soccer_resolution_language_is_soccer(self):
        self.assertEqual(classify("mystery-match", "Team A vs. Team B"), "soccer")

    def test_nba_is_sports_not_soccer(self):
        self.assertEqual(classify("nba-gsw-den-2026-10-26", "Warriors vs. Nuggets: O/U 249.5"), "sports")

    def test_soccer_words_block_without_any_lookup(self):
        api = FakeApi(fail_events={"epl-ars-che-2026-11-01"})
        clf = ww.Classifier(api, {}, ww.SportsIndex(), NOW)
        row = trade(NOW, 10, "epl-ars-che-2026-11-01", "Premier League: Arsenal vs. Chelsea")
        self.assertEqual(clf.classify(row), "soccer")

    def test_failed_lookup_is_held_back_not_allowed(self):
        api = FakeApi(fail_events={"texas-senate-election-winner"})
        clf = ww.Classifier(api, {}, ww.SportsIndex(), NOW)
        with self.assertRaises(ww.Unknown):
            clf.classify(trade(NOW, 10, "texas-senate-election-winner", "Texas Senate"))

    def test_south_carolina_and_series_a_are_not_soccer(self):
        row = trade(NOW, 10, "sc-01-house", "Will Republicans win SC-01? Series A funding for X?")
        self.assertNotEqual(ww.classify_event(None, row, ww.SportsIndex()), "soccer")

    def test_soccer_cannot_be_unblocked_in_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("[alerts]\nblock_soccer = false\n")
        try:
            alerts, _ = ww.load_config(f.name)
            self.assertNotIn("block_soccer", alerts)
        finally:
            os.unlink(f.name)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.state = ww.load_state("/nonexistent/state.json")

    def run_at(self, api, now, rec, alerts=ALERTS):
        return ww.run_once(api, alerts, WHALES, self.state, rec, now=now, log=lambda *a: None)

    def test_first_run_baselines_without_backlog(self):
        api = FakeApi(trades={WHALE: [trade(NOW - 60, 50_000, "texas-senate-election-winner", "Texas Senate")]})
        rec = Recorder()
        self.run_at(api, NOW, rec)
        self.assertEqual(len(rec.sent), 1)
        self.assertIn("Now watching", rec.sent[0]["message"])

    def test_split_order_alerts_once_with_total(self):
        rec = Recorder()
        api = FakeApi(trades={WHALE: []})
        self.run_at(api, NOW, rec)  # baseline
        rec.sent.clear()
        fills = [trade(NOW + 30 + i, 60, "texas-senate-election-winner", "Texas Senate", tx=f"0xa{i}")
                 for i in range(20)]  # 20 fills x $60 = $1,200
        api.trades[WHALE] = fills
        stats = self.run_at(api, NOW + 300, rec)
        self.assertEqual(stats["alerts"], 1)
        self.assertIn("BUY $1.2K", rec.sent[0]["title"])
        self.assertIn("now 74¢", rec.sent[0]["message"])
        self.assertEqual(rec.sent[0]["click"], "https://polymarket.com/event/texas-senate-election-winner")
        # Same data again: no repeat alert.
        stats = self.run_at(api, NOW + 600, rec)
        self.assertEqual(stats["alerts"], 0)
        # They add another $1,000: one more alert for the new money, with the running total.
        api.trades[WHALE] = fills + [trade(NOW + 700, 1000, "texas-senate-election-winner", "Texas Senate", tx="0xb")]
        self.run_at(api, NOW + 900, rec)
        self.assertEqual(len(rec.sent), 2)
        self.assertIn("BUY $1.0K", rec.sent[1]["title"])
        self.assertIn("$2.2K in this market", rec.sent[1]["message"])

    def test_soccer_sports_and_combos_never_alert(self):
        rec = Recorder()
        api = FakeApi(trades={WHALE: []})
        self.run_at(api, NOW, rec)
        rec.sent.clear()
        api.trades[WHALE] = [
            trade(NOW + 10, 180_000, "ballon-dor-winner-2026", "Will Harry Kane win the 2026 Ballon d'Or?", cond="s1"),
            trade(NOW + 11, 90_000, "kor-dae-vis-2026-10-26", "Daejeon Citizen vs. Vissel Kobe", cond="s2"),
            trade(NOW + 12, 50_000, "nba-gsw-den-2026-10-26", "Warriors vs. Nuggets", cond="s3"),
            trade(NOW + 13, 70_000, "texas-senate-election-winner", "Combo", cond="s4", is_combo=True),
        ]
        stats = self.run_at(api, NOW + 300, rec)
        self.assertEqual(rec.sent, [])
        self.assertEqual(stats["soccer_skipped"], 2)
        self.assertEqual(stats["sports_skipped"], 1)

    def test_other_sports_allowed_when_setting_off_but_soccer_still_blocked(self):
        alerts = dict(ALERTS, block_all_sports=False)
        rec = Recorder()
        api = FakeApi(trades={WHALE: []})
        self.run_at(api, NOW, rec, alerts)
        rec.sent.clear()
        api.trades[WHALE] = [
            trade(NOW + 10, 5_000, "nba-gsw-den-2026-10-26", "Warriors vs. Nuggets", cond="n1"),
            trade(NOW + 11, 5_000, "kor-dae-vis-2026-10-26", "Daejeon Citizen vs. Vissel Kobe", cond="k1"),
        ]
        self.run_at(api, NOW + 300, rec, alerts)
        self.assertEqual(len(rec.sent), 1)
        self.assertIn("Warriors", rec.sent[0]["message"])

    def test_unreachable_market_is_retried_then_sent(self):
        rec = Recorder()
        api = FakeApi(trades={WHALE: []}, fail_events={"texas-senate-election-winner"})
        self.run_at(api, NOW, rec)
        rec.sent.clear()
        api.trades[WHALE] = [trade(NOW + 10, 5_000, "texas-senate-election-winner", "Texas Senate")]
        stats = self.run_at(api, NOW + 300, rec)
        self.assertEqual((stats["alerts"], stats["held_back"]), (0, 1))
        api.fail_events.clear()
        stats = self.run_at(api, NOW + 600, rec)
        self.assertEqual(stats["alerts"], 1)

    def test_failed_send_is_retried(self):
        api = FakeApi(trades={WHALE: []})
        self.run_at(api, NOW, Recorder())
        api.trades[WHALE] = [trade(NOW + 10, 5_000, "fed-decision-in-october", "Fed")]
        self.run_at(api, NOW + 300, Recorder(ok=False))
        rec = Recorder()
        self.run_at(api, NOW + 600, rec)
        self.assertEqual(len(rec.sent), 1)

    def test_sells_can_be_muted(self):
        alerts = dict(ALERTS, include_sells=False)
        rec = Recorder()
        api = FakeApi(trades={WHALE: []})
        self.run_at(api, NOW, rec, alerts)
        rec.sent.clear()
        api.trades[WHALE] = [trade(NOW + 10, 5_000, "fed-decision-in-october", "Fed", side="SELL")]
        self.run_at(api, NOW + 300, rec, alerts)
        self.assertEqual(rec.sent, [])


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.state = ww.load_state("/nonexistent/state.json")

    def go(self, group):
        whales = [dict(WHALES[0], group=group)]
        api = FakeApi(trades={WHALE: []})
        rec = Recorder()
        ww.run_once(api, ALERTS, whales, self.state, rec, now=NOW, log=lambda *a: None)
        rec.sent.clear()
        api.trades[WHALE] = [
            trade(NOW + 10, 5_000, "nba-gsw-den-2026-10-26", "Warriors vs. Nuggets", cond="n"),
            trade(NOW + 11, 5_000, "texas-senate-election-winner", "Texas Senate", cond="t"),
            trade(NOW + 12, 90_000, "kor-dae-vis-2026-10-26", "Daejeon Citizen vs. Vissel Kobe", cond="k"),
            trade(NOW + 13, 90_000, "ballon-dor-winner-2026", "Will Harry Kane win the 2026 Ballon d'Or?", cond="b"),
        ]
        ww.run_once(api, ALERTS, whales, self.state, rec, now=NOW + 300, log=lambda *a: None)
        return rec.sent

    def test_sports_group_gets_only_non_soccer_sports(self):
        sent = self.go("sports")
        self.assertEqual(len(sent), 1)
        self.assertIn("Warriors", sent[0]["message"])
        self.assertTrue(sent[0]["title"].startswith("Sports · "))

    def test_events_group_gets_only_non_sports(self):
        sent = self.go("events")
        self.assertEqual(len(sent), 1)
        self.assertIn("Texas Senate", sent[0]["message"])
        self.assertTrue(sent[0]["title"].startswith("Events · "))

    def test_bad_group_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[[whales]]\nname = "x"\nwallet = "0x' + "b" * 40 + '"\ngroup = "soccer"\n')
        try:
            with self.assertRaises(SystemExit):
                ww.load_config(f.name)
        finally:
            os.unlink(f.name)

    def test_wanted_never_lets_soccer_through(self):
        for g in ("sports", "events", "all"):
            for block in (True, False):
                self.assertFalse(ww.wanted(g, "soccer", block))


class DigestTests(unittest.TestCase):
    def test_recap_groups_fills_and_drops_soccer(self):
        api = FakeApi(trades={WHALE: [
            trade(NOW - 3600, 400, "texas-senate-election-winner", "Texas Senate", tx="0x1"),
            trade(NOW - 3500, 400, "texas-senate-election-winner", "Texas Senate", tx="0x2"),
            trade(NOW - 3400, 90_000, "ballon-dor-winner-2026", "Will Harry Kane win the 2026 Ballon d'Or?", cond="s"),
            trade(NOW - 3300, 100, "fed-decision-in-october", "Fed", cond="f"),       # under the $500 floor
            trade(NOW - 90_000, 9_000, "fed-decision-in-october", "Fed", cond="old"),  # older than 24h
        ]})
        d = ww.build_digest(api, ALERTS, WHALES, 24, now=NOW, log=lambda *a: None)
        self.assertEqual(len(d["moves"]), 1)
        self.assertAlmostEqual(d["moves"][0]["usd"], 800)
        self.assertEqual(d["skipped"]["soccer"], 1)
        title, message, md = ww.format_digest(d)
        self.assertIn("1 big move(s)", title)
        self.assertNotIn("Kane", message + md)
        self.assertIn("now 74¢", message)

    def test_quiet_day(self):
        api = FakeApi(trades={WHALE: []})
        d = ww.build_digest(api, ALERTS, WHALES, 24, now=NOW, log=lambda *a: None)
        title, message, _ = ww.format_digest(d)
        self.assertIn("quiet day", title)
        self.assertIn("No big moves: TestWhale", message)


class ScoutTests(unittest.TestCase):
    def test_verdicts(self):
        good = {"alltime_pnl": 300_000, "last_trade_days": 0.5, "markets_ever": 1800, "biggest_win": 20_000,
                "join_ts": NOW - 400 * 86400, "short_term_share": 0, "flip_ratio": 0.05,
                "sports_share": 0, "soccer_share": 0}
        self.assertEqual(ww.verdict(good, now=NOW)[0], "GOOD")
        self.assertIn("loses money all-time", ww.verdict(dict(good, alltime_pnl=-4_900_000), now=NOW)[1])
        self.assertIn("inactive", ww.verdict(dict(good, last_trade_days=700), now=NOW)[1])
        self.assertEqual(ww.verdict(dict(good, markets_ever=2, join_ts=NOW - 86400), now=NOW)[0], "MAYBE")
        self.assertIn("mostly 5-minute crypto (bot-style)", ww.verdict(dict(good, short_term_share=0.8), now=NOW)[1])
        self.assertIn("mostly soccer, which you can't trade", ww.verdict(dict(good, soccer_share=0.9), now=NOW)[1])
        self.assertIn("bot: thousands of markets, tiny bets",
                      ww.verdict(dict(good, markets_ever=58_000, median_order=10), now=NOW)[1])
        self.assertEqual(ww.verdict(dict(good, markets_ever=7_500, median_order=800), now=NOW)[0], "GOOD")


class FormatTests(unittest.TestCase):
    def test_money(self):
        self.assertEqual(ww.money(850), "$850")
        self.assertEqual(ww.money(1_234), "$1.2K")
        self.assertEqual(ww.money(61_655), "$62K")
        self.assertEqual(ww.money(4_891_393), "$4.9M")
        self.assertEqual(ww.money(-4_891_393), "-$4.9M")

    def test_bad_wallet_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[[whales]]\nname = "x"\nwallet = "not-a-wallet"\n')
        try:
            with self.assertRaises(SystemExit):
                ww.load_config(f.name)
        finally:
            os.unlink(f.name)

    def test_starter_watchlist_loads(self):
        alerts, whales = ww.load_config(ww.CONFIG_PATH)
        self.assertGreaterEqual(len(whales), 3)
        self.assertTrue(alerts["block_all_sports"])
        self.assertEqual({w["group"] for w in whales}, {"events", "sports"})


class TelegramTests(unittest.TestCase):
    ENV = {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "111",
           "TELEGRAM_CHAT_SPORTS": "-100222", "TELEGRAM_CHAT_EVENTS": "-100333"}

    def make(self, env, fail=False):
        calls = []

        def poster(url, payload, headers=None):
            calls.append((url, payload, headers))
            if fail:
                raise ConnectionError("down")
            return {"ok": True}
        return ww.make_notifier(ww.DEFAULT_ALERTS, env=env, poster=poster, log=lambda *a: None), calls

    def test_routes_groups_to_their_chats(self):
        send, calls = self.make(self.ENV)
        send("Sports · X: BUY $5K", "Yes — <Lakers> & co", "https://polymarket.com/event/nba", None, 3, "sports")
        send("Events · Y: BUY $5K", "No — Senate", None, None, 3, "events")
        send("Whale recap", "quiet", None, None, 3)
        self.assertEqual([c[1]["chat_id"] for c in calls], ["-100222", "-100333", "111"])
        p = calls[0][1]
        self.assertIn("<b>Sports · X: BUY $5K</b>", p["text"])
        self.assertIn("&lt;Lakers&gt; &amp; co", p["text"])          # escaped for Telegram's HTML mode
        self.assertEqual(p["reply_markup"]["inline_keyboard"][0][0]["text"], "Open market")
        self.assertFalse(p["disable_notification"])

    def test_sells_are_silent(self):
        send, calls = self.make(self.ENV)
        send("X: SELL $5K", "m", None, None, 2, "events")
        self.assertTrue(calls[0][1]["disable_notification"])

    def test_group_chat_falls_back_to_default(self):
        send, calls = self.make({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "111"})
        send("t", "m", None, None, 3, "sports")
        self.assertEqual(calls[0][1]["chat_id"], "111")

    def test_both_channels_and_failure_reporting(self):
        send, calls = self.make(dict(self.ENV, NTFY_TOPIC="topic"))
        self.assertTrue(send("t", "m", None, None, 3))
        self.assertEqual(len(calls), 2)
        send_fail, _ = self.make(self.ENV, fail=True)
        self.assertFalse(send_fail("t", "m", None, None, 3))

    def test_sell_alert_priority_is_silent(self):
        whale = dict(WHALES[0], group="events")
        b = {"side": "SELL", "usd": 5000, "shares": 10000, "first_ts": NOW, "title": "T", "event_slug": "e",
             "outcome": "Yes"}
        self.assertEqual(ww.format_alert(whale, b, 5000, None, "UTC", NOW)[4], 2)

    def test_telegram_chats_lists_people_and_channels(self):
        updates = {"ok": True, "result": [
            {"message": {"chat": {"id": 111, "type": "private", "first_name": "Phin"}}},
            {"my_chat_member": {"chat": {"id": -100222, "type": "channel", "title": "Whales · Sports"}}},
            {"message": {"chat": {"id": 111, "type": "private", "first_name": "Phin"}}}]}
        chats = ww.telegram_chats("t", getter=lambda url: updates)
        self.assertEqual(sorted(c["id"] for c in chats), [-100222, 111])


if __name__ == "__main__":
    unittest.main()
