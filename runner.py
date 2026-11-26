"""Scheduled job runner. Usage: python runner.py routine|window

Config comes from environment variables (BASE_URL, ACCT_USER, ACCT_PASS, ACCT_ZONE).
Public output is limited to step names and counters; details go to detail.log.
"""
import csv, os, re, sys, time, urllib.parse, urllib.request

BASE = os.environ["BASE_URL"].rstrip("/")
USER, PASS, ZONE = os.environ["ACCT_USER"], os.environ["ACCT_PASS"], os.environ.get("ACCT_ZONE", "1")
RATIO = float(os.environ.get("SAFE_RATIO", "1.3"))   # only fight players when all our stats >= RATIO x theirs
HERE = os.path.dirname(os.path.abspath(__file__))
DETAIL = open(os.path.join(HERE, "detail.log"), "a", encoding="utf-8")
STATS = {}
PACE = 0.5
SKIP = {x.strip() for x in os.environ.get("SKIP", "").split(",") if x.strip()}


class Stop(Exception):
    pass


def note(*a):
    DETAIL.write(time.strftime("%Y-%m-%d %H:%M:%S ", time.gmtime(time.time() + 7 * 3600)) + " ".join(str(x) for x in a) + "\n")
    DETAIL.flush()


def bump(step, ok=True):
    s = STATS.setdefault(step, [0, 0]); s[0 if ok else 1] += 1


def g(x, k, d=""):
    m = re.search(rf"<{k}>([^<]*)", x or "")
    return m.group(1) if m else d


def gi(x, k, d=0):
    try: return int(g(x, k, d))
    except ValueError: return d


def attr(tag, k, d=""):
    m = re.search(rf'\b{k}="([^"]*)"', tag or "")
    return m.group(1) if m else d


def code(x):
    m = re.search(r'<RENLONG code="(-?\d+)"', x or "")
    return m.group(1) if m else "?"


def reward(x):
    a = re.search(r"<award.*?</award>", x or "", re.S)
    return re.sub(r"<(cardlist|proplist|chipbag)></\1>|<(\w+)>0</\2>|<dropid>\d+</dropid>", "", a.group(0))[:300] if a else ""


class Client:
    def __init__(self):
        self.uid = self.sid = None

    def _post(self, path, params):
        req = urllib.request.Request(BASE + path, data=urllib.parse.urlencode(params).encode(),
                                     headers={"User-Agent": "Dalvik/2.1.0", "Accept": "application/xml"})
        last = None
        for i in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.read().decode("utf-8", "replace")
            except Exception as e:      # network hiccup: retry reads; never echo details publicly
                last = e; time.sleep(3 + 3 * i)
        raise Stop("network: " + type(last).__name__)

    def login(self):
        x = self._post("/xk_w_login", {"Cmd": 2, "Version": "2.7", "Session": "", "ServerID": ZONE, "Uid": 0, "UserName": USER, "Password": PASS})
        if code(x) != "0": raise Stop("login refused " + code(x))
        self.uid, self.sid = g(x, "userid"), g(x, "session")
        y = self.call("/rl_w_login2", 1)
        if code(y) != "0" or g(y, "hasreg") != "1": raise Stop("enter refused " + code(y))
        self.uid, self.sid = g(y, "uid"), g(y, "session")

    def call(self, path, cmd, **extra):
        p = {"Cmd": cmd, "Uid": self.uid, "Session": self.sid, "Clinettime": time.strftime("%Y/%m/%d %X %A"),
             "Platform": "android", "Version": "2.7", "Pt": 1503, "ServerID": ZONE, "GroupID": 3}
        p.update(extra)
        x = self._post(path, p)
        time.sleep(PACE)
        return x


C = Client()


def do(step, path, cmd, **extra):
    """A state-changing call that is expected to succeed."""
    x = C.call(path, cmd, **extra)
    ok = code(x) == "0"
    bump(step, ok)
    note(step, path, cmd, extra, "->", code(x), g(x, "message"), reward(x))
    return x if ok else ""


def me():
    x = C.call("/rl_r_mainpage", 1)
    if code(x) != "0": raise Stop("main refused " + code(x))
    return {k: gi(x, k) for k in ("level", "exp", "gold", "silver", "curaction", "pk_action", "attacklow", "defenselow", "chakralow")}


GOLD = None


def guard(where):
    """Abort the whole run if premium currency ever goes down."""
    global GOLD
    cur = me()["gold"]
    if GOLD is not None and cur < GOLD:
        note("ALERT premium currency dropped", GOLD, "->", cur, "after", where)
        raise Stop("guard tripped after " + where)
    GOLD = cur


# ---------------------------------------------------------------- claims
def claims():
    x = C.call("/rl_x_small_activity", 7, page=1)
    for it in re.findall(r"<item>(.*?)</item>", x, re.S):
        if g(it, "status") == "0": do("sign", "/rl_x_small_activity", 8, SignId=g(it, "sign_id"))
    x = C.call("/rl_r_vip_daily_gift", 1)
    if code(x) == "0" and attr(re.search(r"<vipgift[^>]*>", x).group(0) if "<vipgift" in x else "", "claimed") == "0":
        do("gift", "/rl_w_vip_daily_gift", 1)
    packs()
    x = C.call("/rl_x_award_msg", 1)
    if code(x) == "0" and re.search(r"<msglist>.*?<id>", x, re.S): do("mail", "/rl_x_award_msg", 3)
    x = C.call("/beast_r_silverconsume", 1)
    for i, st in re.findall(r'<item id="(\d+)" cash="\d+" status="(\d+)"', x):
        if st == "1": do("spend-tier", "/beast_w_silverconsume", 1, ID=i)
    chests()


def packs():
    x = C.call("/rl_r_activity", 9000)
    if code(x) != "0": return
    rest = x.split("</conf>")[-1]
    m = re.search(r'<activity desc="conlogin"[^>]*>', rest)
    if m and attr(m.group(0), "got") == "0":
        do("streak", "/rl_w_activity", 9100, ActType=5, ActID=1, SubID=min(int(attr(m.group(0), "condays", "1")), 7))
    for typ in ("1", "2"):
        blk = re.search(rf'<activity type="{typ}"[^>]*>(.*?)</activity>', rest, re.S)
        for sub in re.findall(r'<sub subid="(\d+)"', blk.group(1) if blk else ""):
            do("pack", "/rl_w_activity", 9100, ActType=typ, ActID=typ, SubID=sub)


def chests():
    x = C.call("/rl_r_dailytask", 1)
    for i, st in re.findall(r'<award id="(\d+)" cost="\d+" status="(\d+)"', x):
        if st == "4": do("chest", "/rl_r_dailytask", 2, AwardID=i)
    x = C.call("/rl_r_multiple_task", 1)
    for i in re.findall(r'<task id="(\d+)"[^>]*can_recv="1"', x):
        do("bonus", "/rl_r_multiple_task", 2, AwardID=i)
    return C.call("/rl_r_dailytask", 1)


# ---------------------------------------------------------------- free daily actions
def freebies():
    x = C.call("/rl_r_wheel", 1401)
    if code(x) == "0" and gi(x, "freetimes") > 0 and gi(x, "lasttime") > 0: do("wheel", "/rl_w_wheel", 1400)
    x = C.call("/rl_x_small_activity", 12)
    if code(x) == "0" and gi(x, "left_free_count") > 0: do("trove", "/rl_x_small_activity", 13)
    gate()
    x = C.call("/rl_r_comm", 3)
    for _ in range(8):
        if code(x) != "0" or gi(x, "mylife") <= 0 or gi(x, "resttime") <= 0: break
        ids = re.findall(r"<npc><id>(\d+)", x)
        pos = max(1, min(gi(x, "npcpos", 1), len(ids)))
        if not ids or not do("rps", "/rl_w_comm", 4, GambleType=1, GambleMustWin=0, GambleNPCID=ids[pos - 1], MoraChoice=1 + int(time.time()) % 3): break
        x = C.call("/rl_r_comm", 3)


def gate():
    for _ in range(4):
        x = C.call("/rl_w_eight_gate", 3, version=2)
        if code(x) != "0": return
        if gi(x, "uncollect_soul") > 0:
            if gi(x, "free_left_count") > 0 and gi(x, "multiplications_used") == 0:
                do("gate-x", "/rl_w_eight_gate", 5, multitype=2, version=2)
            do("gate-take", "/rl_w_eight_gate", 6, version=2)
            continue
        if gi(x, "left_count") <= 0 or me()["silver"] < 50000: return
        if not do("gate", "/rl_w_eight_gate", 4, traintype=1, version=2): return


def extras():
    """Daily tasks that only need free things: the free recruit and opening chests with keys we already hold."""
    prog = dict(re.findall(r'<task id="(\d+)" process="(\d+)"', C.call("/rl_r_dailytask", 1)))
    if int(prog.get("14", "0")) < 1:
        x = C.call("/rl_w_recruit", 1900, Type=1, Free=1)      # refused by the server until the free timer is ready
        note("draw", code(x), g(x, "message"), reward(x)[:120])
        if code(x) == "0": bump("draw")
    need = 2 - int(prog.get("3", "0"))                         # seals: claim finished ones, then start ready ones; never pay to skip the timer
    if need > 0:
        p = C.call("/rl_r_next_plunder", 1)
        seals = [(attr(t, "id"), attr(t, "state")) for t in re.findall(r"<seal [^>]*>", p)]
        for cmd, state in ((2801, "complete"), (2800, "ready")):
            for sid, st in seals:
                if need <= 0 or st != state: continue
                x = C.call("/rl_w_markchip", cmd, MarkID=sid, UseCash=0)
                note("seal", cmd, sid, code(x), g(x, "message") or attr(x, "message"))
                if code(x) == "0":
                    bump("seal"); need -= 1
                else:
                    break
    left = 3 - int(prog.get("15", "0"))
    for gid in ("24", "23", "22"):                             # bronze, then silver, then gold keys
        while left > 0:
            c = C.call("/rl_w_propuse", 1804)
            tag = next((i for i in re.findall(r"<item [^>]*>", c) if attr(i, "propid") == gid), "")
            if int(attr(tag, "amount", "0") or 0) <= 0: break   # no key: opening would cost premium currency
            if not do("box", "/rl_w_propuse", 1803, Goodsid=gid): break
            left -= 1


# ---------------------------------------------------------------- towers ("Battles")
def tower():
    d = C.call("/rl_r_dup", 7002)
    if code(d) != "0": return
    for i in re.findall(r'<milestone id="(\d+)"[^>]*status="ready"', d):
        do("tower-star", "/rl_w_dup", 3101, AwardID=i)
    cnt = re.search(r"<dup_count>.*?</dup_count>", d, re.S)
    remain = gi(cnt.group(0), "remain") if cnt else 0
    ok = [i + 1 for i, x in enumerate(re.findall(r"<dup [^>]*>", d)) if attr(x, "selectable") == "2"]
    if remain > 0 and ok:                                # daily sweeps go to the highest cleared towers
        do("sweep", "/rl_w_dup", 3102, DupId="|".join(str(i) for i in ok[-remain:]))
    head = re.search(r"<duplist[^>]*>", d)
    if not head: return
    top = int(attr(head.group(0), "maxchap", "0"))
    for ch in range(1, top + 1):                         # every floor of every tower can be fought once a day
        for r in range(1, 8):
            x = C.call("/rl_w_dup", 3100, Chap=ch, Round=r)
            m = re.search(r"<preview[^>]*>", x)
            won = code(x) == "0" and m and attr(m.group(0), "result") == "1"
            if not won:
                if code(x) == "0": note("tower", ch, r, "lost")
                break
            bump("tower"); note("tower", ch, r, reward(x))


# ---------------------------------------------------------------- stamina
def levels():
    with open(os.path.join(HERE, "levels.csv")) as f:
        return {int(r["level"]): (int(r["need"]), int(r["cap"])) for r in csv.DictReader(f)}


def story(limit=60):
    stuck = 0
    for _ in range(limit):
        a = C.call("/rl_r_adventure", 1)
        m = re.search(r"<chapinfo[^>]*>", a)
        if not m: return
        ch, st, pos = attr(m.group(0), "currentchap"), attr(m.group(0), "currentstage"), attr(m.group(0), "pos")
        before = me()["curaction"]
        x = C.call("/rl_w_pve", 3, ChapID=ch, StageID=st, Pos=pos, WastCashBoss=0)
        if code(x) != "0":
            note("story stop", ch, st, pos, code(x), g(x, "message")); return
        p = re.search(r"<pve[^>]*>", x)
        after = int(attr(p.group(0), "curaction", str(before))) if p else before
        moved = p and (attr(p.group(0), "curstage"), attr(p.group(0), "pos")) != (st, pos)
        bump("story"); note("story", ch, st, pos, "stamina", before, "->", after, reward(x))
        stuck = 0 if (moved or after != before) else stuck + 1
        if stuck >= 1:                                   # frontier fight lost: spend stamina on an earlier stage instead
            note("story: frontier not passable now, replaying earlier stage"); replay(int(ch), int(st), limit); return


def replay(ch, st, limit):
    ch, st = (ch, st - 1) if st > 1 else (ch - 1, 1)
    if ch < 1: return
    for _ in range(limit):
        s = C.call("/rl_r_next_story", 1, Chapter=ch, Stage=st)
        nodes = [n for n in re.findall(r"<node[^>]*>", s) if attr(n, "available") == "1" and attr(n, "boss") == "0"]
        if not nodes: note("replay: nothing open in", ch, st); return
        n = nodes[0]
        if me()["curaction"] < int(attr(n, "stamina_cost", "999")): return
        x = C.call("/rl_w_pve", 3, ChapID=ch, StageID=st, Pos=attr(n, "position_id"), WastCashBoss=0)
        if code(x) != "0":
            note("replay stop", code(x), g(x, "message")); return
        bump("replay"); note("replay", ch, st, attr(n, "position_id"), reward(x))


def noodles():
    """Free refill: only worth taking when stamina is nearly empty (it sets stamina to the cap)."""
    x = C.call("/rl_r_next_ichiraku", 1)
    m = re.search(r"<ichiraku[^>]*>", x)
    if m and attr(m.group(0), "available") == "1" and int(attr(m.group(0), "remaining", "0")) > 0 and me()["curaction"] < 10:
        return bool(do("noodles", "/rl_w_ramen", 1))
    return False


# ---------------------------------------------------------------- versus players (only when clearly stronger)
def safe(mine, a, d, c):
    return mine["attacklow"] >= RATIO * a and mine["defenselow"] >= RATIO * d and mine["chakralow"] >= RATIO * c


def spar():
    """Targets sorted by silver; fight one only if our attack beats its defense and it is not stronger than us.
    Up to 5 fights per target; spar points are refilled with pills up to the daily pill limit."""
    mine = me()
    lim = C.call("/rl_r_energy_limits", 1)
    tag = re.search(r"<hyourou[^>]*>", lim)
    pills_left = int(attr(tag.group(0), "rest", "0")) if tag else 0
    stock = {i: int(c) for i, c in re.findall(r'<tool id="(\d+)" count="(\d+)"', lim)}
    x = C.call("/rl_r_pk", 1600, Times=1)
    cand = []
    for u in re.findall(r"<user>(.*?)</user>", x, re.S):
        if mine["attacklow"] > gi(u, "defenseHigh") and gi(u, "attackhigh") < mine["attacklow"]:
            cand.append((gi(u, "coin"), g(u, "id")))
    cand.sort(reverse=True)
    note("spar candidates", len(cand), "pills left today", pills_left, "top", cand[:3])
    pk, idx, losses, done = mine["pk_action"], 0, 0, {}
    while idx < len(cand):
        if pk <= 0:
            gid = next((i for i in ("6", "5") if stock.get(i, 0) > 0), None)
            if pills_left <= 0 or not gid: break
            r = do("pill", "/rl_w_propuse", 1800, Goodsid=gid)
            if not r: break
            pills_left -= 1; stock[gid] -= 1
            pk = me()["pk_action"]
            if pk <= 0: break
            continue
        coin, uid = cand[idx]
        r = C.call("/rl_w_pvp", 2701, ToUid=uid)
        if code(r) != "0":
            note("spar skip", uid, code(r), g(r, "message")); idx += 1; continue
        pk -= 1
        m = re.search(r"<preview[^>]*>", r)
        won = bool(m) and attr(m.group(0), "result") == "1"
        bump("spar", won); note("spar", uid, "listed", coin, "win" if won else "LOSS", reward(r)[:160])
        if not won:
            losses += 1; idx += 1
            if losses >= 2:
                note("spar: two losses, stopping"); break
            continue
        done[uid] = done.get(uid, 0) + 1
        if done[uid] >= 5: idx += 1


def ladder():
    """Daily tasks need 5 challenges and 1000 reputation; only clearly weaker players are challenged."""
    prog = dict(re.findall(r'<task id="(\d+)" process="(\d+)"', C.call("/rl_r_dailytask", 1)))
    if int(prog.get("4", "0")) >= 5 and int(prog.get("19", "0")) >= 1000: return
    mine = me()
    for _ in range(5):
        x = C.call("/rl_r_sport", 8100)
        if code(x) != "0" or gi(x, "todayresttime") <= 0 or gi(x, "restbatter") <= 0 or gi(x, "resttime") > 0: return
        pick = None
        for u in sorted(re.findall(r"<user>(.*?)</user>", x, re.S), key=lambda u: gi(u, "rank")):
            if g(u, "host") == "1": continue
            n = C.call("/rl_r_nickinfo", 5000, Nick=g(u, "nick"))
            if code(n) == "0" and safe(mine, gi(n, "attackhigh"), gi(n, "defenseHigh"), gi(n, "chakrahigh")):
                pick = g(u, "rank"); break
        if not pick:
            note("ladder: nobody clearly weaker in sight"); return
        r = do("ladder", "/rl_w_sport", 8200, ToRank=pick)
        m = re.search(r"<preview[^>]*>", r or "")
        if not m or attr(m.group(0), "result") != "1":
            note("ladder: not a win, stopping"); return


# ---------------------------------------------------------------- upkeep with soft currency only
def upkeep(tasks):
    prog = dict(re.findall(r'<task id="(\d+)" process="(\d+)"', tasks or ""))
    if me()["silver"] < 200000: return
    if int(prog.get("18", "0")) < 1 or int(prog.get("20", "0")) < 3:
        do("stock", "/rl_w_shopbuy", 1701, Packageid=1, Count=1)
    need = 3 - int(prog.get("20", "0"))
    if need <= 0: return
    bag = C.call("/rl_r_next_bag", 1)
    cards = [(g(c, "type"), g(c, "id"), g(c, "seq"), gi(c, "level")) for c in re.findall(r"<card>(.*?)</card>", bag, re.S)]
    mats = [s for t, i, s, _ in cards if t == "1" and i.startswith("1000")]
    team = C.call("/rl_r_ninjalist", 1).split("<defenseninjalist>")[0]
    seqs = {s for s in re.findall(r"<ninjaid>(\d+)", team) if s != "0"}
    targets = sorted((lv, s) for t, i, s, lv in cards if t == "1" and s in seqs)
    for lv, src in targets:
        while need > 0 and len(mats) >= 3:
            batch, mats = mats[:3], mats[3:]
            lst = ",".join(batch)
            p = C.call("/rl_r_next_levelup", 1, SRCBagID=src, EATCardsBagIDList=lst)
            tag = re.search(r"<preview[^>]*>", p)
            if code(p) != "0" or not tag or attr(tag.group(0), "applied_exp") != attr(tag.group(0), "material_exp"):
                mats = batch + mats; break       # would waste material on this card: try the next one
            if do("train", "/rl_w_next_strengthen", 1, SRCBagID=src, EATCardsBagIDList=lst, Quote=attr(tag.group(0), "quote")):
                need -= 1
        if need <= 0: break


def trial():
    bag = C.call("/rl_r_next_bag", 1)
    order = [s for _, s in sorted(((gi(c, "level"), g(c, "seq")) for c in re.findall(r"<card>(.*?)</card>", bag, re.S)
                                   if g(c, "type") == "1" and not g(c, "id").startswith("1000")), reverse=True)][:8]
    for _ in range(6):
        won = False
        for seq in order:
            e = C.call("/rl_x_exam", 1)
            if code(e) != "0" or gi(e, "left_count") <= 0: return
            dead = re.findall(r"<bag_index>(\d+)", (re.search(r"<dead_list>.*?</dead_list>", e, re.S) or [""])[0] if "<dead_list>" in e else "")
            if seq in dead: continue
            if code(C.call("/rl_x_exam", 2, card_index_list=seq)) != "0": continue
            do("trial", "/rl_x_exam", 5)
            if g(C.call("/rl_x_exam", 1), "is_win") == "1":
                won = True; break
        if not won: return
        do("trial-box", "/rl_x_exam", 6, box_index=1)      # box 1 is the free one
        if not do("trial-next", "/rl_x_exam", 7): return


# ---------------------------------------------------------------- timed window
def window():
    def info():
        x = C.call("/rl_w_xiao", 1)
        return x, g(x, "inact") == "1", gi(x, "resttime", 60)
    limit = float(os.environ.get("MAX_WAIT") or 3600)
    x, open_, rest = info()
    if not open_:
        if rest > limit:
            note("window: not opening soon", rest); return
        note("window: waiting", rest)
        if rest > 180:                                   # stay off the server while waiting
            time.sleep(rest - 120); C.login(); globals()["GOLD"] = None
        t0 = time.time()
        while True:
            x, open_, rest = info()
            if open_: break
            if time.time() - t0 > 900:
                note("window: did not open", rest); return
            time.sleep(min(30, max(2, rest)))
    note("window open, rest", rest)
    for _ in range(3):                                   # soft-currency boost, capped by the server
        if not do("boost", "/rl_w_xiao", 9, AddAttackType=1): break
    end = time.time() + rest + 30
    while time.time() < end:
        r = C.call("/rl_w_xiao_war", 2)
        ok = code(r) == "0"
        bump("hit", ok)
        x, open_, _ = info()
        if not open_: break
        cd = gi(x, "cdresttime")
        if not ok and cd == 0: time.sleep(5)
        time.sleep(max(1, cd + 1) if cd else 1)
    x, _, _ = info()
    note("window done", {k: g(x, k) for k in ("totlehurt", "score", "rank", "coinadd")})


def war():
    def info():
        x = C.call("/rl_r_warnew", 7200)
        tag = re.search(r"<preview[^>]*>", x)
        return x, bool(tag) and attr(tag.group(0), "open") == "1", int(attr(tag.group(0), "resttime", "60") or 60) if tag else 60
    limit = float(os.environ.get("MAX_WAIT") or 3600)
    x, open_, rest = info()
    if not open_:
        if rest > limit:
            note("war: not opening soon", rest); return
        note("war: waiting", rest)
        if rest > 180:
            time.sleep(rest - 120); C.login(); globals()["GOLD"] = None
        t0 = time.time()
        while True:
            x, open_, rest = info()
            if open_: break
            if time.time() - t0 > 900:
                note("war: did not open", rest); return
            time.sleep(min(30, max(2, rest)))
    note("war open", x[:700])
    t0, fails, seen = time.time(), 0, 0
    while time.time() - t0 < 50 * 60:
        x, open_, _ = info()
        if not open_: break
        u = (re.search(r"<user>.*?</user>", x, re.S) or [""])[0] if "<user>" in x else ""
        if gi(u, "restalive", 1) <= 0:
            time.sleep(60); continue                     # never buy lives; wait in case they come back
        cd = gi(u, "resttime")
        if cd > 0:
            time.sleep(min(cd + 1, 120)); continue
        r = C.call("/rl_w_warnew", 4102)                 # plain attack; 4101/4103/4104 cost premium currency and are never used
        ok = code(r) == "0"
        bump("war", ok)
        if seen < 6 or not ok:
            seen += 1; note("war hit", code(r), g(r, "message"), re.sub(r"<roundlist>.*?</roundlist>", "", r, flags=re.S)[:500])
        if not ok:
            fails += 1
            if fails >= 8: break
            time.sleep(10)
        guard("war")
    x, _, _ = info(); note("war done", (re.search(r"<user>.*?</user>", x, re.S) or [""])[0][:600] if "<user>" in x else x[:300])
    res = C.call("/rl_r_warresultnew", 8100); note("war result", res[:600])


def training():
    """Double-exp pill, then replay the latest stage we can win; refill stamina with owned noodles only."""
    global PACE
    lim = C.call("/rl_r_energy_limits", 1)
    tag = re.search(r"<ramen[^>]*>", lim)
    eats = int(attr(tag.group(0), "rest", "0")) if tag else 0
    stock = {i: int(c) for i, c in re.findall(r'<tool id="(\d+)" count="(\d+)"', lim)}
    a = C.call("/rl_r_adventure", 1)
    m = re.search(r"<chapinfo[^>]*>", a)
    if not m: return
    ch, st = int(attr(m.group(0), "currentchap")), int(attr(m.group(0), "currentstage"))
    stamina = me()["curaction"]
    ich = re.search(r"<ichiraku[^>]*>", C.call("/rl_r_next_ichiraku", 1))
    free = int(attr(ich.group(0), "remaining", "0")) if ich and attr(ich.group(0), "available") == "1" else 0
    boosted = os.environ.get("BOOST", "1") != "0" and bool(do("x2", "/rl_w_propuse", 1800, Goodsid=32))
    until = time.time() + 295
    PACE = 0.15
    backs = 0
    try:
        while backs <= 4 and ch >= 1:
            s = C.call("/rl_r_next_story", 1, Chapter=ch, Stage=st)
            nodes = [n for n in re.findall(r"<node[^>]*>", s) if attr(n, "available") == "1"]
            if not nodes:
                note("training: nothing open in", ch, st); ch, st = (ch, st - 1) if st > 1 else (ch - 1, 1); backs += 1; continue
            n = nodes[0]; cost = int(attr(n, "stamina_cost", "999"))
            if stamina < cost:
                if boosted and time.time() > until:
                    note("training: boost over, keeping remaining noodles"); break
                if free > 0:                             # the two free daily refills go first (they fill to the cap)
                    r = do("noodles", "/rl_w_ramen", 1)
                    free -= 1
                    if r:
                        stamina = gi(r, "bodyvalue", stamina); continue
                gid = next((i for i in ("4", "3") if stock.get(i, 0) > 0), None)
                if eats <= 0 or not gid:
                    note("training: out of stamina and refills"); break
                r = do("noodle", "/rl_w_propuse", 1800, Goodsid=gid)
                if not r: break
                eats -= 1; stock[gid] -= 1; stamina = gi(r, "bodyvalue", stamina)
                continue
            x = C.call("/rl_w_pve", 3, ChapID=ch, StageID=st, Pos=attr(n, "position_id"), WastCashBoss=0)
            f = re.search(r"<preview[^>]*>", x)
            lost = code(x) != "0" or (f and attr(f.group(0), "result") == "0")
            if lost:                                     # cannot win here: step back one stage
                note("training: cannot pass", ch, st, attr(n, "position_id"), code(x), g(x, "message"))
                ch, st = (ch, st - 1) if st > 1 else (ch - 1, 1); backs += 1; continue
            pv = re.search(r"<pve[^>]*>", x)
            stamina = int(attr(pv.group(0), "curaction", str(stamina - cost))) if pv else stamina - cost
            bump("train"); note("train", ch, st, attr(n, "position_id"), "stamina", stamina, reward(x)[:120])
    finally:
        PACE = 0.5
    note("training done", me(), "boost left s", int(until - time.time()))


def closing():
    """Last run of the day: collect chests, then take every money-tree shake so the silver arrives as late as possible."""
    global GOLD
    chests()
    before = me()["gold"]; paid = 0
    for _ in range(10):
        x = C.call("/rl_r_mtree", 0)
        if code(x) != "0" or gi(x, "usedTimes") >= gi(x, "timesperday"): break
        free = gi(x, "freeTimes") > 0
        if not free and gi(x, "shake_cost", 999) > 10:
            note("tree: price above 10, leaving it"); break
        if not do("tree", "/rl_w_mtree", 0): break
        paid += 0 if free else 1
    after = me()["gold"]
    note("tree: paid shakes", paid, "premium", before, "->", after)
    if before - after > 10 * paid:
        raise Stop("tree cost more than expected")
    GOLD = None                                          # the agreed tree cost is not a guard violation
    chests()


def hunt():
    """Fight stage bosses whose featured reward is a 5-star card we do not own yet.
    Free attempts first; paid replays only inside GOLD_BUDGET (premium currency per run, default 0)."""
    global GOLD, PACE
    scan_only = os.environ.get("HUNT_SCAN") == "1"
    budget = int(os.environ.get("GOLD_BUDGET") or 0)
    bag = C.call("/rl_r_next_bag", 1)
    owned = {(g(c, "type"), g(c, "id")) for c in re.findall(r"<card>(.*?)</card>", bag, re.S)}
    m = re.search(r"<chapinfo[^>]*>", C.call("/rl_r_adventure", 1))
    if not m: return
    top = (int(attr(m.group(0), "currentchap")), int(attr(m.group(0), "currentstage")))
    start_gold = me()["gold"]; spent = 0; todo = []; got = 0
    PACE = 0.15
    try:
        for ch in range(1, top[0] + 1):
            for st in range(1, 30):
                if (ch, st) > top: break
                s = C.call("/rl_r_next_story", 1, Chapter=ch, Stage=st)
                if code(s) != "0" or "<node" not in s: break
                for node, feat in re.findall(r"(<node [^>]*>)\s*(<featured_reward [^>]*>)?", s):
                    if attr(node, "boss") == "0" or attr(feat, "stars") != "5": continue
                    key = (attr(feat, "type"), attr(feat, "id"))
                    if key in owned: continue
                    todo.append((ch, st, attr(node, "position_id"), attr(feat, "name"), attr(node, "available"), attr(node, "replay_gold")))
                    if scan_only or attr(node, "available") != "1": continue
                    price = int(attr(node, "replay_gold", "999"))
                    for _ in range(40):
                        x = C.call("/rl_w_pve", 3, ChapID=ch, StageID=st, Pos=attr(node, "position_id"), WastCashBoss=0)
                        if code(x) == "40003" and budget - spent >= price:      # free attempts used up
                            x = C.call("/rl_w_pve", 3, ChapID=ch, StageID=st, Pos=attr(node, "position_id"), WastCashBoss=1)
                            if code(x) == "0": spent += price
                        if code(x) != "0":
                            note("hunt stop", ch, st, code(x), g(x, "message")); break
                        f = re.search(r"<preview[^>]*>", x)
                        if f and attr(f.group(0), "result") == "0":
                            note("hunt: lost at", ch, st); break
                        bump("hunt")
                        if re.search(rf"<type>{key[0]}</type><id>{key[1]}</id>", x):
                            owned.add(key); got += 1; note("hunt: got", attr(feat, "name"), "at", ch, st); break
                    if code(C.call("/rl_r_mainpage", 1)) != "0": raise Stop("session lost")
    finally:
        PACE = 0.5
    note("hunt summary: wanted", len(todo), "got", got, "paid", spent, todo[:60])
    now = me()["gold"]
    if start_gold - now > spent:
        raise Stop("hunt cost more than recorded")
    GOLD = None


def routine():
    for name, fn in (("claims", claims), ("freebies", freebies), ("extras", extras), ("tower", tower), ("spar", spar), ("ladder", ladder)):
        fn(); guard(name)
    if "story" not in SKIP:
        story(); guard("story")
        packs()                                          # level rewards appear right after a level-up
        if noodles():
            story(); guard("story2"); packs()
    tasks = chests()
    upkeep(tasks); guard("upkeep")
    trial(); guard("trial")
    chests()
    note("state", me())


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "routine"
    rc = 0
    hold = os.environ.get("NOT_BEFORE", "").strip()
    if mode == "routine" and hold and time.time() < float(hold):
        print("held"); return
    try:
        C.login(); guard("start"); note("=== run", mode, me())
        {"window": window, "war": war, "train": training, "close": closing, "hunt": hunt}.get(mode, routine)()
        guard("end")
    except Stop as e:
        rc = 1; note("STOP", e); print("stopped:", str(e).split(":")[0])
    except Exception as e:                               # never print response data publicly
        rc = 1; note("CRASH", repr(e)); print("crashed:", type(e).__name__)
    for k, (ok, bad) in STATS.items():
        print(f"{k}: ok={ok} failed={bad}")
    print("done" if rc == 0 else "finished with problems")
    sys.exit(rc)


if __name__ == "__main__":
    main()
