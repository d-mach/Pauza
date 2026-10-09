# PAUZA 2.0

Mapa veřejných WC a sprch v ČR pro cestující, řidiče a kurýry.

## Nasazení
Propojte tento repozitář s existující službou Netlify `poo-pauza.netlify.app` a použijte nastavení z `netlify.toml`. V rámci sestavení se stáhne výpis ČR z Geofabrik (OpenStreetMap) a vytvoří databáze `public/data/wc-cr.json`. Je potřeba kontrolovat úspěšné dokončení buildu a obsah databáze. Bez úspěšného buildu nejsou celorepubliková data dostupná.

Zdroj dat: © OpenStreetMap contributors (ODbL 1.0), Geofabrik. Ceny a vybavení nemusejí být úplné či aktuální. Běžná navigace nenahrazuje kamionovou navigaci.
