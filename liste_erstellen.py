"""Lädt die Mitglieder von S&P 500 und DAX 40 von Wikipedia und speichert sie in aktien.csv.
Diese Datei liest danach scan_index.py. Einmal ausführen, danach höchstens alle paar Wochen
wieder (die Zusammensetzung der Indizes ändert sich nur selten)."""
import io
import sys
import pandas as pd
import requests

QUELLEN = [
    ("S&P 500", "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol", "Security", 450),
    ("DAX 40", "https://en.wikipedia.org/wiki/DAX", "Ticker", "Company", 35),
]
AUSGABE = "aktien.csv"
HEADER = {"User-Agent": "Mozilla/5.0 (privates Lernprojekt; Aktien-Screener)"}


def lese_tabelle(html, spalte_kuerzel, spalte_name):
    """Sucht im HTML die Tabelle, die beide Spalten enthält, und gibt Kürzel und Namen zurück."""
    for tabelle in pd.read_html(io.StringIO(html)):
        spalten = [str(c).strip() for c in tabelle.columns]
        if spalte_kuerzel in spalten and spalte_name in spalten:
            tabelle.columns = spalten
            ergebnis = tabelle[[spalte_kuerzel, spalte_name]].copy()
            ergebnis.columns = ["Kürzel", "Name"]
            return ergebnis
    raise ValueError(f"Keine Tabelle mit den Spalten {spalte_kuerzel} und {spalte_name} gefunden")


def bereinige(df, index_name):
    df = df.dropna()
    df["Kürzel"] = df["Kürzel"].astype(str).str.strip().str.replace(".", "-", regex=False) \
        if index_name == "S&P 500" else df["Kürzel"].astype(str).str.strip()
    df["Name"] = df["Name"].astype(str).str.strip().str.replace(";", ",", regex=False)
    df["Index"] = index_name
    return df[df["Kürzel"] != ""]


def main():
    teile = []
    for index_name, url, sp_kuerzel, sp_name, minimum in QUELLEN:
        try:
            antwort = requests.get(url, headers=HEADER, timeout=30)
            antwort.raise_for_status()
            df = bereinige(lese_tabelle(antwort.text, sp_kuerzel, sp_name), index_name)
        except Exception as fehler:
            print(f"FEHLER bei {index_name}: {fehler}")
            print("Es wurde nichts gespeichert, eine bestehende aktien.csv bleibt unverändert.")
            sys.exit(1)
        if len(df) < minimum:
            print(f"FEHLER: {index_name} hat nur {len(df)} Einträge (erwartet mindestens {minimum}).")
            print("Es wurde nichts gespeichert, eine bestehende aktien.csv bleibt unverändert.")
            sys.exit(1)
        print(f"{index_name}: {len(df)} Aktien geladen")
        teile.append(df)
    alle = pd.concat(teile).drop_duplicates(subset="Kürzel", keep="first")
    alle.to_csv(AUSGABE, sep=";", index=False, encoding="utf-8-sig")
    print(f"Gespeichert: {AUSGABE} mit {len(alle)} Aktien")
    print(alle.head(5).to_string(index=False))


if __name__ == "__main__":
    main()
