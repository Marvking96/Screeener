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
ZEITRAUM = "1y"        # so viel Kursverlauf wird geladen
N = 5                  # Fenster: so viele Tage links und rechts werden verglichen
MAX_ALTER_TAGE = 5     # Signale, die älter sind (in Handelstagen), werden nicht angezeigt
MAX_ABSTAND_PROZENT = 5  # aktive Setups werden nur gezeigt, wenn der Kurs höchstens so weit vom Ausbruch entfernt ist
BLOCK = 100            # so viele Aktien werden pro Abruf geladen
KURS_TAGE = 160        # so viele Handelstage Kursverlauf bekommt jeder Treffer für den Chart in der App
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


def baue_eintrag(ticker, name, index_name, close, t):
    """Wandelt einen Treffer in einen Eintrag für die JSON-Datei um (inklusive Kursverlauf für den Chart)."""
    fenster = close.iloc[-KURS_TAGE:]
    start_pos = len(close) - len(fenster)
    daten = [d.strftime("%Y-%m-%d") for d in fenster.index]
    eintrag = {
        "kuerzel": ticker, "name": name, "index": index_name, "richtung": t["richtung"],
        "punkt2": round(t["punkt2"], 2), "punkt3": round(t["punkt3"], 2), "kurs": round(t["kurs"], 2),
        "kurse": {"d": daten, "c": [round(float(x), 3) for x in fenster.values]},
        "kette": [{"d": close.index[i].strftime("%Y-%m-%d"), "nr": nr, "c": round(float(close.iloc[i]), 3)}
                  for i, nr in t["kette"] if i >= start_pos],
    }
    if t["art"] == "signal":
        eintrag["signaltag"] = close.index[t["signal_index"]].strftime("%Y-%m-%d")
        eintrag["tage_her"] = t["tage_her"]
    else:
        eintrag["abstand"] = round(t["abstand"], 1)
    return eintrag


def lade_block(tickers):
    """Lädt die Schlusskurse für einen Block von Aktien. Gibt eine Tabelle zurück (eine Spalte je Aktie)."""
    daten = yf.download(tickers, period=ZEITRAUM, auto_adjust=True, progress=False, threads=True)
    kurse = daten["Close"]
    if isinstance(kurse, pd.Series):
        kurse = kurse.to_frame(tickers[0])
    return kurse


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


def main():
    start = time.time()
    liste = pd.read_csv(LISTE, sep=";", encoding="utf-8-sig")
    tickers = list(liste["Kürzel"])
    namen = dict(zip(liste["Kürzel"], liste["Name"]))
    indizes = dict(zip(liste["Kürzel"], liste["Index"]))
    print(f"{len(tickers)} Aktien in {LISTE}. Lade Kurse in Blöcken zu {BLOCK} ...")

    signale, setups, ohne_daten, letzte_tage = [], [], [], []
    for b in range(0, len(tickers), BLOCK):
        block = tickers[b : b + BLOCK]
        nr = b // BLOCK + 1
        gesamt = (len(tickers) + BLOCK - 1) // BLOCK
        try:
            kurse = lade_block(block)
        except Exception as fehler:
            ohne_daten += block
            print(f"  Block {nr}/{gesamt}: Abruf fehlgeschlagen ({fehler})")
            continue
        for ticker in block:
            if ticker not in kurse.columns:
                ohne_daten.append(ticker)
                continue
            close = kurse[ticker].dropna()
            if len(close) < 40:
                ohne_daten.append(ticker)
                continue
            try:
                treffer = pruefe_aktie(close)
            except Exception as fehler:
                print(f"  Fehler bei {ticker}: {fehler}")
                ohne_daten.append(ticker)
                continue
            letzte_tage.append(close.index[-1].strftime("%Y-%m-%d"))
            for t in treffer:
                eintrag = baue_eintrag(ticker, str(namen.get(ticker, "-")), indizes.get(ticker, "-"), close, t)
                (signale if t["art"] == "signal" else setups).append(eintrag)
        print(f"  Block {nr}/{gesamt} fertig")

    geprueft = len(tickers) - len(ohne_daten)
    if geprueft < len(tickers) * 0.5:
        # zu viele Ausfälle: lieber das alte Ergebnis behalten als ein halbes zu überschreiben
        print(f"\nFEHLER: Nur {geprueft} von {len(tickers)} Aktien konnten geprüft werden. "
              "Die Ergebnisdatei wird nicht überschrieben.")
        raise SystemExit(1)

    signale.sort(key=lambda e: (e["tage_her"], e["kuerzel"]))
    setups.sort(key=lambda e: e["abstand"])
    ergebnis = {
        "erstellt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "letzter_handelstag": Counter(letzte_tage).most_common(1)[0][0] if letzte_tage else None,
        "anzahl_gesamt": len(tickers), "anzahl_geprueft": geprueft,
        "einstellungen": {"N": N, "max_alter_tage": MAX_ALTER_TAGE, "max_abstand_prozent": MAX_ABSTAND_PROZENT},
        "ohne_daten": ohne_daten,
        "signale": signale, "setups": setups,
    }
    os.makedirs(os.path.dirname(AUSGABE), exist_ok=True)
    with open(AUSGABE, "w", encoding="utf-8") as f:
        json.dump(ergebnis, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\nFertig nach {time.time() - start:.0f} Sekunden. Geprüft: {geprueft} von {len(tickers)} Aktien.")
    print(f"Ergebnis gespeichert in {AUSGABE} ({os.path.getsize(AUSGABE) // 1024} KB)")
    print(f"\n=== SIGNALE der letzten {MAX_ALTER_TAGE} Handelstage (Ausbruch über/unter Punkt 2) ===")
    zeige_tabelle(signale, "signal")
    print(f"\n=== AKTIVE SETUPS (1-2-3 steht, Ausbruch fehlt noch, höchstens {MAX_ABSTAND_PROZENT}% entfernt), nächste zuerst ===")
    zeige_tabelle(setups, "setup")
    if ohne_daten:
        print(f"\nOhne ausreichende Daten ({len(ohne_daten)}): " + ", ".join(ohne_daten[:40]) + (" ..." if len(ohne_daten) > 40 else ""))


if __name__ == "__main__":
    main()
