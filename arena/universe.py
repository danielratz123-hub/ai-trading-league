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
