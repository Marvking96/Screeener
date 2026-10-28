import json
import os
import time
from collections import Counter
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

# ---- Einstellungen (hier kannst du später Werte ändern) ----
LISTE = "aktien.csv"   # wird von liste_erstellen.py angelegt
AUSGABE = os.path.join("docs", "ergebnis.json")   # diese Datei liest die Web-App
KURS_ORDNER = os.path.join("docs", "kurse")       # je Aktie eine kleine Datei mit Kerzen (für Charts ohne Signal)
ZEITRAUM = "1y"        # so viel Kursverlauf wird geladen
N = 5                  # Fenster: so viele Tage links und rechts werden verglichen
MAX_ALTER_TAGE = 5     # Signale, die älter sind (in Handelstagen), werden nicht angezeigt
MAX_ABSTAND_PROZENT = 5  # aktive Setups werden nur gezeigt, wenn der Kurs höchstens so weit vom Ausbruch entfernt ist
BLOCK = 100            # so viele Aktien werden pro Abruf geladen
KURS_TAGE = 160        # so viele Handelstage (Open/High/Low/Close) bekommt jeder Treffer für den Chart in der App
VOLUMEN_TAGE = 20      # so viele Handelstage zählen für das durchschnittliche Handelsvolumen
WAEHRUNG = {"S&P 500": "USD", "DAX 40": "EUR"}   # Währung der Kurse je Index (Yahoo liefert die Heimatwährung)
MAX_NACHLADEN = 150    # so viele Aktien mit fehlendem letzten Handelstag werden einzeln nachgeladen
# -------------------------------------------------------------


def finde_wendepunkte(close, n):
    """Findet Hochs und Tiefs anhand der Schlusskurse.
    Ein Hoch: der Schlusskurs ist der höchste im Fenster von n Tagen davor bis n Tagen danach.
    Ein Tief: der Schlusskurs ist der niedrigste in diesem Fenster."""
    werte = close.values
    hochs, tiefs = [], []
    for i in range(n, len(werte) - n):
        fenster = werte[i - n : i + n + 1]
        if fenster.max() == fenster.min():      # komplett flaches Fenster: kein Wendepunkt
            continue
        if werte[i] == fenster.max():
            hochs.append(i)
        if werte[i] == fenster.min():
            tiefs.append(i)
    return hochs, tiefs


def abwechselnd(close, hochs, tiefs):
    """Sorgt dafür, dass sich Hochs und Tiefs immer abwechseln.
    Folgen zwei Hochs direkt aufeinander (ohne Tief dazwischen), bleibt nur das höhere.
    Folgen zwei Tiefs direkt aufeinander, bleibt nur das tiefere."""
    punkte = sorted([(i, "Hoch") for i in hochs] + [(i, "Tief") for i in tiefs])
    ergebnis = []
    for i, art in punkte:
        if ergebnis and ergebnis[-1][1] == art:
            j = ergebnis[-1][0]
            if art == "Hoch":
                besser = close.iloc[i] > close.iloc[j]
            else:
                besser = close.iloc[i] < close.iloc[j]
            if besser:
                ergebnis[-1] = (i, art)
        else:
            ergebnis.append((i, art))
    return ergebnis


def finde_ausbruch(werte, ab_index, ebene2, ebene3, bis_index):
    """Sucht den ersten Schlusskurs nach Punkt 3, der über Punkt 2 liegt.
    Fällt vorher ein Schlusskurs unter Punkt 3, gibt es kein Signal."""
    for j in range(ab_index + 1, bis_index + 1):
        if werte[j] < ebene3:
            return None
        if werte[j] > ebene2:
            return j
    return None


def suche_123(close, punkte, richtung):
    """Prüft die Kette 1-2-3-2-3... und sucht den Ausbruch über Punkt 2.
    richtung "auf": 1 = Tief, 2 = Hoch, 3 = höheres Tief ... Signal: Schlusskurs ÜBER Punkt 2.
    richtung "ab": alles spiegelverkehrt. Signal: Schlusskurs UNTER Punkt 2.
    Intern werden die Kurse im Abwärtsfall mit -1 multipliziert, dann gilt dieselbe Logik."""
    vz = 1 if richtung == "auf" else -1
    tief_art = "Tief" if richtung == "auf" else "Hoch"
    werte = [vz * float(x) for x in close.values]

    phase = None            # None, "nach_1", "nach_2", "nach_3"
    anker = None            # letzter gültiger Punkt 1 oder 3
    ebene2 = ebene3 = idx3 = None
    kette = []              # aktuelle Kette als (Position, Nummer)
    ketten = []             # fertige Ketten mit mindestens 1-2-3
    signale = []            # (Tag des Ausbruchs, Punkt 2, Punkt 3)

    for i, art in punkte:
        k = werte[i]
        if art == tief_art:
            if phase is None or (phase == "nach_1" and k < anker):
                anker = k
                kette = [(i, 1)]
                phase = "nach_1"
            elif phase == "nach_2":
                if k > anker:                       # höheres Tief -> Punkt 3
                    ebene3, idx3, anker = k, i, k
                    kette.append((i, 3))
                    phase = "nach_3"
                else:                               # tieferes Tief -> Kette gebrochen
                    if any(n == 3 for _, n in kette):
                        ketten.append(kette)
                    anker, kette, phase = k, [(i, 1)], "nach_1"
            elif phase == "nach_3" and k < ebene3:  # tiefer als Punkt 3 -> gebrochen
                ketten.append(kette)
                anker, kette, phase = k, [(i, 1)], "nach_1"
        else:
            if phase == "nach_1":                   # erstes Hoch nach 1 -> Punkt 2
                ebene2 = k
                kette.append((i, 2))
                phase = "nach_2"
            elif phase == "nach_2" and k > ebene2:  # höheres Hoch vor Punkt 3 -> 2 wandert
                ebene2 = k
                kette[-1] = (i, 2)
            elif phase == "nach_3" and k > ebene2:  # neues höheres Hoch -> neuer Punkt 2
                j = finde_ausbruch(werte, idx3, ebene2, ebene3, i)
                if j is not None:
                    signale.append((j, vz * ebene2, vz * ebene3))
                ebene2 = k
                kette.append((i, 2))
                phase = "nach_2"
            # ein niedrigeres Hoch zwischen 3 und dem nächsten 2 wird ignoriert

    zustand = None
    if phase == "nach_3":                           # offenes Setup am Ende der Daten
        j = finde_ausbruch(werte, idx3, ebene2, ebene3, len(werte) - 1)
        if j is not None:
            signale.append((j, vz * ebene2, vz * ebene3))
        else:
            zustand = (vz * ebene2, vz * ebene3)    # wartet auf Ausbruch
    if any(n == 3 for _, n in kette):
        ketten.append(kette)
    return ketten, signale, zustand


def waehle_kette(ketten, close, e3):
    """Wählt die Kette, deren Punkt 3 genau den Kurs e3 hat (sonst die letzte Kette)."""
    for kette in reversed(ketten):
        for i, nr in kette:
            if nr == 3 and abs(float(close.iloc[i]) - e3) < 1e-6:
                return kette
    return ketten[-1] if ketten else []


def pruefe_aktie(close):
    """Prüft eine Aktie in beide Richtungen. Gibt eine Liste von Treffern zurück
    (frische Signale und aktive Setups mit Kette für den Chart)."""
    hochs, tiefs = finde_wendepunkte(close, N)
    punkte = abwechselnd(close, hochs, tiefs)
    letzter = len(close) - 1
    kurs = float(close.iloc[-1])
    treffer = []
    for richtung in ("auf", "ab"):
        ketten, signale, zustand = suche_123(close, punkte, richtung)
        for j, e2, e3 in signale:
            alter = letzter - j
            if alter <= MAX_ALTER_TAGE:
                treffer.append({"art": "signal", "richtung": "long" if richtung == "auf" else "short",
                                "signal_index": j, "tage_her": alter, "punkt2": e2, "punkt3": e3,
                                "kurs": kurs, "kette": waehle_kette(ketten, close, e3)})
        if zustand:
            abstand = abs(zustand[0] / kurs - 1) * 100
            if abstand <= MAX_ABSTAND_PROZENT:
                treffer.append({"art": "setup", "richtung": "long" if richtung == "auf" else "short",
                                "signal_index": None, "abstand": abstand, "punkt2": zustand[0],
                                "punkt3": zustand[1], "kurs": kurs,
                                "kette": waehle_kette(ketten, close, zustand[1])})
    return treffer


def hole_eurusd():
    """Holt den letzten Wechselkurs: wie viele US-Dollar ein Euro kostet. Bei Fehlern (None, None)."""
    for versuch in range(3):
        try:
            kurse = yf.Ticker("EURUSD=X").history(period="10d")["Close"].dropna()
            kurs = float(kurse.iloc[-1])
            if 0.5 < kurs < 2:
                return round(kurs, 5), kurse.index[-1].strftime("%Y-%m-%d")
        except Exception as fehler:
            print(f"  Wechselkurs: Versuch {versuch + 1} fehlgeschlagen ({fehler})")
        time.sleep(1)
    return None, None


def volumen_kennzahlen(serien, indizes):
    """Durchschnittliches Handelsvolumen der letzten VOLUMEN_TAGE Tage je Aktie.
    Die Einstufung hoch/mittel/niedrig ist ein Drittel-Vergleich innerhalb desselben Index (nach Umsatz = Stück x Kurs).
    Hinweis: Yahoo liefert bei deutschen Aktien nur das Xetra-Volumen, nicht den Handel an allen Börsen."""
    kennzahlen, je_index = {}, {}
    for ticker, df in serien.items():
        letzte = df.iloc[-VOLUMEN_TAGE:]
        stueck = float(letzte["Volume"].mean())
        umsatz = float((letzte["Volume"] * letzte["Close"]).mean())
        kennzahlen[ticker] = {"volumen": round(stueck / 1000, 1), "umsatz": round(umsatz / 1e6, 2), "volumen_klasse": None}
        if umsatz > 0:
            je_index.setdefault(indizes.get(ticker, "-"), []).append((umsatz, ticker))
    for gruppe in je_index.values():
        gruppe.sort(reverse=True)
        m = len(gruppe)
        for rang, (_, ticker) in enumerate(gruppe):
            kennzahlen[ticker]["volumen_klasse"] = "hoch" if rang < m / 3 else ("niedrig" if rang >= 2 * m / 3 else "mittel")
    return kennzahlen


def kerzen_daten(df):
    """Die letzten KURS_TAGE Kerzen im Format der App: Datum, Eröffnung, Hoch, Tief, Schluss, Volumen (Tausend Stück)."""
    fenster = df.iloc[-KURS_TAGE:]
    stellen = 3 if float(df["Close"].iloc[-1]) < 5 else 2      # sehr billige Aktien brauchen eine Stelle mehr

    def runde(spalte):
        return [round(float(x), stellen) for x in fenster[spalte].values]

    return {"d": [d.strftime("%Y-%m-%d") for d in fenster.index], "o": runde("Open"), "h": runde("High"),
            "l": runde("Low"), "c": runde("Close"), "v": [int(round(float(x) / 1000)) for x in fenster["Volume"].values]}


def schreibe_kursdateien(serien):
    """Schreibt docs/kurse/<Kürzel>.json für jede Aktie und löscht Dateien von Aktien, die nicht mehr dabei sind."""
    if not serien:
        return
    os.makedirs(KURS_ORDNER, exist_ok=True)
    namen = set()
    for ticker, df in serien.items():
        datei = ticker.replace("/", "_") + ".json"
        namen.add(datei)
        with open(os.path.join(KURS_ORDNER, datei), "w", encoding="utf-8") as f:
            json.dump(kerzen_daten(df), f, separators=(",", ":"))
    for datei in os.listdir(KURS_ORDNER):
        if datei.endswith(".json") and datei not in namen:
            os.remove(os.path.join(KURS_ORDNER, datei))
    print(f"Kursdateien für {len(namen)} Aktien in {KURS_ORDNER} geschrieben")


def baue_eintrag(ticker, name, index_name, df, t):
    """Wandelt einen Treffer in einen Eintrag für die JSON-Datei um (inklusive Kerzendaten für den Chart)."""
    close = df["Close"]
    fenster = df.iloc[-KURS_TAGE:]
    start_pos = len(df) - len(fenster)
    stellen = 3 if t["kurs"] < 5 else 2

    eintrag = {
        "kuerzel": ticker, "name": name, "index": index_name, "waehrung": WAEHRUNG.get(index_name, "EUR"),
        "richtung": t["richtung"],
        "stand": close.index[-1].strftime("%Y-%m-%d"),
        "punkt2": round(t["punkt2"], 2), "punkt3": round(t["punkt3"], 2), "kurs": round(t["kurs"], 2),
        "kurse": kerzen_daten(df),
        "kette": [{"d": close.index[i].strftime("%Y-%m-%d"), "nr": nr, "c": round(float(close.iloc[i]), stellen)}
                  for i, nr in t["kette"] if i >= start_pos],
    }
    if t["art"] == "signal":
        eintrag["signaltag"] = close.index[t["signal_index"]].strftime("%Y-%m-%d")
        eintrag["tage_her"] = t["tage_her"]
    else:
        eintrag["abstand"] = round(t["abstand"], 1)
    return eintrag


FELDER = ("Open", "High", "Low", "Close", "Volume")


ROHFELDER = FELDER + ("Adj Close",)


def lade_block(tickers):
    """Lädt Open/High/Low/Close/Volumen für einen Block von Aktien. Gibt je Feld eine Tabelle zurück (eine Spalte je Aktie).
    Die Kurse kommen unbereinigt; die Bereinigung um Dividenden/Splits macht baue_serie selbst. Grund: Yahoo liefert bei
    europäischen Aktien für den letzten Tag oft noch keinen bereinigten Kurs ("Adj Close"). Mit auto_adjust=True wird
    der Schlusskurs dieses Tages dann leer und der Tag fällt weg."""
    daten = yf.download(tickers, period=ZEITRAUM, auto_adjust=False, progress=False, threads=True)
    tabellen = {}
    for feld in ROHFELDER:
        if feld in daten:
            tabelle = daten[feld]
        elif feld == "Adj Close":
            tabelle = daten["Close"]                  # ohne bereinigte Kurse: unbereinigt verwenden
        else:
            tabelle = daten["Close"] * 0              # fehlt das Volumen, wird 0 eingesetzt
        if isinstance(tabelle, pd.Series):
            tabelle = tabelle.to_frame(tickers[0])
        tabellen[feld] = tabelle
    return tabellen


def bereinige(df):
    """Bereinigt eine Kerzen-Tabelle (Spalten Open/High/Low/Close/Adj Close/Volume) um Dividenden und Splits wie
    auto_adjust: alle Kurse mal (Adj Close / Close). Fehlt der Faktor an einem Tag (typisch: der letzte Tag),
    gilt der vom Vortag. Datum ohne Uhrzeit und Zeitzone."""
    df = df[df["Close"].notna()].copy()
    if "Adj Close" in df:
        faktor = (df["Adj Close"] / df["Close"]).where(lambda f: (f > 0) & (f < 10))
        faktor = faktor.ffill().bfill().fillna(1.0)
        for feld in ("Open", "High", "Low", "Close"):
            df[feld] = df[feld] * faktor
        df = df.drop(columns=["Adj Close"])
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def baue_serie(tabellen, ticker):
    """Macht aus den Tabellen eine Kerzen-Tabelle für eine Aktie. Tage ohne Schlusskurs fallen weg.
    Fehlt bei einem Tag Open/High/Low, wird der Schlusskurs eingesetzt."""
    df = bereinige(pd.DataFrame({feld: tabellen[feld][ticker] for feld in ROHFELDER}))
    for feld in ("Open", "High", "Low"):
        df[feld] = df[feld].fillna(df["Close"])
    df["Volume"] = df["Volume"].fillna(0)
    df["High"] = df[["High", "Open", "Close"]].max(axis=1)   # Sicherheitsnetz gegen unstimmige Daten
    df["Low"] = df[["Low", "Open", "Close"]].min(axis=1)
    return df


def zeige_tabelle(eintraege, art):
    """Gibt die Treffer als Tabelle im Terminal aus."""
    if not eintraege:
        print("Keine Treffer.")
        return
    zeilen = []
    for e in eintraege:
        z = {"Aktie": e["kuerzel"], "Name": e["name"][:24], "Index": e["index"],
             "Richtung": "Long (aufwärts)" if e["richtung"] == "long" else "Short (abwärts)"}
        if art == "signal":
            z.update({"Signaltag": datetime.strptime(e["signaltag"], "%Y-%m-%d").strftime("%d.%m.%Y"),
                      "Tage her": e["tage_her"], "Punkt 2": e["punkt2"], "Punkt 3": e["punkt3"],
                      "Schlusskurs": e["kurs"]})
        else:
            z.update({"Ausbruch bei": e["punkt2"], "Schlusskurs": e["kurs"], "Abstand %": e["abstand"]})
        zeilen.append(z)
    print(pd.DataFrame(zeilen).to_string(index=False))


def lade_serien(tickers):
    """Lädt alle Kurse blockweise. Gibt die Kursreihen je Aktie und die Aktien ohne Daten zurück."""
    serien, ohne_daten = {}, []
    gesamt = (len(tickers) + BLOCK - 1) // BLOCK
    for b in range(0, len(tickers), BLOCK):
        block = tickers[b : b + BLOCK]
        nr = b // BLOCK + 1
        try:
            tabellen = lade_block(block)
        except Exception as fehler:
            ohne_daten += block
            print(f"  Block {nr}/{gesamt}: Abruf fehlgeschlagen ({fehler})")
            continue
        for ticker in block:
            if ticker not in tabellen["Close"].columns:
                ohne_daten.append(ticker)
                continue
            df = baue_serie(tabellen, ticker)
            if len(df) < 40:
                ohne_daten.append(ticker)
                continue
            serien[ticker] = df
        print(f"  Block {nr}/{gesamt} fertig")
    return serien, ohne_daten


def referenzdatum(serien):
    """Der neueste Handelstag, den die Datenquelle für mindestens 10 Aktien liefert."""
    zaehler = Counter(s.index[-1] for s in serien.values())
    kandidaten = [d for d, n in zaehler.items() if n >= min(10, len(serien))]
    return max(kandidaten) if kandidaten else None


def hole_nach(serien, ref):
    """Lädt Aktien, denen der neueste Handelstag fehlt, einzeln nach.
    Gibt die Aktien zurück, die danach immer noch einen älteren Datenstand haben."""
    veraltet = [t for t, s in serien.items() if s.index[-1] < ref]
    if not veraltet:
        return []
    print(f"\n{len(veraltet)} Aktien haben nicht den neuesten Handelstag ({ref:%d.%m.%Y}).")
    if len(veraltet) > MAX_NACHLADEN:
        print("  Zu viele für einen Einzelabruf (vermutlich Feiertag an einer Börse). Sie werden markiert.")
        return veraltet
    for i, t in enumerate(veraltet):
        zeigen = i < 3                                   # für die ersten drei steht im Protokoll, was geliefert wurde
        try:
            neu = baue_serie(lade_block([t]), t)
            if zeigen:
                print(f"    {t}: Einzelabruf liefert bis {neu.index[-1]:%d.%m.%Y}")
            if len(neu) >= 40 and neu.index[-1] > serien[t].index[-1]:
                serien[t] = neu
                continue
        except Exception as fehler:
            if zeigen:
                print(f"    {t}: Einzelabruf fehlgeschlagen ({fehler})")
        time.sleep(0.5)
        # zweiter Weg: Ticker.history mit festem Zeitraum; nur die fehlenden neuen Tage werden angehängt
        try:
            roh = yf.Ticker(t).history(start=(ref - pd.Timedelta(days=30)).strftime("%Y-%m-%d"),
                                        end=(ref + pd.Timedelta(days=3)).strftime("%Y-%m-%d"), auto_adjust=False)
            roh = roh.reindex(columns=list(ROHFELDER))
            if roh["Adj Close"].isna().all():
                roh["Adj Close"] = roh["Close"]
            roh["Volume"] = roh["Volume"].fillna(0)
            kurz = bereinige(roh)
            for feld in ("Open", "High", "Low"):
                kurz[feld] = kurz[feld].fillna(kurz["Close"])
            if zeigen:
                print(f"    {t}: history liefert bis {kurz.index[-1]:%d.%m.%Y}" if len(kurz) else f"    {t}: history leer")
            neue_tage = kurz[kurz.index > serien[t].index[-1]]
            if len(neue_tage):
                serien[t] = pd.concat([serien[t], neue_tage[serien[t].columns]])
        except Exception as fehler:
            if zeigen:
                print(f"    {t}: history fehlgeschlagen ({fehler})")
        time.sleep(0.3)
    rest = [t for t, s in serien.items() if s.index[-1] < ref]
    print(f"  Nachgeladen: {len(veraltet) - len(rest)}, weiterhin mit älterem Datenstand: {len(rest)}")
    return rest


def main():
    start = time.time()
    liste = pd.read_csv(LISTE, sep=";", encoding="utf-8-sig")
    tickers = list(liste["Kürzel"])
    namen = dict(zip(liste["Kürzel"], liste["Name"]))
    indizes = dict(zip(liste["Kürzel"], liste["Index"]))
    print(f"{len(tickers)} Aktien in {LISTE}. Lade Kurse in Blöcken zu {BLOCK} ... (yfinance {getattr(yf, '__version__', '?')})")

    serien, ohne_daten = lade_serien(tickers)
    ref = referenzdatum(serien)
    veraltet = hole_nach(serien, ref) if ref is not None else []

    vol = volumen_kennzahlen(serien, indizes)

    signale, setups = [], []
    for ticker, df in serien.items():
        try:
            treffer = pruefe_aktie(df["Close"])
        except Exception as fehler:
            print(f"  Fehler bei {ticker}: {fehler}")
            ohne_daten.append(ticker)
            continue
        for t in treffer:
            eintrag = baue_eintrag(ticker, str(namen.get(ticker, "-")), indizes.get(ticker, "-"), df, t)
            eintrag.update(vol.get(ticker, {}))
            (signale if t["art"] == "signal" else setups).append(eintrag)

    geprueft = len(tickers) - len(ohne_daten)
    if geprueft < len(tickers) * 0.5:
        # zu viele Ausfälle: lieber das alte Ergebnis behalten als ein halbes zu überschreiben
        print(f"\nFEHLER: Nur {geprueft} von {len(tickers)} Aktien konnten geprüft werden. "
              "Die Ergebnisdatei wird nicht überschrieben.")
        raise SystemExit(1)

    # Kurzliste aller geprüften Aktien (für Suche und Watchlist in der App)
    alle = []
    for ticker, df in serien.items():
        close = df["Close"]
        kurs = float(close.iloc[-1])
        vorher = float(close.iloc[-2]) if len(close) > 1 else kurs
        alle.append({"kuerzel": ticker, "name": str(namen.get(ticker, "-")), "index": indizes.get(ticker, "-"),
                     "waehrung": WAEHRUNG.get(indizes.get(ticker, "-"), "EUR"), "kurs": round(kurs, 2), "stand": close.index[-1].strftime("%Y-%m-%d"),
                     **vol.get(ticker, {}),
                     "veraenderung": round((kurs / vorher - 1) * 100, 2) if vorher else 0.0})
    alle.sort(key=lambda a: a["name"].lower())

    eur_usd, eur_usd_datum = hole_eurusd()
    print(f"Wechselkurs EUR/USD: {eur_usd} ({eur_usd_datum})" if eur_usd else "Wechselkurs konnte nicht geladen werden (Euro-Anzeige in der App ist dann aus).")

    signale.sort(key=lambda e: (e["tage_her"], e["kuerzel"]))
    setups.sort(key=lambda e: e["abstand"])
    ergebnis = {
        "erstellt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "letzter_handelstag": ref.strftime("%Y-%m-%d") if ref is not None else None,
        "anzahl_gesamt": len(tickers), "anzahl_geprueft": geprueft,
        "einstellungen": {"N": N, "max_alter_tage": MAX_ALTER_TAGE, "max_abstand_prozent": MAX_ABSTAND_PROZENT,
                          "volumen_tage": VOLUMEN_TAGE},
        "veraltet": veraltet,
        "ohne_daten": ohne_daten,
        "signale": signale, "setups": setups,
        "alle": alle,
        "eur_usd": eur_usd, "eur_usd_datum": eur_usd_datum,
    }
    os.makedirs(os.path.dirname(AUSGABE), exist_ok=True)
    schreibe_kursdateien(serien)
    with open(AUSGABE, "w", encoding="utf-8") as f:
        json.dump(ergebnis, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\nFertig nach {time.time() - start:.0f} Sekunden. Geprüft: {geprueft} von {len(tickers)} Aktien.")
    print(f"Ergebnis gespeichert in {AUSGABE} ({os.path.getsize(AUSGABE) // 1024} KB)")
    print(f"\n=== SIGNALE der letzten {MAX_ALTER_TAGE} Handelstage (Ausbruch über/unter Punkt 2) ===")
    zeige_tabelle(signale, "signal")
    print(f"\n=== AKTIVE SETUPS (1-2-3 steht, Ausbruch fehlt noch, höchstens {MAX_ABSTAND_PROZENT}% entfernt), nächste zuerst ===")
    zeige_tabelle(setups, "setup")
    if veraltet:
        print(f"\nHINWEIS: {len(veraltet)} Aktien haben einen älteren Datenstand als {ref:%d.%m.%Y}: "
              + ", ".join(veraltet[:20]) + (" ..." if len(veraltet) > 20 else ""))
    if ohne_daten:
        print(f"\nOhne ausreichende Daten ({len(ohne_daten)}): " + ", ".join(ohne_daten[:40]) + (" ..." if len(ohne_daten) > 40 else ""))


if __name__ == "__main__":
    main()
