"""ETL pipeline: scrape the data site, transform to typed records, load the warehouse.

The Extract -> Transform -> Load stages live in ``scrape``, ``transform`` and
``load``; ``run_etl`` orchestrates them and writes the scrape manifest.
"""
