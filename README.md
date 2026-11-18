# flaskhals-data

Produktion per kraftslag för SE1–SE4 och grannområdena (NO1–5, DK1–2, FI, EE, LV, LT, DE, PL, NL) från ENTSO-E (A75, faktisk produktion) till flaskhals.nu.
GitHub Actions kör `hamta_elpris.py` var 15:e minut. Tokenen ligger som hemligheten `ENTSOE_TOKEN`.

- `data/SE1.json`–`SE4.json`: de senaste 6 timmarna, skrivs över varje körning.
- `data/historik/<zon>/<ÅÅÅÅ-MM>.json`: alla kvartar för månaden (UTC-tider, MW). Nya kvartar läggs till.
- `data/historik/<zon>/dygn.json`: dygnssnitt per kraftslag, med antal kvartar per dygn (96 = komplett).

Läses via GitHub Pages, till exempel https://floppyaidev.github.io/flaskhals-data/data/historik/SE3/dygn.json
