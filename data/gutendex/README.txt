The live Gutendex metadata snapshot is generated here by running:

    python3 tools/sync_gutendex.py

The script writes data/gutendex_catalog.json in the project root. That JSON is metadata only.
Full texts are fetched on demand and cached under instance/gutenberg_cache/.
