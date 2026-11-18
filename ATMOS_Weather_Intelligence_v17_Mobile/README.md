# ATMOS — Weather Intelligence

Application météo desktop (interface web locale) propulsée par Python / Flask.

## Installation
Python 3.10+ recommandé.

Windows (PowerShell) :
```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```
Puis ouvrir http://127.0.0.1:5000

## Données et API
- Météo, prévisions horaires / 7 jours, UV : Open-Meteo (sans clé API).
- Recherche de villes : géocodage Open-Meteo.
- Carte interactive : Leaflet + tuiles OpenStreetMap ; clic = météo aux coordonnées.
- Vigilance : flux CAP Meteoalarm lorsqu'il répond. La grille des phénomènes liste les grandes familles de risques ; elle ne remplace pas la vigilance officielle Météo-France.
- Actualités : flux RSS publics de BFMTV, Le Parisien, Franceinfo et France 24. La disponibilité dépend des flux et de la connexion.

## Notes importantes
- Le service météo dépend d'Internet et des services tiers. Les API peuvent être temporairement indisponibles ou limiter les requêtes.
- Les flux d'alertes ne garantissent pas une couverture exhaustive de tous les départements français. Pour une décision de sécurité, consulter vigilance.meteofrance.fr.
- L'image de la présentatrice est un visuel généré pour cette interface à partir de la direction artistique de la référence ; ce n'est pas une reproduction exacte de la personne photographiée dans la capture.
- Pour utiliser l'application sur un autre ordinateur du réseau local, lancer `app.run(host="0.0.0.0")` et autoriser le port 5000 dans le pare-feu.


## Vigilances — version 16
- Source principale : flux CAP MeteoAlarm (alertes européennes).
- Source de secours : couche départementale Météo-France relayée par Esri France.
- La couche cartographique de secours fournit le niveau maximal départemental (vert/jaune/orange/rouge), mais pas le détail du phénomène ni ses horaires ; elle est annoncée comme mise à jour deux fois par jour.
- Si les deux sources sont indisponibles, ATMOS affiche un message d'indisponibilité et conserve un diagnostic dans la console du serveur. Ne pas interpréter une panne de données comme une absence de vigilance.
- Pour les décisions de sécurité, le bulletin officiel Météo-France reste la référence : https://vigilance.meteofrance.fr/fr
