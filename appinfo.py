"""Versionsangaben der gebauten App (werden beim Bauen von GitHub gesetzt).

RUNTIME erhöhen, wenn sich mitgelieferte Programme/Bibliotheken ändern (requirements.txt,
packaging/SongFinder.spec). Code-Updates mit höherem min_runtime werden dann nicht mehr
automatisch geladen; stattdessen weist die App auf eine neue App-Version hin.
"""
VERSION = "1.0.0"
RUNTIME = 1
