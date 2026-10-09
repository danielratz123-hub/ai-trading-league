"""Tradable universe: S&P 500 members + a fixed set of major ETFs.

The S&P 500 list is a snapshot (index membership changes a few times a
year). Tickers Alpaca has no data for are simply skipped by the price
pipeline. Note for simulate mode: using today's members for past weeks has
survivorship bias, which is acceptable for testing the loop end-to-end.

Alpaca writes class shares with a dot (BRK.B, BF.B).
"""
from __future__ import annotations

_SP500_BY_SECTOR: dict[str, str] = {
    "Information Technology": """
        AAPL MSFT NVDA AVGO ORCL CRM ADBE AMD CSCO ACN IBM INTU NOW TXN QCOM
        AMAT MU LRCX ADI KLAC INTC ANET PANW CRWD SNPS CDNS APH MSI ROP FTNT
        ADSK NXPI MCHP TEL IT CTSH GLW HPQ HPE DELL MPWR KEYS ON CDW FICO TYL
        PTC FSLR SMCI NTAP STX WDC TER ZBRA TRMB GDDY VRSN JBL FFIV AKAM SWKS
        EPAM ENPH GEN PLTR APP DDOG WDAY""",
    "Communication Services": """
        GOOGL GOOG META NFLX TMUS DIS CMCSA VZ T CHTR EA TTWO WBD OMC IPG LYV
        FOXA FOX NWSA NWS MTCH TKO""",
    "Consumer Discretionary": """
        AMZN TSLA HD MCD LOW BKNG TJX SBUX NKE ABNB ORLY CMG AZO MAR HLT GM F
        ROST DHI LEN RCL YUM LULU EBAY TSCO DECK NVR PHM GRMN CCL EXPE ULTA
        DRI BBY GPC LKQ POOL KMX WSM TPR RL HAS MGM WYNN LVS CZR NCLH MHK
        APTV DPZ DASH""",
    "Consumer Staples": """
        WMT COST PG KO PEP PM MO MDLZ CL TGT KMB GIS KDP STZ SYY KR HSY ADM
        KHC MNST CHD CLX K MKC TSN HRL CAG SJM CPB LW BG TAP EL DG DLTR KVUE
        BF.B""",
    "Energy": """
        XOM CVX COP EOG SLB MPC PSX VLO OXY WMB KMI OKE FANG BKR HAL DVN CTRA
        EQT TRGP APA EXE""",
    "Financials": """
        BRK.B JPM V MA BAC WFC GS MS SCHW AXP BLK C SPGI PGR CB MMC ICE CME
        AON PYPL USB PNC COF TFC MCO AJG MET AIG AFL TRV ALL PRU AMP BK MSCI
        FIS ACGL HIG MTB FITB STT RJF TROW NDAQ WTW BRO CINF NTRS RF HBAN CFG
        KEY SYF CBOE PFG L EG WRB GL AIZ JKHY FDS BEN IVZ MKTX ERIE COIN HOOD
        KKR APO BX XYZ GPN CPAY""",
    "Health Care": """
        LLY UNH JNJ ABBV MRK TMO ABT ISRG DHR PFE AMGN BSX SYK VRTX GILD MDT
        BMY ELV CI REGN ZTS CVS BDX MCK HCA COR EW IDXX IQV A GEHC HUM CNC
        RMD DXCM MTD BIIB WST CAH STE ZBH LH DGX COO BAX HOLX WAT ALGN PODD
        RVTY INCY MRNA TECH CRL VTRS MOH UHS HSIC SOLV DVA""",
    "Industrials": """
        GE CAT RTX UNP HON ETN UBER BA DE LMT ADP UPS GD NOC WM TT PH ITW
        CTAS EMR TDG CSX MMM FDX NSC PCAR CARR JCI GWW CPRT RSG URI OTIS PAYX
        FAST CMI AME LHX VRSK ODFL PWR HWM ROK IR XYL DOV EFX WAB BR AXON
        LDOS HUBB VLTO BLDR J EXPD SNA TXT IEX MAS CHRW JBHT PNR NDSN ALLE
        AOS SWK DAL UAL LUV ROL HII GNRC PAYC DAY GEV TDY""",
    "Materials": """
        LIN SHW APD ECL FCX NEM CTVA DOW DD NUE PPG MLM VMC IFF LYB SW PKG IP
        AMCR STLD BALL AVY CF MOS ALB CE EMN""",
    "Real Estate": """
        PLD AMT EQIX WELL SPG PSA O DLR CCI CBRE EXR VICI AVB IRM EQR VTR SBAC
        WY INVH ESS MAA ARE KIM DOC UDR HST CPT REG BXP FRT""",
    "Utilities": """
        NEE SO DUK CEG SRE AEP D EXC PCG XEL PEG ED VST WEC EIX ETR DTE AEE
        PPL FE ES CNP CMS ATO NI NRG LNT EVRG PNW AES""",
}

ETFS: dict[str, str] = {
    "SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000", "DIA": "Dow 30",
    "XLK": "Technology", "XLF": "Financials", "XLV": "Health Care",
    "XLE": "Energy", "XLI": "Industrials", "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples", "XLU": "Utilities", "XLB": "Materials",
    "XLRE": "Real Estate", "XLC": "Communication Services",
    "TLT": "20+Y Treasuries", "IEF": "7-10Y Treasuries", "GLD": "Gold",
}

SECTOR_ETF: dict[str, str] = {
    "Information Technology": "XLK", "Communication Services": "XLC",
    "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
    "Energy": "XLE", "Financials": "XLF", "Health Care": "XLV",
    "Industrials": "XLI", "Materials": "XLB", "Real Estate": "XLRE",
    "Utilities": "XLU",
}

BENCHMARK = "SPY"


def _build() -> dict[str, str]:
    sectors: dict[str, str] = {}
    for sector, blob in _SP500_BY_SECTOR.items():
        for t in blob.split():
            sectors.setdefault(t, sector)   # first sector wins on duplicates
    for t in ETFS:
        sectors[t] = "ETF"
    return sectors


SECTOR_OF: dict[str, str] = _build()
SP500: list[str] = sorted(t for t, s in SECTOR_OF.items() if s != "ETF")
ALL_TICKERS: list[str] = sorted(SECTOR_OF)


def is_tradable(ticker: str) -> bool:
    return ticker in SECTOR_OF


def sector(ticker: str) -> str | None:
    return SECTOR_OF.get(ticker)


# --------------------------------------------------------------------------
# Refreshable S&P 500 list (python -m arena.main refresh-universe)
# --------------------------------------------------------------------------
# The lists above are the built-in snapshot. After a refresh, the current
# list lives in the universe_members table and load() swaps it in when a
# database is opened. The module-level dicts/lists are updated IN PLACE, so
# every module that imported them sees the new list.
#
# Tickers that leave the index stay in REMOVED: positions in them are
# never force-sold. They can be held or reduced (risk.validate_portfolio
# with `reduce_only`) and keep getting prices, but they can't be increased.

DEFAULT_SOURCE = ("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
                  "main/data/constituents.csv")
SOURCE_COMMITS = ("https://api.github.com/repos/datasets/s-and-p-500-companies/commits"
                  "?path=data/constituents.csv&per_page=1")
SNAPSHOT_LABEL = "built-in snapshot"
GICS_SECTORS = set(_SP500_BY_SECTOR)
REMOVED: dict[str, str] = {}          # ticker -> sector, left the index


def _swap_in(members: dict[str, str]) -> None:
    current = dict(members)
    for t in ETFS:
        current[t] = "ETF"
    SECTOR_OF.clear()
    SECTOR_OF.update(current)
    SP500[:] = sorted(t for t, s in SECTOR_OF.items() if s != "ETF")
    ALL_TICKERS[:] = sorted(SECTOR_OF)


def load(conn) -> None:
    """Use the database's list if it has one, else the built-in snapshot."""
    try:
        rows = conn.execute("SELECT ticker, sector, removed_on FROM universe_members").fetchall()
    except Exception:  # noqa: BLE001 - very old database without the table
        rows = []
    REMOVED.clear()
    if not rows:
        _swap_in({t: s for t, s in _build().items() if s != "ETF"})
        return
    _swap_in({r[0]: r[1] for r in rows if r[2] is None})
    REMOVED.update({r[0]: r[1] for r in rows if r[2] is not None and r[0] not in ETFS})


def list_info(conn) -> dict:
    """Which list is in use, for the dashboard and the briefing."""
    get = lambda k: (conn.execute("SELECT value FROM league_state WHERE key=?", (k,)).fetchone() or [None])[0]  # noqa: E731
    return {"list_date": get("universe_list_date"), "source": get("universe_source") or SNAPSHOT_LABEL,
            "refreshed_at": get("universe_refreshed_at"), "members": len(SP500), "removed": sorted(REMOVED)}


def parse_csv(text: str) -> list[tuple[str, str, str]]:
    """(ticker, name, sector) rows from the datasets/s-and-p-500-companies
    CSV (columns Symbol, Security, GICS Sector). Alpaca uses BRK.B, as the CSV does."""
    import csv
    import io
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        t = (r.get("Symbol") or "").strip().upper().replace("-", ".")
        if t:
            rows.append((t, (r.get("Security") or "").strip(), (r.get("GICS Sector") or "").strip()))
    return rows


def fetch(source: str = DEFAULT_SOURCE) -> tuple[list[tuple[str, str, str]], str | None]:
    """Download the list. Returns (rows, list_date): the date of the last
    change to the list (from the source's git history), when available."""
    import httpx
    if not source.startswith(("http://", "https://")):
        from pathlib import Path
        return parse_csv(Path(source).read_text(encoding="utf-8")), None
    r = httpx.get(source, timeout=30, follow_redirects=True)
    r.raise_for_status()
    list_date = None
    if source == DEFAULT_SOURCE:
        try:
            c = httpx.get(SOURCE_COMMITS, timeout=15, headers={"Accept": "application/vnd.github+json"})
            list_date = c.json()[0]["commit"]["committer"]["date"][:10]
        except Exception:  # noqa: BLE001 - the date is nice to have
            list_date = None
    return parse_csv(r.text), list_date


def refresh(conn, rows: list[tuple[str, str, str]], today: str, source: str,
            list_date: str | None) -> dict:
    """Store a new list. Returns {'added': [...], 'removed': [...], 'back': [...]}.
    Positions are never touched: removed tickers become hold-or-reduce only."""
    from .db import transaction
    if len(rows) < 400:
        raise ValueError(f"The new list has only {len(rows)} tickers; expected about 500. Not used.")
    bad = sorted({s for _, _, s in rows if s not in GICS_SECTORS})
    if bad:
        raise ValueError(f"Unknown sectors in the new list: {bad}. Not used.")
    new = {t: (name, s) for t, name, s in rows if t not in ETFS}
    existing = {r[0]: r for r in conn.execute("SELECT ticker, sector, removed_on FROM universe_members")}
    if not existing:
        # First refresh: start from the built-in snapshot, so tickers that
        # left the index since then are recorded as removed.
        existing = {t: (t, s, None) for t, s in _build().items() if s != "ETF"}
        with transaction(conn):
            conn.executemany("INSERT INTO universe_members(ticker, sector, name, added_on) VALUES (?,?,?,?)",
                             [(t, s, None, SNAPSHOT_LABEL) for t, (_, s, _) in existing.items()])
    current = {t for t, r in existing.items() if r[2] is None}
    added = sorted(set(new) - set(existing))
    back = sorted(t for t in new if t in existing and existing[t][2] is not None)
    removed = sorted(current - set(new))
    with transaction(conn):
        for t, (name, s) in new.items():
            conn.execute("INSERT INTO universe_members(ticker, sector, name, added_on) VALUES (?,?,?,?) "
                         "ON CONFLICT(ticker) DO UPDATE SET sector=excluded.sector, name=excluded.name, "
                         "removed_on=NULL", (t, s, name, today))
        conn.executemany("UPDATE universe_members SET removed_on=? WHERE ticker=?", [(today, t) for t in removed])
        for k, v in (("universe_list_date", list_date or today), ("universe_source", source),
                     ("universe_refreshed_at", today)):
            conn.execute("INSERT OR REPLACE INTO league_state(key, value) VALUES (?,?)", (k, v))
        from .db import log_event
        log_event(conn, "info", "universe", f"refreshed from {source} (list of {list_date or today}): "
                  f"+{len(added)} {added[:20]}, -{len(removed)} {removed[:20]}")
    load(conn)
    return {"added": added, "removed": removed, "back": back}
