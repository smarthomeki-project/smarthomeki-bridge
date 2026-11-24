# SmartHomeKI Bridge 2.1

Die Bridge läuft als benutzerdefinierte Home-Assistant-Integration. Die YAML-Aktivierung erfolgt mit `smarthomeki_bridge:`. Standardnamen sind das Dashboard `smarthomeki`, der Freigabetaster `input_button.smarthomeki_approve` und der Kalender `calendar.smarthomeki_events`. Für die Energiefunktion werden kumulative kWh-Sensoren mit Recorder-Langzeitstatistik erwartet: `sensor.smarthomeki_total_energy_import`, `sensor.smarthomeki_total_energy_export`, `sensor.smarthomeki_total_load_consumption`, `sensor.smarthomeki_total_production`, `sensor.smarthomeki_total_battery_charge` und `sensor.smarthomeki_total_battery_discharge`. Version 2.1 berechnet Energie und variable Kosten aus den kumulierten Recorder-Werten an den Grenzen lokaler Kalendertage. Das Enddatum ist exklusiv. Fehlende Tageswerte ergeben weiterhin `null`.

## Marketplace

Die Repository-Struktur und `hacs.json` sind für den Marketplace vorbereitet. Home Assistant verlangt hierfür ein öffentliches GitHub-Repository. Für die öffentliche Verteilung einen neuen Repository-Verlauf unter einem separaten Projektkonto verwenden. Für die Veröffentlichung eine stabile GitHub-Version mit Tag `v2.1.0` und dazugehörigen Release Notes aus dem geprüften Commit erstellen.

1. Vor Installation ein aktuelles vollständiges Home-Assistant-Backup erstellen. Abschluss prüfen und eine Kopie samt Zugriff auf den Wiederherstellungsschlüssel sicherstellen. Bei externer Recorder-Datenbank deren Sicherung separat prüfen.
2. Unter **Einstellungen → Marketplace → Per Link hinzufügen** das veröffentlichte Repository auswählen. Bestehende YAML-Aktivierung und den Freigabetaster beibehalten. Eine korrekt verwaltete Installation erhält Updates unter **Einstellungen → Updates**. Updates bewusst nach geprüftem Backup installieren.
3. Home Assistant neu starten. Systemprotokoll, Bridge-Verbindung, Statistik und Energieabfrage prüfen. Für einen Test vor dem Produktiveinsatz eine getrennte Home-Assistant-Instanz verwenden.

## Rücksicherung

Bei einem fehlgeschlagenen Update das vor dem Update erstellte Backup über **Einstellungen → System → Backups** wiederherstellen. Bei nicht startendem Home Assistant beim Einrichten einer Ersatzinstallation das Backup auswählen. Die Wiederherstellung überschreibt den gesicherten Umfang und kann zwischenzeitliche Änderungen zurücksetzen. Nach dem Start Bridge-Version, Verbindung und Dashboard prüfen. Eine frühere Marketplace-Version allein stellt Konfiguration und gespeicherte Daten nicht wieder her.

Referenzen [Marketplace](https://www.home-assistant.io/integrations/marketplace/) und [Backups](https://www.home-assistant.io/common-tasks/general/#restoring-a-backup).
