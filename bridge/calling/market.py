"""The international market (Arms Against Tyranny): what each country has put on sale,
putting our equipment on sale, buying from another country. Found 26.10 on 1.19.3.

CEquipmentVariantPool (vtable 0x438ECB8, 0x40 bytes; ctor 0xCFADF0 (this); add 0xCFE290
(this, CEquipmentVariant*, amount x1e5)): +0x08 a per-type index (24-byte entries),
+0x20 CPdxArray<{CEquipmentVariant*; int64 amount x1e5}>, +0x38 a flag. The country's
stockpile is one (production subsystem +0x200), its convoys another (CCountry+0x11D8);
its MARKET STOCKPILE (what it offers for sale) sits at +0x38 of a heap object with
(1, allocator, 0, owner tag index) at +0x08 (found by a heap scan). The pool near
CCountry+0x111000 is NOT ours on sale (it held Sweden's offers: a view of some market).

Selling: NInternationalMarket::CMarketStockpileEquipmentTransferCommand (0xB0 bytes, ctor
0xE2D040 (this, tag, pool A, pool B)): the UI passes the equipment chosen as A and an empty
pool as B. CanExecute (slot 10) checks both pools against the country.

Buying: NInternationalMarket::CRequestEquipmentPurchaseAction (0x170 bytes, ctor 0xDFEE60
(this, contract definition*, scope*)), a diplomatic action. The UI builds a draft with
0xE0A780 (out, &seller, &buyer, pool, subsidies CPdxArray, 1) and the definition with
0xDFABB0 (out, draft, prices pool (empty), price levels std::map (empty)); the game then
prices it with 0xDFACC0 (definition). Definition: +0x40 the draft (seller tag, buyer
tag), +0x48 its pool, +0x88 subsidies, +0xB0 price levels, +0xE8/+0xF0 the price.
"""
import struct

VT_POOL, POOL_SIZE, POOL_CTOR, POOL_ADD = 0x438ECB8, 0x40, 0xCFADF0, 0xCFE290
POOL_ITEMS = 0x20
ALLOC = 0x4505128                 # the default CPdxArray allocator object
MARKET_FROM, MARKET_TO = 0x110000, 0x112000
TRANSFER_CTOR, TRANSFER_SIZE, VT_CANEXE = 0xE2D040, 0xB0, 10
DRAFT_FN, DEFN_FN, PURCHASE_CTOR, PURCHASE_SIZE = 0xE0A780, 0xDFABB0, 0xDFEE60, 0x170
RECALC_FN = 0xDFACC0                # (definition): totals at +0xF0 (cost), +0xE8 (after subsidies)
FIXED = 1e5
_OFF = {}


MS_OWNER, MS_POOL = 0x20, 0x38    # the market stockpile object: owner tag, its pool
_STOCKS = {}                      # pid -> {tag index: pool address}


def _scan_stocks(g):
    """{tag index: pool} of every country's market stockpile: a heap scan (~7 s) for
    pools inside an object with (1, allocator, 0, owner tag) at +0x08 (26.10: the 5 rifles
    we put on sale were found there, and USA's 80 P-36A as the game shows them)."""
    p = g.p
    vt = struct.pack("<Q", p.base + VT_POOL)
    out = {}
    for m in p.regions():
        if "rw" not in m["perms"] or m["path"] not in ("", "[heap]"):
            continue
        a = m["start"]
        while a < m["end"]:
            n = min(64 << 20, m["end"] - a)
            b = p.try_read(a, n)
            if b:
                i = b.find(vt)
                while i != -1:
                    if i % 8 == 0 and i >= MS_POOL:
                        one, alloc, z, tag = struct.unpack_from("<QQQi", b, i - MS_POOL + 8)
                        if one == 1 and alloc == p.base + ALLOC and z == 0 and 0 < tag < 2000:
                            out[tag] = a + i
                    i = b.find(vt, i + 1)
            a += n
    _STOCKS[p.pid] = out
    return out


def market_pool(g, tag_index):
    """The pool of a country's market stockpile (by tag index), or None."""
    p = g.p
    st = _STOCKS.get(p.pid)
    ok = st is not None and all(
        p.u64(x) == p.base + VT_POOL and p.i32(x - MS_POOL + MS_OWNER) == t
        for t, x in list(st.items())[:5])
    if not ok:
        st = _scan_stocks(g)
    x = st.get(tag_index)
    if x is None and len(st) and tag_index not in st:
        return None
    return x


def pool_items(g, pool):
    """[(variant ptr, equipment, design, amount)] of a pool."""
    import industry
    p = g.p
    g._load_equipment()
    d, n = p.u64(pool + POOL_ITEMS), p.i32(pool + POOL_ITEMS + 0xC)
    out = []
    for i in range(max(0, min(n, 500))):
        v, amt = struct.unpack("<qq", p.read(d + 16 * i, 16))
        if v and amt:
            out.append((v, g._eq_by_id.get(p.i32(v + industry.EV_TYPE_ID)),
                        p.stdstring(v + industry.EV_NAME) or "", amt / FIXED))
    return out


def on_sale(g, tag=None):
    """What a country has put on the market: [(variant ptr, equipment, design, amount)]."""
    import diplomacy as D
    pool = market_pool(g, D.tag_index(g, tag or g.player().tag))
    return pool_items(g, pool) if pool else []


def offers(g, exclude=(), equipment=None):
    """{tag: [(variant ptr, equipment, design, amount)]} of every country with something
    on sale (pure reads)."""
    import diplomacy as D
    market_pool(g, -1)                       # fill the cache once
    idx = {}
    for t in g.tags():
        try:
            idx[D.tag_index(g, t)] = t
        except Exception:
            continue
    out = {}
    for ti, pool in _STOCKS.get(g.p.pid, {}).items():
        t = idx.get(ti)
        if not t or t in exclude:
            continue
        try:
            items = pool_items(g, pool)
        except Exception:
            continue
        if equipment:
            items = [x for x in items if x[1] and x[1].startswith(equipment)]
        if items:
            out[t] = items
    return out


def _pool(g, items=()):
    """A pool in the game's memory, built by its own functions."""
    import api
    for f in (POOL_CTOR, POOL_ADD):
        api.allow_call(f)
    pool = g.malloc(POOL_SIZE)
    g.p.write(pool, b"\0" * POOL_SIZE)
    g.call(POOL_CTOR, [pool])
    for v, amount in items:
        g.call(POOL_ADD, [pool, v, int(round(amount * FIXED))])
    return pool


def _can(g, cmd):
    import api
    p = g.p
    fn = p.u64(p.u64(cmd) + 8 * VT_CANEXE) - p.base
    api.allow_call(fn)
    return bool(g.call(fn, [cmd, 0]) & 0xFF)


def put_on_sale(g, variant_ptr, amount, dry_run=False):
    """Move `amount` of one of our designs from our stockpile to our market stockpile,
    where buyers find it. (ok, why)."""
    import api
    own = g._inj is None
    if own:
        g.attach()
    try:
        a = _pool(g, [(variant_ptr, amount)])
        b = _pool(g)
        api.allow_call(TRANSFER_CTOR)
        cmd = g.malloc(TRANSFER_SIZE)
        g.p.write(cmd, b"\0" * TRANSFER_SIZE)
        g.call(TRANSFER_CTOR, [cmd, g.player().tag_id, a, b])
        if not _can(g, cmd):
            return False, "the game refuses (not so much in our stockpile?)"
        if dry_run:
            return True, "allowed (dry run)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own:
            g.detach()


def buy(g, seller, variant_ptr, amount, dry_run=False):
    """Ask `seller` for `amount` of a design it has on sale (a purchase contract, paid
    in civilian factories). (ok, why, price)."""
    import api
    import diplomacy as D
    p = g.p
    own = g._inj is None
    if own:
        g.attach()
    try:
        for f in (DRAFT_FN, DEFN_FN, PURCHASE_CTOR, RECALC_FN):
            api.allow_call(f)
        pool = _pool(g, [(variant_ptr, amount)])
        tags = g.malloc(8)
        p.write(tags, struct.pack("<ii", D.tag_index(g, seller), D.tag_index(g, g.player().tag)))
        subs = g.malloc(0x18)
        p.write(subs, struct.pack("<QiiQ", 0, 0, 0, p.base + ALLOC))
        draft = g.malloc(0x200)
        p.write(draft, b"\0" * 0x200)
        g.call(DRAFT_FN, [draft, tags, tags + 4, pool, subs, 1])
        prices = _pool(g)
        lv = g.malloc(0x30)        # an empty std::map: compare, header (colour, parent,
        hdr = lv + 8               # left, right), count
        p.write(lv, struct.pack("<QiiQQQQ", 0, 0, 0, 0, hdr, hdr, 0)[:0x30])
        defn = g.malloc(0x200)
        p.write(defn, b"\0" * 0x200)
        g.call(DEFN_FN, [defn, draft, prices, lv])
        g.call(RECALC_FN, [defn])           # the UI prices it in a separate call
        act = g.malloc(PURCHASE_SIZE)
        p.write(act, b"\0" * PURCHASE_SIZE)
        g.call(PURCHASE_CTOR, [act, defn, D._scope(g)])
        totals = (p.i64(defn + 0xE8) / FIXED, p.i64(defn + 0xF0) / FIXED)
        ok, why = D._post_action(g, act, "buying from %s" % seller, dry_run)
        return ok, why, totals
    finally:
        if own:
            g.detach()
